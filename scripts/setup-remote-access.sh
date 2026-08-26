#!/usr/bin/env bash
#
# Makes the camera reachable from outside your home without opening any router
# port. Uses Tailscale: a private encrypted network between your devices.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"

require_root

cat <<'EOF'
Remote access setup
===================

This installs Tailscale and joins this Raspberry Pi to your private network.

Why Tailscale instead of port forwarding:
  - Nothing on your camera is exposed to the public internet.
  - No router configuration, no dynamic DNS, works behind CGNAT.
  - Traffic is end-to-end encrypted with WireGuard.
  - Your phone reaches the camera on a stable private address.

You will need a free Tailscale account (github/google/email login works).
EOF

read -r -p $'\nContinue? [Y/n]: ' answer </dev/tty || answer=""
case "$answer" in n|N|no|NO) say "Cancelled."; exit 0 ;; esac

step "Installing Tailscale"
if has_cmd tailscale; then
  ok "tailscale is already installed ($(tailscale version | head -n1))"
else
  if ! curl -fsSL https://tailscale.com/install.sh | sh; then
    die "the Tailscale installer failed.
  Likely cause: no internet access, or an unsupported OS release.
  What still works: everything local. Only remote access is missing.
  Do this next: check connectivity, or follow https://tailscale.com/kb/1174/install-debian-bookworm"
  fi
  ok "tailscale installed"
fi

step "Connecting this device"
if tailscale status >/dev/null 2>&1; then
  ok "already connected as $(tailscale status --json 2>/dev/null | grep -o '"DNSName":"[^"]*"' | head -n1 | cut -d'"' -f4)"
else
  say "  A login URL will be printed. Open it on your phone or computer and approve this device."
  tailscale up --hostname "$(hostname)-securecam" --accept-dns=true || \
    die "'tailscale up' did not complete. Run it again once you have approved the device."
fi

TS_IP="$(tailscale ip -4 2>/dev/null | head -n1)"
TS_NAME="$(tailscale status --json 2>/dev/null | grep -o '"DNSName":"[^"]*"' | head -n1 | cut -d'"' -f4)"
TS_NAME="${TS_NAME%.}"
[ -n "$TS_IP" ] || die "Tailscale did not report an IP address. Run: sudo tailscale status"
ok "this camera is $TS_IP${TS_NAME:+ ($TS_NAME)}"

step "Teaching WebRTC about the new address"
# MediaMTX must advertise the Tailscale address as an ICE candidate, otherwise the
# live view connects only from the local network.
if grep -q 'webrtc_additional_hosts:' "$SECURECAM_CONFIG"; then
  hosts="\"$TS_IP\""
  [ -n "$TS_NAME" ] && hosts="$hosts, \"$TS_NAME\""
  awk -v hosts="$hosts" '
    {
      if ($0 ~ /^mediamtx:/) { inblock = 1 }
      else if ($0 ~ /^[A-Za-z_]+:/) { inblock = 0 }
      if (inblock && $0 ~ /^[[:space:]]*webrtc_additional_hosts:/) {
        match($0, /^[[:space:]]*/)
        printf "%swebrtc_additional_hosts: [%s]\n", substr($0, 1, RLENGTH), hosts
        next
      }
      print
    }' "$SECURECAM_CONFIG" > "$SECURECAM_CONFIG.new" && mv "$SECURECAM_CONFIG.new" "$SECURECAM_CONFIG"
  chown "$SECURECAM_USER:$SECURECAM_USER" "$SECURECAM_CONFIG"
  chmod 0640 "$SECURECAM_CONFIG"
  ok "added $hosts to mediamtx.webrtc_additional_hosts"
else
  warn "webrtc_additional_hosts was not found in $SECURECAM_CONFIG"
  explain "Live view will work on your home network but not over Tailscale." \
          "the configuration file was heavily edited" \
          "add 'webrtc_additional_hosts: [\"$TS_IP\"]' under the 'mediamtx:' section"
fi

PORT="$(awk '/^api:/{f=1} f && /^[[:space:]]*port:/{print $2; exit}' "$SECURECAM_CONFIG")"
PORT="${PORT:-8080}"
BASE="http://${TS_NAME:-$TS_IP}:$PORT"
if grep -q 'public_base_url:' "$SECURECAM_CONFIG"; then
  awk -v url="$BASE" '
    {
      if ($0 ~ /^api:/) { inblock = 1 }
      else if ($0 ~ /^[A-Za-z_]+:/) { inblock = 0 }
      if (inblock && $0 ~ /^[[:space:]]*public_base_url:/) {
        match($0, /^[[:space:]]*/)
        printf "%spublic_base_url: \"%s\"\n", substr($0, 1, RLENGTH), url
        next
      }
      print
    }' "$SECURECAM_CONFIG" > "$SECURECAM_CONFIG.new" && mv "$SECURECAM_CONFIG.new" "$SECURECAM_CONFIG"
  chown "$SECURECAM_USER:$SECURECAM_USER" "$SECURECAM_CONFIG"
  chmod 0640 "$SECURECAM_CONFIG"
  ok "notification links will now point at $BASE"
fi

step "Restarting services"
systemctl restart securecam-mediamtx.service
systemctl restart securecam.service
ok "services restarted with the new address"

printf '\n%s\n' "${C_BOLD}================ Remote access is ready ================${C_RESET}"
printf '\n'
printf '  Web interface : %s/\n' "$BASE"
printf '  Direct IP     : http://%s:%s/\n' "$TS_IP" "$PORT"
printf '\n%s\n' "${C_YELLOW}${C_BOLD}ACTION REQUIRED${C_RESET}"
printf '  1. Install the Tailscale app on your phone and sign in with the same account.\n'
printf '  2. Turn the Tailscale VPN on, then open %s/ in the phone browser.\n' "$BASE"
printf '  3. Optional but recommended: in the Tailscale admin console, disable key expiry\n'
printf '     for this device so it never silently drops off the network.\n'
printf '\n%s\n' "${C_BOLD}Notes${C_RESET}"
printf '  - Your router still has no ports open. Nothing here is reachable from the public internet.\n'
printf '  - Live video goes directly between your phone and the Pi over WireGuard.\n'
printf '  - To undo this: sudo tailscale down && sudo apt-get remove tailscale\n'
printf '%s\n' "${C_BOLD}=======================================================${C_RESET}"
