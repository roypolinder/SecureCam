"""Shared handle on every subsystem, passed to the API layer and the CLI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from .arming import ArmingState
from .auth import TokenSigner, UserStore
from .config import Config
from .events import EventStore
from .health import HealthMonitor
from .mediamtx import MediaMTXClient, MediaMTXSupervisor
from .networking import NetworkMonitor
from .pipeline import EventPipeline
from .pir import PirMonitor
from .storage import StorageManager


@dataclass
class AppContext:
    """Everything the API and CLI need, wired up once in main."""

    config: Config
    device_id: str
    store: EventStore
    users: UserStore
    signer: TokenSigner
    storage: StorageManager
    network: NetworkMonitor
    client: MediaMTXClient
    supervisor: MediaMTXSupervisor
    pipeline: EventPipeline
    service_credentials: Tuple[str, str]
    pir: Optional[PirMonitor] = None
    health: Optional[HealthMonitor] = None
    arming: Optional[ArmingState] = None
    set_armed: Optional[Callable[[bool, str], Dict[str, Any]]] = None
    controller_state: Callable[[], Dict[str, Any]] = dict
    version: str = ""
