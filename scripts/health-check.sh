#!/usr/bin/env bash
#
# Quick liveness check, meant for cron or an external monitor.
# Exit code 0 = healthy, 1 = degraded, 2 = critical. Add --quiet for cron.
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"

QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1
out() { [ "$QUIET" -eq 1 ] || printf '%s\n' "$*"; }

STATUS=0
problem() {
  STATUS=$(( $2 > STATUS ? $2 : STATUS ))
  out "$1"
}

for unit in securecam.service securecam-mediamtx.service; do
  if unit_active "$unit"; then
    out "ok      $unit is running"
  else
    problem "CRIT    $unit is not running (sudo systemctl status $unit)" 2
  fi
done

API_PORT="$(awk '/^mediamtx:/{f=1} f && /api_address:/{print $2; exit}' "$SECURECAM_CONFIG" 2>/dev/null | sed 's/.*://;s/"//g')"
API_PORT="${API_PORT:-9997}"
if has_cmd curl && curl -fsS --max-time 5 "http://127.0.0.1:${API_PORT}/v3/paths/list" 2>/dev/null | grep -q '"ready":true'; then
  out "ok      the camera is publishing"
else
  problem "CRIT    the camera is not publishing (sudo ./scripts/diagnose-camera.sh)" 2
fi

BUFFER="$SECURECAM_DATA_DIR/buffer"
if [ -d "$BUFFER" ] && [ -n "$(find "$BUFFER" -name '*.mp4' -newermt '-3 minutes' 2>/dev/null | head -n1)" ]; then
  out "ok      the rolling buffer is being written"
else
  problem "CRIT    no recording in the last 3 minutes (sudo ./scripts/diagnose-storage.sh)" 2
fi

FREE_KB="$(df -Pk "$SECURECAM_DATA_DIR" 2>/dev/null | awk 'NR==2 {print $4}')"
if [ "${FREE_KB:-0}" -lt 204800 ]; then
  problem "CRIT    less than 200 MB free on $SECURECAM_DATA_DIR" 2
elif [ "${FREE_KB:-0}" -lt 1048576 ]; then
  problem "WARN    less than 1 GB free on $SECURECAM_DATA_DIR" 1
else
  out "ok      $((FREE_KB / 1024)) MB free"
fi

TEMP_RAW="$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo 0)"
TEMP=$((TEMP_RAW / 1000))
if [ "$TEMP" -ge 80 ]; then
  problem "WARN    CPU at ${TEMP} C; the Pi is throttling (add a heatsink or improve airflow)" 1
elif [ "$TEMP" -gt 0 ]; then
  out "ok      CPU at ${TEMP} C"
fi

case "$STATUS" in
  0) out "healthy" ;;
  1) out "degraded" ;;
  *) out "critical" ;;
esac
exit "$STATUS"
