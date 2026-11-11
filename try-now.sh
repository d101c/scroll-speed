#!/usr/bin/env bash
# Try Scroll Speed right away, without logging out: asks for the one-time
# authorization if it is missing or older than this version, then rescales the
# mouse wheel, the touchpad and the pointing stick until Ctrl+C.
#
#   ./try-now.sh 0.5     half speed (the default)
#   ./try-now.sh 2       twice as fast
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

factor="${1:-0.5}"
if ! gjs -m tools/authorize.js --check > /dev/null; then
    echo "Asking for the one-time authorization (a udev rule)…"
    gjs -m tools/authorize.js
    sleep 1
fi
echo "Scroll speed ×$factor. Ctrl+C stops and gives the normal speed back."
exec python3 extension/helper/scroll_speed_helper.py \
    --vertical "$factor" --touchpad "$factor" --pointingstick "$factor"
