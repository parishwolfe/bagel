#!/bin/sh
# Install lox on this machine (macOS or Linux).
#
#   src/lox/install.sh                 install the agents into this checkout
#   src/lox/install.sh --harden        also write hard-deny policies (dedicated machine)
#   src/lox/install.sh --dry-run       show what would change
#   src/lox/install.sh --uninstall     remove the agents and commands
#   src/lox/install.sh --model review=anthropic/claude-sonnet-5-5 --model all=ollama/qwen3.8:27b
#
# It installs Python dependencies with uv, creates research/ with the seed
# backlog, copies the agents and commands into .opencode/, and runs a check.
# It does not touch providers, models or MCP servers, and it needs no sudo.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$ROOT"

need() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "lox install: '$1' was not found on PATH. $2" >&2
        exit 1
    }
}
need uv "Install it from https://docs.astral.sh/uv/ and run this again."
need git "Install git and run this again."
command -v opencode >/dev/null 2>&1 ||
    echo "lox install: 'opencode' is not on PATH yet; the agents are installed anyway." >&2

if [ "$(id -u)" -eq 0 ]; then
    echo "lox install: do not run this as root. Use the dedicated non-admin user." >&2
    exit 1
fi

# The install commands are for the operator; an agent's shell sets this variable.
unset OPENCODE_SESSION_ID

case " $* " in
*" --dry-run "* | *" --uninstall "*) ;;
*)
    uv sync
    uv run lox init
    ;;
esac

uv run lox install "$@"

case " $* " in
*" --dry-run "* | *" --uninstall "*) ;;
*)
    echo
    uv run lox doctor || {
        echo
        echo "lox install: fix the FAIL lines above, then run 'uv run lox doctor' again." >&2
        exit 1
    }
    ;;
esac
