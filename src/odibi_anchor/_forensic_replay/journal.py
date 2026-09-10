"""Append-only, redacted forensic journal (not an execution replay facility)."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

SCHEMA_VERSION = 1
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_DEPTH = 8
MAX_ITEMS = 512
ZERO_HASH = "0" * 64
EVENT_TYPES = frozenset({"start", "completed", "failed", "evidence", "unavailable"})

_DDL = """
CREATE TABLE forensic_schema(singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL, checksum TEXT NOT NULL) STRICT;
CREATE TABLE forensic_events(
 task_window_id TEXT NOT NULL CHECK(length(task_window_id) BETWEEN 1 AND 256),
 sequence INTEGER NOT NULL CHECK(sequence > 0), event_id TEXT NOT NULL CHECK(length(event_id) BETWEEN 1 AND 256),
 event_type TEXT NOT NULL CHECK(event_type IN ('start','completed','failed','evidence','unavailable')),
 payload_json TEXT NOT NULL, occurred_at TEXT NOT NULL, previous_hash TEXT NOT NULL CHECK(length(previous_hash)=64),
 event_hash TEXT NOT NULL CHECK(length(event_hash)=64),
 PRIMARY KEY(task_window_id,sequence), UNIQUE(task_window_id,event_id)
) STRICT;
CREATE TRIGGER forensic_no_update BEFORE UPDATE ON forensic_events BEGIN SELECT RAISE(ABORT,'forensic journal is append-only'); END;
CREATE TRIGGER forensic_no_delete BEFORE DELETE ON forensic_events BEGIN SELECT RAISE(ABORT,'forensic journal is append-only'); END;
""".strip()
SCHEMA_CHECKSUM = hashlib.sha256(("forensic-journal-v1\0" + _DDL).encode()).hexdigest()
_SENSITIVE_KEY = re.compile(r"(?:password|passwd|secret|token|credential|authorization|api[_-]?key|cookie|private[_-]?key|env(?:ironment)?[_-]?value)", re.I)
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
_CREDENTIAL = re.compile(r"(?i)(?:bearer\s+[A-Za-z0-9._~+/=-]{8,}|(?:password|token|api[_-]?key)\s*[:=]\s*\S+)")
_TOKEN = re.compile(
    r"(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|(?:AKIA|ASIA)[A-Z0-9]{16}|"
    r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}|[sr]k_(?:live|test)_[A-Za-z0-9]{16,}|"
    r"glpat-[A-Za-z0-9_-]{20,}|dapi[a-fA-F0-9]{32,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"
)
_ABS_PATH = re.compile(r"^(?:/|[A-Za-z]:[\\/])")


class ForensicJournalError(RuntimeError):
    """The journal cannot safely perform the requested operation."""


class EventConflict(ForensicJournalError):
    """An event id was reused with different content."""


def canonical_json(value: Any) -> str:
    """Return stable UTF-8 JSON text."""
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _clean(value: Any, *, key: str = "", depth: int = 0, budget: list[int] | None = None) -> Any:
    if budget is None:
        budget = [MAX_ITEMS]
    if depth > MAX_DEPTH:
        raise ForensicJournalError("payload nesting exceeds the safe limit")
    budget[0] -= 1
    if budget[0] < 0:
        raise ForensicJournalError("payload contains too many values")
    if _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ForensicJournalError("non-finite numbers are not JSON evidence")
        return value
    if isinstance(value, str):
        if _PRIVATE_KEY.search(value):
            raise ForensicJournalError("private key material is forbidden")
        if _CREDENTIAL.search(value) or _TOKEN.search(value):
            return "[REDACTED]"
        if _ABS_PATH.match(value):
            return "[REDACTED_PATH]"
        parts = urlsplit(value)
        if parts.scheme and (parts.query or parts.username or parts.password):
            host = parts.hostname or ""
            if parts.port:
                host = f"{host}:{parts.port}"
            return urlunsplit((parts.scheme, host, parts.path, "", "")) + "[REDACTED_URL]"
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key in sorted(value, key=lambda item: str(item)):
            if not isinstance(raw_key, str) or not raw_key or len(raw_key) > 128:
                raise ForensicJournalError("payload keys must be short non-empty strings")
            result[raw_key] = _clean(value[raw_key], key=raw_key, depth=depth + 1, budget=budget)
        return result
    if isinstance(value, (list, tuple)):
        return [_clean(item, depth=depth + 1, budget=budget) for item in value]
    raise ForensicJournalError(f"unsupported payload value: {type(value).__name__}")


def redact_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize and redact a payload, rejecting unsafe structures."""
    if not isinstance(payload, Mapping):
        raise ForensicJournalError("payload must be an object")
    cleaned = _clean(payload)
    assert isinstance(cleaned, dict)
    if len(canonical_json(cleaned).encode()) > MAX_PAYLOAD_BYTES:
        raise ForensicJournalError("payload exceeds the safe size limit")
    return cleaned


def _hash(task: str, sequence: int, event_id: str, kind: str, payload: str, occurred: str, previous: str) -> str:
    body = canonical_json({"event_id": event_id, "event_type": kind, "occurred_at": occurred, "payload": json.loads(payload), "previous_hash": previous, "sequence": sequence, "task_window_id": task})
    return hashlib.sha256(body.encode()).hexdigest()


