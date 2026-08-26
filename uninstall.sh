#!/usr/bin/env bash
#
# Removes SecureCam. Recordings, the configuration and the accounts are kept
# unless you explicitly ask for them to be deleted with --purge.
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
. "$REPO_DIR/scripts/lib/common.sh"

PURGE=0
KEEP_MEDIAMTX=0
ASSUME_YES=0

usage() {
  cat <<'EOF'
Usage: sudo ./uninstall.sh [options]

  --purge            also delete recordings, configuration, secrets and accounts
  --keep-mediamtx    leave /usr/local/bin/mediamtx in place
  --yes              do not ask for confirmation
  -h, --help         this text

Without --purge nothing under /var/lib/securecam or /etc/securecam is touched,
so you can reinstall later and keep every recording.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --purge) PURGE=1 ;;
    --keep-mediamtx) KEEP_MEDIAMTX=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option '$1' (try --help)" ;;
  esac
  shift
done

require_root

recordings_size() {
  du -sh "$SECURECAM_DATA_DIR/events" 2>/dev/null | awk '{print $1}' || echo "unknown"
}

confirm() {
  [ "$ASSUME_YES" -eq 1 ] && return 0
  local answer=""
  read -r -p "$1 [y/N]: " answer </dev/tty || answer=""
  case "$answer" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

step "Stopping services"
for unit in securecam.service securecam-mediamtx.service; do
  if systemctl list-unit-files | grep -q "^$unit"; then
    systemctl disable --now "$unit" >/dev/null 2>&1 || true
    ok "stopped and disabled $unit"
  fi
done

step "Removing system files"
rm -f /etc/systemd/system/securecam.service /etc/systemd/system/securecam-mediamtx.service
rm -f /etc/sudoers.d/securecam /etc/sysctl.d/99-securecam.conf /etc/tmpfiles.d/securecam.conf
rm -f /usr/local/bin/securecam /usr/local/bin/securecam-admin /usr/local/bin/securecam-pir-test
systemctl daemon-reload
ok "systemd units, sudoers rule and command links removed"

if [ "$KEEP_MEDIAMTX" -eq 0 ] && [ -x /usr/local/bin/mediamtx ]; then
  rm -f /usr/local/bin/mediamtx
  ok "removed /usr/local/bin/mediamtx"
fi

step "Removing the application"
rm -rf "$SECURECAM_PREFIX"
ok "removed $SECURECAM_PREFIX"

if [ "$PURGE" -eq 1 ]; then
  step "Purging data (this cannot be undone)"
  say "  This deletes every recording in $SECURECAM_DATA_DIR/events (currently $(recordings_size)),"
  say "  the configuration in $SECURECAM_CONFIG_DIR, the signing key and all accounts."
  if confirm "  Delete all SecureCam data?"; then
    rm -rf "$SECURECAM_DATA_DIR" "$SECURECAM_CONFIG_DIR" /run/securecam
    ok "all data removed"
    if id -u "$SECURECAM_USER" >/dev/null 2>&1 && confirm "  Also delete the '$SECURECAM_USER' system user?"; then
      userdel "$SECURECAM_USER" >/dev/null 2>&1 || true
      ok "user removed"
    fi
  else
    warn "purge cancelled; your data was kept"
  fi
else
  step "Keeping your data"
  ok "recordings kept in $SECURECAM_DATA_DIR/events ($(recordings_size))"
  ok "configuration kept in $SECURECAM_CONFIG_DIR"
  say ""
  say "  Reinstalling later will pick these up again."
  say "  To remove them as well, run: sudo ./uninstall.sh --purge"
fi

printf '\n%s\n' "${C_GREEN}${C_BOLD}SecureCam has been uninstalled.${C_RESET}"
say "The apt packages it used (ffmpeg, python3-gpiozero, python3-lgpio) were left installed"
say "because other software may need them."
