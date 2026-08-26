#!/usr/bin/env bash
#
# SecureCam installer. Safe to run repeatedly: every step checks the current
# state first and only changes what is different.
#
#   sudo ./install.sh                 interactive install or upgrade
#   sudo ./install.sh --unattended    no prompts, keep every existing answer
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
. "$REPO_DIR/scripts/lib/common.sh"

APP_DIR="$SECURECAM_PREFIX/app"
VENV="$SECURECAM_VENV"
ENV_FILE="$SECURECAM_CONFIG_DIR/securecam.env"
MEDIAMTX_BIN="/usr/local/bin/mediamtx"

MEDIAMTX_VERSION="${MEDIAMTX_VERSION:-v1.20.1}"
MEDIAMTX_SHA256_ARM64="d1689f0bfefb1864e5ed3dcc8495eb2d7ec0a654f90bf3cd48980cb3bd08718a"

UNATTENDED=0
SKIP_APT=0
SKIP_MEDIAMTX=0
PIR_GPIO=""
FRESH_CONFIG=0
ACTIONS=()

usage() {
  cat <<'EOF'
Usage: sudo ./install.sh [options]

  --unattended            never prompt; keep existing settings and skip account creation
  --gpio N                use BCM GPIO N for the PIR sensor (default: ask, or 17)
  --mediamtx-version vX   install a specific MediaMTX release (default: v1.20.1)
  --skip-apt              do not touch apt (useful for repeat runs and offline machines)
  --skip-mediamtx         do not download or replace the MediaMTX binary
  -h, --help              this text
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --unattended) UNATTENDED=1 ;;
    --gpio) PIR_GPIO="${2:-}"; shift ;;
    --mediamtx-version) MEDIAMTX_VERSION="${2:-}"; MEDIAMTX_SHA256_ARM64=""; shift ;;
    --skip-apt) SKIP_APT=1 ;;
    --skip-mediamtx) SKIP_MEDIAMTX=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option '$1' (try --help)" ;;
  esac
  shift
done

action() { ACTIONS+=("$1"); }

# ---------------------------------------------------------------- preflight

preflight() {
  step "1/11  Checking the machine"
  require_root

  local arch; arch="$(uname -m)"
  if [ "$arch" != "aarch64" ]; then
    warn "architecture is '$arch', not 'aarch64'"
    explain "SecureCam is built for 64-bit Raspberry Pi OS." \
            "you are running a 32-bit or non-ARM system" \
            "reflash with Raspberry Pi OS Lite (64-bit) if the camera does not work"
  else
    ok "64-bit ARM system"
  fi

  if is_raspberry_pi; then
    ok "$(pi_model)"
  else
    warn "this does not look like a Raspberry Pi; the camera and PIR steps may fail"
  fi

  local mem_kb; mem_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
  if [ "$mem_kb" -lt 900000 ]; then
    warn "only $((mem_kb / 1024)) MB of RAM; SecureCam is tuned for 1 GB or more"
  else
    ok "$((mem_kb / 1024)) MB RAM"
  fi

  local free_kb; free_kb="$(df -Pk /var | awk 'NR==2 {print $4}')"
  if [ "$free_kb" -lt 8000000 ]; then
    warn "only $((free_kb / 1024 / 1024)) GB free on /var"
    explain "Recordings and the rolling buffer live in $SECURECAM_DATA_DIR." \
            "the SD card is small or nearly full" \
            "lower storage.retention_days in $SECURECAM_CONFIG after installing"
  else
    ok "$((free_kb / 1024 / 1024)) GB free on /var"
  fi

  for tool in systemctl awk sed tar; do
    has_cmd "$tool" || die "'$tool' is missing and is required"
  done
}

# ------------------------------------------------------------------- apt

install_packages() {
  step "2/11  Installing system packages"
  if [ "$SKIP_APT" -eq 1 ]; then
    say "  skipped (--skip-apt)"
    return 0
  fi
  has_cmd apt-get || { warn "apt-get not found; install the dependencies manually"; return 0; }

  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq

  local packages=(
    python3 python3-venv python3-pip python3-setuptools
    python3-yaml python3-gpiozero python3-lgpio
    ffmpeg curl ca-certificates tar
  )
  apt-get install -y -qq "${packages[@]}"
  ok "core packages installed"

  # The camera tools are only needed for diagnostics; MediaMTX drives libcamera itself.
  if apt-get install -y -qq rpicam-apps 2>/dev/null; then
    ok "rpicam-apps installed"
  elif apt-get install -y -qq libcamera-apps 2>/dev/null; then
    ok "libcamera-apps installed"
  else
    warn "no camera command line tools available"
    explain "Recording still works, but ./scripts/diagnose-camera.sh cannot list cameras." \
            "the package is not in your apt sources" \
            "run: sudo apt-get install rpicam-apps"
  fi
}

