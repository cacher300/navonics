#!/usr/bin/env sh
set -eu

IMAGE_NAME="${IMAGE_NAME:-navonics-downloader}"
CONTAINER_WORKDIR="/app"

docker build -t "$IMAGE_NAME" .

if [ "$#" -eq 0 ]; then
  set -- --help
fi

docker run --rm -it \
  --shm-size=1g \
  -v "$(pwd):$CONTAINER_WORKDIR" \
  -w "$CONTAINER_WORKDIR" \
  "$IMAGE_NAME" "$@"
