#!/usr/bin/env bash
set -euo pipefail

read -rp "CSV file: " INPUT

if [[ ! -f "$INPUT" ]]; then
echo "File not found: $INPUT"
exit 1
fi

BASE="${INPUT%.csv}"
OUTPUT="${BASE}_out.csv"
JOB="$(basename "$BASE")"

LOG_FILE="${JOB}.log"
PID_FILE="${JOB}.pid"
PGID_FILE="${JOB}.pgid"

rm -f "$PID_FILE" "$PGID_FILE"

setsid env \
PYTHONUNBUFFERED=1 \
GEONODE_TYPE=residential \
GEONODE_COUNTRY=pe \
uv run robot \
--input "$INPUT" \
--output "$OUTPUT" \
--workers 8 \
--page-size 5000 \
--session-budget 1 \
--wait-min-s 0 \
--wait-max-s 0 \
--ban-cooldown-s 30 \
--env-file .env \
--debug \
> "$LOG_FILE" 2>&1 &

PID=$!

sleep 1

STAT=$(ps -o stat= -p "$PID" 2>/dev/null | tr -d ' ')

if [[ -z "$STAT" || "$STAT" == Z* ]]; then
echo
echo "Process exited during startup. Last log lines:"
tail -n 40 "$LOG_FILE" || true
wait "$PID" 2>/dev/null || true
rm -f "$PID_FILE" "$PGID_FILE"
exit 1
fi

PGID=$(ps -o pgid= "$PID" | tr -d ' ')

echo "$PID" > "$PID_FILE"
echo "$PGID" > "$PGID_FILE"

echo
echo "Started:"
echo "  Input : $INPUT"
echo "  Output: $OUTPUT"
echo "  Log   : $LOG_FILE"
echo
echo "Monitor:"
echo "  tail -f $LOG_FILE"
echo
echo "Stop gracefully:"
echo "  kill -- -$PGID"
echo
echo "Force stop everything (robot, workers):"
echo "  kill -9 -- -$PGID"
echo
echo "Or later:"
echo "  kill -- -$(cat $PGID_FILE)"
echo "  kill -9 -- -$(cat $PGID_FILE)"
