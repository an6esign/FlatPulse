#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")/.."

git pull --ff-only
docker compose config --quiet
docker compose build bot worker migrate
docker compose run --rm migrate
docker compose up -d bot worker
docker compose run --rm --no-deps worker cian-rent-alerts --healthcheck
docker compose ps
