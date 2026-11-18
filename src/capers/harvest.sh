#!/usr/bin/env bash
set -euo pipefail

for dependency in brew macparakeet-cli jq instaloader; do
    if ! command -v "$dependency" >/dev/null 2>&1; then
        printf 'Required command not found: %s\n' "$dependency" >&2
        exit 1
    fi
done

# Find config.json beside this script, regardless of the current directory.
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
config="$script_dir/config.json"

jq -e '
    (.options | type == "object") and
    (.videos | type == "array") and
    (.videos | all(.[]; type == "string"))
' "$config" >/dev/null

options=()
output_dir=""
while IFS= read -r option; do
    # Expand output paths without evaluating shell code.
    case "$option" in
        "--dirname-pattern=~") option="--dirname-pattern=$HOME" ;;
        "--dirname-pattern=~/"*) option="--dirname-pattern=$HOME/${option#--dirname-pattern=\~/}" ;;
    esac
    if [[ "$option" == --dirname-pattern=* ]]; then
        output_dir="${option#--dirname-pattern=}"
    fi
    options+=("$option")
done < <(jq -r '
    .options | to_entries[] |
    if .value == true then "--\(.key)"
    elif .value == false or .value == null then empty
    else "--\(.key)=\(.value)"
    end
' "$config")

# Search the configured download folder, including its per-Reel subfolders.
output_dir="${output_dir%/\{target\}}"
if [[ -z "$output_dir" || "$output_dir" == *'{'* || "$output_dir" == *'}'* ]]; then
    printf 'dirname-pattern must be a folder, optionally ending in /{target}\n' >&2
    exit 1
fi
mkdir -p -- "$output_dir"

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

# Only start transcription once every download has finished.
while IFS= read -r -d '' video; do
    transcript="${video%.*}.txt"
    if [[ -e "$transcript" ]]; then
        printf 'Transcript already exists; keeping video: %s\n' "$video" >&2
        exit 1
    fi

    printf 'Transcribing %s\n' "$video"
    temporary_transcript="$(mktemp "$transcript.XXXXXX")"
    if macparakeet-cli transcribe "$video" --format transcript --no-history > "$temporary_transcript" &&
        [[ -s "$temporary_transcript" ]]; then
        mv -- "$temporary_transcript" "$transcript"
        rm -- "$video"
    else
        rm -- "$temporary_transcript"
        printf 'Transcription failed or was empty; keeping video: %s\n' "$video" >&2
        exit 1
    fi
done < <(find "$output_dir" -type f -iname '*.mp4' -print0)
