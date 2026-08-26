#!/usr/bin/env bash
# Network diagnostics: link, DNS, internet, Wi-Fi quality and the ports we serve.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"

step "Interfaces"
ip -brief address 2>/dev/null | sed 's/^/          /' || ifconfig 2>/dev/null | sed 's/^/          /'
route="$(ip route show default 2>/dev/null | head -n1)"
if [ -n "$route" ]; then
  ok "default route: $route"
else
  bad "there is no default route"
  explain "Notifications, AI and remote access are all impossible; recording still works." \
          "Wi-Fi is not connected, or DHCP failed" \
          "sudo nmcli device wifi list, then: sudo nmcli device wifi connect <SSID> --ask"
fi

step "Wi-Fi quality"
if has_cmd iw && iw dev 2>/dev/null | grep -q Interface; then
  wifi_dev="$(iw dev 2>/dev/null | awk '/Interface/ {print $2; exit}')"
  signal="$(iw dev "$wifi_dev" link 2>/dev/null | awk '/signal/ {print $2}')"
  if [ -n "$signal" ]; then
    if [ "$signal" -lt -75 ]; then
      warn "signal is ${signal} dBm on $wifi_dev (weak)"
      explain "Live viewing will stutter and notifications may be delayed." \
              "the Pi is far from the access point or behind metal" \
              "move the Pi, add a repeater, or use Ethernet"
    else
      ok "signal is ${signal} dBm on $wifi_dev"
    fi
  else
    warn "$wifi_dev is not associated with an access point"
  fi
else
  ok "no Wi-Fi interface in use (wired connection)"
fi

step "DNS and internet"
if getent hosts github.com >/dev/null 2>&1; then
  ok "DNS resolves"
else
  bad "DNS does not resolve"
  explain "AI analysis and push notifications cannot reach their servers." \
          "no DNS server was handed out by DHCP" \
          "check /etc/resolv.conf, or set a DNS server in your router"
fi

if has_cmd curl && curl -fsS --max-time 8 -o /dev/null https://www.google.com/generate_204 2>/dev/null; then
  ok "outbound HTTPS works"
else
  bad "outbound HTTPS failed"
  explain "Notifications and AI requests will be queued and retried, not lost." \
          "no internet, a captive portal, or a firewall blocking outbound 443" \
          "test from another device on the same network"
fi

step "Local time"
if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q yes; then
  ok "the clock is synchronised ($(date))"
else
  warn "the system clock is not synchronised with NTP"
  explain "Event timestamps and clip extraction can be wrong by minutes." \
          "no internet at boot, or NTP is blocked" \
          "sudo timedatectl set-ntp true"
fi

step "Listening ports"
if has_cmd ss; then
  ss -ltnp 2>/dev/null | grep -E ':(8080|8443|8554|8889|8890|9095|9996|9997)\b' | sed 's/^/          /' || \
    warn "none of the SecureCam ports are listening"
  for port in 8554 8889 9997; do
    if ss -ltn 2>/dev/null | grep -q ":$port\b"; then
      ok "port $port is listening"
    else
      bad "port $port is not listening"
      explain "RTSP, WebRTC or the control API is unavailable." \
              "securecam-mediamtx.service is not running" \
              "sudo systemctl status securecam-mediamtx"
    fi
  done
else
  warn "ss is not installed, so listening ports were not checked"
fi

step "Remote access"
if has_cmd tailscale && tailscale status >/dev/null 2>&1; then
  ok "Tailscale is up: $(tailscale ip -4 2>/dev/null | head -n1)"
else
  say "          Tailscale is not configured. To reach this camera from outside your home"
  say "          without opening any router ports, run: sudo ./scripts/setup-remote-access.sh"
fi

summary
