"""securecam-admin: configuration checks, user management and on-device diagnostics."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import tempfile
from typing import List, Optional

from .. import __version__
from ..ai import create_provider as create_ai_provider
from ..auth.store import AuthError
from ..config import DEFAULT_CONFIG_PATH, Config, ConfigError, load_config, redacted_dict
from ..device import load_env_file
from ..diagnostics import build_report, format_report
from ..logging_setup import configure_logging
from ..mediamtx import render_config, write_config


def _load(args) -> Config:
    """Load the configuration or exit with a readable explanation."""
    try:
        return load_config(args.config)
    except ConfigError as exc:
        print(exc.render(), file=sys.stderr)
        raise SystemExit(2)


def _controller(config: Config):
    """Build the wiring without starting any threads."""
    from ..main import Controller

    return Controller(config)


def _prompt_password(prompt: str = "Password: ") -> str:
    """Ask twice for a password on a terminal, or read one line from stdin when piped."""
    if not sys.stdin.isatty():
        return sys.stdin.readline().strip()
    first = getpass.getpass(prompt)
    second = getpass.getpass("Repeat password: ")
    if first != second:
        print("The two passwords do not match.", file=sys.stderr)
        raise SystemExit(1)
    return first


# -- commands ---------------------------------------------------------------


def cmd_check_config(args) -> int:
    """Validate config.yaml and report every problem at once."""
    config = _load(args)
    for key in config.unknown_keys:
        print(f"warning: unknown option '{key}' is ignored - check the spelling")
    print(f"Configuration at {args.config} is valid.")
    print(f"  device            : {config.device.name}")
    print(f"  camera            : {config.camera.width}x{config.camera.height} @ {config.camera.fps} fps, "
          f"{config.camera.bitrate // 1000} kbps")
    print(f"  rolling buffer    : {config.storage.buffer_retain_minutes} minutes at {config.storage.buffer_path}")
    print(f"  events            : {config.storage.events_path}")
    print(f"  motion            : {'GPIO ' + str(config.motion.gpio) if config.motion.enabled else 'disabled'}")
    print(f"  ai                : {config.ai.provider if config.ai.enabled else 'disabled'}")
    print(f"  notifications     : {config.notifications.provider if config.notifications.enabled else 'disabled'}")
    print(f"  api               : {config.api.address}:{config.api.port}"
          f"{' (TLS)' if config.api.tls.enabled else ''}" if config.api.enabled else "  api               : disabled")
    if args.show:
        print()
        print(json.dumps(redacted_dict(config), indent=2))
    return 0


def cmd_render_mediamtx(args) -> int:
    """Write /etc/securecam/mediamtx.yml from the template and config.yaml."""
    config = _load(args)
    user = os.environ.get("SECURECAM_MEDIAMTX_SERVICE_USER", "securecam-service")
    password = os.environ.get("SECURECAM_MEDIAMTX_SERVICE_PASS", "")
    if args.stdout:
        sys.stdout.write(render_config(config, user, password))
        return 0
    changed = write_config(config, user, password)
    print(f"{'Wrote' if changed else 'Unchanged'}: {config.mediamtx.config_path}")
    if changed:
        print("Restart the stream service to apply it: sudo systemctl restart securecam-mediamtx.service")
    return 0


def cmd_device_show(args) -> int:
    """Print this camera's identity and the URLs it serves."""
    config = _load(args)
    controller = _controller(config)
    scheme = "https" if config.api.tls.enabled else "http"
    print(f"device id   : {controller.device_id}")
    print(f"device name : {config.device.name}")
    print(f"version     : {__version__}")
    print(f"web ui      : {scheme}://<pi-address>:{config.api.port}/" if config.api.web_ui else "web ui      : disabled")
    print(f"rtsp        : {config.mediamtx.rtsp_url}")
    print(f"webrtc      : http://<pi-address>:{config.mediamtx.webrtc_port}/{config.mediamtx.path_name}")
    print(f"users       : {controller.users.count()} ({controller.users.admin_count()} admin)")
    return 0


