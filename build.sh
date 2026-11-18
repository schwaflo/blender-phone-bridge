#!/usr/bin/env bash
# Baut blender_phone_bridge.zip zum Installieren in Blender.
# Kopiert die Web-App (docs/) ins Addon, damit Blender sie auch lokal ausliefern kann.
set -euo pipefail
cd "$(dirname "$0")"
rm -rf blender_phone_bridge/web blender_phone_bridge/__pycache__
mkdir -p blender_phone_bridge/web dist
cp docs/index.html blender_phone_bridge/web/
rm -f dist/blender_phone_bridge.zip
zip -rq dist/blender_phone_bridge.zip blender_phone_bridge -x '*/__pycache__/*' '*.DS_Store'
echo "Fertig: dist/blender_phone_bridge.zip"
