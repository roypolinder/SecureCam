#!/usr/bin/env bash
# Runs every diagnostic and finishes with the service's own health report.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"

TOTAL_FAILED=0

run() {
  local name="$1" script="$2"
  printf '\n%s\n' "${C_BOLD}############ $name ############${C_RESET}"
  if [ ! -f "$script" ]; then
    printf '  missing: %s\n' "$script"
    TOTAL_FAILED=$((TOTAL_FAILED + 1))
    return
  fi
  bash "$script" || TOTAL_FAILED=$((TOTAL_FAILED + 1))
}

printf '%s\n' "${C_BOLD}SecureCam diagnostics${C_RESET}"
printf '  host   : %s\n' "$(hostname)"
printf '  model  : %s\n' "$(pi_model)"
printf '  kernel : %s\n' "$(uname -r)"
printf '  date   : %s\n' "$(date)"
printf '  uptime : %s\n' "$(uptime -p 2>/dev/null || true)"

printf '\n%s\n' "${C_BOLD}############ Services ############${C_RESET}"
for unit in securecam-mediamtx.service securecam.service; do
  if unit_active "$unit"; then
    ok "$unit active since $(systemctl show -p ActiveEnterTimestamp --value "$unit")"
  else
    bad "$unit is not running"
    explain "" "" "sudo journalctl -u ${unit%.service} -n 50 --no-pager"
  fi
done

printf '\n%s\n' "${C_BOLD}############ Configuration ############${C_RESET}"
if [ -x "$SECURECAM_ADMIN" ]; then
  "$SECURECAM_ADMIN" --config "$SECURECAM_CONFIG" check-config || TOTAL_FAILED=$((TOTAL_FAILED + 1))
else
  bad "securecam-admin is not installed at $SECURECAM_ADMIN"
  explain "" "the installer did not finish" "sudo ./install.sh"
fi

run "Camera" "$HERE/diagnose-camera.sh"
run "Storage" "$HERE/diagnose-storage.sh"
run "Network" "$HERE/diagnose-network.sh"
run "PIR sensor" "$HERE/diagnose-pir.sh"

printf '\n%s\n' "${C_BOLD}############ Service health report ############${C_RESET}"
if [ -x "$SECURECAM_ADMIN" ]; then
  "$SECURECAM_ADMIN" --config "$SECURECAM_CONFIG" diagnose || TOTAL_FAILED=$((TOTAL_FAILED + 1))
fi

printf '\n%s\n' "${C_BOLD}############ Recent errors ############${C_RESET}"
journalctl -u securecam -u securecam-mediamtx -p err -n 20 --no-pager 2>/dev/null | sed 's/^/  /' || \
  say "  (journalctl is unavailable)"

printf '\n'
if [ "$TOTAL_FAILED" -eq 0 ]; then
  printf '%s\n' "${C_GREEN}${C_BOLD}All diagnostics passed.${C_RESET}"
  exit 0
fi
printf '%s\n' "${C_RED}${C_BOLD}$TOTAL_FAILED diagnostic section(s) reported a failure.${C_RESET}"
printf '%s\n' "Read the FAIL lines above: each one says what broke, why, and what to do next."
printf '%s\n' "If you are opening a bug report, include the output of this script."
exit 1