def cmd_user_list(args) -> int:
    """List accounts and their role on this camera."""
    controller = _controller(_load(args))
    users = controller.users.list_users()
    if not users:
        print("No users yet. Create one with: sudo securecam-admin user add --username <name> --role admin")
        return 0
    print(f"{'USERNAME':<20}{'ROLE':<10}{'STATE':<10}{'LAST LOGIN'}")
    for user in sorted(users, key=lambda u: u.username.lower()):
        role = user.role_for(controller.device_id) or "-"
        state = "enabled" if user.enabled else "disabled"
        print(f"{user.username:<20}{role:<10}{state:<10}{user.last_login_at or 'never'}")
    return 0


def cmd_user_add(args) -> int:
    """Create an account."""
    controller = _controller(_load(args))
    password = args.password or _prompt_password()
    try:
        controller.users.add_user(args.username, password, args.role)
    except AuthError as exc:
        print(f"Could not create the user: {exc}", file=sys.stderr)
        return 1
    print(f"Created '{args.username}' with role '{args.role}' on device {controller.device_id}.")
    return 0


def cmd_user_passwd(args) -> int:
    """Change an account's password."""
    controller = _controller(_load(args))
    password = args.password or _prompt_password("New password: ")
    try:
        controller.users.set_password(args.username, password)
    except AuthError as exc:
        print(f"Could not change the password: {exc}", file=sys.stderr)
        return 1
    print(f"Password updated for '{args.username}'. Existing sessions stay valid until they expire.")
    return 0


def cmd_user_role(args) -> int:
    """Change an account's role on this camera."""
    controller = _controller(_load(args))
    try:
        controller.users.set_role(args.username, args.role)
    except AuthError as exc:
        print(f"Could not change the role: {exc}", file=sys.stderr)
        return 1
    print(f"'{args.username}' is now '{args.role}' on device {controller.device_id}.")
    return 0


def cmd_user_delete(args) -> int:
    """Delete an account."""
    controller = _controller(_load(args))
    try:
        controller.users.delete_user(args.username)
    except AuthError as exc:
        print(f"Could not delete the user: {exc}", file=sys.stderr)
        return 1
    print(f"Deleted '{args.username}'.")
    return 0


def cmd_user_enable(args) -> int:
    """Enable or disable an account."""
    controller = _controller(_load(args))
    try:
        controller.users.set_enabled(args.username, not args.disable)
    except AuthError as exc:
        print(f"Could not update the account: {exc}", file=sys.stderr)
        return 1
    print(f"'{args.username}' is now {'disabled' if args.disable else 'enabled'}.")
    return 0


def cmd_test_notify(args) -> int:
    """Send a test alarm to every enabled recipient."""
    config = _load(args)
    if not config.notifications.enabled:
        print("Notifications are disabled in config.yaml (notifications.enabled: false).", file=sys.stderr)
        return 1
    controller = _controller(config)
    for problem in controller.pipeline.check_providers():
        print(f"warning: {problem}")
    results = controller.pipeline.send_test_notification()
    if not results:
        print("No recipients are enabled. Add one under notifications.recipients in config.yaml.", file=sys.stderr)
        return 1
    failed = 0
    for result in results:
        status = "sent" if result.ok else "FAILED"
        detail = result.error if result.error else ", ".join(f"{k}={v}" for k, v in result.detail.items())
        print(f"  {result.recipient_id or result.provider:<20} {status:<8} {detail}")
        failed += 0 if result.ok else 1
    if failed:
        print("\nSome notifications failed. Check the token, the topic name and internet access.", file=sys.stderr)
        return 1
    print("\nAll test alarms were accepted by the provider. If your phone stayed silent, the problem is on the "
          "phone: allow the app to bypass Do Not Disturb and set the channel to a loud, repeating sound.")
    return 0


