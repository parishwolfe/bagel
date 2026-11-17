#!/bin/sh
# Optional unattended loop, on the research machine after installation.
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
trap 'exit 130' INT TERM
while [ ! -f research/STOP ] && grep -q '^- \[ \]' research/backlog.md; do
    before=$(cksum research/researched.md)
    opencode run --agent lox "Run exactly one pending topic through your prescribed parent/subagent pipeline."
    if [ "$before" = "$(cksum research/researched.md)" ]; then
        echo "No topic recorded; inspect the session and checkpoint before resuming." >&2
        exit 1
    fi
done
