#!/bin/sh
set -eu
base=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -x "$base/dist/chefas-proxy-linux-x64/chefas-proxy" ]; then
  cd "$base"
  exec "$base/dist/chefas-proxy-linux-x64/chefas-proxy" "$@"
fi
if [ -d "$base/src" ]; then runtime="$base/bin/chefas-proxy"; else runtime="$base"; fi
if [ ! -f "$runtime/launch.py" ]; then
  echo "Compiled runtime missing. Run: python scripts/build_ext.py" >&2
  exit 1
fi
cd "$base"
exec "${CHEFAS_PROXY_PYTHON:-python}" "$runtime/launch.py" "$@"
