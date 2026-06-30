#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_FILE="$PROJECT_DIR/logs/cron.log"
LOCK_FILE="$PROJECT_DIR/logs/hh_applier.lock"

mkdir -p "$PROJECT_DIR/logs"
exec 200>"$LOCK_FILE"
flock -n 200 || exit 0

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting hh-applier" >> "$LOG_FILE"
cd "$PROJECT_DIR"
# Передать переменную BYPASS_DAILY_CHECK внутрь контейнера, если она есть
if [ -n "${BYPASS_DAILY_CHECK-}" ]; then
  docker compose run --rm -e BYPASS_DAILY_CHECK="$BYPASS_DAILY_CHECK" hh-applier >> "$LOG_FILE" 2>&1
else
  docker compose run --rm hh-applier >> "$LOG_FILE" 2>&1
fi
STATUS=$?
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Finished with status $STATUS" >> "$LOG_FILE"
exit $STATUS
