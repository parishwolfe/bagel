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

jq -e '
    (.options | type == "object") and
    (.videos | type == "array") and
    (.videos | all(.[]; type == "string"))
' "$config" >/dev/null

options=()
while IFS= read -r option; do
    # Expand output paths without evaluating shell code.
    case "$option" in
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

first_download=true
reel_url_pattern='^https://(www\.)?instagram\.com/reels?/([A-Za-z0-9_-]+)/?(\?[^#]*)?(#.*)?$'

while IFS= read -r url; do
    if [[ ! "$url" =~ $reel_url_pattern ]]; then
        printf 'Invalid Instagram Reel URL: %s\n' "$url" >&2
        exit 1
    fi
    shortcode="${BASH_REMATCH[2]}"

    if [[ "$first_download" == false ]]; then
        delay=$((RANDOM % 4 + 2))
        printf 'Waiting %s seconds before the next download\n' "$delay"
        sleep "$delay"
    fi

    printf 'Downloading Reel %s\n' "$shortcode"
    instaloader "${options[@]}" -- "-$shortcode"
    first_download=false
done < <(jq -r '.videos[]' "$config")
