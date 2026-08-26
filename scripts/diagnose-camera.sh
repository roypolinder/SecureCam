#!/usr/bin/env bash
# Camera diagnostics: is a camera attached, is MediaMTX publishing, is it recording?
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"

step "Camera hardware"
if has_cmd rpicam-hello; then
  CAM_TOOL=rpicam-hello
elif has_cmd libcamera-hello; then
  CAM_TOOL=libcamera-hello
else
  CAM_TOOL=""
fi

if [ -n "$CAM_TOOL" ]; then
  if output="$($CAM_TOOL --list-cameras 2>&1)" && printf '%s' "$output" | grep -q ':'; then
    ok "libcamera sees a camera"
    printf '%s\n' "$output" | sed 's/^/          /'
  else
    bad "libcamera does not see any camera"
    explain "Without a camera there is no stream and no recording." \
            "the ribbon cable is loose, inserted backwards, or in the wrong port" \
            "power off, reseat the cable (contacts towards the board), power on, rerun this script"
    printf '%s\n' "$output" | sed 's/^/          /'
  fi
else
  warn "rpicam-hello is not installed, so the camera cannot be probed directly"
  explain "" "" "sudo apt-get install rpicam-apps"
fi

step "Kernel and firmware"
if [ -e /dev/video0 ] || ls /dev/media* >/dev/null 2>&1; then
  ok "video devices are present ($(ls /dev/video* /dev/media* 2>/dev/null | tr '\n' ' '))"
else
  bad "no /dev/video* or /dev/media* devices exist"
  explain "The camera stack did not load." \
          "camera_auto_detect is off in /boot/firmware/config.txt, or the OS is 32-bit legacy" \
          "check that /boot/firmware/config.txt contains 'camera_auto_detect=1', then reboot"
fi

if dmesg 2>/dev/null | grep -qi 'over.current\|under.voltage'; then
  warn "the kernel logged under-voltage or over-current events"
  explain "An underpowered Pi drops frames and corrupts recordings." \
          "the power supply is too weak or the cable is thin" \
          "use the official 5V/3A USB-C supply"
fi

step "MediaMTX service"
if unit_active securecam-mediamtx.service; then
  ok "securecam-mediamtx.service is running"
else
  bad "securecam-mediamtx.service is not running"
  explain "Nothing is being streamed or recorded right now." \
          "an invalid mediamtx.yml, a busy camera, or a crash loop" \
          "sudo journalctl -u securecam-mediamtx -n 50 --no-pager"
fi

step "Stream status"
API_PORT="$(awk '/^mediamtx:/{f=1} f && /api_address:/{print $2; exit}' "$SECURECAM_CONFIG" 2>/dev/null | sed 's/.*://;s/"//g')"
API_PORT="${API_PORT:-9997}"
if has_cmd curl; then
  if response="$(curl -fsS --max-time 5 "http://127.0.0.1:${API_PORT}/v3/paths/list" 2>/dev/null)"; then
    ok "the MediaMTX control API answers on port $API_PORT"
    if printf '%s' "$response" | grep -q '"ready":true'; then
      ok "the camera path is publishing"
    else
      bad "the camera path exists but is not publishing"
      explain "The stream is down, so motion events will have no video." \
              "the camera is in use by another process, or libcamera failed to start" \
              "sudo journalctl -u securecam-mediamtx -n 50 --no-pager"
    fi
    printf '%s\n' "$response" | sed 's/,/,\n          /g' | head -n 25 | sed 's/^/          /'
  else
    bad "the MediaMTX control API does not answer on 127.0.0.1:$API_PORT"
    explain "Health checks and clip extraction both depend on this API." \
            "MediaMTX is not running, or api_address was changed" \
            "sudo systemctl restart securecam-mediamtx"
  fi
else
  warn "curl is not installed, so the stream status could not be read"
fi

step "Recording buffer"
BUFFER="$SECURECAM_DATA_DIR/buffer"
if [ -d "$BUFFER" ]; then
  count="$(find "$BUFFER" -name '*.mp4' -newermt '-5 minutes' 2>/dev/null | wc -l)"
  if [ "$count" -gt 0 ]; then
    ok "$count buffer segment(s) written in the last 5 minutes ($(du -sh "$BUFFER" 2>/dev/null | awk '{print $1}') total)"
  else
    bad "no buffer segments were written in the last 5 minutes"
    explain "Pre-event video will be missing from every recording." \
            "recording is disabled in mediamtx.yml, or the disk is full or read-only" \
            "sudo ./scripts/diagnose-storage.sh"
  fi
else
  bad "the buffer directory $BUFFER does not exist"
  explain "" "the installer did not finish" "sudo ./install.sh"
fi

summary
