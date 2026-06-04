#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")/.."

PREVIOUS_COMMIT="$(git rev-parse HEAD)"

sh deploy/backup.sh
git pull --ff-only
printf "%s" "${PREVIOUS_COMMIT}" > .flatpulse_previous_commit
docker compose config --quiet
docker compose build bot worker webhook migrate
docker compose run --rm migrate
docker compose up -d bot worker webhook caddy
docker compose run --rm --no-deps worker cian-rent-alerts --healthcheck
docker compose ps
