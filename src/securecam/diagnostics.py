"""One structured report describing everything about this camera's current state."""

from __future__ import annotations

import os
import shutil
from dataclasses import asdict
from typing import Any, Dict, List

from .appcontext import AppContext
from .health import collect_system_info
from .util import human_bytes, to_rfc3339, utcnow, which


def build_report(context: AppContext, include_events: bool = True) -> Dict[str, Any]:
    """Collect configuration, subsystem status and environment facts into one dict."""
    config = context.config
    report: Dict[str, Any] = {
        "generated_at": to_rfc3339(utcnow()),
        "version": context.version,
        "device": {
            "id": context.device_id,
            "name": config.device.name,
            "config_path": config.source_path,
            "unknown_config_keys": config.unknown_keys,
        },
        "system": asdict(collect_system_info()),
        "dependencies": _dependencies(config),
        "stream": asdict(context.supervisor.health()),
        "storage": asdict(context.storage.status(refresh_sizes=include_events)),
        "network": asdict(context.network.status()),
        "controller": context.controller_state() if context.controller_state else {},
        "providers": {
            "ai": {
                "enabled": config.ai.enabled,
                "provider": config.ai.provider,
                "problems": [],
            },
            "notifications": {
                "enabled": config.notifications.enabled,
                "provider": config.notifications.provider,
                "recipients": [r.id for r in config.notifications.active_recipients()],
                "alarm": config.notifications.alarm,
                "problems": [],
            },
        },
        "users": {
            "count": context.users.count(),
            "admins": context.users.admin_count(),
        },
    }

    problems = context.pipeline.check_providers()
    report["providers"]["ai"]["problems"] = [p for p in problems if p.startswith("ai")]
    report["providers"]["notifications"]["problems"] = [p for p in problems if p.startswith("notifications")]

    if context.pir is not None:
        report["pir"] = asdict(context.pir.status())
    else:
        report["pir"] = {"available": False, "description": "not started"}

    if context.arming is not None:
        report["arming"] = context.arming.status()
    else:
        report["arming"] = {"armed": config.motion.armed_default, "changed_by": "not started"}

    if context.health is not None:
        report["health"] = context.health.report()

    if include_events:
        report["events"] = _event_summary(context)

    report["warnings"] = _warnings(context, report)
    return report


def _dependencies(config) -> Dict[str, Any]:
    """Which external programs are present and where."""
    entries: Dict[str, Any] = {}
    for name in ("ffmpeg", "vcgencmd", "systemctl", "tailscale"):
        path = which(name)
        entries[name] = {"present": bool(path), "path": path or ""}
    entries["mediamtx"] = {
        "present": os.path.isfile(config.mediamtx.binary),
        "path": config.mediamtx.binary,
    }
    entries["mediamtx_config"] = {
        "present": os.path.isfile(config.mediamtx.config_path),
        "path": config.mediamtx.config_path,
    }
    try:
        import gpiozero  # type: ignore import-not-found

        entries["gpiozero"] = {"present": True, "version": getattr(gpiozero, "__version__", "unknown")}
    except ImportError:
        entries["gpiozero"] = {"present": False, "version": ""}
    return entries


def _event_summary(context: AppContext) -> Dict[str, Any]:
    """Counts that make it obvious whether the pipeline is keeping up."""
    total = 0
    by_status: Dict[str, int] = {}
    pending = {"recording": 0, "ai": 0, "notification": 0}
    latest: List[Dict[str, Any]] = []
    for event in context.store.iter_events():
        total += 1
        by_status[event.status] = by_status.get(event.status, 0) + 1
        for kind in pending:
            task = getattr(event, kind)
            if task.state in ("pending", "in_progress"):
                pending[kind] += 1
        if len(latest) < 5:
            latest.append(event.summary())
    return {"total": total, "by_status": by_status, "pending": pending, "latest": latest}


