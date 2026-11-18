# Blender Phone Bridge

Dein Handy als virtuelle Kamera für Blender:

- **Umschauen per Gyrosensor**: Handy drehen → Blender-Kamera dreht sich mit
- **AR-Tracking (6DoF)** über die Handykamera (WebXR/ARCore): Wenn du durch den Raum gehst, bewegt sich die Kamera in Blender mit
- **Live-Bild** der Szene auf dem Handy-Display
- **Joystick** zum Laufen, Regler für hoch/runter, Wischen zum Drehen, zwei Finger für Zoom
- **Fotos** (schnell aus dem Viewport oder als echter Render) und **Videos**:
  die Kamerafahrt wird als Keyframes in Blender gespeichert, das Livebild als JPEG-Sequenz, und auf Wunsch zusätzlich als Video auf dem Handy

Das Addon braucht keine zusätzlichen Python-Pakete. QR-Code, TLS-Zertifikat und WebSocket-Server sind komplett mit der Python-Standardbibliothek umgesetzt.

## Aufbau

```
blender_phone_bridge/   Blender-Addon
  __init__.py           UI, Kamera-Steuerung, Live-Stream, Foto/Video
  server.py             HTTPS- + WebSocket-Server (Threads)
  certgen.py            selbstsigniertes TLS-Zertifikat (pures Python)
  qr.py                 QR-Code-Encoder
docs/index.html         Handy-Web-App (wird über GitHub Pages veröffentlicht)
build.sh                baut dist/blender_phone_bridge.zip
```

## Einrichtung

### 1. Web-App auf GitHub Pages veröffentlichen

1. Repository `blender-phone-bridge` auf GitHub anlegen und dieses Projekt pushen.
2. Auf GitHub unter **Settings → Pages**: *Deploy from a branch*, Branch `main`, Ordner `/docs`.
3. Danach ist die App unter `https://schwaflo.github.io/blender-phone-bridge/` erreichbar.
   Eine andere Adresse kannst du in Blender unter *Einstellungen → Add-ons → Phone Bridge → GitHub-Pages-URL* eintragen.

### 2. Addon installieren

```bash
./build.sh
```

In Blender: *Bearbeiten → Einstellungen → Add-ons → ⌄ → Von Datenträger installieren…* → `dist/blender_phone_bridge.zip`.

### 3. Verbinden

1. Im 3D-Viewport mit `N` die Seitenleiste öffnen → Tab **Phone** → **Handy verbinden**.
2. Im Viewport erscheint ein QR-Code. Mit der Handykamera scannen.
3. Die GitHub-Seite öffnet sich mit Blenders Adresse und einem Zugangstoken. Auf **Verbinden** tippen und den Zugriff auf die Bewegungssensoren erlauben.

Handy und Computer müssen im **selben WLAN** sein.

#### Einmalig: Zertifikat akzeptieren

Handy-Browser geben Sensoren, Kamera und AR nur auf HTTPS-Seiten frei, und eine HTTPS-Seite darf nur verschlüsselt (`wss://`) mit Blender sprechen. Blender erzeugt dafür ein eigenes, selbstsigniertes Zertifikat. Beim ersten Mal zeigt die App deshalb **„Zertifikat akzeptieren“** an:
Link öffnen → *Erweitert → Trotzdem fortfahren* (iPhone: *Details einblenden → diese Website besuchen*) → **Zurück zur App**.

Klappt es trotzdem nicht (vor allem auf dem iPhone), tippe auf **„App direkt von Blender laden“**. Dann kommt dieselbe App direkt von Blender, und alles läuft über dieselbe Adresse.
Alternativ: In den Addon-Einstellungen *QR-Code öffnet → Direkt von Blender* wählen.

Fragt Chrome nach Zugriff auf **Geräte im lokalen Netzwerk**, musst du ihn erlauben.

## Bedienung am Handy

| Geste / Button        | Wirkung                                              |
|-----------------------|------------------------------------------------------|
| Handy drehen          | Kamera schaut sich um                                |
| Handy bewegen (AR)    | Kamera bewegt sich im Raum (nur im AR-Modus)         |
| Joystick links        | vorwärts/rückwärts/seitlich laufen                   |
| Regler rechts         | hoch/runter                                          |
| Wischen               | Blickrichtung drehen (gut im Sitzen)                 |
| Zwei Finger           | Zoom (Brennweite)                                    |
| ◎ oben                | Ausrichtung zurücksetzen                             |
| Weißer Auslöser       | Foto: wird in Blender gespeichert und aufs Handy geschickt |
| Roter Auslöser        | Video starten/stoppen                                |

Fotos und Videobilder landen in `//phone_captures/` neben der .blend-Datei. Ist die Datei noch nicht gespeichert, landen sie in `~/BlenderPhoneBridge`.

## Hinweise & Grenzen

- **AR-Tracking (6DoF)** gibt es nur auf Android mit Chrome und ARCore. iOS Safari unterstützt WebXR-AR nicht, dort steht Gyro + Joystick zur Verfügung.
- Der Live-Stream nutzt die Shading-Einstellung des größten 3D-Viewports (Solid/Material Preview). Für flüssiges Streaming lieber nicht *Rendered* mit Cycles verwenden. Für schöne Bilder gibt es den Foto-Modus **Render**.
- Ein 3D-Viewport muss sichtbar sein, damit gestreamt werden kann.
- Ist der Port belegt, nimmt Blender automatisch den nächsten freien Port (8766, 8767, …).
- Jede Sitzung bekommt ein neues Zufallstoken. Nur wer den QR-Code hat, kann sich verbinden.
