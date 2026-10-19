#!/bin/bash
# automate_workflow.sh — fetch -> analyze -> sort example.
#
# The original use case: a DJ's download pipeline.
#   1. Fetch:  new files land in an incoming folder (download manager,
#              store, sample pack, or auto-downloader like Lidarr/SABnzbd).
#   2. Analyze: lossless_verify.py scans the incoming folder.
#   3. Sort:   files land in PASS / SUSPECT / FAIL (+ tiers).
#              Only SUSPECT / FAIL need human review. PASS goes to the library.
#
# Usage:
#   ./automate_workflow.sh [incoming_dir] [output_base_dir]
#
# Defaults: ./incoming -> ./verified/<timestamp>/
# Pass --move behaviour by setting MOVE=1 (default is copy, non-destructive).
#
# Other automation hooks:
#   - Download-manager post-processing / custom-script hook:
#       python3 lossless_verify.py "<downloaded path>" --output-dir ./verified --move
#   - Cron nightly sweep:
#       0 2 * * * /path/to/automate_workflow.sh ~/Music/incoming ~/Music/verified
#   - Pre-import gate: run before importing into Rekordbox / Serato / DAW.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INCOMING="${1:-$SCRIPT_DIR/incoming}"
OUTPUT_BASE="${2:-$SCRIPT_DIR/verified}"
MOVE="${MOVE:-0}"

if [ ! -d "$INCOMING" ]; then
  echo "incoming dir not found: $INCOMING" >&2
  exit 1
fi

# Preflight: ffmpeg + Python deps, so a cron run fails fast with a clear
# message instead of a traceback mid-scan.
command -v ffmpeg >/dev/null 2>&1 || {
  echo "ffmpeg not found on PATH — install ffmpeg first." >&2; exit 1; }
python3 -c "import numpy, mutagen" 2>/dev/null || {
  echo "Python deps missing — run: pip install -r requirements.txt" >&2; exit 1; }

# If nothing arrived since the last run, exit quietly instead of leaving
# an empty timestamped folder behind.
FILE_COUNT=$(find "$INCOMING" -type f \( \
  -iname '*.flac' -o -iname '*.wav' -o -iname '*.aiff' -o -iname '*.aif' \
  -o -iname '*.mp3' -o -iname '*.m4a' -o -iname '*.ogg' -o -iname '*.opus' \
  \) -print -quit | wc -l)
if [ "$FILE_COUNT" -eq 0 ]; then
  echo "incoming is empty ($INCOMING) — nothing to verify, exiting."
  exit 0
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
OUTPUT="$OUTPUT_BASE/$STAMP"

ARGS=(python3 "$SCRIPT_DIR/lossless_verify.py" "$INCOMING"
  --output-dir "$OUTPUT"
  --json)
if [ "$MOVE" = "1" ]; then
  ARGS+=(--move)
fi

echo "fetch:   $INCOMING"
echo "analyze: lossless_verify.py"
echo "sort to: $OUTPUT (mode: $([ "$MOVE" = "1" ] && echo move || echo copy))"
"${ARGS[@]}"

echo
echo "Done."
echo "  PASS files:    $OUTPUT/PASS/        -> import these"
echo "  Review:        $OUTPUT/SUSPECT/     -> audition by tier (NEAR_CD / BORDERLINE / LOW_QUALITY)"
echo "  Review:        $OUTPUT/FAIL/        -> audition by tier before deleting"
echo "  Summary:       $OUTPUT/summary.json"
