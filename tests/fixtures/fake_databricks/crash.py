"""Deterministic crash injection at filesystem and remote mutation points.

``CrashInjector`` wraps the mutation primitives product code reaches at call time
(``os.replace``, ``os.rename``, ``os.link``, ``os.symlink``, ``os.mkdir``, write-mode
``os.open``/``open``/``io.open``/``tarfile.bltn_open``, ``shutil.copytree`` and
``shutil.move``) plus remote mutations reported by the fake Databricks APIs. It
numbers every in-scope mutation from 1 and, when ``crash_at=N``, raises before the
Nth mutation is applied.

The default crash is :class:`InjectedCrash`, a ``BaseException``: ``except
Exception`` recovery handlers do not run, while ``finally`` blocks and context
managers still do. That models abrupt termination for persistent state; it cannot
model power loss of unsynchronized bytes. Pass ``error=`` to inject an ordinary
exception and exercise recovery handlers instead.

Limitations: SQLite page writes, deletions (``unlink``/``rmdir``/``rmtree``), and
names bound with ``from module import function`` before installation are not
mutation points.
"""
from __future__ import annotations

import builtins
import contextlib
import io
import os
import shutil
import tarfile
import threading
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any, TypeVar

_T = TypeVar("_T")
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_local = threading.local()
_active: list[CrashInjector] = []


@dataclass(frozen=True)
class MutationPoint:
    index: int
    kind: str  # "filesystem" | "remote"
    operation: str
    path: str


class InjectedCrash(BaseException):
    """Abrupt termination injected immediately before one mutation point."""

    def __init__(self, point: MutationPoint) -> None:
        super().__init__(f"injected crash before mutation {point.index}: {point.operation} {point.path}")
        self.point = point


@contextlib.contextmanager
def suppressed() -> Iterator[None]:
    """Hide a fake's own backing-store I/O from mutation accounting."""
    previous = getattr(_local, "suppressed", False)
    _local.suppressed = True
    try:
        yield
    finally:
        _local.suppressed = previous


def record_remote_mutation(operation: str, path: str) -> None:
    """Report one remote mutation from a fake API to every active injector."""
    for injector in list(_active):
        injector._record("remote", operation, path)


