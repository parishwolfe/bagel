# Capers

Download individual Instagram Reels, then transcribe them locally in one batch.
Requires Homebrew, `jq`, `instaloader`, and `macparakeet-cli` on your PATH.

## Get Reel URLs

On a computer, open the Instagram video, click **...**, then **Copy link**.

Create or edit `targets.txt` beside `harvest.sh` and paste one complete Reel URL
per line, without quotes, bullets, or comments:

```text
https://www.instagram.com/reel/SHORTCODE_ONE/
https://www.instagram.com/reel/SHORTCODE_TWO/
```

Blank lines are ignored. `targets.txt` is ignored by Git.

## Run

From the repository root:

```sh
./src/capers/harvest.sh
```

Downloads run sequentially without login. After downloads finish, all videos
are transcribed in one MacParakeet invocation. Videos and `.txt` transcripts use
one flat folder, defaulting to `~/Desktop/capers/`, created if missing.
Videos are deleted only after the batch succeeds and every transcript is nonempty.
Existing transcripts are never overwritten; filename collisions stop the script.

## Configure

Edit `config.json`. `options.dirname-pattern` sets the output folder.
`wait-seconds` sets the inclusive random delay between downloads:

```json
"wait-seconds": {
    "min": 2,
    "max": 5
}
```

Use whole seconds from 0 to 32767, with `min` no greater than `max`.
These pauses do not guarantee protection from Instagram rate limits.

## Legality

This tool **is** in compliance with the law and Instagram's Terms of Service. The legal case setting this precedent can be found here:  
 [https://www.govinfo.gov/app/details/USCOURTS-cand-3_23-cv-00077/USCOURTS-cand-3_23-cv-00077-7/summary](https://www.govinfo.gov/app/details/USCOURTS-cand-3_23-cv-00077/USCOURTS-cand-3_23-cv-00077-7/summary)
