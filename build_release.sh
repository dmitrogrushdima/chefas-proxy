#!/bin/sh
set -eu
base=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "${KONTUR_BUILD_PYTHON:-python3}" "$base/scripts/build_release.py" "$@"
