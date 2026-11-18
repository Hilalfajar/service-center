#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
PREFIX="$DATA_HOME/service-center"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$DATA_HOME/applications"
ICON_ROOT="$DATA_HOME/icons/hicolor"
DESKTOP_ID="io.github.hilalfajar.ServiceCenter.desktop"

python3 - <<'PY' || { echo "Butuh python3-gobject + GTK4 + libadwaita." >&2; exit 1; }
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw  # noqa: F401
PY

install -d "$PREFIX" "$BIN_DIR" "$APP_DIR" "$ICON_ROOT/scalable/apps"

rm -rf "$PREFIX/app"
cp -a "$ROOT/app" "$PREFIX/"
cp -a "$ROOT/README.md" "$PREFIX/"
find "$PREFIX" -type d -name __pycache__ -prune -exec rm -rf {} +

cat > "$BIN_DIR/service-center" <<EOF
#!/usr/bin/env bash
exec python3 "$PREFIX/app/main.py" "\$@"
EOF
chmod +x "$BIN_DIR/service-center"

install -m 0644 "$ROOT/data/service-center.svg" "$ICON_ROOT/scalable/apps/service-center.svg"
for size in 64 128 256; do
    if [ -f "$ROOT/data/icons/service-center-$size.png" ]; then
        install -d "$ICON_ROOT/${size}x${size}/apps"
        install -m 0644 "$ROOT/data/icons/service-center-$size.png" \
            "$ICON_ROOT/${size}x${size}/apps/service-center.png"
    fi
done

sed "s|@EXEC@|$BIN_DIR/service-center|" "$ROOT/data/service-center.desktop.in" \
    > "$APP_DIR/$DESKTOP_ID"
chmod 0644 "$APP_DIR/$DESKTOP_ID"

if command -v update-desktop-database >/dev/null; then
    update-desktop-database -q "$APP_DIR" || true
fi
if command -v gtk4-update-icon-cache >/dev/null; then
    gtk4-update-icon-cache -q -t -f "$ICON_ROOT" 2>/dev/null || true
fi
if command -v kbuildsycoca6 >/dev/null; then
    kbuildsycoca6 --noincremental >/dev/null 2>&1 || true
fi

echo "Service Center terpasang."
echo "  launcher : $BIN_DIR/service-center"
echo "  menu     : $APP_DIR/$DESKTOP_ID"
