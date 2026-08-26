"""Device identity and secret material: stable id, signing key, env-file secrets."""

from __future__ import annotations

import os
import re
import secrets
import socket
from typing import Dict, Optional

from .config import DEVICE_ID_PATH, ENV_FILE_PATH, SECRET_KEY_PATH, Config
from .logging_setup import get_logger, register_secret

log = get_logger("device")

_SAFE_ID = re.compile(r"[^a-z0-9-]+")


def slugify(value: str) -> str:
    """Reduce arbitrary text to a lowercase, hyphenated identifier."""
    slug = _SAFE_ID.sub("-", value.strip().lower()).strip("-")
    return slug or "securecam"


def resolve_device_id(config: Config, persist: bool = True, path: str = DEVICE_ID_PATH) -> str:
    """Return this Pi's stable device id, creating and persisting one if needed."""
    if config.device.id:
        return slugify(config.device.id)

    stored = _read_text(path)
    if stored:
        return slugify(stored)

    generated = f"{slugify(config.device.name or socket.gethostname())}-{secrets.token_hex(3)}"
    if persist:
        try:
            _write_private(path, generated + "\n")
            log.info("Generated device id %s and stored it in %s", generated, path)
        except OSError as exc:
            log.warning(
                "Could not persist the generated device id to %s (%s). A new id will be generated on the "
                "next restart, which invalidates existing logins. Fix the permissions on that directory, "
                "or set device.id in config.yaml.",
                path,
                exc,
            )
    return generated


def load_secret_key(path: str = SECRET_KEY_PATH, create: bool = True) -> bytes:
    """Load the HMAC key that signs sessions and stream tickets, creating it once."""
    env_value = os.environ.get("SECURECAM_SECRET_KEY", "").strip()
    if env_value:
        register_secret(env_value)
        return env_value.encode("utf-8")

    stored = _read_text(path)
    if stored:
        register_secret(stored)
        return stored.encode("utf-8")

    if not create:
        raise FileNotFoundError(
            f"No signing key found. Expected SECURECAM_SECRET_KEY in {ENV_FILE_PATH} or a key file at {path}."
        )

    generated = secrets.token_hex(32)
    _write_private(path, generated + "\n")
    register_secret(generated)
    log.warning(
        "No signing key existed, so a new one was generated at %s. Any previously issued session tokens "
        "are now invalid and users must log in again.",
        path,
    )
    return generated.encode("utf-8")


def load_env_file(path: str = ENV_FILE_PATH, override: bool = False) -> Dict[str, str]:
    """Read a systemd-style KEY=VALUE file into the environment for non-systemd runs."""
    values: Dict[str, str] = {}
    if not os.path.isfile(path):
        return values
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError as exc:
        log.warning("Cannot read the secrets file %s (%s). Features that need API keys will be skipped.", path, exc)
        return values

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        values[key] = value
        register_secret(value)
        if override or key not in os.environ:
            os.environ[key] = value
    return values


def get_secret(env_name: str) -> Optional[str]:
    """Read a secret from the environment, treating blank as missing."""
    if not env_name:
        return None
    value = os.environ.get(env_name, "").strip()
    if not value:
        return None
    register_secret(value)
    return value


def _read_text(path: str) -> str:
    """Read and strip a small text file, returning '' when it does not exist."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _write_private(path: str, content: str) -> None:
    """Write a file that only the owner can read."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o750, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.chmod(path, 0o600)
