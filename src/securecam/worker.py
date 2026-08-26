"""Background workers: a small task pool for live events and a retry scanner for stalled work."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from .config import Config
from .events import EventStore
from .logging_setup import get_logger
from .networking import ConnectivityStatus
from .pipeline import EventPipeline

log = get_logger("worker")


class TaskRunner:
    """Bounded thread pool so slow AI or notification calls never stall motion detection."""

    def __init__(self, max_workers: int = 2) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="securecam-task")
        self._closed = False

    def submit(self, function: Callable, *args) -> None:
        """Run a callable on the pool, logging any escape."""
        if self._closed:
            return
        future = self._pool.submit(function, *args)
        future.add_done_callback(_log_failure)

    def shutdown(self, wait: bool = True) -> None:
        """Stop accepting work and optionally wait for what is running."""
        self._closed = True
        self._pool.shutdown(wait=wait)


def _log_failure(future) -> None:
    """Surface exceptions that would otherwise be swallowed by the pool."""
    error = future.exception()
    if error is not None:
        log.error("A background task failed: %s", error, exc_info=error)


class PendingWorker:
    """Periodically retries AI, notification and clip work that did not finish."""

    def __init__(
        self,
        config: Config,
        store: EventStore,
        pipeline: EventPipeline,
        interval_seconds: float = 60.0,
    ) -> None:
        self._config = config
        self._store = store
        self._pipeline = pipeline
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_pending = 0

    @property
    def pending_count(self) -> int:
        """Number of events waiting for another attempt at the last scan."""
        return self._last_pending

    def start(self) -> None:
        """Begin scanning."""
        self._thread = threading.Thread(target=self._run, name="securecam-pending", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Stop scanning."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def wake(self) -> None:
        """Run a scan immediately, for example when connectivity comes back."""
        self._wake.set()

    def on_connectivity_change(self, status: ConnectivityStatus) -> None:
        """Connectivity listener that flushes the queue as soon as the internet returns."""
        if status.online:
            log.info("Connectivity restored; retrying queued AI and notification work")
            self.wake()

    def run_once(self) -> int:
        """Process everything that is due. Returns how many events were touched."""
        try:
            events = self._store.pending()
        except Exception:
            log.exception("Could not scan for pending event work")
            return 0
        self._last_pending = len(events)
        for event in events:
            if self._stop.is_set():
                break
            self._pipeline.process_pending(event)
        return len(events)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self._interval)
            self._wake.clear()
            if self._stop.is_set():
                break
            handled = self.run_once()
            if handled:
                log.debug("Retried background work for %d event(s)", handled)
