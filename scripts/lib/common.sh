#!/usr/bin/env bash
# Shared helpers for the SecureCam shell scripts.

if [ -n "${SECURECAM_COMMON_SH:-}" ]; then return 0; fi
SECURECAM_COMMON_SH=1

SECURECAM_PREFIX="${SECURECAM_PREFIX:-/opt/securecam}"
SECURECAM_CONFIG_DIR="${SECURECAM_CONFIG_DIR:-/etc/securecam}"
SECURECAM_CONFIG="${SECURECAM_CONFIG:-$SECURECAM_CONFIG_DIR/config.yaml}"
SECURECAM_DATA_DIR="${SECURECAM_DATA_DIR:-/var/lib/securecam}"
SECURECAM_USER="${SECURECAM_USER:-securecam}"
SECURECAM_VENV="${SECURECAM_VENV:-$SECURECAM_PREFIX/venv}"
SECURECAM_ADMIN="$SECURECAM_VENV/bin/securecam-admin"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RESET=$'\033[0m'; C_RED=$'\033[31m'; C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'; C_BLUE=$'\033[34m'; C_BOLD=$'\033[1m'
else
  C_RESET=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_BOLD=""
fi

FAIL_COUNT=0
WARN_COUNT=0

say()  { printf '%s\n' "$*"; }
info() { printf '%s\n' "${C_BLUE}==>${C_RESET} $*"; }
ok()   { printf '%s\n' "  ${C_GREEN}OK${C_RESET}      $*"; }
warn() { WARN_COUNT=$((WARN_COUNT + 1)); printf '%s\n' "  ${C_YELLOW}WARN${C_RESET}    $*"; }
bad()  { FAIL_COUNT=$((FAIL_COUNT + 1)); printf '%s\n' "  ${C_RED}FAIL${C_RESET}    $*"; }
step() { printf '\n%s\n' "${C_BOLD}$*${C_RESET}"; }
die()  { printf '%s\n' "${C_RED}error:${C_RESET} $*" >&2; exit 1; }

# Explain a failure the way the rest of the project does: what, why, impact, next step.
explain() {
  printf '          %s\n' "$1"
  [ -n "${2:-}" ] && printf '          %s\n' "Likely cause: $2"
  [ -n "${3:-}" ] && printf '          %s\n' "Do this next: $3"
  return 0
}

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    die "this script must run as root. Try: sudo $0 $*"
  fi
}

has_cmd() { command -v "$1" >/dev/null 2>&1; }

is_raspberry_pi() {
  grep -qi 'raspberry pi' /proc/device-tree/model 2>/dev/null ||
    grep -qi 'raspberry pi' /proc/cpuinfo 2>/dev/null
}

pi_model() {
  tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo "unknown"
}

unit_active() { systemctl is-active --quiet "$1"; }

# Print the exit summary and return a shell-friendly status code.
summary() {
  printf '\n'
  if [ "$FAIL_COUNT" -eq 0 ] && [ "$WARN_COUNT" -eq 0 ]; then
    printf '%s\n' "${C_GREEN}Everything checked out.${C_RESET}"
    return 0
  fi
  printf '%s\n' "${C_BOLD}Summary:${C_RESET} $FAIL_COUNT failure(s), $WARN_COUNT warning(s)."
  [ "$FAIL_COUNT" -gt 0 ] && return 1
  return 0
}
