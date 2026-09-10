"""Fail-closed foundational sidecar kernel."""

from __future__ import annotations

from typing import Any

from ._protocol import OPERATIONS, Envelope, EnvelopeError, response, validate_envelope
from ._readiness_protocol import ReadinessProtocolError, validate_bootstrap_payload
from ._state import KernelState, Lifecycle


class Kernel:
    def __init__(self) -> None:
        self._state = KernelState()

    @property
    def shutdown(self) -> bool:
        return self._state.lifecycle is Lifecycle.SHUTDOWN

    def local_shutdown(self) -> None:
        """Absorb process loss without producing a clean-closure receipt."""
        with self._state.lock:
            if not self.shutdown:
                self._state.transition(Lifecycle.SHUTDOWN)

    def handle(self, value: dict[str, Any]) -> dict[str, Any]:
        envelope = validate_envelope(value)
        with self._state.lock:
            if envelope.request_id in self._state.consumed_request_ids:
                return response(envelope, error="duplicate_request")
            self._state.consumed_request_ids.add(envelope.request_id)
            try:
                return self._dispatch_locked(envelope)
            except Exception:
                return response(envelope, error="internal_error")

    def _dispatch_locked(self, envelope: Envelope) -> dict[str, Any]:
        """Dispatch one consumed request while the lifecycle lock is held."""
        if envelope.operation not in OPERATIONS:
            return response(envelope, error="invalid_request")
        if self.shutdown:
            return response(envelope, error="not_bootstrapped")
        if envelope.operation == "shutdown":
            if envelope.payload:
                return response(envelope, error="invalid_request")
            self._state.transition(Lifecycle.SHUTDOWN)
            return response(
                envelope,
                result={"revoked_pre_allow_authorities": 0, "unresolved_allowed_calls": 0, "closed": True},
            )
        if envelope.operation == "bootstrap":
            try:
                validate_bootstrap_payload(envelope.payload)
            except ReadinessProtocolError:
                return response(envelope, error="invalid_request")
            return response(envelope, error="readiness_unavailable")
        return response(envelope, error="not_bootstrapped")


def correlated_error(exc: EnvelopeError) -> dict[str, Any] | None:
    if not exc.correlatable:
        return None
    envelope = Envelope(1, exc.request_id or "", exc.operation or "", "", {})
    return response(envelope, error=exc.code)
