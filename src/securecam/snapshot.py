"""Single-frame JPEG snapshots pulled from the local RTSP stream with FFmpeg."""

from __future__ import annotations

import os
import urllib.parse
from typing import Callable, Optional, Tuple

from .config import Config
from .logging_setup import get_logger
from .util import ensure_dir, run_command, which

log = get_logger("snapshot")

Credentials = Tuple[str, str]


class SnapshotError(Exception):
    """Raised when a snapshot could not be produced."""


class SnapshotCapturer:
    """Grabs one already-encoded frame and decodes it to JPEG. Costs a few hundred ms of CPU."""

    def __init__(
        self,
        config: Config,
        credentials_provider: Callable[[], Credentials],
        max_width: int = 1280,
    ) -> None:
        self._config = config
        self._credentials = credentials_provider
        self._max_width = max_width
        self._ffmpeg: Optional[str] = which("ffmpeg")
        self._warned_missing = False

    @property
    def available(self) -> bool:
        """True when FFmpeg is installed."""
        return self._ffmpeg is not None

    def capture(self, destination: str, timeout: float = 15.0) -> int:
        """Write a JPEG snapshot and return its size in bytes."""
        if self._ffmpeg is None:
            self._ffmpeg = which("ffmpeg")
        if self._ffmpeg is None:
            if not self._warned_missing:
                self._warned_missing = True
                log.error(
                    "FFmpeg is not installed, so no snapshots can be taken.\n"
                    "  What still works: recording, streaming and notifications without an image.\n"
                    "  AI person detection is disabled because it has no image to send.\n"
                    "  Fix: sudo apt install -y ffmpeg && sudo systemctl restart securecam"
                )
            raise SnapshotError("ffmpeg is not installed")

        ensure_dir(os.path.dirname(os.path.abspath(destination)))
        partial = destination + ".part"
        command = [
            self._ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-rtsp_transport",
            "tcp",
            "-i",
            self._stream_url(),
            "-frames:v",
            "1",
            "-an",
            "-vf",
            f"scale='min({self._max_width},iw)':-2",
            "-q:v",
            "4",
            "-f",
            "image2",
            "-y",
            partial,
        ]
        result = run_command(command, timeout=timeout)
        if not result.ok or not os.path.isfile(partial) or os.path.getsize(partial) == 0:
            _unlink(partial)
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise SnapshotError(detail[-1] if detail else f"ffmpeg exited with code {result.returncode}")
        size = os.path.getsize(partial)
        os.replace(partial, destination)
        return size

    def _stream_url(self) -> str:
        """Local RTSP URL carrying short-lived read-only credentials."""
        user, password = self._credentials()
        parts = urllib.parse.urlsplit(self._config.mediamtx.rtsp_url)
        host = parts.hostname or "127.0.0.1"
        if ":" in host:
            host = f"[{host}]"
        if parts.port:
            host = f"{host}:{parts.port}"
        # RTSP only accepts credentials in the userinfo; query parameters are ignored and the read is denied.
        credentials = f"{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(password, safe='')}"
        return urllib.parse.urlunsplit(
            (parts.scheme, f"{credentials}@{host}", parts.path, parts.query, parts.fragment)
        )


def _unlink(path: str) -> None:
    """Delete a file if it exists."""
    try:
        os.unlink(path)
    except OSError:
        pass
