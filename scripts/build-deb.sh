#!/bin/bash
# Costruisce il pacchetto .deb (Architecture: all, compressione xz).
#
# Uso:
#   ./scripts/build-deb.sh
#
# Richiede i pacchetti di build: debhelper (>= 12), dh-python, python3.
# Su Debian/Ubuntu: apt-get install -y debhelper dh-python python3
# Il .deb finisce in dist/.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/firstcontact/__init__.py)
if [ -z "$VERSION" ]; then
    echo "versione non trovata in src/firstcontact/__init__.py" >&2
    exit 1
fi

mkdir -p dist

dpkg-buildpackage -us -uc -b
# dpkg-buildpackage scrive nella directory superiore.
for pkg in ../first-contact_"$VERSION"_*.deb; do
    [ -f "$pkg" ] && cp -v "$pkg" dist/
done
echo "pacchetti in $ROOT/dist:"
ls -1 dist/*.deb
