#!/bin/sh
# Esegue la suite di test (solo stdlib + il pacchetto).
set -eu
cd "$(dirname "$0")/.."
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src exec python3 -m unittest discover -s tests -t . -v
