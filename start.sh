#!/bin/sh
set -eu
base=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -d "$base/src" ]; then runtime="$base/bin/kontur"; else runtime="$base"; fi
if [ ! -f "$runtime/launch.py" ]; then
  echo "Compiled runtime missing. Run: python build_ext.py" >&2
  exit 1
fi
cd "$base"
exec "${KONTUR_PYTHON:-python}" "$runtime/launch.py" "$@"
