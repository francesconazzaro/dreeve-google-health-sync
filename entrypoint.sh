#!/bin/bash
set -euo pipefail

# If a command was passed (e.g. `docker compose run --rm sync uv run --script
# /app/sync.py` for a one-off backfill), run that instead of the loop.
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

: "${SYNC_INTERVAL_SECONDS:=3600}"

echo "dreeve-google-health-sync starting (interval=${SYNC_INTERVAL_SECONDS}s)"

while true; do
    uv run --script /app/sync.py || echo "sync run failed, will retry next interval" >&2
    sleep "$SYNC_INTERVAL_SECONDS"
done
