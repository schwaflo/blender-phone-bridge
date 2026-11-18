# SPDX-License-Identifier: MIT
"""Blender Phone Bridge

Steuere eine Blender-Kamera mit Gyrosensor / AR-Tracking deines Handys,
sieh die Szene live auf dem Handy und nimm Fotos & Videos auf.
"""

bl_info = {
    "name": "Phone Bridge (Handy-Kamera)",
    "author": "schwaflo",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Seitenleiste (N) > Phone",
    "description": "Handy als virtuelle Kamera: Gyro/AR-Tracking, Live-Stream, Foto & Video",
    "category": "3D View",
}

import math
import os
import secrets
import tempfile
import time

import bpy
import gpu
import blf
from bpy.props import (BoolProperty, EnumProperty, FloatProperty, IntProperty,
                       PointerProperty, StringProperty)
from mathutils import Quaternion, Vector

from . import certgen, qr, server as bridge_server

ADDON_ID = __package__ or __name__
ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PAGES_URL = "https://schwaflo.github.io/blender-phone-bridge/"

MSG_FRAME = 1
MSG_PHOTO = 2


# =============================================================================
# Runtime state (not saved in the .blend)
# =============================================================================
class State:
    server = None
    token = ""
    ip = ""
    port = 0
    app_url = ""
    local_url = ""
    qr_modules = None
    error = ""

    active_client = None
    client_addr = ""
    tracking_mode = ""
    view_size = (390, 844)

    # pose
    phone_q = None          # latest phone orientation (Quaternion, Blender space)
    phone_p = None          # latest phone position (Vector) - only in AR mode
    move = (0.0, 0.0, 0.0)  # joystick velocity (right, forward, up), -1..1
    need_recenter = True
    yaw_q = Quaternion()
    base_loc = Vector()
    p0 = Vector()
    smooth_q = None

    # streaming
    want_frame = False
    pixels = None           # (w, h, numpy array RGBA uint8, purpose)
    inflight = 0
    last_frame_sent = 0.0
    last_ack = 0.0
    fps_counter = 0
    fps_t0 = 0.0
    fps = 0.0
    offscreen = None
    offscreen_size = (0, 0)
    target_region_ptr = 0

    # capture
    photo_pending = None     # "VIEWPORT" | "RENDER"
    recording = False
    rec_t0 = 0.0
    rec_start_frame = 1
    rec_last_frame = None
    rec_dir = ""
    rec_count = 0

    handlers = []
    last_tick = 0.0
    qr_batch_key = None
    qr_batch = None


def prefs():
    addon = bpy.context.preferences.addons.get(ADDON_ID)
    return addon.preferences if addon else None


def settings(context=None):
    return (context or bpy.context).scene.phone_bridge


# =============================================================================
# Helpers
# =============================================================================
def web_dir():
    for d in (os.path.join(ADDON_DIR, "web"), os.path.join(os.path.dirname(ADDON_DIR), "docs")):
        if os.path.isfile(os.path.join(d, "index.html")):
            return d
    return os.path.join(ADDON_DIR, "web")


def capture_dir():
    s = settings()
    path = bpy.path.abspath(s.capture_dir) if s.capture_dir else ""
    if not path or (s.capture_dir.startswith("//") and not bpy.data.filepath):
        path = os.path.join(os.path.expanduser("~"), "BlenderPhoneBridge")
    os.makedirs(path, exist_ok=True)
    return path


def get_camera(create=True):
    s = settings()
    cam = s.camera
    if cam and cam.type == 'CAMERA' and cam.name in bpy.context.scene.objects:
        return cam
    if not create:
        return None
    cam = bpy.data.objects.get("PhoneCam")
    if cam is None or cam.type != 'CAMERA':
        data = bpy.data.cameras.new("PhoneCam")
        data.lens = 26.0
        data.sensor_fit = 'AUTO'
        cam = bpy.data.objects.new("PhoneCam", data)
        cam.location = (0.0, -6.0, 1.6)
        cam.rotation_mode = 'QUATERNION'
        cam.rotation_quaternion = Quaternion((1, 0, 0), math.radians(90))
    if cam.name not in bpy.context.scene.objects:
        bpy.context.scene.collection.objects.link(cam)
    s.camera = cam
    return cam


def find_view3d():
    """Return (window, area, region, space) of the largest 3D viewport."""
    best = None
    wm = bpy.context.window_manager
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type != 'VIEW_3D':
                continue
            region = next((r for r in area.regions if r.type == 'WINDOW'), None)
            if region is None:
                continue
            size = region.width * region.height
            if best is None or size > best[0]:
                best = (size, window, area, region, area.spaces.active)
    return best[1:] if best else (None, None, None, None)


