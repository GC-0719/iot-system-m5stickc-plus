#!/usr/bin/env bash
# Compile the AI entry points to standalone binaries with Nuitka (HW2, req 3).
#
#   bash ai/build.sh
#
# Produces ai/dist/main_ai_mqtt.bin and ai/dist/main_ai_influx.bin -- each a
# single self-contained executable with all imported modules (numpy, paho-mqtt,
# influxdb, yaml, and processing.py) tracked and bundled. This resists casual
# reverse engineering: there is no readable .py to inspect.
#
# config.yaml is deliberately NOT bundled -- it is read at runtime from next to
# the binary or the current directory, so re-configuring needs no rebuild (that
# is the point of the YAML requirement).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${PY:-$HOME/.cps-venv/bin/python}"
DIST="$HERE/dist"

"$PY" -m pip install -q nuitka

mkdir -p "$DIST"
for entry in main_ai_mqtt main_ai_influx; do
  echo "=== compiling $entry ==="
  "$PY" -m nuitka \
      --standalone --onefile \
      --include-module=processing \
      --output-dir="$DIST" \
      --output-filename="$entry.bin" \
      --assume-yes-for-downloads \
      --remove-output \
      "$HERE/$entry.py"
done

echo
echo "built:"
ls -lh "$DIST"/*.bin
echo
echo "config.yaml stays external -- run a binary from a dir containing it,"
echo "or pass --config <path>."
