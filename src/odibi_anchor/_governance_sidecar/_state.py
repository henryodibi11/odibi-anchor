"""Central lifecycle state and generation-wide replay ledger."""

from __future__ import annotations

import threading
from enum import Enum


class Lifecycle(str, Enum):
    UNBOOTSTRAPPED = "UNBOOTSTRAPPED"
    READY_UNLEASED = "READY_UNLEASED"
    LEASED = "LEASED"
    CLOSING = "CLOSING"
    SHUTDOWN = "SHUTDOWN"


_LEGAL = {
    Lifecycle.UNBOOTSTRAPPED: {Lifecycle.READY_UNLEASED, Lifecycle.SHUTDOWN},
    Lifecycle.READY_UNLEASED: {Lifecycle.LEASED, Lifecycle.SHUTDOWN},
    Lifecycle.LEASED: {Lifecycle.CLOSING, Lifecycle.SHUTDOWN},
    Lifecycle.CLOSING: {Lifecycle.SHUTDOWN},
    Lifecycle.SHUTDOWN: set(),
}


class KernelState:
    def __init__(self) -> None:
        self.lifecycle = Lifecycle.UNBOOTSTRAPPED
        self.consumed_request_ids: set[str] = set()
        self.lock = threading.RLock()

    def transition(self, target: Lifecycle) -> None:
        if target not in _LEGAL[self.lifecycle]:
            raise RuntimeError("illegal lifecycle transition")
        self.lifecycle = target
