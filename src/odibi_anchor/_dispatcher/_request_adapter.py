"""Versioned, transport-neutral request validation for remote-style callers."""
from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = 1
MAX_REQUEST_BYTES = 1_048_576
MAX_NESTING_DEPTH = 32
MAX_INTEGER_DIGITS = 1_000
_ARG_KEY = re.compile(r"arg(0|[1-9]\d*)\Z")
_ARG_LIKE_KEY = re.compile(r"arg\d+\Z")
_SECRET_ASSIGNMENT = re.compile(
    r"(?is)(?P<prefix>(?:['\"]?(?:password|passwd|token|secret|api[_-]?key|authorization)['\"]?)\s*[=:]\s*)"
    r"(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;}]+)"
)
_AUTHORIZATION = re.compile(
    r"(?i)(?P<prefix>['\"]?authorization['\"]?\s*[:=]\s*)"
    r"(?P<quote>['\"]?)(?:bearer|basic)\s+[^\s,;}\]'\"]+(?P=quote)"
)


class RequestError(ValueError):
    """A request cannot be safely normalized."""


class BootstrapError(RuntimeError):
    """CLI bootstrap failed before a dispatcher became available."""


@dataclass(frozen=True)
class NormalizedRequest:
    """Validated arguments ready for the sole core dispatcher."""

    schema_version: int
    action: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


def _depth(value: Any, level: int = 0) -> int:
    if level > MAX_NESTING_DEPTH:
        raise RequestError(f"request nesting exceeds {MAX_NESTING_DEPTH}")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise RequestError("object keys must be strings")
            _depth(item, level + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _depth(item, level + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        raise RequestError("request numbers must be finite JSON numbers")
    elif type(value) is int and value.bit_length() > 3_322:
        raise RequestError(f"JSON integer exceeds {MAX_INTEGER_DIGITS} digits")
    elif value is not None and type(value) not in (str, int, float, bool):
        raise RequestError("request must contain only JSON values")
    return level


def _reject_constant(value: str) -> None:
    raise RequestError(f"invalid JSON constant: {value}")


def _parse_int(value: str) -> int:
    digits = value.lstrip("-")
    if len(digits) > MAX_INTEGER_DIGITS:
        raise RequestError(f"JSON integer exceeds {MAX_INTEGER_DIGITS} digits")
    return int(value)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RequestError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def decode_json_document(raw: str | bytes | bytearray, *, array: bool = False) -> Any:
    """Decode one bounded strict JSON document for transport callers."""
    if type(raw) not in (str, bytes, bytearray):
        raise RequestError("request must be a string, bytes, bytearray, or mapping")
    encoded = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    if len(encoded) > MAX_REQUEST_BYTES:
        raise RequestError(f"request exceeds {MAX_REQUEST_BYTES} UTF-8 bytes")
    try:
        value = json.loads(
            encoded.decode("utf-8"), object_pairs_hook=_object,
            parse_constant=_reject_constant, parse_int=_parse_int,
        )
    except RequestError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise RequestError("request is not valid UTF-8 JSON") from exc
    _depth(value)
    expected = list if array else dict
    if not isinstance(value, expected):
        raise RequestError("batch JSON must be an array" if array else "request must be a JSON object")
    return value


def _load(request: str | bytes | bytearray | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(request, Mapping):
        value = dict(request)
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise RequestError("request must contain only JSON values") from exc
    else:
        return decode_json_document(request)
    if len(encoded) > MAX_REQUEST_BYTES:
        raise RequestError(f"request exceeds {MAX_REQUEST_BYTES} UTF-8 bytes")
    if not isinstance(value, dict):
        raise RequestError("request must be a JSON object")
    _depth(value)
    return value


def normalize_request(
    request: str | bytes | bytearray | Mapping[str, Any],
    *,
    resolver: Callable[[Any, str], Any] | None = None,
) -> NormalizedRequest:
    """Validate an envelope and optionally resolve values without transport coupling."""
    value = _load(request)
    version = value.pop("schema_version", SCHEMA_VERSION)
    if type(version) is not int or version != SCHEMA_VERSION:
        raise RequestError(f"unsupported schema_version: {version!r}")
    action = value.pop("action", None)
    if not isinstance(action, str) or not action.strip():
        raise RequestError("action must be a non-empty string")
    has_args = "args" in value
    has_kwargs = "kwargs" in value
    explicit_args = value.pop("args", None)
    kwargs = value.pop("kwargs", None)
    if has_args and not isinstance(explicit_args, list):
        raise RequestError("args must be a JSON array")
    if has_kwargs and not isinstance(kwargs, dict):
        raise RequestError("kwargs must be a JSON object")
    kwargs = dict(kwargs or {})

    legacy: dict[str, Any] = {}
    for key in list(value):
        match = _ARG_KEY.fullmatch(key)
        if match:
            legacy[key] = value.pop(key)
        elif _ARG_LIKE_KEY.fullmatch(key):
            raise RequestError(f"noncanonical positional key: {key}")
    if has_args and legacy:
        raise RequestError("duplicate positional representations: args and argN")
    expected_legacy = [f"arg{index}" for index in range(len(legacy))]
    if legacy and set(legacy) != set(expected_legacy):
        raise RequestError("legacy positional keys must be contiguous from arg0")
    for key, item in value.items():
        if key in kwargs:
            raise RequestError(f"duplicate keyword representation: {key}")
        kwargs[key] = item
    args = list(explicit_args or [legacy[key] for key in expected_legacy])
    if resolver is not None:
        args = [resolver(item, f"args[{index}]") for index, item in enumerate(args)]
        kwargs = {key: resolver(item, f"kwargs.{key}") for key, item in kwargs.items()}
    return NormalizedRequest(version, action.strip(), tuple(args), kwargs)


def redact_message(message: str) -> str:
    """Redact common credential assignments without exposing exception internals."""
    message = _AUTHORIZATION.sub(lambda match: f"{match.group('prefix')}<redacted>", message)
    return _SECRET_ASSIGNMENT.sub(
        lambda match: re.sub(r"\s*[=:]\s*$", "=", match.group("prefix")) + "<redacted>", message
    )


def error_information(exc: Exception) -> dict[str, str]:
    """Return stable, traceback-free error information safe for transports."""
    message = redact_message(str(exc)).replace("\r\n", "\n").replace("\r", "\n")
    return {"type": type(exc).__name__, "message": message}


def execute_request(dispatcher: Callable[..., Any], request: NormalizedRequest) -> dict[str, Any]:
    """Execute at the adapter boundary; deliberately allow BaseException through."""
    try:
        return {"ok": True, "result": dispatcher(request.action, *request.args, **request.kwargs)}
    except Exception as exc:
        return {"ok": False, "error": error_information(exc)}
