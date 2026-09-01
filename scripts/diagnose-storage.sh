#!/usr/bin/env bash
# Storage diagnostics: space, permissions, retention and buffer growth.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"

EVENTS="$SECURECAM_DATA_DIR/events"
BUFFER="$SECURECAM_DATA_DIR/buffer"

step "Filesystem"
df -h "$SECURECAM_DATA_DIR" 2>/dev/null | sed 's/^/          /'
used="$(df -P "$SECURECAM_DATA_DIR" 2>/dev/null | awk 'NR==2 {gsub(/%/,"",$5); print $5}')"
free_kb="$(df -Pk "$SECURECAM_DATA_DIR" 2>/dev/null | awk 'NR==2 {print $4}')"
used="${used:-0}"; free_kb="${free_kb:-0}"

if [ "$free_kb" -lt 204800 ]; then
  bad "less than 200 MB free"
  explain "Recording stops and new events cannot be written." \
          "retention is too long, or something else filled the card" \
          "lower storage.retention_days in $SECURECAM_CONFIG, then: sudo systemctl restart securecam"
elif [ "$used" -gt 90 ]; then
  warn "the filesystem is ${used}% full"
else
  ok "${used}% used, $((free_kb / 1024)) MB free"
fi

if mount | grep -q " $(df -P "$SECURECAM_DATA_DIR" | awk 'NR==2 {print $6}') .*[(,]ro[,)]"; then
  bad "the filesystem is mounted read-only"
  explain "Nothing can be recorded at all." \
          "the SD card hit a hardware error and Linux remounted it read-only" \
          "check 'dmesg | tail -50'; if the card is failing, replace it"
fi

step "Permissions"
for dir in "$SECURECAM_DATA_DIR" "$EVENTS" "$BUFFER" "$SECURECAM_CONFIG_DIR"; do
  if [ ! -d "$dir" ]; then
    bad "$dir does not exist"
    continue
  fi
  owner="$(stat -c '%U:%G %a' "$dir")"
  if sudo -u "$SECURECAM_USER" test -w "$dir" 2>/dev/null; then
    ok "$dir writable by $SECURECAM_USER ($owner)"
  else
    bad "$dir is not writable by $SECURECAM_USER ($owner)"
    explain "Events or buffer segments cannot be written." \
            "the directory was created by root during a manual step" \
            "sudo chown -R $SECURECAM_USER:$SECURECAM_USER $dir"
  fi
done

step "Secrets"
for file in "$SECURECAM_CONFIG_DIR/securecam.env" "$SECURECAM_CONFIG_DIR/users.json" "$SECURECAM_CONFIG_DIR/secret.key"; do
  [ -f "$file" ] || continue
  mode="$(stat -c '%a' "$file")"
  if [ "$mode" = "600" ]; then
    ok "$file is 0600"
  else
    warn "$file is $mode; it should be 0600"
    explain "Other local users can read your tokens or password hashes." "" "sudo chmod 600 $file"
  fi
done

step "Usage"
if [ -d "$EVENTS" ]; then
  count="$(find "$EVENTS" -mindepth 4 -maxdepth 4 -type d 2>/dev/null | wc -l)"
  ok "$count event(s), $(du -sh "$EVENTS" 2>/dev/null | awk '{print $1}') on disk"
  oldest="$(find "$EVENTS" -mindepth 4 -maxdepth 4 -type d -printf '%T@ %p\n' 2>/dev/null | sort -n | head -n1 | cut -d' ' -f2-)"
  [ -n "$oldest" ] && say "          oldest: $(basename "$oldest")"
fi
if [ -d "$BUFFER" ]; then
  buffer_size="$(du -sm "$BUFFER" 2>/dev/null | awk '{print $1}')"
  ok "rolling buffer: ${buffer_size:-0} MB"
  if [ "${buffer_size:-0}" -gt 4096 ]; then
    warn "the buffer is larger than 4 GB"
    explain "Old buffer segments are not being deleted." \
            "recordDeleteAfter is not being applied, or the clock jumped" \
            "sudo systemctl restart securecam-mediamtx"
  fi
fi

step "Card health"
if dmesg 2>/dev/null | grep -qiE 'mmc.*(error|timeout)|I/O error'; then
  warn "the kernel logged SD card I/O errors"
  explain "Recordings can be silently corrupted." \
          "the SD card is wearing out" \
          "back up $EVENTS and replace the card; prefer a USB SSD for continuous recording"
else
  ok "no SD card I/O errors in the kernel log"
fi

summary