def heading(q):
    f = q @ Vector((0.0, 0.0, -1.0))
    if abs(f.z) > 0.95:  # looking straight up/down -> use the camera's up vector
        f = q @ Vector((0.0, 1.0, 0.0)) * (1 if f.z < 0 else -1)
    return math.atan2(f.y, f.x)


def stream_size(max_side):
    w, h = State.view_size
    w, h = max(1, w), max(1, h)
    scale = max_side / max(w, h)
    return max(16, int(w * scale) // 2 * 2), max(16, int(h * scale) // 2 * 2)


def tag_redraw_all():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type in ('VIEW_3D', 'PROPERTIES'):
                area.tag_redraw()


# =============================================================================
# Camera control
# =============================================================================
def recenter(cam):
    if State.phone_q is None:
        return
    cam_q = cam.matrix_world.to_quaternion()
    State.yaw_q = Quaternion((0, 0, 1), heading(cam_q) - heading(State.phone_q))
    State.base_loc = cam.matrix_world.translation.copy()
    State.p0 = State.phone_p.copy() if State.phone_p is not None else Vector()
    State.smooth_q = None
    State.need_recenter = False


def rebase_position(cam):
    State.base_loc = cam.location.copy()
    State.p0 = State.phone_p.copy() if State.phone_p is not None else Vector()


def apply_pose(cam, dt):
    s = settings()
    if State.phone_q is None:
        return
    if State.need_recenter:
        recenter(cam)

    target = State.yaw_q @ State.phone_q
    if State.smooth_q is None or s.smoothing <= 0.0:
        State.smooth_q = target
    else:
        k = 1.0 - math.pow(s.smoothing, dt * 60.0) * 0.98
        State.smooth_q = State.smooth_q.slerp(target, min(1.0, max(0.02, k)))

    # Joystick movement (relative to viewing direction, horizontal)
    mx, my, mz = State.move
    if mx or my or mz:
        fwd = State.smooth_q @ Vector((0.0, 0.0, -1.0))
        fwd.z = 0.0
        if fwd.length < 1e-4:
            fwd = State.smooth_q @ Vector((0.0, 1.0, 0.0))
            fwd.z = 0.0
        fwd.normalize()
        right = Vector((fwd.y, -fwd.x, 0.0))
        delta = (right * mx + fwd * my + Vector((0, 0, 1)) * mz) * s.move_speed * dt
        State.base_loc += delta

    loc = State.base_loc.copy()
    if State.phone_p is not None:
        loc += State.yaw_q @ ((State.phone_p - State.p0) * s.position_scale)

    if cam.rotation_mode != 'QUATERNION':
        cam.rotation_mode = 'QUATERNION'
    if cam.parent is None:
        cam.location = loc
        cam.rotation_quaternion = State.smooth_q
    else:
        mw = cam.matrix_world.copy()
        from mathutils import Matrix
        mw = Matrix.LocRotScale(loc, State.smooth_q, mw.to_scale())
        cam.matrix_world = mw


# =============================================================================
# Message handling
# =============================================================================
def handle_message(cid, kind, msg):
    srv = State.server
    if kind == "connect":
        State.active_client = cid
        State.client_addr = msg.get("addr", "")
        State.need_recenter = True
        State.inflight = 0
        State.phone_p = None
        cam = get_camera()
        s = settings()
        srv.send_json({
            "t": "welcome", "scene": bpy.context.scene.name, "camera": cam.name,
            "lens": cam.data.lens, "fps": s.stream_fps, "quality": s.jpeg_quality,
            "size": s.stream_resolution, "speed": s.move_speed, "photo": s.photo_mode,
            "blender": bpy.app.version_string,
        }, cid)
        if s.follow_in_viewport:
            look_through(cam)
        return
    if kind == "disconnect":
        if cid == State.active_client:
            State.active_client = None
            State.phone_q = None
            State.move = (0.0, 0.0, 0.0)
            if State.recording:
                stop_recording()
        return
    if kind != "json" or cid != State.active_client:
        return

    t = msg.get("t")
    s = settings()
    cam = get_camera()
    if t == "pose":
        q = msg.get("q")
        if q and len(q) == 4:
            nq = Quaternion([float(v) for v in q])
            if nq.magnitude > 1e-6:
                nq.normalize()
                State.phone_q = nq
        p = msg.get("p")
        new_p = Vector([float(v) for v in p]) if p and len(p) == 3 else None
        if (new_p is None) != (State.phone_p is None):
            State.phone_p = new_p
            rebase_position(cam)
        State.phone_p = new_p
        State.tracking_mode = msg.get("mode", State.tracking_mode)
    elif t == "move":
        v = msg.get("v") or (0, 0, 0)
        State.move = tuple(max(-1.0, min(1.0, float(x))) for x in v[:3])
    elif t == "yaw":
        State.yaw_q = Quaternion((0, 0, 1), float(msg.get("d", 0.0))) @ State.yaw_q
        rebase_position(cam)
    elif t == "recenter":
        State.need_recenter = True
    elif t in ("hello", "view"):
        w, h = int(msg.get("w", 390)), int(msg.get("h", 844))
        State.view_size = (max(1, w), max(1, h))
        if t == "hello":
            State.tracking_mode = msg.get("mode", "gyro")
    elif t == "ack":
        State.inflight = max(0, State.inflight - 1)
        State.last_ack = time.time()
    elif t == "lens":
        cam.data.lens = max(4.0, min(300.0, float(msg.get("mm", cam.data.lens))))
    elif t == "settings":
        if "quality" in msg:
            s.jpeg_quality = int(msg["quality"])
        if "size" in msg:
            s.stream_resolution = int(msg["size"])
        if "speed" in msg:
            s.move_speed = float(msg["speed"])
        if "fps" in msg:
            s.stream_fps = int(msg["fps"])
        if msg.get("photo") in ("VIEWPORT", "RENDER"):
            s.photo_mode = msg["photo"]
    elif t == "photo":
        State.photo_pending = msg.get("mode") or s.photo_mode
        srv.send_json({"t": "status", "msg": "Foto wird erstellt …"}, cid)
    elif t == "rec":
        if msg.get("on"):
            start_recording()
        else:
            stop_recording()


def look_through(cam):
    scene = bpy.context.scene
    scene.camera = cam
    _w, _a, region, space = find_view3d()
    if space and space.region_3d:
        space.region_3d.view_perspective = 'CAMERA'


# =============================================================================
# Recording (camera keyframes + JPEG image sequence of the stream)
# =============================================================================
def start_recording():
    if State.recording:
        return
    s = settings()
    State.recording = True
    State.rec_t0 = time.time()
    State.rec_start_frame = bpy.context.scene.frame_current
    State.rec_last_frame = None
    State.rec_count = 0
    State.rec_dir = ""
    if s.record_stream_frames:
        State.rec_dir = os.path.join(capture_dir(), time.strftime("video_%Y%m%d_%H%M%S"))
        os.makedirs(State.rec_dir, exist_ok=True)
    if State.server and State.active_client:
        State.server.send_json({"t": "rec", "on": True}, State.active_client)


def stop_recording():
    if not State.recording:
        return
    State.recording = False
    scene = bpy.context.scene
    if State.rec_last_frame is not None and settings().record_keyframes:
        scene.frame_end = max(scene.frame_end, State.rec_last_frame)
    info = "%d Frames" % State.rec_count
    if State.server and State.active_client:
        State.server.send_json({"t": "rec", "on": False, "frames": State.rec_count,
                                "dir": State.rec_dir}, State.active_client)
        State.server.send_json({"t": "status", "msg": "Aufnahme gespeichert: " + info}, State.active_client)


def record_tick(cam):
    s = settings()
    scene = bpy.context.scene
    fps = scene.render.fps / scene.render.fps_base
    frame = State.rec_start_frame + int(round((time.time() - State.rec_t0) * fps))
    if frame == State.rec_last_frame:
        return
    State.rec_last_frame = frame
    State.rec_count += 1
    if s.record_keyframes:
        cam.keyframe_insert("location", frame=frame, group="PhoneBridge")
        cam.keyframe_insert("rotation_quaternion", frame=frame, group="PhoneBridge")
        cam.data.keyframe_insert("lens", frame=frame)


# =============================================================================
# Offscreen rendering (runs inside the 3D view draw callback)
# =============================================================================
def _read_pixels(w, h):
    import numpy as np
    with State.offscreen.bind():
        fb = gpu.state.active_framebuffer_get()
        buf = fb.read_color(0, 0, w, h, 4, 0, 'UBYTE')
    try:
        arr = np.frombuffer(buf, dtype=np.uint8)
    except (TypeError, ValueError):
        buf.dimensions = w * h * 4
        arr = np.array(buf.to_list(), dtype=np.uint8)
    return arr.copy()


def render_offscreen(context, w, h):
    cam = get_camera(create=False)
    if cam is None:
        return None
    if State.offscreen is None or State.offscreen_size != (w, h):
        if State.offscreen is not None:
            State.offscreen.free()
        State.offscreen = gpu.types.GPUOffScreen(w, h)
        State.offscreen_size = (w, h)

    scene = context.scene
    depsgraph = context.evaluated_depsgraph_get()
    view_matrix = cam.matrix_world.inverted()
    proj = cam.calc_matrix_camera(depsgraph, x=w, y=h)
    space = context.space_data
    hide = settings(context).hide_overlays
    old_overlay = space.overlay.show_overlays
    if hide and old_overlay:
        space.overlay.show_overlays = False
    try:
        State.offscreen.draw_view3d(scene, context.view_layer, space, context.region,
                                    view_matrix, proj, do_color_management=True)
    finally:
        if hide and old_overlay:
            space.overlay.show_overlays = True
    return _read_pixels(w, h)


def draw_callback():
    context = bpy.context
    region = context.region
    if region is None:
        return
    # 1) Offscreen render for the stream / photo
    if State.want_frame and region.as_pointer() == State.target_region_ptr:
        purpose = State.want_frame
        State.want_frame = False
        s = settings(context)
        size = s.photo_resolution if purpose == "photo" else s.stream_resolution
        w, h = stream_size(size)
        try:
            arr = render_offscreen(context, w, h)
            if arr is not None:
                State.pixels = (w, h, arr, purpose)
        except Exception as exc:  # never break the viewport drawing
            State.error = "Render-Fehler: %s" % exc
            print("[PhoneBridge]", State.error)
    # 2) QR code overlay
    if State.server and settings(context).show_qr and State.active_client is None:
        draw_qr_overlay(region)


def _uniform_shader():
    for name in ('UNIFORM_COLOR', '2D_UNIFORM_COLOR'):
        try:
            return gpu.shader.from_builtin(name)
        except (ValueError, TypeError):
            continue
    return None


def draw_qr_overlay(region):
    from gpu_extras.batch import batch_for_shader
    mods = State.qr_modules
    if not mods:
        return
    n = len(mods)
    quiet = 4
    side = int(min(region.width, region.height) * 0.6)
    side = max(120, min(side, 640))
    cell = side / (n + 2 * quiet)
    x0 = (region.width - side) / 2
    y0 = (region.height - side) / 2 + 18

    shader = _uniform_shader()
    if shader is None:
        return
    key = (region.width, region.height, n, id(mods))
    if State.qr_batch_key != key:
        dark = []
        for r, row in enumerate(mods):
            for c, v in enumerate(row):
                if v:
                    x = x0 + (c + quiet) * cell
                    y = y0 + side - (r + quiet + 1) * cell
                    dark += [(x, y), (x + cell, y), (x + cell, y + cell),
                             (x, y), (x + cell, y + cell), (x, y + cell)]
        pad = 10
        bg = [(x0 - pad, y0 - 52), (x0 + side + pad, y0 - 52), (x0 + side + pad, y0 + side + pad),
              (x0 - pad, y0 - 52), (x0 + side + pad, y0 + side + pad), (x0 - pad, y0 + side + pad)]
        white = [(x0, y0), (x0 + side, y0), (x0 + side, y0 + side),
                 (x0, y0), (x0 + side, y0 + side), (x0, y0 + side)]
        State.qr_batch = (batch_for_shader(shader, 'TRIS', {"pos": bg}),
                          batch_for_shader(shader, 'TRIS', {"pos": white}),
                          batch_for_shader(shader, 'TRIS', {"pos": dark}))
        State.qr_batch_key = key

    gpu.state.blend_set('ALPHA')
    shader.bind()
    for batch, color in zip(State.qr_batch, ((0.08, 0.09, 0.11, 0.92), (1, 1, 1, 1), (0, 0, 0, 1))):
        shader.uniform_float("color", color)
        batch.draw(shader)
    gpu.state.blend_set('NONE')

    font = 0
    blf.color(font, 1, 1, 1, 1)
    blf.size(font, 15)
    text = "Mit dem Handy scannen – Phone Bridge"
    tw, _ = blf.dimensions(font, text)
    blf.position(font, x0 + (side - tw) / 2, y0 - 22, 0)
    blf.draw(font, text)
    blf.size(font, 11)
    blf.color(font, 0.7, 0.7, 0.7, 1)
    sub = "%s:%d  •  QR ausblenden: Seitenleiste > Phone" % (State.ip, State.port)
    tw, _ = blf.dimensions(font, sub)
    blf.position(font, x0 + (side - tw) / 2, y0 - 40, 0)
    blf.draw(font, sub)


# =============================================================================
# Encoding and sending
# =============================================================================
_ENC_IMAGE = ".PhoneBridgeFrame"


def encode_image(w, h, arr, fmt='JPEG', quality=75):
    import numpy as np
    img = bpy.data.images.get(_ENC_IMAGE)
    if img is None or tuple(img.size) != (w, h):
        if img is not None:
            bpy.data.images.remove(img)
        img = bpy.data.images.new(_ENC_IMAGE, w, h, alpha=False)
    px = arr.astype(np.float32)
    px *= 1.0 / 255.0
    px[3::4] = 1.0
    img.pixels.foreach_set(px)
    ext = ".jpg" if fmt == 'JPEG' else ".png"
    path = os.path.join(tempfile.gettempdir(), "phone_bridge_frame" + ext)
    img.file_format = fmt
    try:
        img.save(filepath=path, quality=quality)
    except TypeError:
        img.filepath_raw = path
        img.save()
    with open(path, "rb") as f:
        return f.read()


def process_pixels():
    w, h, arr, purpose = State.pixels
    State.pixels = None
    s = settings()
    srv = State.server
    cid = State.active_client
    if purpose == "photo":
        data_png = encode_image(w, h, arr, 'PNG')
        name = time.strftime("foto_%Y%m%d_%H%M%S.png")
        path = os.path.join(capture_dir(), name)
        with open(path, "wb") as f:
            f.write(data_png)
        if srv and cid:
            srv.send_json({"t": "photo", "name": name, "path": path}, cid)
            srv.send_binary(bytes([MSG_PHOTO]) + data_png, cid)
        return
    data = encode_image(w, h, arr, 'JPEG', s.jpeg_quality)
    if srv and cid:
        srv.send_frame(bytes([MSG_FRAME]) + data, cid)
        State.inflight += 1
        State.fps_counter += 1
    if State.recording and State.rec_dir:
        with open(os.path.join(State.rec_dir, "frame_%05d.jpg" % (State.rec_count)), "wb") as f:
            f.write(data)


def do_render_photo():
    """Full render (Eevee/Cycles) from the phone camera. Blocks Blender."""
    scene = bpy.context.scene
    cam = get_camera()
    old_cam = scene.camera
    scene.camera = cam
    name = time.strftime("render_%Y%m%d_%H%M%S.png")
    path = os.path.join(capture_dir(), name)
    old_fmt = scene.render.image_settings.file_format
    try:
        window, area, region, _space = find_view3d()
        if window:
            with bpy.context.temp_override(window=window, area=area, region=region):
                bpy.ops.render.render(write_still=False)
        else:
            bpy.ops.render.render(write_still=False)
        scene.render.image_settings.file_format = 'PNG'
        bpy.data.images['Render Result'].save_render(path, scene=scene)
    finally:
        scene.render.image_settings.file_format = old_fmt
        scene.camera = old_cam
    srv, cid = State.server, State.active_client
    if srv and cid:
        with open(path, "rb") as f:
            data = f.read()
        srv.send_json({"t": "photo", "name": name, "path": path}, cid)
        srv.send_binary(bytes([MSG_PHOTO]) + data, cid)


# =============================================================================
# Main timer
# =============================================================================
def tick():
    srv = State.server
    if srv is None:
        return None
    now = time.time()
    dt = min(0.1, now - State.last_tick) if State.last_tick else 1 / 60
    State.last_tick = now

    try:
        # 1) messages
        for _ in range(500):
            try:
                cid, kind, msg = srv.inbox.get_nowait()
            except Exception:
                break
            handle_message(cid, kind, msg)
            if kind in ("connect", "disconnect"):
                tag_redraw_all()

        cam = get_camera(create=False)
        s = settings()

        # 2) pose
        if cam and State.active_client:
            apply_pose(cam, dt)
            if State.recording:
                record_tick(cam)

        # 3) encode/send frames produced by the draw callback
        if State.pixels is not None:
            process_pixels()

        # 4) photo request
        if State.photo_pending and State.active_client:
            mode = State.photo_pending
            State.photo_pending = None
            if mode == "RENDER":
                srv.send_json({"t": "status", "msg": "Rendere Bild … (Blender ist kurz blockiert)"}, State.active_client)
                try:
                    do_render_photo()
                except Exception as exc:
                    srv.send_json({"t": "status", "msg": "Render fehlgeschlagen: %s" % exc}, State.active_client)
            else:
                request_frame("photo")

        # 5) request next stream frame
        if State.active_client and cam:
            if State.inflight and now - State.last_ack > 2.0 and now - State.last_frame_sent > 2.0:
                State.inflight = 0  # lost acks -> resync
            if (not State.want_frame and State.pixels is None and State.inflight < 2
                    and now - State.last_frame_sent >= 1.0 / max(1, s.stream_fps)):
                request_frame("stream")
                State.last_frame_sent = now

        if now - State.fps_t0 >= 1.0:
            State.fps = State.fps_counter / (now - State.fps_t0)
            State.fps_counter = 0
            State.fps_t0 = now
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == 'VIEW_3D':
                        for region in area.regions:
                            if region.type == 'UI':
                                region.tag_redraw()
    except Exception as exc:
        State.error = str(exc)
        print("[PhoneBridge] tick error:", exc)
        import traceback
        traceback.print_exc()
    return 1.0 / 60.0


def request_frame(purpose):
    _w, area, region, _space = find_view3d()
    if region is None:
        State.error = "Kein 3D-Viewport offen – Stream benötigt einen sichtbaren 3D-Viewport."
        return
    State.target_region_ptr = region.as_pointer()
    State.want_frame = purpose
    area.tag_redraw()


# =============================================================================
# Start / stop
# =============================================================================
def build_urls():
    p = prefs()
    hostport = "%s:%d" % (State.ip, State.port)
    query = "?host=%s&token=%s" % (hostport, State.token)
    pages = (p.pages_url if p else DEFAULT_PAGES_URL).strip() or DEFAULT_PAGES_URL
    if not pages.endswith("/") and not pages.endswith(".html"):
        pages += "/"
    State.app_url = pages + query
    State.local_url = "https://%s/%s" % (hostport, query)


def start_bridge(context):
    if State.server:
        return
    p = prefs()
    s = settings(context)
    State.ip = (p.ip_override.strip() if p and p.ip_override.strip() else bridge_server.get_local_ip())
    State.port = s.port
    State.token = secrets.token_urlsafe(9)
    cert_dir = bpy.utils.user_resource('CONFIG', path="phone_bridge", create=True)
    cert, key, _fp = certgen.ensure_certificate(cert_dir, State.ip)
    build_urls()

    def url_builder(_kind):
        return State.local_url

    srv = None
    last_exc = None
    for port in range(s.port, s.port + 20):  # fall back to the next free port
        State.port = port
        build_urls()
        srv = bridge_server.BridgeServer(port, cert, key, State.token, web_dir(), url_builder)
        try:
            srv.start()
            break
        except OSError as exc:
            last_exc, srv = exc, None
    if srv is None:
        raise last_exc
    State.server = srv
    target = State.app_url if (p is None or p.qr_target == 'PAGES') else State.local_url
    State.qr_modules = qr.matrix(target, "M")
    State.qr_batch_key = None
    State.error = ""
    State.active_client = None
    State.need_recenter = True
    State.last_tick = 0.0
    State.fps_t0 = time.time()
    get_camera()

    State.handlers.append(bpy.types.SpaceView3D.draw_handler_add(draw_callback, (), 'WINDOW', 'POST_PIXEL'))
    bpy.app.timers.register(tick, first_interval=0.05, persistent=True)
    tag_redraw_all()


def stop_bridge():
    if State.recording:
        stop_recording()
    if State.server:
        State.server.stop()
    State.server = None
    State.active_client = None
    State.phone_q = None
    for h in State.handlers:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(h, 'WINDOW')
        except ValueError:
            pass
    State.handlers.clear()
    if bpy.app.timers.is_registered(tick):
        bpy.app.timers.unregister(tick)
    if State.offscreen is not None:
        State.offscreen.free()
        State.offscreen = None
        State.offscreen_size = (0, 0)
    img = bpy.data.images.get(_ENC_IMAGE)
    if img is not None:
        bpy.data.images.remove(img)
    State.qr_modules = None
    tag_redraw_all()


# =============================================================================
# Properties
# =============================================================================
def _poll_camera(_self, obj):
    return obj.type == 'CAMERA'


class PhoneBridgeSettings(bpy.types.PropertyGroup):
    port: IntProperty(name="Port", default=8765, min=1024, max=65535)
    camera: PointerProperty(name="Kamera", type=bpy.types.Object, poll=_poll_camera,
                            description="Kamera, die vom Handy gesteuert wird (leer = PhoneCam wird erstellt)")
    stream_resolution: IntProperty(name="Stream-Auflösung", default=720, min=160, max=1920,
                                   description="Längste Bildseite des Live-Streams in Pixeln")
    photo_resolution: IntProperty(name="Foto-Auflösung", default=2400, min=320, max=8192,
                                  description="Längste Bildseite für Viewport-Fotos")
    jpeg_quality: IntProperty(name="JPEG-Qualität", default=70, min=10, max=100)
    stream_fps: IntProperty(name="Max. FPS", default=30, min=1, max=60)
    move_speed: FloatProperty(name="Laufgeschwindigkeit", default=1.5, min=0.01, max=100, unit='VELOCITY',
                              description="Geschwindigkeit des Joysticks in m/s")
    position_scale: FloatProperty(name="AR-Weg-Skalierung", default=1.0, min=0.01, max=1000,
                                  description="1 m Bewegung des Handys = X m in Blender (nur AR/6DoF)")
    smoothing: FloatProperty(name="Glättung", default=0.35, min=0.0, max=0.95,
                             description="Glättet Zittern der Sensoren (0 = aus)")
    hide_overlays: BoolProperty(name="Overlays im Stream ausblenden", default=True)
    show_qr: BoolProperty(name="QR-Code im Viewport", default=True)
    follow_in_viewport: BoolProperty(name="Viewport schaut durch Handy-Kamera", default=True)
    photo_mode: EnumProperty(name="Foto-Modus", items=[
        ('VIEWPORT', "Viewport (schnell)", "Foto in Viewport-Qualität in hoher Auflösung"),
        ('RENDER', "Render (Eevee/Cycles)", "Vollständiger Render mit der Render-Engine der Szene"),
    ], default='VIEWPORT')
    record_keyframes: BoolProperty(name="Kamerafahrt als Keyframes", default=True,
                                   description="Beim Video die Kamerabewegung als Animation aufzeichnen")
    record_stream_frames: BoolProperty(name="Stream-Bilder speichern", default=True,
                                       description="Beim Video die Livebilder als JPEG-Sequenz speichern")
    capture_dir: StringProperty(name="Speicherort", subtype='DIR_PATH', default="//phone_captures/")


class PhoneBridgePreferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_ID

    pages_url: StringProperty(name="GitHub-Pages-URL", default=DEFAULT_PAGES_URL,
                              description="Adresse der Handy-Web-App (docs/ Ordner auf GitHub Pages)")
    qr_target: EnumProperty(name="QR-Code öffnet", items=[
        ('PAGES', "GitHub-Seite", "Handy öffnet die Web-App auf GitHub Pages"),
        ('LOCAL', "Direkt von Blender", "Handy lädt die Web-App direkt von Blender (selbstsigniertes Zertifikat)"),
    ], default='PAGES')
    ip_override: StringProperty(name="IP-Adresse (optional)",
                                description="Falls die automatisch erkannte IP falsch ist (z.B. VPN)")

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "pages_url")
        col.prop(self, "qr_target")
        col.prop(self, "ip_override")


