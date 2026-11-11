#!/bin/sh
set -eu
base=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "${CHEFAS_PROXY_BUILD_PYTHON:-python3}" "$base/build_release.py" "$@"
