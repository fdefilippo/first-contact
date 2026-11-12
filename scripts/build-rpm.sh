#!/bin/bash
# Costruisce i pacchetti RPM noarch per Enterprise Linux 8/9/10.
#
# Uso:
#   ./scripts/build-rpm.sh                 # build nella macchina corrente
#   podman run --rm -v "$PWD:/src:z" -w /src rockylinux:9 \
#       bash -c 'dnf install -y rpm-build python3-rpm-macros \
#                systemd-rpm-macros && ./scripts/build-rpm.sh'
#
# Su EL8 le macro systemd sono fornite dal pacchetto systemd; da EL9 in poi
# serve systemd-rpm-macros.
#
# I pacchetti finiscono in dist/.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/firstcontact/__init__.py)
if [ -z "$VERSION" ]; then
    echo "versione non trovata in src/firstcontact/__init__.py" >&2
    exit 1
fi

NAME=first-contact
TOP="$ROOT/build/rpm"
DIST="$ROOT/dist"

rm -rf "$TOP"
mkdir -p "$TOP"/BUILD "$TOP"/BUILDROOT "$TOP"/RPMS "$TOP"/SOURCES "$TOP"/SPECS "$TOP"/SRPMS
mkdir -p "$DIST"

# Sorgente: albero corrente, senza file di build e senza metadati VCS.
tar -czf "$TOP/SOURCES/$NAME-$VERSION.tar.gz" \
    --exclude-vcs \
    --exclude='./build' \
    --exclude='./dist' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --transform "s,^\.,$NAME-$VERSION," \
    .

rpmbuild -bb packaging/first-contact.spec \
    --define "_topdir $TOP" \
    --define "_sourcedir $TOP/SOURCES"

find "$TOP/RPMS" -type f -name '*.rpm' -exec cp -v {} "$DIST"/ \;
echo "pacchetti in $DIST:"
ls -1 "$DIST"/*.rpm
