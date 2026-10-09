"""Outcome-neutral bootstrap phase timing and bounded remote-call policy.

Timing is recorded only while :func:`recording` is active (managed bootstrap); every
other caller pays one context-variable lookup. Recording never swallows, replaces or
retries an exception: phases mark their outcome and re-raise the original error.
"""
from __future__ import annotations

import contextlib
import functools
import importlib
import math
import os
import sys
import threading
import time
import types
from collections.abc import Callable, Iterator, Mapping
from contextvars import ContextVar
from copy import deepcopy
from typing import Any, TypeVar

TIMINGS_SCHEMA = "odibi-anchor-bootstrap-timings-v1"
PHASE_TIMEOUT_ERROR_CODE = "bootstrap_phase_timeout"
WORKSPACE_TIMEOUT_ENV = "ANCHOR_WORKSPACE_API_TIMEOUT_SECONDS"
DEFAULT_WORKSPACE_TIMEOUT_SECONDS = 30.0
SLOWEST_FILE_LIMIT = 5
_MAX_TIMEOUT_SECONDS = 600.0
_LAYERS = frozenset({"workspace_api", "files_api", "pypi", "filesystem"})
_STATE_MODULE = "_odibi_anchor_bootstrap_phase_state"

_T = TypeVar("_T")


class BootstrapPhaseTimeout(TimeoutError, RuntimeError):
    """A bounded bootstrap remote call exceeded its deadline; nothing was assumed."""


def elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


class PhaseRecorder:
    """Collect one bootstrap's nested phase timings on the calling thread."""

    def __init__(self) -> None:
        self._started = time.perf_counter()
        self._phases: list[dict[str, Any]] = []
        self._stack: list[dict[str, Any]] = []

    def _append(self, entry: dict[str, Any]) -> None:
        if self._stack:
            self._stack[-1].setdefault("sub_phases", []).append(entry)
        else:
            self._phases.append(entry)

    @contextlib.contextmanager
    def phase(self, name: str, **details: Any) -> Iterator[dict[str, Any]]:
        entry: dict[str, Any] = {"phase": name, "elapsed_ms": 0.0, "outcome": "ok", **details}
        self._append(entry)
        self._stack.append(entry)
        started = time.perf_counter()
        try:
            yield entry
        except BaseException as exc:
            # "raised", not "error": a caller may handle it (for example restore
            # reporting "no durable snapshot exists"); the packet carries the result.
            entry["outcome"] = (
                "timeout" if getattr(exc, "error_code", None) == PHASE_TIMEOUT_ERROR_CODE else "raised"
            )
            entry["error_type"] = type(exc).__name__
            raise
        finally:
            entry["elapsed_ms"] = elapsed_ms(started)
            self._stack.pop()

    def record(self, name: str, *, outcome: str, **details: Any) -> None:
        self._append({"phase": name, "elapsed_ms": 0.0, "outcome": outcome, **details})

    def summary(self) -> dict[str, Any]:
        total = elapsed_ms(self._started)
        attributed = sum(float(entry["elapsed_ms"]) for entry in self._phases)
        return {
            "schema": TIMINGS_SCHEMA,
            "clock": "time.perf_counter",
            "unit": "ms",
            "total_elapsed_ms": total,
            "unattributed_ms": round(max(0.0, total - attributed), 3),
            "phases": deepcopy(self._phases),
        }


def _process_state() -> Any:
    """Return state shared by every copy of this module in the process.

    ``odibi_anchor.bootstrap.init`` purges ``odibi_anchor*`` from ``sys.modules``; a
    module imported after that purge would otherwise get a new ContextVar and client
    cache and silently stop recording into the active bootstrap.
    """
    state: Any = sys.modules.get(_STATE_MODULE)
    if state is None:
        state = types.ModuleType(_STATE_MODULE)
        state.active = ContextVar("odibi_anchor_bootstrap_phase_recorder", default=None)
        state.client_lock = threading.Lock()
        state.client_cache = []
        state = sys.modules.setdefault(_STATE_MODULE, state)
    return state


_STATE = _process_state()
_ACTIVE: ContextVar[PhaseRecorder | None] = _STATE.active


@contextlib.contextmanager
def recording() -> Iterator[PhaseRecorder]:
    recorder = PhaseRecorder()
    token = _ACTIVE.set(recorder)
    try:
        yield recorder
    finally:
        _ACTIVE.reset(token)


@contextlib.contextmanager
def phase(name: str, **details: Any) -> Iterator[dict[str, Any]]:
    """Time one phase under the active recorder; yield a detached dict otherwise."""
    recorder = _ACTIVE.get()
    if recorder is None:
        yield {}
        return
    with recorder.phase(name, **details) as entry:
        yield entry


def record_phase(name: str, *, outcome: str, **details: Any) -> None:
    """Record a phase that did not run (for example ``not_required``)."""
    recorder = _ACTIVE.get()
    if recorder is not None:
        recorder.record(name, outcome=outcome, **details)


