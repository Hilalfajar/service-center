#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' app/main.py)"
if [ -z "$VERSION" ]; then
    echo "Versi tidak ditemukan di app/main.py" >&2
    exit 1
fi

CONFIG_VERSION="$(sed -n 's/^version: \(.*\)$/\1/p' packaging/nfpm.yaml)"
if [ "$CONFIG_VERSION" != "$VERSION" ]; then
    echo "Versi packaging/nfpm.yaml ($CONFIG_VERSION) tidak sama dengan app/main.py ($VERSION)" >&2
    exit 1
fi

NFPM="${NFPM:-nfpm}"
if ! command -v "$NFPM" >/dev/null 2>&1; then
    echo "nfpm tidak ditemukan. Set NFPM=/path/to/nfpm" >&2
    exit 1
fi

STAGE="$ROOT/build/stage"
DIST="$ROOT/dist"
rm -rf "$STAGE"
mkdir -p \
    "$STAGE/usr/bin" \
    "$STAGE/usr/lib/service-center" \
    "$STAGE/usr/share/applications" \
    "$STAGE/usr/share/icons/hicolor/scalable/apps" \
    "$STAGE/usr/share/doc/service-center" \
    "$DIST"

cp -a app "$STAGE/usr/lib/service-center/"
find "$STAGE/usr/lib/service-center" -type d -name __pycache__ -prune -exec rm -rf {} +
install -m 0755 data/service-center.sh "$STAGE/usr/bin/service-center"
install -m 0644 README.md "$STAGE/usr/share/doc/service-center/README.md"
install -m 0644 data/service-center.svg \
    "$STAGE/usr/share/icons/hicolor/scalable/apps/service-center.svg"
sed "s|@EXEC@|/usr/bin/service-center|" data/service-center.desktop.in \
    > "$STAGE/usr/share/applications/io.github.hilalfajar.ServiceCenter.desktop"
chmod 0644 "$STAGE/usr/share/applications/io.github.hilalfajar.ServiceCenter.desktop"

"$NFPM" package --config packaging/nfpm.yaml --packager deb \
    --target "$DIST/service-center_${VERSION}_amd64.deb"
"$NFPM" package --config packaging/nfpm.yaml --packager rpm \
    --target "$DIST/service-center-${VERSION}-1.x86_64.rpm"
git archive --format=tar.gz --prefix="service-center-${VERSION}/" \
    -o "$DIST/service-center-${VERSION}.tar.gz" HEAD

echo "Paket dibuat di $DIST:"
ls -lh "$DIST"
