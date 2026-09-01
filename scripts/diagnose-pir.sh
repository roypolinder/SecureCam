#!/usr/bin/env bash
# PIR diagnostics: is the pin readable, is it wired, does it ever change?
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"

GPIO="$(awk '/^motion:/{f=1} f && /^[[:space:]]*gpio:/{print $2; exit}' "$SECURECAM_CONFIG" 2>/dev/null || true)"
GPIO="${GPIO:-17}"
WATCH_SECONDS="${1:-20}"

step "Configuration"
ok "config says the PIR data pin is BCM GPIO $GPIO"
source_mode="$(awk '/^motion:/{f=1} f && /^[[:space:]]*source:/{print $2; exit}' "$SECURECAM_CONFIG" 2>/dev/null || true)"
if [ "$source_mode" = "disabled" ]; then
  warn "motion.source is 'disabled', so no events will ever be recorded"
  explain "" "someone turned motion detection off" "set motion.source: pir in $SECURECAM_CONFIG"
fi

step "Kernel GPIO support"
if [ -e /dev/gpiochip0 ]; then
  ok "/dev/gpiochip0 exists"
  if has_cmd gpiodetect; then
    gpiodetect 2>/dev/null | sed 's/^/          /'
  fi
else
  bad "/dev/gpiochip0 does not exist"
  explain "The PIR sensor cannot be read at all." \
          "an unusual kernel, or a container without device access" \
          "run this on Raspberry Pi OS directly, not inside a container"
fi

if getent group gpio >/dev/null 2>&1; then
  if id -nG "$SECURECAM_USER" 2>/dev/null | tr ' ' '\n' | grep -qx gpio; then
    ok "$SECURECAM_USER is in the 'gpio' group"
  else
    bad "$SECURECAM_USER is not in the 'gpio' group"
    explain "The service cannot open the pin, so motion detection is dead." \
            "the account was created before the group existed" \
            "sudo usermod -aG gpio $SECURECAM_USER && sudo systemctl restart securecam"
  fi
fi

step "Claiming the pin as $SECURECAM_USER"
PYTHON="$SECURECAM_VENV/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
CLAIM="from gpiozero import DigitalInputDevice; d = DigitalInputDevice($GPIO); print(d.pin_factory.__class__.__name__); d.close()"
if unit_active securecam.service; then
  warn "securecam.service holds the pin, so this test is skipped"
  explain "" "the service claimed GPIO $GPIO first" \
          "sudo systemctl stop securecam && sudo $0 && sudo systemctl start securecam"
elif claim_output="$(runuser -u "$SECURECAM_USER" -- "$PYTHON" -c "$CLAIM" 2>&1)"; then
  ok "$SECURECAM_USER can open GPIO $GPIO (pin factory: $claim_output)"
else
  bad "$SECURECAM_USER cannot open GPIO $GPIO"
  say "$claim_output" | sed 's/^/          /'
  explain "The service reports 'pir - critical' even though this script works as root." \
          "a missing gpio group membership, or a pin factory that only root can use" \
          "sudo usermod -aG gpio $SECURECAM_USER && sudo systemctl restart securecam"
fi

step "Live reading for ${WATCH_SECONDS}s"
say "  Walk in front of the sensor now. HIGH means motion."
if [ -f "$HERE/pir-test.py" ]; then
  if "$PYTHON" "$HERE/pir-test.py" --gpio "$GPIO" --seconds "$WATCH_SECONDS"; then
    ok "the sensor was read successfully"
  else
    bad "reading the sensor failed"
    explain "Motion detection will not work." \
            "wrong GPIO number, no 5V on VCC, or a dead sensor" \
            "check VCC on physical pin 2, GND on pin 6, and OUT on the GPIO you configured"
  fi
else
  warn "pir-test.py was not found next to this script"
fi

step "Service view"
if unit_active securecam.service; then
  ok "securecam.service is running; recent motion appears in: sudo journalctl -u securecam -n 30"
else
  warn "securecam.service is not running, so nothing is watching the sensor"
fi

summary
