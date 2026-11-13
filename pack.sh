#!/usr/bin/env bash
# Builds scroll-speed@griffit.gmail.com.shell-extension.zip in the repo root.
# Install it with: gnome-extensions install --force <zip>
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$REPO/extension"
DOMAIN="scroll-speed"
ZIP="$REPO/scroll-speed@griffit.gmail.com.shell-extension.zip"

# Keep the source tree's .mo files current, for runs straight from a checkout.
for po in "$SRC"/po/*.po; do
    lang="$(basename "$po" .po)"
    dest="$SRC/locale/$lang/LC_MESSAGES"
    mkdir -p "$dest"
    msgfmt --check "$po" -o "$dest/$DOMAIN.mo"
done

gnome-extensions pack "$SRC" \
  --extra-source=model.js \
  --extra-source=permission.js \
  --podir=po \
  --gettext-domain="$DOMAIN" \
  --schema=schemas/org.gnome.shell.extensions.scroll-speed.gschema.xml \
  --out-dir="$REPO" \
  --force

# `gnome-extensions pack` flattens extra sources to their basename, so the
# helper/ directory is added to the zip afterwards with its path preserved.
# The licence is copied next to the code so the zip is self-contained.
cp "$REPO/LICENSE" "$SRC/LICENSE"
( cd "$SRC" && zip -q "$ZIP" helper/scroll_speed_helper.py LICENSE )

echo "Built: $ZIP"
