"""Small, peer-neutral security-bound RFC 8785 canonicalization helpers."""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

JS_SAFE_INTEGER = 9_007_199_254_740_991
MAX_DEPTH = 32
MAX_ITEMS = 10_000
MAX_STRING_BYTES = 1_048_576


class CanonicalError(ValueError):
    pass


def safe_integer(value: object, *, minimum: int = 0, maximum: int = JS_SAFE_INTEGER) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise CanonicalError("invalid integer")
    return value


def _string(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    try:
        raw = normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalError("invalid string encoding") from exc
    if len(raw) > MAX_STRING_BYTES or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in normalized):
        raise CanonicalError("invalid string")
    return normalized


def normalize(value: object, *, _depth: int = 0, _budget: list[int] | None = None) -> Any:
    """Return a detached NFC-normalized security object suitable for JCS."""
    if _depth > MAX_DEPTH:
        raise CanonicalError("object is too deeply nested")
    budget = [MAX_ITEMS] if _budget is None else _budget
    budget[0] -= 1
    if budget[0] < 0:
        raise CanonicalError("object is too large")
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        return safe_integer(value, minimum=-JS_SAFE_INTEGER)
    if isinstance(value, float):
        raise CanonicalError("floats are not permitted in security objects")
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalError("object keys must be strings")
            normalized_key = _string(key)
            if normalized_key in result:
                raise CanonicalError("NFC key collision")
            result[normalized_key] = normalize(item, _depth=_depth + 1, _budget=budget)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [normalize(item, _depth=_depth + 1, _budget=budget) for item in value]
    raise CanonicalError("unsupported canonical value")


def dumps(value: object) -> bytes:
    try:
        import rfc8785
    except ImportError as exc:
        raise CanonicalError("RFC 8785 canonicalization support is unavailable") from exc
    try:
        return rfc8785.dumps(normalize(value))
    except (rfc8785.CanonicalizationError, UnicodeError) as exc:
        raise CanonicalError("JCS canonicalization failed") from exc


def domain_digest(domain: str, value: object) -> str:
    if not domain.startswith("anchor-amp/") or not domain.endswith("/v1"):
        raise CanonicalError("invalid digest domain")
    return hashlib.sha256(domain.encode("ascii") + b"\0" + dumps(value)).hexdigest()


def domain_preimage(domain: str, value: object) -> bytes:
    if not domain.startswith("anchor-amp/") or not domain.endswith("/v1"):
        raise CanonicalError("invalid signature domain")
    return domain.encode("ascii") + b"\0" + dumps(value)
