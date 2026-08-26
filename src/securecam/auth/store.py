"""User database: PBKDF2 password hashing, roles, and explicit per-device bindings.

A user account only grants access to the devices it is explicitly bound to. The
file lives on one Pi, so an account created here says nothing about any other
camera you own.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger
from ..util import atomic_write_json, read_json, to_rfc3339, utcnow

log = get_logger("auth")

SCHEMA_VERSION = 1
PBKDF2_ITERATIONS = 210_000
SALT_BYTES = 16
MIN_PASSWORD_LENGTH = 10


class Permission(str, Enum):
    VIEW_LIVE = "view_live"
    VIEW_EVENTS = "view_events"
    RECEIVE_NOTIFICATIONS = "receive_notifications"
    CONFIGURE = "configure"
    MANAGE_USERS = "manage_users"
    MANAGE_DEVICE = "manage_device"


VIEWER_PERMISSIONS = {
    Permission.VIEW_LIVE,
    Permission.VIEW_EVENTS,
    Permission.RECEIVE_NOTIFICATIONS,
}

ROLES: Dict[str, set] = {
    "viewer": set(VIEWER_PERMISSIONS),
    "admin": set(VIEWER_PERMISSIONS)
    | {Permission.CONFIGURE, Permission.MANAGE_USERS, Permission.MANAGE_DEVICE},
}


def has_permission(role: str, permission: Permission) -> bool:
    """True when a role grants a permission."""
    return permission in ROLES.get(role, set())


class AuthError(Exception):
    """Raised for invalid user-management requests."""


@dataclass
class DeviceBinding:
    device_id: str
    role: str = "viewer"


@dataclass
class User:
    id: str
    username: str
    password_hash: str = ""
    salt: str = ""
    iterations: int = PBKDF2_ITERATIONS
    algorithm: str = "pbkdf2_sha256"
    enabled: bool = True
    created_at: str = ""
    last_login_at: Optional[str] = None
    devices: List[DeviceBinding] = field(default_factory=list)

    def role_for(self, device_id: str) -> Optional[str]:
        """Role this user holds on a device, or None when not bound to it."""
        for binding in self.devices:
            if binding.device_id == device_id:
                return binding.role
        return None

    def public(self, device_id: str = "") -> Dict[str, Any]:
        """Representation safe to return from the API."""
        return {
            "id": self.id,
            "username": self.username,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "last_login_at": self.last_login_at,
            "role": self.role_for(device_id) if device_id else None,
            "devices": [{"device_id": b.device_id, "role": b.role} for b in self.devices],
        }


class UserStore:
    """Thread-safe JSON-backed user database with 0600 permissions."""

    def __init__(self, path: str, device_id: str) -> None:
        self.path = path
        self.device_id = device_id
        self._lock = threading.RLock()
        self._users: Dict[str, User] = {}
        self._load()

    # -- persistence --------------------------------------------------------

    def _load(self) -> None:
        if not os.path.isfile(self.path):
            self._users = {}
            return
        try:
            data = read_json(self.path)
        except (OSError, ValueError) as exc:
            raise AuthError(
                f"The user database {self.path} could not be read: {exc}\n"
                "  Nothing was started, because starting without it would leave the API unauthenticated.\n"
                "  Fix: restore the file from a backup, or delete it and recreate the admin account with\n"
                "       sudo securecam-admin user add --username <name> --role admin"
            ) from exc
        users: Dict[str, User] = {}
        for entry in data.get("users", []) if isinstance(data, dict) else []:
            try:
                user = User(
                    id=str(entry["id"]),
                    username=str(entry["username"]),
                    password_hash=str(entry.get("password_hash", "")),
                    salt=str(entry.get("salt", "")),
                    iterations=int(entry.get("iterations", PBKDF2_ITERATIONS)),
                    algorithm=str(entry.get("algorithm", "pbkdf2_sha256")),
                    enabled=bool(entry.get("enabled", True)),
                    created_at=str(entry.get("created_at", "")),
                    last_login_at=entry.get("last_login_at"),
                    devices=[
                        DeviceBinding(str(b.get("device_id", "")), str(b.get("role", "viewer")))
                        for b in entry.get("devices", [])
                        if isinstance(b, dict)
                    ],
                )
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("Skipping malformed user entry in %s: %s", self.path, exc)
                continue
            users[user.username.lower()] = user
        self._users = users

    def _save(self) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "device_id": self.device_id,
            "users": [
                {
                    "id": user.id,
                    "username": user.username,
                    "password_hash": user.password_hash,
                    "salt": user.salt,
                    "iterations": user.iterations,
                    "algorithm": user.algorithm,
                    "enabled": user.enabled,
                    "created_at": user.created_at,
                    "last_login_at": user.last_login_at,
                    "devices": [{"device_id": b.device_id, "role": b.role} for b in user.devices],
                }
                for user in self._users.values()
            ],
        }
        atomic_write_json(self.path, payload, mode=0o600)

    # -- queries ------------------------------------------------------------

    def list_users(self) -> List[User]:
        """All accounts in this database."""
        with self._lock:
            return list(self._users.values())

    def get(self, username: str) -> Optional[User]:
        """Look up an account by username, case-insensitively."""
        with self._lock:
            return self._users.get(username.strip().lower())

    def count(self) -> int:
        with self._lock:
            return len(self._users)

    def admin_count(self) -> int:
        """Number of accounts that can still administer this device."""
        with self._lock:
            return sum(1 for user in self._users.values() if user.enabled and user.role_for(self.device_id) == "admin")

    # -- mutations ----------------------------------------------------------

    def add_user(self, username: str, password: str, role: str = "viewer", device_ids: Optional[List[str]] = None) -> User:
        """Create an account bound to this device (or to an explicit device list)."""
        username = username.strip()
        if not username or not username.replace("_", "").replace("-", "").replace(".", "").isalnum():
            raise AuthError("username may only contain letters, digits, '.', '-' and '_'")
        if role not in ROLES:
            raise AuthError(f"role must be one of: {', '.join(sorted(ROLES))}")
        validate_password(password)
        with self._lock:
            if username.lower() in self._users:
                raise AuthError(f"user '{username}' already exists")
            salt = secrets.token_hex(SALT_BYTES)
            user = User(
                id=secrets.token_hex(8),
                username=username,
                password_hash=_hash_password(password, salt, PBKDF2_ITERATIONS),
                salt=salt,
                created_at=to_rfc3339(utcnow()),
                devices=[DeviceBinding(device_id, role) for device_id in (device_ids or [self.device_id])],
            )
            self._users[username.lower()] = user
            self._save()
        log.info("Created user '%s' with role '%s' on device %s", username, role, self.device_id)
        return user

    def set_password(self, username: str, password: str) -> None:
        """Replace an account's password."""
        validate_password(password)
        with self._lock:
            user = self._require(username)
            user.salt = secrets.token_hex(SALT_BYTES)
            user.iterations = PBKDF2_ITERATIONS
            user.algorithm = "pbkdf2_sha256"
            user.password_hash = _hash_password(password, user.salt, user.iterations)
            self._save()
        log.info("Password changed for user '%s'", username)

    def set_role(self, username: str, role: str, device_id: Optional[str] = None) -> None:
        """Bind an account to a device with a role, or change the role it already has."""
        if role not in ROLES:
            raise AuthError(f"role must be one of: {', '.join(sorted(ROLES))}")
        target_device = device_id or self.device_id
        with self._lock:
            user = self._require(username)
            if (
                target_device == self.device_id
                and user.role_for(target_device) == "admin"
                and role != "admin"
                and self.admin_count() <= 1
            ):
                raise AuthError("refusing to remove the last administrator of this device")
            for binding in user.devices:
                if binding.device_id == target_device:
                    binding.role = role
                    break
            else:
                user.devices.append(DeviceBinding(target_device, role))
            self._save()
        log.info("User '%s' now has role '%s' on device %s", username, role, target_device)

    def revoke_device(self, username: str, device_id: Optional[str] = None) -> None:
        """Remove an account's access to a device without deleting the account."""
        target_device = device_id or self.device_id
        with self._lock:
            user = self._require(username)
            if target_device == self.device_id and user.role_for(target_device) == "admin" and self.admin_count() <= 1:
                raise AuthError("refusing to remove the last administrator of this device")
            user.devices = [b for b in user.devices if b.device_id != target_device]
            self._save()
        log.info("Revoked access for user '%s' on device %s", username, target_device)

    def set_enabled(self, username: str, enabled: bool) -> None:
        """Enable or disable an account."""
        with self._lock:
            user = self._require(username)
            if not enabled and user.role_for(self.device_id) == "admin" and self.admin_count() <= 1:
                raise AuthError("refusing to disable the last administrator of this device")
            user.enabled = enabled
            self._save()

    def delete_user(self, username: str) -> None:
        """Remove an account entirely."""
        with self._lock:
            user = self._require(username)
            if user.role_for(self.device_id) == "admin" and self.admin_count() <= 1:
                raise AuthError("refusing to delete the last administrator of this device")
            self._users.pop(username.strip().lower(), None)
            self._save()
        log.info("Deleted user '%s'", username)

    # -- authentication -----------------------------------------------------

    def authenticate(self, username: str, password: str, device_id: Optional[str] = None) -> Optional[User]:
        """Verify credentials and device binding. Returns None on any failure."""
        target_device = device_id or self.device_id
        with self._lock:
            user = self._users.get(username.strip().lower())
        if user is None:
            # Spend the same time as a real verification so usernames cannot be probed.
            _hash_password(password, "0" * (SALT_BYTES * 2), PBKDF2_ITERATIONS)
            return None
        if not user.enabled:
            return None
        expected = _hash_password(password, user.salt, user.iterations)
        if not hmac.compare_digest(expected, user.password_hash):
            return None
        if user.role_for(target_device) is None:
            log.warning("User '%s' authenticated but is not bound to device %s", user.username, target_device)
            return None
        with self._lock:
            user.last_login_at = to_rfc3339(utcnow())
            self._save()
        return user

    def _require(self, username: str) -> User:
        user = self._users.get(username.strip().lower())
        if user is None:
            raise AuthError(f"user '{username}' does not exist")
        return user


def validate_password(password: str) -> None:
    """Reject passwords that are trivially guessable."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if password.lower() in ("password12", "changeme12", "securecam1", "raspberrypi"):
        raise AuthError("password is too common, choose another one")


def _hash_password(password: str, salt: str, iterations: int) -> str:
    """PBKDF2-HMAC-SHA256, from the standard library so there is no extra dependency."""
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations)
    return derived.hex()
