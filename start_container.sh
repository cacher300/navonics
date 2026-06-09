#!/usr/bin/env sh
set -eu

# Edit these few values if you want a different area or output folder.
IMAGE_NAME="${IMAGE_NAME:-navonics-downloader}"
WEST="${WEST:--180}"
SOUTH="${SOUTH:--85.05112878}"
EAST="${EAST:-180}"
NORTH="${NORTH:-85.05112878}"
ZOOM_MAX="${ZOOM_MAX:-16}"
ZOOM_MIN="${ZOOM_MIN:-0}"
OUT_DIR="${OUT_DIR:-./tiles_store}"
WORKERS="${WORKERS:-2}"
DELAY="${DELAY:-0.01}"
DELAY_JITTER="${DELAY_JITTER:-0.00}"
DEDUPE_REPORT_INTERVAL="${DEDUPE_REPORT_INTERVAL:-60}"
AUTH_RETRIES="${AUTH_RETRIES:-8}"

echo "Downloading whole-world bbox: $WEST $SOUTH $EAST $NORTH"
echo "Zoom range: $ZOOM_MIN-$ZOOM_MAX."
echo "Mode: sonar only, feet only, 10 ft shallow shading."

docker build -t "$IMAGE_NAME" .

docker run --rm -it \
  --shm-size=1g \
  -v "$(pwd):/app" \
  -w /app \
  "$IMAGE_NAME" \
  --refresh-headless \
  --bbox "$WEST" "$SOUTH" "$EAST" "$NORTH" \
  --zoom-min "$ZOOM_MIN" \
  --zoom-max "$ZOOM_MAX" \
  --out "$OUT_DIR" \
  --workers "$WORKERS" \
  --delay "$DELAY" \
  --delay-jitter "$DELAY_JITTER" \
  --dedupe-report-interval "$DEDUPE_REPORT_INTERVAL" \
  --auth-retries "$AUTH_RETRIES"
