#!/bin/sh
set -eu
CLASH_APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec python3 "$CLASH_APP_DIR/install.py"
