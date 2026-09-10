"""Bounded, redacted contention handling for the shared memory database."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from pathlib import Path
from typing import Any, TypeVar

TIMEOUT_MS = 5000
STORE = "shared_memory"

_T = TypeVar("_T")
_BUSY_CODES = frozenset({sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})
_BUSY_MESSAGES = ("database is locked", "database table is locked")


class PersistenceContentionError(RuntimeError):
    """A shared SQLite write could not acquire its lock within the fixed bound."""

    def __init__(
        self,
        *,
        store: str,
        operation: str,
        owner_key_kind: str,
        duration_ms: int,
        timeout_ms: int,
    ) -> None:
        self.store = store
        self.operation = operation
        self.owner_key_kind = owner_key_kind
        self.duration_ms = duration_ms
        self.timeout_ms = timeout_ms
        self.retryable = True
        super().__init__(
            "SQLite contention: "
            f"store={store} operation={operation} owner_key={owner_key_kind} "
            f"duration_ms={duration_ms} timeout_ms={timeout_ms} retryable=true"
        )


def _is_contention(error: sqlite3.OperationalError) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(code, int):
        return code & 0xFF in _BUSY_CODES
    message = str(error).lower()
    return any(
        message == prefix or message.startswith(f"{prefix}:")
        for prefix in _BUSY_MESSAGES
    )


def _operation(sql: str) -> str:
    statement = sql.lstrip().upper()
    if statement.startswith("PRAGMA JOURNAL_MODE"):
        return "configure_journal"
    if statement.startswith("BEGIN"):
        return "acquire_write"
    if statement.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")):
        return "mutate"
    if statement.startswith(("CREATE", "ALTER", "DROP")):
        return "migrate_schema"
    return "database_operation"


class _ContentionConnection(sqlite3.Connection):
    _cw_store = STORE
    _cw_owner_key_kind = "owner"
    _cw_timeout_ms = TIMEOUT_MS

    def _mapped(self, operation: str, call: Callable[..., _T], *args: Any) -> _T:
        started = time.monotonic()
        try:
            return call(*args)
        except sqlite3.OperationalError as error:
            if not _is_contention(error):
                raise
            with suppress(sqlite3.Error):
                super().rollback()
            duration_ms = round((time.monotonic() - started) * 1000)
            raise PersistenceContentionError(
                store=self._cw_store,
                operation=operation,
                owner_key_kind=self._cw_owner_key_kind,
                duration_ms=duration_ms,
                timeout_ms=self._cw_timeout_ms,
            ) from error

    def execute(self, sql: str, parameters: Iterable[Any] = (), /) -> sqlite3.Cursor:
        return self._mapped(_operation(sql), super().execute, sql, parameters)

    def executemany(
        self, sql: str, parameters: Iterable[Iterable[Any]], /,
    ) -> sqlite3.Cursor:
        return self._mapped(_operation(sql), super().executemany, sql, parameters)

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:
        return self._mapped("migrate_schema", super().executescript, sql_script)

    def commit(self) -> None:
        self._mapped("commit_write", super().commit)

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        return self._mapped(
            "commit_write", super().__exit__, exc_type, exc_value, traceback,
        )


def connect_shared_memory(
    database: str | Path,
    *,
    owner_key_kind: str,
    uri: bool = False,
    isolation_level: str | None = "",
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    """Open one shared-memory connection with an explicit five-second busy bound."""
    connection = sqlite3.connect(
        database,
        uri=uri,
        isolation_level=isolation_level,
        timeout=TIMEOUT_MS / 1000,
        check_same_thread=check_same_thread,
        factory=_ContentionConnection,
    )
    assert isinstance(connection, _ContentionConnection)
    connection._cw_owner_key_kind = owner_key_kind
    connection.execute(f"PRAGMA busy_timeout={TIMEOUT_MS}")
    return connection