# ------------------------------------------------------------- user and dirs

create_user() {
  step "3/11  Creating the service account"
  if id -u "$SECURECAM_USER" >/dev/null 2>&1; then
    ok "user '$SECURECAM_USER' already exists"
  else
    useradd --system --home-dir "$SECURECAM_DATA_DIR" --shell /usr/sbin/nologin "$SECURECAM_USER"
    ok "created system user '$SECURECAM_USER'"
  fi
  for group in video render gpio; do
    if getent group "$group" >/dev/null 2>&1; then
      usermod -aG "$group" "$SECURECAM_USER"
    fi
  done
  ok "group membership: $(id -nG "$SECURECAM_USER" | tr ' ' ',')"
}

create_dirs() {
  step "4/11  Creating directories"
  install -d -m 0755 -o root -g root "$SECURECAM_PREFIX"
  install -d -m 0750 -o "$SECURECAM_USER" -g "$SECURECAM_USER" "$SECURECAM_CONFIG_DIR"
  install -d -m 0750 -o "$SECURECAM_USER" -g "$SECURECAM_USER" "$SECURECAM_DATA_DIR"
  install -d -m 0750 -o "$SECURECAM_USER" -g "$SECURECAM_USER" "$SECURECAM_DATA_DIR/events"
  install -d -m 0750 -o "$SECURECAM_USER" -g "$SECURECAM_USER" "$SECURECAM_DATA_DIR/buffer"
  install -d -m 0750 -o "$SECURECAM_USER" -g "$SECURECAM_USER" /run/securecam
  printf 'd /run/securecam 0750 %s %s -\n' "$SECURECAM_USER" "$SECURECAM_USER" > /etc/tmpfiles.d/securecam.conf
  ok "$SECURECAM_CONFIG_DIR, $SECURECAM_DATA_DIR and /run/securecam are ready"
}

# ------------------------------------------------------------- application

install_app() {
  step "5/11  Installing the application"
  rm -rf "$APP_DIR"
  install -d -m 0755 "$APP_DIR"
  tar --exclude=.git --exclude=__pycache__ --exclude='*.pyc' --exclude=.venv \
      -cf - -C "$REPO_DIR" . | tar -xf - -C "$APP_DIR"
  chown -R root:root "$APP_DIR"
  ok "copied the repository to $APP_DIR"

  if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv --system-site-packages "$VENV"
    ok "created the virtual environment at $VENV"
  else
    ok "virtual environment already present"
  fi

  # --no-build-isolation keeps pip away from PyPI: every dependency comes from apt.
  if ! "$VENV/bin/pip" install --quiet --no-input --no-build-isolation --upgrade "$APP_DIR"; then
    die "installing the Python package failed.
  Likely cause: python3-setuptools or python3-yaml is missing.
  Do this next: sudo apt-get install python3-setuptools python3-yaml, then run this installer again."
  fi
  chown -R root:root "$VENV"
  ok "installed securecam $("$VENV/bin/securecam" --version 2>/dev/null | awk '{print $2}')"

  install -m 0755 "$APP_DIR/scripts/pir-test.py" /usr/local/bin/securecam-pir-test 2>/dev/null || true
  ln -sfn "$VENV/bin/securecam" /usr/local/bin/securecam
  ln -sfn "$VENV/bin/securecam-admin" /usr/local/bin/securecam-admin
  ok "securecam and securecam-admin are on PATH"
}

# --------------------------------------------------------------- mediamtx

