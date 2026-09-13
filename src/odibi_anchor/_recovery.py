"""Transport-neutral recovery metadata for agent-facing operations and errors."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, TypeVar

_ExceptionT = TypeVar("_ExceptionT", bound=Exception)


def dispatcher_operation(
    action: str,
    *args: Any,
    kwargs: Mapping[str, Any] | None = None,
    reason: str,
    requires_owner: bool = False,
    retry_safety: str = "idempotent",
) -> dict[str, Any]:
    """Describe one exact, state-valid dispatcher call without executing it."""
    operation_kwargs = dict(kwargs or {})
    rendered = [repr(action), *(repr(value) for value in args)]
    rendered.extend(f"{key}={value!r}" for key, value in operation_kwargs.items())
    return {
        "action": action,
        "args": list(args),
        "kwargs": operation_kwargs,
        "copy_ready": f"anchor({', '.join(rendered)})",
        "reason": reason,
        "requires_owner": requires_owner,
        "retry_safety": retry_safety,
    }


def attach_recovery(
    exc: _ExceptionT,
    *,
    error_code: str,
    context: Mapping[str, Any],
    next_operations: Sequence[Mapping[str, Any]] = (),
) -> _ExceptionT:
    """Attach additive recovery fields while preserving the exception class and message."""
    operations = [dict(operation) for operation in next_operations]
    exc.error_code = error_code  # type: ignore[attr-defined]
    exc.context = dict(context)  # type: ignore[attr-defined]
    exc.next_operations = operations  # type: ignore[attr-defined]
    if operations:
        first = operations[0]
        exc.next_operation = first  # type: ignore[attr-defined]
        for field in ("copy_ready", "requires_owner", "retry_safety"):
            if field in first:
                setattr(exc, field, first[field])
    return exc