# =============================================================================
# Operators
# =============================================================================
class PHONEBRIDGE_OT_start(bpy.types.Operator):
    bl_idname = "phone_bridge.start"
    bl_label = "Handy verbinden"
    bl_description = "Startet den Server und zeigt den QR-Code im Viewport"

    def execute(self, context):
        try:
            start_bridge(context)
        except OSError as exc:
            stop_bridge()
            self.report({'ERROR'}, "Server konnte nicht starten (Port belegt?): %s" % exc)
            return {'CANCELLED'}
        self.report({'INFO'}, "Phone Bridge läuft – QR-Code scannen")
        return {'FINISHED'}


class PHONEBRIDGE_OT_stop(bpy.types.Operator):
    bl_idname = "phone_bridge.stop"
    bl_label = "Stoppen"

    def execute(self, context):
        stop_bridge()
        return {'FINISHED'}


class PHONEBRIDGE_OT_recenter(bpy.types.Operator):
    bl_idname = "phone_bridge.recenter"
    bl_label = "Ausrichtung zurücksetzen"
    bl_description = "Aktuelle Handy-Richtung = aktuelle Kamerarichtung"

    def execute(self, context):
        State.need_recenter = True
        return {'FINISHED'}


class PHONEBRIDGE_OT_copy_url(bpy.types.Operator):
    bl_idname = "phone_bridge.copy_url"
    bl_label = "Link kopieren"
    local: BoolProperty(default=False)

    def execute(self, context):
        context.window_manager.clipboard = State.local_url if self.local else State.app_url
        self.report({'INFO'}, "Link kopiert")
        return {'FINISHED'}


