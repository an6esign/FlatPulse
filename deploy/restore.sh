#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")/.."

if [ -z "${BACKUP_FILE:-}" ]; then
    echo "BACKUP_FILE is required, for example:"
    echo "BACKUP_FILE=flatpulse_YYYYmmdd_HHMMSS.dump sh deploy/restore.sh"
    exit 2
fi

docker compose config --quiet
docker compose stop bot worker webhook
BACKUP_FILE="${BACKUP_FILE}" docker compose --profile ops run --rm restore
docker compose run --rm migrate
docker compose up -d bot worker webhook
docker compose run --rm --no-deps worker cian-rent-alerts --healthcheck
docker compose ps
