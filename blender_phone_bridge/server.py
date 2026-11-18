# SPDX-License-Identifier: MIT
"""HTTPS + WebSocket server using only the Python standard library.

Runs in background threads. Blender's main thread talks to it only through
``BridgeServer.inbox`` (incoming messages) and the ``send_*`` methods, which
are thread safe. Nothing in here touches ``bpy``.
"""

import base64
import hashlib
import hmac
import html
import json
import mimetypes
import os
import queue
import select
import socket
import ssl
import struct
import threading
import time
import urllib.parse

_WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_PAYLOAD = 8 * 1024 * 1024


def get_local_ip():
    """Best guess for the LAN address of this machine (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except OSError:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


class _Client:
    """One WebSocket connection. A single thread does all socket I/O, because
    an ``ssl.SSLSocket`` must not be read and written from two threads."""
    _next_id = 1

    def __init__(self, server, sock, addr):
        self.id = _Client._next_id
        _Client._next_id += 1
        self.server = server
        self.sock = sock
        self.addr = addr
        self.alive = True
        self.connected_at = time.time()
        self._lock = threading.Lock()
        self._out = []          # control/binary messages, delivered in order
        self._frame = None      # only the newest video frame is kept
        self._rbuf = bytearray()
        self._fragments = []
        self._frag_opcode = 0

    # ---- queueing (any thread) ------------------------------------------
    def queue_json(self, obj):
        data = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        with self._lock:
            self._out.append((0x1, data))

    def queue_binary(self, data, replace_frame=True):
        with self._lock:
            if replace_frame:
                self._frame = data
            else:
                self._out.append((0x2, data))

    # ---- I/O thread -------------------------------------------------------
    @staticmethod
    def _encode(opcode, payload):
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", 0x80 | opcode, n)
        elif n < 65536:
            header = struct.pack("!BBH", 0x80 | opcode, 126, n)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 127, n)
        return header + payload

    def _flush(self):
        with self._lock:
            items, self._out = self._out, []
            frame, self._frame = self._frame, None
        if frame is not None:
            items.append((0x2, frame))
        for opcode, data in items:
            self.sock.sendall(self._encode(opcode, data))

    def run(self):
        self.sock.settimeout(10)
        try:
            while self.alive:
                self._flush()
                readable = self.sock.pending() > 0
                if not readable:
                    readable = bool(select.select([self.sock], [], [], 0.004)[0])
                if readable:
                    chunk = self.sock.recv(65536)
                    if not chunk:
                        break
                    self._rbuf += chunk
                    if not self._parse():
                        break
        except (OSError, ValueError):
            pass
        finally:
            self.close()

    def _parse(self):
        """Consume complete frames from the read buffer. False = close."""
        buf = self._rbuf
        while True:
            if len(buf) < 2:
                return True
            b1, b2 = buf[0], buf[1]
            fin, opcode = b1 & 0x80, b1 & 0x0F
            masked, length = b2 & 0x80, b2 & 0x7F
            pos = 2
            if length == 126:
                if len(buf) < 4:
                    return True
                length = struct.unpack_from("!H", buf, 2)[0]
                pos = 4
            elif length == 127:
                if len(buf) < 10:
                    return True
                length = struct.unpack_from("!Q", buf, 2)[0]
                pos = 10
            if length > _MAX_PAYLOAD:
                return False
            need = pos + (4 if masked else 0) + length
            if len(buf) < need:
                return True
            if masked:
                mask = bytes(buf[pos:pos + 4])
                payload = _unmask_fast(bytes(buf[pos + 4:need]), mask) if length else b""
            else:
                payload = bytes(buf[pos:need])
            del buf[:need]

            if opcode == 0x8:  # close
                try:
                    self.sock.sendall(self._encode(0x8, payload[:2]))
                except OSError:
                    pass
                return False
            if opcode == 0x9:  # ping -> pong
                self.sock.sendall(self._encode(0xA, payload))
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                self._frag_opcode = opcode
                self._fragments = [payload]
            elif opcode == 0x0:
                self._fragments.append(payload)
            else:
                return False
            if not fin:
                continue
            data = b"".join(self._fragments)
            self._fragments = []
            if self._frag_opcode == 0x1:
                try:
                    msg = json.loads(data.decode("utf-8"))
                except ValueError:
                    continue
                self.server.inbox.put((self.id, "json", msg))
            else:
                self.server.inbox.put((self.id, "binary", data))

    def close(self):
        if not self.alive:
            return
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self.server._client_closed(self)


def _unmask_fast(payload, mask):
    n = len(payload)
    m = int.from_bytes(mask * ((n + 3) // 4), "big") >> (8 * ((4 - n % 4) % 4))
    return (int.from_bytes(payload, "big") ^ m).to_bytes(n, "big")


class BridgeServer:
    def __init__(self, port, certfile, keyfile, token, web_dir, app_url_builder=None, max_clients=4):
        self.port = port
        self.token = token
        self.web_dir = web_dir
        self.app_url_builder = app_url_builder
        self.max_clients = max_clients
        self.inbox = queue.Queue()
        self.clients = {}
        self._lock = threading.Lock()
        self._running = False
        self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ctx.load_cert_chain(certfile, keyfile)
        self._sock = None
        self._thread = None

    # ---- lifecycle -------------------------------------------------------
    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.port))
        sock.listen(16)
        sock.settimeout(0.5)
        self._sock = sock
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        for c in list(self.clients.values()):
            c.close()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=2)
        self._sock = None

    @property
    def running(self):
        return self._running

    # ---- public API (thread safe) ---------------------------------------
    def client_ids(self):
        with self._lock:
            return list(self.clients.keys())

    def send_json(self, obj, client_id=None):
        with self._lock:
            targets = [self.clients[client_id]] if client_id in self.clients else \
                (list(self.clients.values()) if client_id is None else [])
        for c in targets:
            c.queue_json(obj)

    def send_frame(self, data, client_id):
        with self._lock:
            c = self.clients.get(client_id)
        if c:
            c.queue_binary(data, replace_frame=True)

    def send_binary(self, data, client_id):
        with self._lock:
            c = self.clients.get(client_id)
        if c:
            c.queue_binary(data, replace_frame=False)

    # ---- internals -------------------------------------------------------
    def _client_closed(self, client):
        with self._lock:
            removed = self.clients.pop(client.id, None)
        if removed is not None:
            self.inbox.put((client.id, "disconnect", None))

    def _accept_loop(self):
        while self._running:
            try:
                raw, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(raw, addr), daemon=True).start()

    def _handle(self, raw, addr):
        raw.settimeout(10)
        try:
            sock = self._ctx.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            # Typically the phone rejected our self-signed certificate.
            try:
                raw.close()
            except OSError:
                pass
            return
        try:
            request = self._read_http_head(sock)
            if request is None:
                sock.close()
                return
            method, target, headers = request
            url = urllib.parse.urlsplit(target)
            params = dict(urllib.parse.parse_qsl(url.query))

            if headers.get("upgrade", "").lower() == "websocket":
                self._upgrade(sock, addr, headers, params)
                return
            self._serve_http(sock, method, url.path, params)
        except (OSError, ValueError):
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _read_http_head(self, sock):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                return None
            data += chunk
            if len(data) > 32768:
                return None
        head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        lines = head.split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) < 2:
            return None
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        return parts[0], parts[1], headers

    # ---- websocket -------------------------------------------------------
    def _upgrade(self, sock, addr, headers, params):
        token = params.get("token", "")
        if not hmac.compare_digest(token.encode(), self.token.encode()):
            self._respond(sock, 403, "text/plain", b"invalid token")
            sock.close()
            return
        with self._lock:
            if len(self.clients) >= self.max_clients:
                full = True
            else:
                full = False
        if full:
            self._respond(sock, 503, "text/plain", b"too many clients")
            sock.close()
            return
        key = headers.get("sec-websocket-key", "")
        accept = base64.b64encode(hashlib.sha1(key.encode() + _WS_GUID).digest()).decode()
        sock.sendall((
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode())
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        client = _Client(self, sock, addr)
        with self._lock:
            self.clients[client.id] = client
        self.inbox.put((client.id, "connect", {"addr": addr[0]}))
        client.run()

    # ---- plain https -----------------------------------------------------
    def _respond(self, sock, status, ctype, body, extra_headers=None):
        reason = {200: "OK", 302: "Found", 403: "Forbidden", 404: "Not Found", 503: "Service Unavailable"}.get(status, "OK")
        head = ["HTTP/1.1 %d %s" % (status, reason),
                "Content-Type: %s" % ctype,
                "Content-Length: %d" % len(body),
                "Cache-Control: no-store",
                "Access-Control-Allow-Origin: *",
                "Access-Control-Allow-Private-Network: true",
                "Connection: close"]
        for k, v in (extra_headers or {}).items():
            head.append("%s: %s" % (k, v))
        sock.sendall(("\r\n".join(head) + "\r\n\r\n").encode() + body)

    def _serve_http(self, sock, method, path, params):
        if method == "OPTIONS":
            self._respond(sock, 200, "text/plain", b"", {
                "Access-Control-Allow-Methods": "GET", "Access-Control-Allow-Headers": "*"})
            return
        if path == "/ping":
            self._respond(sock, 200, "application/json", b'{"ok":true,"app":"blender-phone-bridge"}')
            return
        if path == "/accept":
            self._respond(sock, 200, "text/html; charset=utf-8", self._accept_page(params).encode("utf-8"))
            return
        if path in ("", "/"):
            path = "/index.html"
        rel = os.path.normpath(urllib.parse.unquote(path).lstrip("/"))
        full = os.path.join(self.web_dir, rel)
        if rel.startswith("..") or not os.path.isfile(full):
            self._respond(sock, 404, "text/plain", b"not found")
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            self._respond(sock, 200, ctype, f.read())

    def _accept_page(self, params):
        back = params.get("return", "")
        if not back.startswith("https://"):
            back = ""
        local = self.app_url_builder("local") if self.app_url_builder else "/"
        back_html = ('<a class="btn" href="%s">Zurück zur App</a>' % html.escape(back, quote=True)) if back else ""
        return """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Blender Phone Bridge</title>
<style>body{font-family:system-ui,sans-serif;background:#16181d;color:#eee;display:flex;min-height:100vh;
align-items:center;justify-content:center;margin:0;padding:16px;box-sizing:border-box}
.card{max-width:420px;text-align:center}.ok{font-size:56px}
.btn{display:block;margin:12px 0;padding:14px;border-radius:12px;background:#ea7600;color:#fff;
text-decoration:none;font-weight:600}.btn.alt{background:#2c313a}</style></head><body><div class="card">
<div class="ok">✓</div><h2>Zertifikat akzeptiert</h2>
<p>Dein Handy vertraut jetzt der Verbindung zu Blender.</p>
%s<a class="btn alt" href="%s">App direkt von Blender laden</a></div></body></html>""" % (
            back_html, html.escape(local, quote=True))
