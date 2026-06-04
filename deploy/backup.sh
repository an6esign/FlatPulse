#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")/.."

docker compose config --quiet
docker compose --profile ops run --rm backup
docker compose --profile ops run --rm backup sh -c 'ls -1t /backups | head -n 5'