def _warnings(context: AppContext, report: Dict[str, Any]) -> List[str]:
    """Plain-language list of everything that needs attention."""
    warnings: List[str] = []
    config = context.config

    if not report["dependencies"]["mediamtx"]["present"]:
        warnings.append(
            f"MediaMTX is missing at {config.mediamtx.binary}. Nothing can be recorded. Re-run install.sh."
        )
    if not report["dependencies"]["ffmpeg"]["present"]:
        warnings.append("FFmpeg is missing, so snapshots and therefore AI analysis are unavailable. Run: sudo apt install -y ffmpeg")
    if not report["stream"]["path_ready"]:
        warnings.append("The camera stream is not publishing. Run: sudo ./scripts/diagnose-camera.sh")
    if config.motion.enabled and not report["pir"].get("available"):
        warnings.append("The PIR sensor is unavailable, so no motion will be recorded. Run: sudo ./scripts/diagnose-pir.sh")
    if not report["arming"].get("armed"):
        warnings.append(
            f"The camera is disarmed (by {report['arming'].get('changed_by', 'unknown')}), so motion will not be "
            "recorded. Arm it from the web UI."
        )
    if report["storage"]["over_limit"]:
        warnings.append("Storage is above the configured limit; old events are being deleted.")
    if not report["storage"]["writable"]:
        warnings.append(f"{config.storage.base_path} is not writable; no new events can be saved.")
    if not report["network"]["internet_ok"] and (config.ai.enabled or config.notifications.enabled):
        warnings.append("There is no internet connectivity, so AI and notifications are queued.")
    throttling = report["system"].get("throttling", {})
    if throttling.get("under_voltage_now") or throttling.get("under_voltage_occurred"):
        warnings.append("The Pi has reported under-voltage. Replace the power supply or cable.")
    if context.users.admin_count() == 0:
        warnings.append(
            "There is no administrator account. Create one with: "
            "sudo securecam-admin user add --username <name> --role admin"
        )
    if config.api.enabled and not config.api.tls.enabled and config.api.address not in ("127.0.0.1", "localhost"):
        warnings.append(
            "The API is reachable on the network without TLS. Use Tailscale (scripts/setup-remote-access.sh) "
            "or enable api.tls."
        )
    warnings.extend(report["providers"]["ai"]["problems"])
    warnings.extend(report["providers"]["notifications"]["problems"])
    for key in config.unknown_keys:
        warnings.append(f"config.yaml contains an unknown option '{key}', which is ignored. Check the spelling.")
    return warnings


def format_report(report: Dict[str, Any]) -> str:
    """Render the report as readable text for the terminal."""
    lines: List[str] = []
    device = report["device"]
    lines.append("SecureCam diagnostics")
    lines.append("=" * 60)
    lines.append(f"Device      : {device['name']} ({device['id']})")
    lines.append(f"Version     : {report.get('version', 'unknown')}")
    lines.append(f"Config      : {device['config_path']}")
    system = report["system"]
    lines.append(f"Model       : {system.get('model') or 'unknown'}")
    lines.append(f"CPU temp    : {system.get('cpu_temp_celsius')} C   load: {system.get('cpu_load_1m')}")
    lines.append("")

    stream = report["stream"]
    lines.append(f"Camera      : {'publishing' if stream['path_ready'] else 'NOT PUBLISHING'} "
                 f"({stream['readers']} viewer(s))")
    pir = report["pir"]
    lines.append(f"PIR sensor  : {'available' if pir.get('available') else 'UNAVAILABLE'} - {pir.get('description', '')}")
    arming = report.get("arming", {})
    lines.append(f"Arming      : {'ARMED' if arming.get('armed') else 'DISARMED'} (set by {arming.get('changed_by', '?')})")
    storage = report["storage"]
    lines.append(
        f"Storage     : {human_bytes(storage['free_bytes'])} free of {human_bytes(storage['total_bytes'])} "
        f"({storage['used_percent']}% used)"
    )
    network = report["network"]
    lines.append(f"Network     : {network.get('detail', 'unknown')}")
    events = report.get("events", {})
    if events:
        lines.append(f"Events      : {events.get('total', 0)} stored, pending {events.get('pending', {})}")
    lines.append("")

    warnings = report.get("warnings", [])
    if warnings:
        lines.append(f"ACTION REQUIRED ({len(warnings)}):")
        for warning in warnings:
            lines.append(f"  - {warning}")
    else:
        lines.append("No problems detected.")
    return "\n".join(lines)


def disk_free(path: str) -> str:
    """Human-readable free space, used by the shell diagnostics."""
    try:
        usage = shutil.disk_usage(path)
        return human_bytes(usage.free)
    except OSError:
        return "unknown"