class CrashInjector:
    """Count mutation points and optionally crash before one of them.

    ``scope`` limits filesystem accounting to paths inside the given roots; remote
    mutations are always counted. Crash before the ``crash_at``-th point (1-based)
    or before the first point for which ``crash_if(point)`` is true. ``hook(point)``
    runs before every counted point and can model a competing writer. Use as a
    context manager; installation is process-global for its duration.
    """

    def __init__(
        self,
        *,
        scope: Iterable[str | os.PathLike[str]],
        crash_at: int | None = None,
        crash_if: Callable[[MutationPoint], bool] | None = None,
        error: Callable[[MutationPoint], BaseException] | None = None,
        hook: Callable[[MutationPoint], None] | None = None,
    ) -> None:
        self.scope = tuple(os.path.realpath(os.fspath(root)) for root in scope)
        if not self.scope:
            raise ValueError("scope must name at least one root")
        if crash_at is not None and crash_at < 1:
            raise ValueError("crash_at is 1-based")
        if crash_at is not None and crash_if is not None:
            raise ValueError("use crash_at or crash_if, not both")
        self.crash_at = crash_at
        self.crash_if = crash_if
        self.error = error or InjectedCrash
        self.hook = hook
        self.points: list[MutationPoint] = []
        self.crashed: MutationPoint | None = None
        self._originals: dict[tuple[Any, str], Any] = {}

    # ── accounting ───────────────────────────────────────────────────────────
    def _in_scope(self, path: Any) -> bool:
        try:
            text = os.fspath(path)
        except TypeError:
            return False
        if isinstance(text, bytes):
            text = os.fsdecode(text)
        resolved = os.path.realpath(os.path.abspath(text))
        return any(resolved == root or resolved.startswith(root + os.sep) for root in self.scope)

    def _record(self, kind: str, operation: str, path: Any) -> None:
        if getattr(_local, "suppressed", False) or self.crashed is not None:
            return
        if kind == "filesystem" and not self._in_scope(path):
            return
        point = MutationPoint(len(self.points) + 1, kind, operation, os.fsdecode(os.fspath(path)))
        self.points.append(point)
        if point.index == self.crash_at or (self.crash_if is not None and self.crash_if(point)):
            self.crashed = point
            raise self.error(point)
        if self.hook is not None:
            with suppressed():
                self.hook(point)

    # ── installation ─────────────────────────────────────────────────────────
    def _wrap(self, owner: Any, name: str, factory: Callable[[Any], Any]) -> None:
        original = getattr(owner, name)
        self._originals[(owner, name)] = original
        setattr(owner, name, factory(original))

    def __enter__(self) -> CrashInjector:
        record = self._record

        def path_call(operation: str, index: int, keyword: str) -> Callable[[Any], Any]:
            def factory(original: Any) -> Any:
                def wrapper(*args: Any, **kwargs: Any) -> Any:
                    target = args[index] if len(args) > index else kwargs.get(keyword)
                    if target is not None:
                        record("filesystem", operation, target)
                    return original(*args, **kwargs)
                return wrapper
            return factory

        def opener(operation: str) -> Callable[[Any], Any]:
            def factory(original: Any) -> Any:
                def wrapper(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
                    if isinstance(file, (str, bytes, os.PathLike)) and set(mode) & set("wax+"):
                        record("filesystem", f"{operation}:{mode}", file)
                    return original(file, mode, *args, **kwargs)
                return wrapper
            return factory

        def os_open(original: Any) -> Any:
            def wrapper(path: Any, flags: int, *args: Any, **kwargs: Any) -> Any:
                if flags & _WRITE_FLAGS:
                    record("filesystem", "os.open", path)
                return original(path, flags, *args, **kwargs)
            return wrapper

        self._wrap(os, "replace", path_call("os.replace", 1, "dst"))
        self._wrap(os, "rename", path_call("os.rename", 1, "dst"))
        self._wrap(os, "link", path_call("os.link", 1, "dst"))
        self._wrap(os, "symlink", path_call("os.symlink", 1, "dst"))
        self._wrap(os, "mkdir", path_call("os.mkdir", 0, "path"))
        self._wrap(os, "open", os_open)
        self._wrap(shutil, "copytree", path_call("shutil.copytree", 1, "dst"))
        self._wrap(shutil, "move", path_call("shutil.move", 1, "dst"))
        shared_open = opener("open")
        self._wrap(builtins, "open", shared_open)
        if io.open is not builtins.open:
            self._wrap(io, "open", shared_open)
        else:
            self._originals[(io, "open")] = io.open
            io.open = builtins.open
        self._wrap(tarfile, "bltn_open", opener("tarfile.open"))
        _active.append(self)
        return self

    def __exit__(self, *_exc: object) -> None:
        _active.remove(self)
        for (owner, name), original in reversed(self._originals.items()):
            setattr(owner, name, original)
        self._originals.clear()


def record_mutations(
    operation: Callable[[], _T], *, scope: Iterable[str | os.PathLike[str]],
) -> tuple[_T, list[MutationPoint]]:
    """Run ``operation`` once without crashing and return its mutation points."""
    with CrashInjector(scope=scope) as injector:
        result = operation()
    return result, injector.points


def crash_matrix(
    *,
    prepare: Callable[[], _T],
    operation: Callable[[_T], Any],
    verify: Callable[[_T, MutationPoint | None], None],
    scope: Callable[[_T], Iterable[str | os.PathLike[str]]],
) -> list[MutationPoint]:
    """Crash ``operation`` before every mutation point and verify invariants after each.

    ``prepare`` must build fresh, equivalent state on every call. The uncrashed run
    is verified with ``point=None``; each crashed run is verified with the point it
    stopped before. The operation's mutation sequence must be deterministic.
    """
    baseline_state = prepare()
    with CrashInjector(scope=scope(baseline_state)) as baseline:
        operation(baseline_state)
    verify(baseline_state, None)
    expected = [(point.kind, point.operation) for point in baseline.points]
    for point in baseline.points:
        state = prepare()
        with CrashInjector(scope=scope(state), crash_at=point.index) as injector:
            try:
                operation(state)
            except InjectedCrash as crash:
                if crash.point.index != point.index:
                    raise AssertionError(f"crash raised for {crash.point}, expected {point}") from crash
            else:
                raise AssertionError(f"operation finished before mutation {point.index}")
        observed = [(item.kind, item.operation) for item in injector.points]
        if observed != expected[: point.index]:
            raise AssertionError(
                f"non-deterministic mutation sequence before point {point.index}: {observed}"
            )
        verify(state, point)
    return baseline.points