class PHONEBRIDGE_OT_look_through(bpy.types.Operator):
    bl_idname = "phone_bridge.look_through"
    bl_label = "Durch Handy-Kamera schauen"

    def execute(self, context):
        look_through(get_camera())
        return {'FINISHED'}


class PHONEBRIDGE_OT_clear_anim(bpy.types.Operator):
    bl_idname = "phone_bridge.clear_animation"
    bl_label = "Kamerafahrt löschen"
    bl_description = "Entfernt aufgezeichnete Keyframes der Handy-Kamera"

    def execute(self, context):
        cam = get_camera(create=False)
        if cam:
            cam.animation_data_clear()
            cam.data.animation_data_clear()
        return {'FINISHED'}


# =============================================================================
# UI
# =============================================================================
class PHONEBRIDGE_PT_main(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Phone"
    bl_label = "Phone Bridge"

    def draw(self, context):
        layout = self.layout
        s = settings(context)
        if State.server is None:
            layout.operator("phone_bridge.start", icon='PLAY')
            layout.prop(s, "port")
            layout.prop(s, "camera")
            return

        row = layout.row(align=True)
        row.operator("phone_bridge.stop", icon='PAUSE')
        row.prop(s, "show_qr", text="", icon='HIDE_OFF' if s.show_qr else 'HIDE_ON')

        box = layout.box()
        if State.active_client:
            box.label(text="Verbunden: %s" % State.client_addr, icon='CHECKMARK')
            mode = {"xr": "AR / 6DoF (Kamera-Tracking)", "gyro": "Gyrosensor"}.get(State.tracking_mode, "–")
            box.label(text="Tracking: %s" % mode, icon='ORIENTATION_GIMBAL')
            box.label(text="Stream: %.1f FPS" % State.fps, icon='VIEW_CAMERA')
            if State.recording:
                box.label(text="● Aufnahme läuft (%d)" % State.rec_count, icon='REC')
        else:
            box.label(text="Warte auf Handy …", icon='TIME')
            box.label(text="%s:%d" % (State.ip, State.port))
        row = box.row(align=True)
        row.operator("phone_bridge.copy_url", text="Link (GitHub)", icon='COPYDOWN').local = False
        row.operator("phone_bridge.copy_url", text="Link (lokal)", icon='COPYDOWN').local = True
        if State.error:
            box.label(text=State.error[:80], icon='ERROR')

        layout.prop(s, "camera")
        row = layout.row(align=True)
        row.operator("phone_bridge.recenter", icon='ORIENTATION_VIEW')
        row.operator("phone_bridge.look_through", text="", icon='OUTLINER_OB_CAMERA')


class PHONEBRIDGE_PT_stream(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Phone"
    bl_label = "Bewegung & Stream"
    bl_parent_id = "PHONEBRIDGE_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        s = settings(context)
        col = self.layout.column()
        col.prop(s, "move_speed")
        col.prop(s, "position_scale")
        col.prop(s, "smoothing", slider=True)
        col.separator()
        col.prop(s, "stream_resolution")
        col.prop(s, "jpeg_quality", slider=True)
        col.prop(s, "stream_fps")
        col.prop(s, "hide_overlays")
        col.prop(s, "follow_in_viewport")


class PHONEBRIDGE_PT_capture(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Phone"
    bl_label = "Foto & Video"
    bl_parent_id = "PHONEBRIDGE_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        s = settings(context)
        col = self.layout.column()
        col.prop(s, "photo_mode")
        col.prop(s, "photo_resolution")
        col.prop(s, "record_keyframes")
        col.prop(s, "record_stream_frames")
        col.prop(s, "capture_dir")
        col.operator("phone_bridge.clear_animation", icon='TRASH')


# =============================================================================
# Registration
# =============================================================================
classes = (
    PhoneBridgeSettings,
    PhoneBridgePreferences,
    PHONEBRIDGE_OT_start,
    PHONEBRIDGE_OT_stop,
    PHONEBRIDGE_OT_recenter,
    PHONEBRIDGE_OT_copy_url,
    PHONEBRIDGE_OT_look_through,
    PHONEBRIDGE_OT_clear_anim,
    PHONEBRIDGE_PT_main,
    PHONEBRIDGE_PT_stream,
    PHONEBRIDGE_PT_capture,
)


@bpy.app.handlers.persistent
def _on_load_pre(*_args):
    stop_bridge()


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.phone_bridge = PointerProperty(type=PhoneBridgeSettings)
    bpy.app.handlers.load_pre.append(_on_load_pre)


def unregister():
    stop_bridge()
    if _on_load_pre in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_on_load_pre)
    del bpy.types.Scene.phone_bridge
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