install_mediamtx() {
  step "6/11  Installing MediaMTX ($MEDIAMTX_VERSION)"
  if [ "$SKIP_MEDIAMTX" -eq 1 ]; then
    say "  skipped (--skip-mediamtx)"
    return 0
  fi
  if [ -x "$MEDIAMTX_BIN" ] && "$MEDIAMTX_BIN" --version 2>/dev/null | grep -q "${MEDIAMTX_VERSION#v}"; then
    ok "$MEDIAMTX_VERSION is already installed"
    return 0
  fi

  local tmp; tmp="$(mktemp -d)"
  # shellcheck disable=SC2064
  trap "rm -rf '$tmp'" RETURN

  local archive="mediamtx_${MEDIAMTX_VERSION}_linux_arm64.tar.gz"
  local url="https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/${archive}"

  if ! curl -fsSL --retry 3 --connect-timeout 20 -o "$tmp/$archive" "$url"; then
    die "could not download MediaMTX from $url.
  Likely cause: no internet access, or that release does not exist.
  What still works: nothing yet - the camera stream needs this binary.
  Do this next: check connectivity with 'ping -c1 github.com', then rerun the installer.
  Offline alternative: download $archive on another machine, copy it to this Pi,
  extract 'mediamtx' to $MEDIAMTX_BIN, then rerun with --skip-mediamtx."
  fi

  local expected="$MEDIAMTX_SHA256_ARM64"
  if [ -z "$expected" ]; then
    if curl -fsSL --retry 3 -o "$tmp/checksums.sha256" \
        "https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/checksums.sha256"; then
      expected="$(awk -v name="$archive" '$2 == name || $2 == "*"name {print $1}' "$tmp/checksums.sha256" | head -n1)"
    fi
  fi
  if [ -n "$expected" ]; then
    local actual; actual="$(sha256sum "$tmp/$archive" | awk '{print $1}')"
    [ "$actual" = "$expected" ] || die "the MediaMTX download does not match its published SHA-256 checksum.
  Expected $expected
  Got      $actual
  Do this next: delete any proxy or mirror from the picture and rerun the installer."
    ok "checksum verified"
  else
    warn "no checksum available for $MEDIAMTX_VERSION; the download was not verified"
  fi

  tar -xzf "$tmp/$archive" -C "$tmp" mediamtx
  install -m 0755 -o root -g root "$tmp/mediamtx" "$MEDIAMTX_BIN"
  ok "installed $("$MEDIAMTX_BIN" --version 2>/dev/null || echo "$MEDIAMTX_VERSION") to $MEDIAMTX_BIN"
}

# ------------------------------------------------------------ configuration

install_config() {
  step "7/11  Writing configuration and secrets"

  if [ ! -f "$SECURECAM_CONFIG" ]; then
    install -m 0640 -o "$SECURECAM_USER" -g "$SECURECAM_USER" \
      "$APP_DIR/config/config.example.yaml" "$SECURECAM_CONFIG"
    FRESH_CONFIG=1
    ok "created $SECURECAM_CONFIG from the example"
  else
    ok "kept your existing $SECURECAM_CONFIG"
  fi

  install -m 0644 -o root -g root "$APP_DIR/config/mediamtx.template.yml" \
    "$SECURECAM_CONFIG_DIR/mediamtx.template.yml"

  if [ ! -f "$ENV_FILE" ]; then
    umask 077
    {
      echo "# Secrets for SecureCam. Never commit this file."
      echo "SECURECAM_SECRET_KEY=$(head -c 48 /dev/urandom | base64 | tr -d '\n=' | tr '+/' '-_')"
      echo "SECURECAM_MEDIAMTX_SERVICE_USER=securecam-service"
      echo "SECURECAM_MEDIAMTX_SERVICE_PASS=$(head -c 32 /dev/urandom | base64 | tr -d '\n=' | tr '+/' '-_')"
      echo "# Uncomment and fill in the ones you use:"
      echo "#SECURECAM_AI_API_KEY="
      echo "#SECURECAM_NTFY_TOKEN="
      echo "#SECURECAM_PUSHOVER_TOKEN="
      echo "#SECURECAM_PUSHOVER_USER_KEY="
    } > "$ENV_FILE"
    umask 022
    ok "generated $ENV_FILE with a fresh signing key"
    action "Add your AI and notification tokens to $ENV_FILE, then: sudo systemctl restart securecam"
  else
    ok "kept your existing $ENV_FILE"
  fi
  chown "$SECURECAM_USER:$SECURECAM_USER" "$ENV_FILE"
  chmod 0600 "$ENV_FILE"
}

set_motion_gpio() {
  awk -v pin="$1" '
    {
      if ($0 ~ /^motion:/) { inblock = 1 }
      else if ($0 ~ /^[A-Za-z_]+:/) { inblock = 0 }
      if (inblock && $0 ~ /^[[:space:]]*gpio:[[:space:]]*[0-9]+/) {
        sub(/gpio:[[:space:]]*[0-9]+/, "gpio: " pin)
      }
      print
    }' "$2" > "$2.new" && mv "$2.new" "$2"
  chown "$SECURECAM_USER:$SECURECAM_USER" "$2"
  chmod 0640 "$2"
}

