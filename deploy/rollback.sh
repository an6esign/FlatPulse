#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")/.."

TARGET_COMMIT="${1:-${ROLLBACK_COMMIT:-}}"
if [ -z "${TARGET_COMMIT}" ] && [ -f .flatpulse_previous_commit ]; then
    TARGET_COMMIT="$(cat .flatpulse_previous_commit)"
fi

if [ -z "${TARGET_COMMIT}" ]; then
    echo "Target commit is required, for example:"
    echo "sh deploy/rollback.sh <commit>"
    echo "or set ROLLBACK_COMMIT=<commit>"
    exit 2
fi

sh deploy/backup.sh
git fetch --all --tags
git checkout "${TARGET_COMMIT}"
docker compose config --quiet

BUILD_SERVICES="bot worker migrate"
SERVICES="bot worker"
if docker compose config --services | grep -q '^webhook$'; then
    BUILD_SERVICES="${BUILD_SERVICES} webhook"
    SERVICES="${SERVICES} webhook"
fi
if docker compose config --services | grep -q '^caddy$'; then
    SERVICES="${SERVICES} caddy"
fi

docker compose build ${BUILD_SERVICES}

if [ -n "${RESTORE_BACKUP_FILE:-}" ]; then
    docker compose stop ${SERVICES}
    BACKUP_FILE="${RESTORE_BACKUP_FILE}" docker compose --profile ops run --rm restore
    docker compose run --rm migrate
    docker compose up -d ${SERVICES}
    docker compose run --rm --no-deps worker cian-rent-alerts --healthcheck
    docker compose ps
else
    docker compose run --rm migrate
    docker compose up -d ${SERVICES}
    docker compose run --rm --no-deps worker cian-rent-alerts --healthcheck
    docker compose ps
fi