class ForensicJournal:
    """A SQLite journal using a fresh connection for every public operation."""

    def __init__(self, path: str | Path, *, create: bool = True):
        self.path = Path(path)
        if create:
            self._initialize()

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        target = f"file:{self.path}?mode=ro" if read_only else str(self.path)
        connection = sqlite3.connect(
            target, timeout=10, isolation_level=None, uri=read_only,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='forensic_schema'").fetchone()
            if tables is None:
                connection.executescript(_DDL)
                connection.execute("INSERT INTO forensic_schema VALUES(1,?,?)", (SCHEMA_VERSION, SCHEMA_CHECKSUM))
            self._check_schema(connection)
            connection.commit()

    @staticmethod
    def _check_schema(connection: sqlite3.Connection) -> None:
        try:
            row = connection.execute("SELECT version,checksum FROM forensic_schema WHERE singleton=1").fetchone()
        except sqlite3.Error as exc:
            raise ForensicJournalError("forensic schema is missing or corrupt") from exc
        if tuple(row or ()) != (SCHEMA_VERSION, SCHEMA_CHECKSUM):
            raise ForensicJournalError("unsupported or modified forensic schema")
        expected = sqlite3.connect(":memory:")
        try:
            expected.executescript(_DDL)
            expected_objects = expected.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE name LIKE 'forensic_%' ORDER BY type,name"
            ).fetchall()
        finally:
            expected.close()
        actual_objects = connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name LIKE 'forensic_%' ORDER BY type,name"
        ).fetchall()
        if [tuple(item) for item in actual_objects] != expected_objects:
            raise ForensicJournalError("forensic schema checksum mismatch")

    def append(self, task_window_id: str, event_type: str, payload: Mapping[str, Any], *, event_id: str, occurred_at: str | None = None) -> dict[str, Any]:
        """Atomically append an event; identical event ids are idempotent."""
        if not isinstance(task_window_id, str) or not task_window_id.strip() or len(task_window_id) > 256:
            raise ForensicJournalError("invalid task_window_id")
        if event_type not in EVENT_TYPES:
            raise ForensicJournalError("unsupported event type")
        if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 256:
            raise ForensicJournalError("invalid event_id")
        payload_json = canonical_json(redact_payload(payload))
        occurred = occurred_at or datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._check_schema(connection)
                existing = connection.execute("SELECT * FROM forensic_events WHERE task_window_id=? AND event_id=?", (task_window_id, event_id)).fetchone()
                if existing:
                    event = self._event(existing)
                    if event["event_type"] != event_type or canonical_json(event["payload"]) != payload_json:
                        raise EventConflict("event_id already identifies different content")
                    connection.commit()
                    return event
                tail = connection.execute("SELECT sequence,event_hash FROM forensic_events WHERE task_window_id=? ORDER BY sequence DESC LIMIT 1", (task_window_id,)).fetchone()
                sequence, previous = (tail["sequence"] + 1, tail["event_hash"]) if tail else (1, ZERO_HASH)
                digest = _hash(task_window_id, sequence, event_id, event_type, payload_json, occurred, previous)
                connection.execute("INSERT INTO forensic_events VALUES(?,?,?,?,?,?,?,?)", (task_window_id, sequence, event_id, event_type, payload_json, occurred, previous, digest))
                connection.commit()
                return self.inspect(task_window_id)[-1]
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _event(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def inspect(self, task_window_id: str) -> list[dict[str, Any]]:
        """Read one task's events in sequence order."""
        if not self.path.is_file():
            return []
        with self._connect(read_only=True) as connection:
            self._check_schema(connection)
            rows = connection.execute("SELECT * FROM forensic_events WHERE task_window_id=? ORDER BY sequence", (task_window_id,)).fetchall()
        return [self._event(row) for row in rows]

    def verify(self, task_window_id: str) -> dict[str, Any]:
        """Verify sequence and SHA-256 links, failing closed on tampering."""
        events = self.inspect(task_window_id)
        previous = ZERO_HASH
        for expected, event in enumerate(events, 1):
            payload_json = canonical_json(event["payload"])
            digest = _hash(task_window_id, expected, event["event_id"], event["event_type"], payload_json, event["occurred_at"], previous)
            if event["sequence"] != expected or event["previous_hash"] != previous or event["event_hash"] != digest:
                raise ForensicJournalError(f"forensic chain invalid at sequence {expected}")
            previous = digest
        return {"task_window_id": task_window_id, "valid": True, "event_count": len(events), "head_hash": previous}

    def context_package(self, task_window_id: str) -> dict[str, Any]:
        """Build a deterministic, read-only context projection with explicit gaps."""
        verification = self.verify(task_window_id)
        events = self.inspect(task_window_id)
        starts = {event["payload"].get("action_id"): event for event in events if event["event_type"] == "start" and event["payload"].get("action_id")}
        terminal = {event["payload"].get("action_id") for event in events if event["event_type"] in {"completed", "failed"}}
        unavailable = [event for event in events if event["event_type"] == "unavailable"]
        for action_id in sorted(set(starts) - terminal):
            unavailable.append({"kind": "incomplete_action", "action_id": action_id, "reason": "no completed or failed event recorded"})
        if not events:
            unavailable.append({"kind": "task_journal", "reason": "no events recorded"})
        return {"format": "odibi-anchor-forensic-package-v1", "task_window_id": task_window_id, "verification": verification, "events": events, "unavailable_evidence": unavailable}


def initialize(path: str | Path) -> ForensicJournal:
    return ForensicJournal(path)


def append_event(
    path: str | Path,
    task_window_id: str,
    event_type: str,
    payload: Mapping[str, Any],
    *,
    event_id: str,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    return ForensicJournal(path).append(
        task_window_id, event_type, payload, event_id=event_id, occurred_at=occurred_at
    )


def inspect(path: str | Path, task_window_id: str) -> list[dict[str, Any]]:
    return ForensicJournal(path, create=False).inspect(task_window_id)


def verify(path: str | Path, task_window_id: str) -> dict[str, Any]:
    return ForensicJournal(path, create=False).verify(task_window_id)


def build_context_package(path: str | Path, task_window_id: str) -> dict[str, Any]:
    return ForensicJournal(path, create=False).context_package(task_window_id)
