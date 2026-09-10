"""Owned binary JSONL framing for the sidecar protocol."""

from __future__ import annotations

import json
from typing import Any, BinaryIO

from odibi_anchor._dispatcher._request_adapter import RequestError, decode_json_document

MAX_JSON_BYTES = 1_048_576


class FramingError(ValueError):
    """Terminal loss of the JSONL transport boundary."""


def read_frame(stream: BinaryIO) -> dict[str, Any] | None:
    """Read one strict LF-terminated JSON object, or clean EOF."""
    raw = stream.readline(MAX_JSON_BYTES + 2)
    if raw == b"":
        return None
    if not raw.endswith(b"\n"):
        raise FramingError("partial or oversized frame")
    document = raw[:-1]
    if not document or document.endswith(b"\r") or len(document) > MAX_JSON_BYTES:
        raise FramingError("invalid frame boundary")
    try:
        value = decode_json_document(document)
    except RequestError as exc:
        raise FramingError("invalid JSON frame") from exc
    return value


def write_frame(stream: BinaryIO, value: object) -> None:
    """Write one compact strict bounded JSON object and flush it."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise FramingError("response is not strict JSON") from exc
    if len(encoded) > MAX_JSON_BYTES:
        raise FramingError("response exceeds frame limit")
    remaining = memoryview(encoded + b"\n")
    while remaining:
        written = stream.write(remaining)
        if not isinstance(written, int) or written <= 0:
            raise FramingError("response write did not complete")
        remaining = remaining[written:]
    stream.flush()