configure_pir() {
  step "8/11  Configuring the PIR sensor"
  local current
  current="$(awk '/^motion:/{f=1} f && /^[[:space:]]*gpio:/{print $2; exit}' "$SECURECAM_CONFIG")"
  current="${current:-17}"

  if [ -n "$PIR_GPIO" ]; then
    set_motion_gpio "$PIR_GPIO" "$SECURECAM_CONFIG"
    ok "PIR data pin set to BCM GPIO $PIR_GPIO"
  elif [ "$UNATTENDED" -eq 1 ] || [ "$FRESH_CONFIG" -eq 0 ]; then
    ok "PIR data pin stays on BCM GPIO $current"
  else
    say ""
    say "  Wire the PIR sensor to: VCC -> pin 2 (5V), GND -> pin 6, OUT -> a free GPIO."
    say "  Enter the BCM number of the GPIO you used (not the physical pin number)."
    read -r -p "  PIR GPIO [$current]: " answer </dev/tty || answer=""
    answer="${answer:-$current}"
    if ! printf '%s' "$answer" | grep -Eq '^[0-9]+$' || [ "$answer" -gt 27 ]; then
      warn "'$answer' is not a BCM GPIO number between 0 and 27; keeping $current"
    else
      set_motion_gpio "$answer" "$SECURECAM_CONFIG"
      current="$answer"
      ok "PIR data pin set to BCM GPIO $current"
    fi
  fi
  action "Test the sensor before you trust it: sudo securecam-pir-test --gpio $current"
}

# ------------------------------------------------------------ system files

install_system_files() {
  step "9/11  Installing systemd units and system settings"

  install -m 0644 "$APP_DIR/systemd/securecam.service" /etc/systemd/system/securecam.service
  install -m 0644 "$APP_DIR/systemd/securecam-mediamtx.service" /etc/systemd/system/securecam-mediamtx.service
  ok "systemd units installed"

  # One narrow rule: the controller may restart the stream service and nothing else.
  local sudoers=/etc/sudoers.d/securecam
  local systemctl_path; systemctl_path="$(command -v systemctl)"
  cat > "$sudoers.tmp" <<EOF
$SECURECAM_USER ALL=(root) NOPASSWD: $systemctl_path restart securecam-mediamtx.service
EOF
  chmod 0440 "$sudoers.tmp"
  if visudo -cf "$sudoers.tmp" >/dev/null 2>&1; then
    mv "$sudoers.tmp" "$sudoers"
    ok "sudoers rule for restarting the stream installed"
  else
    rm -f "$sudoers.tmp"
    warn "the sudoers rule was rejected and was not installed"
    explain "SecureCam cannot restart the stream by itself if it wedges." \
            "an unusual systemctl path or a locked-down sudo configuration" \
            "add this line manually with visudo: $SECURECAM_USER ALL=(root) NOPASSWD: $systemctl_path restart securecam-mediamtx.service"
  fi

  # MediaMTX reads RTP over UDP; the default socket buffer is too small for 1080p.
  cat > /etc/sysctl.d/99-securecam.conf <<'EOF'
# Larger UDP receive buffers so MediaMTX does not drop RTP packets under load.
net.core.rmem_max = 1000000
net.core.rmem_default = 1000000
EOF
  sysctl -q --system >/dev/null 2>&1 || warn "could not apply /etc/sysctl.d/99-securecam.conf right now"
  ok "UDP buffer sizes configured"

  systemd-tmpfiles --create /etc/tmpfiles.d/securecam.conf >/dev/null 2>&1 || true
  systemctl daemon-reload
}

# --------------------------------------------------------------- accounts

create_admin() {
  step "10/11  Administrator account"
  local admins
  admins="$(sudo -u "$SECURECAM_USER" "$VENV/bin/securecam-admin" --config "$SECURECAM_CONFIG" device show 2>/dev/null \
    | awk '/^users/ {gsub(/\(/, "", $4); print $4; exit}')"
  if [ "${admins:-0}" -gt 0 ] 2>/dev/null; then
    ok "an administrator account already exists"
    return 0
  fi
  if [ "$UNATTENDED" -eq 1 ]; then
    warn "no administrator account exists yet"
    action "Create the first login: sudo securecam-admin user add --username <name> --role admin"
    return 0
  fi

  say ""
  say "  Create the first login for the web interface."
  local username=""
  read -r -p "  Username [admin]: " username </dev/tty || username=""
  username="${username:-admin}"
  if sudo -u "$SECURECAM_USER" "$VENV/bin/securecam-admin" --config "$SECURECAM_CONFIG" \
        user add --username "$username" --role admin </dev/tty; then
    ok "administrator '$username' created"
  else
    warn "the account was not created"
    action "Try again: sudo securecam-admin user add --username $username --role admin"
  fi
}

