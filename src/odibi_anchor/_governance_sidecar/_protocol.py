"""Validation and response construction for protocol-v1 common envelopes."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from . import PROTOCOL_VERSION

ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
ENVELOPE_KEYS = frozenset({"protocol_version", "request_id", "operation", "thread_id", "payload"})
OPERATIONS = frozenset(
    {
        "bootstrap",
        "execute",
        "prepare_owner_confirmation",
        "record_owner_approval",
        "authorize_tool_call",
        "reconcile_tool_result",
        "close_task",
        "shutdown",
    }
)


@dataclass(frozen=True)
class Envelope:
    protocol_version: int
    request_id: str
    operation: str
    thread_id: str
    payload: dict[str, Any]


class EnvelopeError(ValueError):
    def __init__(self, code: str, request_id: str | None = None, operation: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.request_id = request_id
        self.operation = operation

    @property
    def correlatable(self) -> bool:
        return self.request_id is not None and self.operation is not None


def _id(value: object) -> str | None:
    return value if isinstance(value, str) and ID_PATTERN.fullmatch(value) else None


def _opaque_id(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = unicodedata.normalize("NFC", value)
    try:
        encoded = normalized.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(encoded) > 512 or any(
        ord(char) < 32 or 127 <= ord(char) <= 159 for char in normalized
    ):
        return None
    return normalized


def validate_envelope(value: dict[str, Any]) -> Envelope:
    """Validate exactly the common envelope while preserving safe correlation."""
    request_id = _id(value.get("request_id"))
    operation = _id(value.get("operation"))
    if set(value) != ENVELOPE_KEYS:
        raise EnvelopeError("invalid_request", request_id, operation)
    version = value["protocol_version"]
    if type(version) is not int or version != PROTOCOL_VERSION:
        code = "unsupported_version" if type(version) is int else "invalid_request"
        raise EnvelopeError(code, request_id, operation)
    thread_id = _opaque_id(value["thread_id"])
    if request_id is None or operation is None or thread_id is None:
        raise EnvelopeError("invalid_request", request_id, operation)
    if not isinstance(value["payload"], dict):
        raise EnvelopeError("invalid_request", request_id, operation)
    return Envelope(version, request_id, operation, thread_id, value["payload"])


_MESSAGES = {
    "invalid_request": "The request is invalid.",
    "unsupported_version": "The protocol version is unsupported.",
    "duplicate_request": "The request ID was already consumed.",
    "readiness_unavailable": "External governance readiness is unavailable.",
    "not_bootstrapped": "The sidecar is not bootstrapped.",
    "internal_error": "The sidecar could not process the request.",
}


def response(envelope: Envelope, *, result: dict[str, Any] | None = None, error: str | None = None) -> dict[str, Any]:
    common: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": envelope.request_id,
        "operation": envelope.operation,
        "ok": error is None,
    }
    if error is None:
        common["result"] = result if result is not None else {}
    else:
        common["error"] = {"code": error, "message": _MESSAGES.get(error, _MESSAGES["internal_error"])}
    return common
