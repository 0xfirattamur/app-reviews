#!/usr/bin/env sh
# Prepare versioned files on release/vX.Y.Z. Never commits, tags, pushes, or merges.
set -eu

exec python3 "$(dirname "$0")/prepare_release.py" "$@"
