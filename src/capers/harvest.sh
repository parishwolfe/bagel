#!/usr/bin/env bash
set -euo pipefail

# Find config.json beside this script, regardless of the current directory.
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
config="$script_dir/config.json"

for dependency in jq instaloader; do
    if ! command -v "$dependency" >/dev/null 2>&1; then
        printf 'Required command not found: %s\n' "$dependency" >&2
        exit 1
    fi
done

jq -e '(.options | type == "object") and (.users | type == "array")' "$config" >/dev/null

options=()
while IFS= read -r option; do
    # Normalize usernames and expand output paths without evaluating shell code.
    case "$option" in
        --login=@*) option="--login=${option#--login=@}" ;;
        "--dirname-pattern=~") option="--dirname-pattern=$HOME" ;;
        "--dirname-pattern=~/"*) option="--dirname-pattern=$HOME/${option#--dirname-pattern=\~/}" ;;
    esac
    options+=("$option")
done < <(jq -r '
    .options | to_entries[] |
    if .value == true then "--\(.key)"
    elif .value == false or .value == null then empty
    else "--\(.key)=\(.value)"
    end
' "$config")

while IFS= read -r user; do
    printf 'Downloading Reels from %s\n' "$user"
    instaloader "${options[@]}" -- "${user#@}"
done < <(jq -r '.users[]' "$config")