def timed_call(name: str, function: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
    with phase(name):
        return function(*args, **kwargs)


def records_bootstrap_timings(function: Callable[..., _T]) -> Callable[..., _T]:
    """Attach phase timings to the returned startup packet or to the raised exception."""

    @functools.wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> _T:
        with recording() as recorder:
            try:
                result = function(*args, **kwargs)
            except BaseException as exc:
                with contextlib.suppress(Exception):
                    if getattr(exc, "bootstrap_timings", None) is None:
                        exc.bootstrap_timings = recorder.summary()  # type: ignore[attr-defined]
                raise
        packet = result.get("startup_packet") if isinstance(result, dict) else None
        if isinstance(packet, dict):
            packet["timings"] = recorder.summary()
        return result

    return wrapper


def slowest(observations: list[dict[str, Any]], limit: int = SLOWEST_FILE_LIMIT) -> list[dict[str, Any]]:
    return sorted(observations, key=lambda item: item["elapsed_ms"], reverse=True)[:limit]


def workspace_timeout_policy(environment: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Resolve the explicit Workspace API deadline from one environment variable."""
    source = os.environ if environment is None else environment
    raw = source.get(WORKSPACE_TIMEOUT_ENV)
    seconds = DEFAULT_WORKSPACE_TIMEOUT_SECONDS
    if raw is not None:
        try:
            seconds = float(raw)
        except ValueError:
            seconds = math.nan
        if not math.isfinite(seconds) or not 0 < seconds <= _MAX_TIMEOUT_SECONDS:
            raise ValueError(
                f"{WORKSPACE_TIMEOUT_ENV} must be a number of seconds greater than 0 and at most "
                f"{_MAX_TIMEOUT_SECONDS:g}; got {raw!r}"
            )
    return {
        "setting": WORKSPACE_TIMEOUT_ENV,
        "source": "environment" if raw is not None else "default",
        "http_timeout_seconds": seconds,
        "retry_timeout_seconds": 2 * seconds,
        # One request may start just before the retry budget ends.
        "item_deadline_seconds": 3 * seconds,
    }


_CLIENT_LOCK: threading.Lock = _STATE.client_lock
_CLIENT_CACHE: list[tuple[tuple[Any, int, int], Any]] = _STATE.client_cache


def databricks_workspace_client(policy: Mapping[str, Any] | None = None) -> Any:
    """Return one process-reused WorkspaceClient with explicit HTTP and retry timeouts."""
    resolved = workspace_timeout_policy() if policy is None else policy
    sdk = importlib.import_module("databricks.sdk")
    factory = sdk.WorkspaceClient
    http_timeout = max(1, math.ceil(resolved["http_timeout_seconds"]))
    retry_timeout = max(1, math.ceil(resolved["retry_timeout_seconds"]))
    key = (factory, http_timeout, retry_timeout)
    with _CLIENT_LOCK:
        if _CLIENT_CACHE and _CLIENT_CACHE[0][0] == key:
            return _CLIENT_CACHE[0][1]
        config = importlib.import_module("databricks.sdk.config").Config(
            http_timeout_seconds=http_timeout, retry_timeout_seconds=retry_timeout,
        )
        client = factory(config=config)
        _CLIENT_CACHE[:] = [(key, client)]
        return client


def is_timeout(exc: BaseException) -> bool:
    """Classify transport, SDK retry-budget and gateway deadline failures as timeouts."""
    return any("Timeout" in cls.__name__ for cls in type(exc).__mro__) or str(
        getattr(exc, "error_code", "")
    ).upper() == "DEADLINE_EXCEEDED"


def phase_timeout(
    *,
    phase_name: str,
    layer: str,
    elapsed: float,
    limit_seconds: float,
    item: str | None = None,
    sub_phase: str | None = None,
    setting: str | None = None,
) -> BootstrapPhaseTimeout:
    """Build the structured ``bootstrap_phase_timeout`` error with a copy-ready retry."""
    from odibi_anchor._recovery import attach_recovery

    if layer not in _LAYERS:
        raise ValueError(f"unsupported bootstrap timeout layer: {layer}")
    location = f" while accessing {item}" if item else ""
    message = (
        f"bootstrap phase {phase_name}{'/' + sub_phase if sub_phase else ''} timed out after "
        f"{elapsed:.0f} ms at the {layer} layer{location} (limit {limit_seconds:g} s). "
        "Classification: a bounded remote call exceeded its deadline; no integrity check was "
        "skipped and no result was assumed."
    )
    operations: list[dict[str, Any]] = []
    if setting is not None:
        larger = f"{min(_MAX_TIMEOUT_SECONDS, max(2 * limit_seconds, 1)):g}"
        operations.append({
            "operation": "rerun_launcher_with_larger_timeout",
            "environment": {setting: larger},
            "copy_ready": f'os.environ["{setting}"] = "{larger}"  # then rerun the launcher',
            "reason": "Retry the idempotent read with a larger explicit deadline.",
            "requires_owner": False,
            "retry_safety": "idempotent",
        })
    return attach_recovery(
        BootstrapPhaseTimeout(message),
        error_code=PHASE_TIMEOUT_ERROR_CODE,
        context={
            "phase": phase_name,
            "sub_phase": sub_phase,
            "layer": layer,
            "item": item,
            "elapsed_ms": round(elapsed, 3),
            "limit_seconds": limit_seconds,
            "setting": setting,
        },
        next_operations=operations,
    )


__all__ = [
    "PHASE_TIMEOUT_ERROR_CODE",
    "BootstrapPhaseTimeout",
    "PhaseRecorder",
    "databricks_workspace_client",
    "is_timeout",
    "phase",
    "phase_timeout",
    "record_phase",
    "recording",
    "records_bootstrap_timings",
    "slowest",
    "timed_call",
    "workspace_timeout_policy",
]