# ----------------------------------------------------------------- start

start_services() {
  step "11/11  Starting services"
  systemctl enable --now securecam-mediamtx.service >/dev/null 2>&1 || true
  systemctl restart securecam-mediamtx.service
  sleep 3
  if unit_active securecam-mediamtx.service; then
    ok "securecam-mediamtx.service is running"
  else
    bad "securecam-mediamtx.service did not start"
    explain "The camera stream is not running, so nothing is being recorded." \
            "the camera is not detected, or mediamtx.yml is invalid" \
            "run: sudo journalctl -u securecam-mediamtx -n 50 --no-pager"
  fi

  systemctl enable --now securecam.service >/dev/null 2>&1 || true
  systemctl restart securecam.service
  sleep 3
  if unit_active securecam.service; then
    ok "securecam.service is running"
  else
    bad "securecam.service did not start"
    explain "Motion detection, notifications and the web interface are down." \
            "a configuration error, or a port that is already in use" \
            "run: sudo journalctl -u securecam -n 50 --no-pager"
  fi
}

print_summary() {
  local ip port scheme
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  ip="${ip:-<pi-address>}"
  port="$(awk '/^api:/{f=1} f && /^[[:space:]]*port:/{print $2; exit}' "$SECURECAM_CONFIG")"
  port="${port:-8080}"
  scheme="http"

  printf '\n%s\n' "${C_BOLD}================ SecureCam installation summary ================${C_RESET}"
  printf '\n'
  printf '  Web interface   : %s://%s:%s/\n' "$scheme" "$ip" "$port"
  printf '  Live stream     : rtsp://%s:8554/cam  (VLC, Frigate, Blue Iris)\n' "$ip"
  printf '  Configuration   : %s\n' "$SECURECAM_CONFIG"
  printf '  Secrets         : %s\n' "$ENV_FILE"
  printf '  Recordings      : %s/events\n' "$SECURECAM_DATA_DIR"
  printf '  Rolling buffer  : %s/buffer\n' "$SECURECAM_DATA_DIR"
  printf '  Services        : securecam.service, securecam-mediamtx.service\n'
  printf '  Logs            : sudo journalctl -u securecam -f\n'
  printf '  Health check    : sudo securecam-admin diagnose\n'

  if [ "${#ACTIONS[@]}" -gt 0 ]; then
    printf '\n%s\n' "${C_YELLOW}${C_BOLD}ACTION REQUIRED${C_RESET}"
    local i=1
    for item in "${ACTIONS[@]}"; do
      printf '  %d. %s\n' "$i" "$item"
      i=$((i + 1))
    done
  fi

  printf '\n%s\n' "${C_BOLD}Recommended next steps${C_RESET}"
  printf '  1. Open the web interface and confirm you can see the live picture.\n'
  printf '  2. Walk in front of the camera and confirm an event appears.\n'
  printf '  3. Send a test alarm:  sudo securecam-admin test-notify\n'
  printf '  4. Reach the camera from outside your home: sudo ./scripts/setup-remote-access.sh\n'
  printf '  5. Reboot once and confirm everything comes back:  sudo reboot\n'

  if [ "$FAIL_COUNT" -gt 0 ]; then
    printf '\n%s\n' "${C_RED}${C_BOLD}$FAIL_COUNT step(s) failed. Run: sudo ./scripts/diagnose.sh${C_RESET}"
  elif [ "$WARN_COUNT" -gt 0 ]; then
    printf '\n%s\n' "${C_YELLOW}Installed with $WARN_COUNT warning(s). Run: sudo ./scripts/diagnose.sh${C_RESET}"
  else
    printf '\n%s\n' "${C_GREEN}${C_BOLD}SecureCam is installed and running.${C_RESET}"
  fi
  printf '%s\n' "${C_BOLD}===============================================================${C_RESET}"
}

main() {
  preflight
  install_packages
  create_user
  create_dirs
  install_app
  install_mediamtx
  install_config
  configure_pir
  install_system_files
  create_admin
  start_services
  print_summary
  [ "$FAIL_COUNT" -eq 0 ]
}

main "$@"
