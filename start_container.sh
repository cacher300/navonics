#!/usr/bin/env sh
set -eu

# Edit these few values if you want a different area or output folder.
IMAGE_NAME="${IMAGE_NAME:-navonics-downloader}"
ANCHOR_TILE="${ANCHOR_TILE:-16/18322/24033}"
MARGIN="${MARGIN:-4}"
ZOOM_MAX="${ZOOM_MAX:-16}"
ZOOM_MIN="${ZOOM_MIN:-0}"
OUT_DIR="${OUT_DIR:-./tiles_store}"
WORKERS="${WORKERS:-2}"
DELAY="${DELAY:-0.20}"
DELAY_JITTER="${DELAY_JITTER:-0.20}"

docker build -t "$IMAGE_NAME" .

docker run --rm -it \
  --shm-size=1g \
  -v "$(pwd):/app" \
  -w /app \
  "$IMAGE_NAME" \
  --refresh-headless \
  --anchor-tile "$ANCHOR_TILE" \
  --margin "$MARGIN" \
  --zoom-min "$ZOOM_MIN" \
  --zoom-max "$ZOOM_MAX" \
  --out "$OUT_DIR" \
  --workers "$WORKERS" \
  --delay "$DELAY" \
  --delay-jitter "$DELAY_JITTER"
