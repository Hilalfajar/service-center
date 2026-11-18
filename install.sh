#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$DATA_HOME/applications"
ICON_ROOT="$DATA_HOME/icons/hicolor"
DESKTOP_ID="service-center.desktop"

cd "$ROOT"
cargo build --release

install -d "$BIN_DIR" "$APP_DIR" "$ICON_ROOT/scalable/apps"
install -m 0755 "$ROOT/target/release/service-center-vrust" "$BIN_DIR/service-center"

# Ikon sama persis dengan versi Python
install -m 0644 "$ROOT/data/service-center.svg" "$ICON_ROOT/scalable/apps/service-center.svg"
for size in 64 128 256; do
    install -d "$ICON_ROOT/${size}x${size}/apps"
    install -m 0644 "$ROOT/data/icons/service-center-$size.png" \
        "$ICON_ROOT/${size}x${size}/apps/service-center.png"
done

sed "s|@EXEC@|$BIN_DIR/service-center|" "$ROOT/data/service-center.desktop.in" \
    > "$APP_DIR/$DESKTOP_ID"
chmod 0644 "$APP_DIR/$DESKTOP_ID"

command -v update-desktop-database >/dev/null && \
    update-desktop-database -q "$APP_DIR" || true
command -v gtk4-update-icon-cache >/dev/null && \
    gtk4-update-icon-cache -q -t -f "$ICON_ROOT" 2>/dev/null || true
command -v kbuildsycoca6 >/dev/null && \
    kbuildsycoca6 --noincremental >/dev/null 2>&1 || true

echo "Service Center (Rust) terpasang."
echo "  launcher : $BIN_DIR/service-center"
echo "  menu     : $APP_DIR/$DESKTOP_ID"
echo "  ikon     : $ICON_ROOT/*/apps/service-center.{svg,png}"