def cmd_test_ai(args) -> int:
    """Capture (or read) an image and run the configured AI provider on it."""
    config = _load(args)
    if not config.ai.enabled:
        print("AI is disabled in config.yaml (ai.enabled: false).", file=sys.stderr)
        return 1
    controller = _controller(config)
    provider = create_ai_provider(config.ai)
    ok, message = provider.check()
    if not ok:
        print(f"The AI provider is not usable: {message}", file=sys.stderr)
        return 1

    if args.image:
        try:
            with open(args.image, "rb") as handle:
                image = handle.read()
        except OSError as exc:
            print(f"Could not read {args.image}: {exc}", file=sys.stderr)
            return 1
    else:
        path = os.path.join(tempfile.gettempdir(), "securecam-test.jpg")
        try:
            controller.snapshotter.capture(path)
        except Exception as exc:
            print(f"Could not capture a snapshot: {exc}", file=sys.stderr)
            print("Is the stream running? Check: systemctl status securecam-mediamtx.service", file=sys.stderr)
            return 1
        with open(path, "rb") as handle:
            image = handle.read()
        print(f"Captured a test snapshot ({len(image)} bytes) from the live stream.")

    try:
        result = provider.analyze([image], {"device_name": config.device.name})
    except Exception as exc:
        print(f"The AI request failed: {exc}", file=sys.stderr)
        return 1
    print(f"provider        : {result.provider}")
    print(f"person detected : {result.person_detected}")
    print(f"confidence      : {result.confidence}")
    print(f"label           : {result.label}")
    print(f"summary         : {result.summary}")
    return 0


def cmd_diagnose(args) -> int:
    """Full health and configuration report."""
    controller = _controller(_load(args))
    report = build_report(controller.context())
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(format_report(report))
    return 1 if report["warnings"] else 0


# -- argument parsing -------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the securecam-admin command tree."""
    parser = argparse.ArgumentParser(prog="securecam-admin", description="SecureCam administration")
    parser.add_argument("--config", default=os.environ.get("SECURECAM_CONFIG", DEFAULT_CONFIG_PATH))
    parser.add_argument("--version", action="version", version=f"securecam {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check-config", help="validate config.yaml")
    check.add_argument("--show", action="store_true", help="print the effective configuration with secrets masked")
    check.set_defaults(func=cmd_check_config)

    render = sub.add_parser("render-mediamtx", help="regenerate mediamtx.yml from config.yaml")
    render.add_argument("--stdout", action="store_true", help="print instead of writing the file")
    render.set_defaults(func=cmd_render_mediamtx)

    device = sub.add_parser("device", help="device identity and URLs")
    device_sub = device.add_subparsers(dest="device_command", required=True)
    device_sub.add_parser("show", help="print device identity").set_defaults(func=cmd_device_show)

    user = sub.add_parser("user", help="manage accounts")
    user_sub = user.add_subparsers(dest="user_command", required=True)

    user_sub.add_parser("list", help="list accounts").set_defaults(func=cmd_user_list)

    add = user_sub.add_parser("add", help="create an account")
    add.add_argument("--username", required=True)
    add.add_argument("--role", default="viewer", choices=["viewer", "admin"])
    add.add_argument("--password", help="read from a prompt or stdin when omitted")
    add.set_defaults(func=cmd_user_add)

    passwd = user_sub.add_parser("passwd", help="change a password")
    passwd.add_argument("--username", required=True)
    passwd.add_argument("--password")
    passwd.set_defaults(func=cmd_user_passwd)

    role = user_sub.add_parser("role", help="change a role")
    role.add_argument("--username", required=True)
    role.add_argument("--role", required=True, choices=["viewer", "admin"])
    role.set_defaults(func=cmd_user_role)

    enable = user_sub.add_parser("enable", help="enable or disable an account")
    enable.add_argument("--username", required=True)
    enable.add_argument("--disable", action="store_true")
    enable.set_defaults(func=cmd_user_enable)

    delete = user_sub.add_parser("delete", help="delete an account")
    delete.add_argument("--username", required=True)
    delete.set_defaults(func=cmd_user_delete)

    notify = sub.add_parser("test-notify", help="send a test alarm to every recipient")
    notify.set_defaults(func=cmd_test_notify)

    ai = sub.add_parser("test-ai", help="run the AI provider on a live or supplied image")
    ai.add_argument("--image", help="use this JPEG instead of capturing one")
    ai.set_defaults(func=cmd_test_ai)

    diagnose = sub.add_parser("diagnose", help="full health report")
    diagnose.add_argument("--json", action="store_true")
    diagnose.set_defaults(func=cmd_diagnose)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for the securecam-admin console script."""
    load_env_file()
    configure_logging(level="WARNING", fmt="text")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except PermissionError as exc:
        print(
            f"Permission denied: {exc}\n"
            "  This command needs to read or write files under /etc/securecam.\n"
            "  Run it with sudo, or as the securecam service user.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
