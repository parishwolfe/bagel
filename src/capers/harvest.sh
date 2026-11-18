#!/usr/bin/env bash
set -euo pipefail

for dependency in brew macparakeet-cli jq instaloader; do
    if ! command -v "$dependency" >/dev/null 2>&1; then
        printf 'Required command not found: %s\n' "$dependency" >&2
        exit 1
    fi
done

# Find input files beside this script, regardless of the current directory.
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
config="$script_dir/config.json"
targets="$script_dir/targets.txt"

if ! jq -e '
    .["wait-seconds"].min as $min |
    .["wait-seconds"].max as $max |
    (.options | type == "object") and
    ([$min, $max] | all(.[]; type == "number" and . == floor and . >= 0 and . <= 32767)) and
    ($min <= $max)
' "$config" >/dev/null; then
    printf 'Config requires options and wait-seconds with integers 0 <= min <= max <= 32767\n' >&2
    exit 1
fi
min_wait="$(jq -r '.["wait-seconds"].min | floor' "$config")"
max_wait="$(jq -r '.["wait-seconds"].max | floor' "$config")"

if [[ ! -f "$targets" ]]; then
    printf 'Create %s with one Instagram Reel URL per line\n' "$targets" >&2
    exit 1
fi

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

# Downloads and transcripts share one flat output folder.
if [[ -z "$output_dir" || "$output_dir" == *'{'* || "$output_dir" == *'}'* ]]; then
    printf 'dirname-pattern must be a folder without placeholders\n' >&2
    exit 1
fi
mkdir -p -- "$output_dir"

first_download=true
reel_url_pattern='^https://(www\.)?instagram\.com/reels?/([A-Za-z0-9_-]+)/?(\?[^#]*)?(#.*)?$'

while IFS= read -r url || [[ -n "$url" ]]; do
    url="${url%$'\r'}"
    if [[ "$url" =~ ^[[:space:]]*$ ]]; then
        continue
    fi
    if [[ ! "$url" =~ $reel_url_pattern ]]; then
        printf 'Invalid Instagram Reel URL: %s\n' "$url" >&2
        exit 1
    fi
    shortcode="${BASH_REMATCH[2]}"

    if [[ "$first_download" == false ]]; then
        delay=$((RANDOM % (max_wait - min_wait + 1) + min_wait))
        printf 'Waiting %s seconds before the next download\n' "$delay"
        sleep "$delay"
    fi

    printf 'Downloading Reel %s\n' "$shortcode"
    instaloader "${options[@]}" -- "-$shortcode"
    first_download=false
done < "$targets"

# Flatten videos and transcripts left by earlier runs; never overwrite files.
while IFS= read -r -d '' file; do
    destination="$output_dir/${file##*/}"
    if [[ -e "$destination" || -L "$destination" ]]; then
        printf 'Cannot flatten; filename already exists: %s\n' "$destination" >&2
        exit 1
    fi
    mv -- "$file" "$destination"
done < <(find "$output_dir" -mindepth 2 -type f \( -iname '*.mp4' -o -name '*.txt' \) -print0)
find "$output_dir" -depth -mindepth 1 -type d -empty -delete

# Pass every video in one call so the transcription model is loaded only once.
videos=()
while IFS= read -r -d '' video; do
    transcript="${video%.*}.txt"
    if [[ -e "$transcript" || -L "$transcript" ]]; then
        printf 'Transcript already exists; keeping video: %s\n' "$video" >&2
        exit 1
    fi
    videos+=("$video")
done < <(find "$output_dir" -maxdepth 1 -type f -iname '*.mp4' -print0)

if [[ ${#videos[@]} -eq 0 ]]; then
    exit 0
fi

macparakeet-cli transcribe "${videos[@]}" \
    --output-dir "$output_dir" --format transcript --no-history

# Keep all videos if the batch fails or any transcript is missing or empty.
for video in "${videos[@]}"; do
    if [[ ! -f "${video%.*}.txt" || ! -s "${video%.*}.txt" ]]; then
        printf 'Missing or empty transcript; keeping videos: %s\n' "$video" >&2
        exit 1
    fi
done
rm -- "${videos[@]}"
