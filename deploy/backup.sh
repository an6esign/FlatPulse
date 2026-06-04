#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")/.."

BACKUP_RETENTION_COUNT="${BACKUP_RETENTION_COUNT:-30}"

docker compose config --quiet
docker compose --profile ops run --rm backup
docker compose --profile ops run --rm -e BACKUP_RETENTION_COUNT="${BACKUP_RETENTION_COUNT}" backup sh -c '
set -eu
retention="${BACKUP_RETENTION_COUNT:-30}"
case "$retention" in
    ""|*[!0-9]*)
        echo "BACKUP_RETENTION_COUNT must be a non-negative integer"
        exit 2
        ;;
esac
if [ "$retention" -gt 0 ]; then
    ls -1t /backups/flatpulse_*.dump 2>/dev/null \
        | awk -v keep="$retention" "NR > keep {print}" \
        | while IFS= read -r file; do
            rm -f "$file"
        done
fi
'
docker compose --profile ops run --rm backup sh -c 'ls -1t /backups | head -n 5'
