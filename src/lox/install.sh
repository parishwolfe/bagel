#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
for argument in "$@"; do
    if [ "$argument" = "--dry-run" ]; then
        exec uv run --no-sync python src/lox/install.py "$@"
    fi
done
command -v opencode >/dev/null
uv sync --locked
exec uv run --no-sync python src/lox/install.py "$@"
