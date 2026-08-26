"""Deterministic event state machine. Pure logic with an injected clock, so it is fully testable."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional


class EventState(Enum):
    IDLE = "idle"
    MOTION_DETECTED = "motion_detected"
    RECORDING = "recording"
    POST_MOTION = "post_motion"
    FINALIZING = "finalizing"
    COMPLETED = "completed"


class ActionKind(Enum):
    START_EVENT = "start_event"
    MOTION_RESUMED = "motion_resumed"
    MOTION_PAUSED = "motion_paused"
    FINALIZE_EVENT = "finalize_event"


class FinalizeReason(Enum):
    QUIET_PERIOD = "quiet_period"
    MAX_DURATION = "max_duration"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    at: float
    reason: Optional[FinalizeReason] = None


class MotionStateMachine:
    """IDLE -> MOTION_DETECTED -> RECORDING <-> POST_MOTION -> FINALIZING -> COMPLETED -> IDLE."""

    def __init__(
        self,
        post_motion_seconds: float,
        max_event_seconds: float,
        cooldown_seconds: float = 0.0,
    ) -> None:
        self.post_motion_seconds = float(post_motion_seconds)
        self.max_event_seconds = float(max_event_seconds)
        self.cooldown_seconds = float(cooldown_seconds)
        self._state = EventState.IDLE
        self._motion_active = False
        self._started_at: Optional[float] = None
        self._quiet_until: Optional[float] = None
        self._cooldown_until = 0.0
        self._last_motion_at: Optional[float] = None

    # -- introspection ------------------------------------------------------

    @property
    def state(self) -> EventState:
        return self._state

    @property
    def motion_active(self) -> bool:
        return self._motion_active

    @property
    def started_at(self) -> Optional[float]:
        """Monotonic timestamp of the current event's first trigger."""
        return self._started_at

    @property
    def last_motion_at(self) -> Optional[float]:
        return self._last_motion_at

    @property
    def in_event(self) -> bool:
        return self._state in (EventState.MOTION_DETECTED, EventState.RECORDING, EventState.POST_MOTION)

    def elapsed(self, now: float) -> float:
        """Seconds since the current event started, 0 when idle."""
        return 0.0 if self._started_at is None else max(0.0, now - self._started_at)

    def quiet_remaining(self, now: float) -> float:
        """Seconds left before the quiet period ends, 0 when not counting down."""
        if self._state is not EventState.POST_MOTION or self._quiet_until is None:
            return 0.0
        return max(0.0, self._quiet_until - now)

    def cooldown_remaining(self, now: float) -> float:
        """Seconds left before a new event may start."""
        return max(0.0, self._cooldown_until - now)

    # -- inputs -------------------------------------------------------------

    def on_motion_start(self, now: float) -> List[Action]:
        """Handle a debounced rising edge from the PIR."""
        self._motion_active = True
        self._last_motion_at = now
        if self._state is EventState.IDLE:
            if now < self._cooldown_until:
                return []
            return self._begin(now)
        if self._state is EventState.POST_MOTION:
            self._state = EventState.RECORDING
            self._quiet_until = None
            return [Action(ActionKind.MOTION_RESUMED, now)]
        return []

    def on_motion_end(self, now: float) -> List[Action]:
        """Handle a debounced falling edge from the PIR."""
        self._motion_active = False
        self._last_motion_at = now
        if self._state in (EventState.MOTION_DETECTED, EventState.RECORDING):
            self._state = EventState.POST_MOTION
            self._quiet_until = now + self.post_motion_seconds
            return [Action(ActionKind.MOTION_PAUSED, now)]
        return []

    def mark_recording(self) -> None:
        """Called once the event record exists and buffering is confirmed."""
        if self._state is EventState.MOTION_DETECTED:
            self._state = EventState.RECORDING

    def tick(self, now: float) -> List[Action]:
        """Advance timers. Call this regularly; it is the only source of timeouts."""
        actions: List[Action] = []
        if self.in_event and self._started_at is not None:
            if now - self._started_at >= self.max_event_seconds:
                return self._finalize(now, FinalizeReason.MAX_DURATION)
            if self._state is EventState.POST_MOTION and self._quiet_until is not None and now >= self._quiet_until:
                return self._finalize(now, FinalizeReason.QUIET_PERIOD)
        elif self._state is EventState.IDLE and self._motion_active and now >= self._cooldown_until:
            # Motion never stopped after the previous event was capped; start the next one.
            actions.extend(self._begin(now))
        return actions

    def force_finalize(self, now: float, reason: FinalizeReason = FinalizeReason.SHUTDOWN) -> List[Action]:
        """Finalize immediately, used on shutdown."""
        if not self.in_event:
            return []
        return self._finalize(now, reason)

    def notify_finalized(self, now: float) -> None:
        """Called once clip extraction has been handed off; returns the machine to idle."""
        if self._state is EventState.FINALIZING:
            self._state = EventState.COMPLETED
        self._state = EventState.IDLE
        self._started_at = None
        self._quiet_until = None
        self._cooldown_until = now + self.cooldown_seconds

    # -- internals ----------------------------------------------------------

    def _begin(self, now: float) -> List[Action]:
        self._state = EventState.MOTION_DETECTED
        self._started_at = now
        self._quiet_until = None
        return [Action(ActionKind.START_EVENT, now)]

    def _finalize(self, now: float, reason: FinalizeReason) -> List[Action]:
        self._state = EventState.FINALIZING
        return [Action(ActionKind.FINALIZE_EVENT, now, reason)]
