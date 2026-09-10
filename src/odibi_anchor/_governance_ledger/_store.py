"""A deliberately narrow and inert SQLite authority ledger substrate."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

APPLICATION_ID = 0x43574C31  # CWL1
SCHEMA_VERSION = 1
_ZERO_HASH = b"\0" * 32
_DDL = """
CREATE TABLE ledger_schema (singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema_version INTEGER NOT NULL, schema_hash BLOB NOT NULL CHECK(length(schema_hash)=32)) STRICT;
CREATE TABLE checkouts (checkout_id BLOB PRIMARY KEY CHECK(length(checkout_id)=32), creation_blob BLOB NOT NULL CHECK(length(creation_blob) BETWEEN 1 AND 1048576), creation_hash BLOB NOT NULL CHECK(length(creation_hash)=32), next_sequence INTEGER NOT NULL CHECK(next_sequence>=1), created_ns INTEGER NOT NULL CHECK(created_ns>=0)) STRICT;
CREATE TABLE generations (generation_id BLOB PRIMARY KEY CHECK(length(generation_id)=32), checkout_id BLOB NOT NULL REFERENCES checkouts(checkout_id), sequence INTEGER NOT NULL CHECK(sequence>=1), state TEXT NOT NULL CHECK(state IN ('OPEN','CLOSED','CONSUMED','QUARANTINED')), open_blob BLOB NOT NULL CHECK(length(open_blob) BETWEEN 1 AND 1048576), created_ns INTEGER NOT NULL CHECK(created_ns>=0), UNIQUE(checkout_id,sequence)) STRICT;
CREATE UNIQUE INDEX one_open_generation ON generations(checkout_id) WHERE state='OPEN';
CREATE TABLE quarantine_intents (intent_id BLOB PRIMARY KEY CHECK(length(intent_id)=32), generation_id BLOB NOT NULL UNIQUE REFERENCES generations(generation_id), reason BLOB NOT NULL CHECK(length(reason) BETWEEN 1 AND 1048576), created_ns INTEGER NOT NULL CHECK(created_ns>=0)) STRICT;
CREATE TABLE events (event_number INTEGER PRIMARY KEY CHECK(event_number>=1), event_type TEXT NOT NULL, domain_id BLOB NOT NULL, payload BLOB NOT NULL CHECK(length(payload) BETWEEN 1 AND 1048576), previous_hash BLOB NOT NULL CHECK(length(previous_hash)=32), event_hash BLOB NOT NULL UNIQUE CHECK(length(event_hash)=32), created_ns INTEGER NOT NULL CHECK(created_ns>=0), UNIQUE(event_type,domain_id)) STRICT;
CREATE TABLE replay_records (operation TEXT NOT NULL, replay_key BLOB NOT NULL CHECK(length(replay_key) BETWEEN 1 AND 1048576), record_hash BLOB NOT NULL CHECK(length(record_hash)=32), request BLOB NOT NULL CHECK(length(request) BETWEEN 1 AND 1048576), response BLOB NOT NULL CHECK(length(response) BETWEEN 1 AND 1048576), event_number INTEGER NOT NULL UNIQUE REFERENCES events(event_number), PRIMARY KEY(operation,replay_key)) STRICT;
CREATE TRIGGER generations_no_transition BEFORE UPDATE ON generations BEGIN SELECT RAISE(ABORT,'generation rows are immutable'); END;
CREATE TRIGGER generations_no_delete BEFORE DELETE ON generations BEGIN SELECT RAISE(ABORT,'generation rows are immutable'); END;
CREATE TRIGGER generations_open_only BEFORE INSERT ON generations WHEN NEW.state!='OPEN' BEGIN SELECT RAISE(ABORT,'V1 only permits OPEN generations'); END;
CREATE TRIGGER checkouts_no_delete BEFORE DELETE ON checkouts BEGIN SELECT RAISE(ABORT,'checkout rows are immutable'); END;
CREATE TRIGGER checkouts_identity_immutable BEFORE UPDATE ON checkouts WHEN OLD.checkout_id!=NEW.checkout_id OR OLD.creation_blob!=NEW.creation_blob OR OLD.creation_hash!=NEW.creation_hash OR OLD.created_ns!=NEW.created_ns BEGIN SELECT RAISE(ABORT,'checkout identity is immutable'); END;
CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
CREATE TRIGGER quarantine_no_update BEFORE UPDATE ON quarantine_intents BEGIN SELECT RAISE(ABORT,'quarantine intents are append-only'); END;
CREATE TRIGGER quarantine_no_delete BEFORE DELETE ON quarantine_intents BEGIN SELECT RAISE(ABORT,'quarantine intents are append-only'); END;
CREATE TRIGGER replay_no_update BEFORE UPDATE ON replay_records BEGIN SELECT RAISE(ABORT,'replay records are immutable'); END;
CREATE TRIGGER replay_no_delete BEFORE DELETE ON replay_records BEGIN SELECT RAISE(ABORT,'replay records are immutable'); END;
""".strip()
SCHEMA_HASH = hashlib.sha256(b"odibi-anchor-governance-ledger-schema-v1\0" + _DDL.encode()).digest()


class LedgerError(RuntimeError):
    """The ledger cannot safely satisfy the operation."""


class LedgerConflict(LedgerError):
    """A replay key was reused with different request bytes."""


def _validate_path(path: Path) -> None:
    if not isinstance(path, Path) or not path.is_absolute():
        raise LedgerError("ledger path must be an explicit absolute Path")
    if os.getuid() != os.geteuid() or os.fspath(path).startswith("//"):
        raise LedgerError("ledger path requires an unambiguous effective owner")
    try:
        parent = path.parent
        parent_stat = parent.stat(follow_symlinks=False)
    except OSError as exc:
        raise LedgerError("ledger parent must already exist") from exc
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or parent_stat.st_uid != os.geteuid()
        or stat.S_IMODE(parent_stat.st_mode) & 0o077
    ):
        raise LedgerError("ledger parent must be an owner-private directory")
    if parent.resolve(strict=True) != parent:
        raise LedgerError("ledger parent may not traverse symlinks")


def _file_identity(path: Path) -> tuple[int, int]:
    _validate_path(path)
    try:
        file_stat = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise LedgerError("ledger does not exist") from exc
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_uid != os.geteuid()
        or stat.S_IMODE(file_stat.st_mode) != 0o600
        or file_stat.st_nlink != 1
    ):
        raise LedgerError("ledger must be a private, singly-linked regular file")
    return file_stat.st_dev, file_stat.st_ino


def _configure(connection: sqlite3.Connection, *, initializing: bool = False) -> None:
    if initializing:
        connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("PRAGMA synchronous=EXTRA")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    observed = {
        "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
        "synchronous": connection.execute("PRAGMA synchronous").fetchone()[0],
        "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
        "trusted_schema": connection.execute("PRAGMA trusted_schema").fetchone()[0],
    }
    if observed != {
        "journal_mode": "delete",
        "synchronous": 3,
        "foreign_keys": 1,
        "trusted_schema": 0,
    }:
        raise LedgerError("required SQLite durability settings are unavailable")


def _connect(path: Path) -> sqlite3.Connection:
    expected_identity = _file_identity(path)
    try:
        encoded_path = quote(os.fspath(path), safe="/")
        connection = sqlite3.connect(
            f"file:{encoded_path}?mode=rw", uri=True, timeout=0.1, isolation_level=None
        )
        if _file_identity(path) != expected_identity:
            raise LedgerError("ledger identity changed while opening")
        _configure(connection)
        return connection
    except (LedgerError, sqlite3.Error) as exc:
        if "connection" in locals():
            connection.close()
        raise LedgerError("ledger unavailable or busy") from exc


def _fsync_parent(path: Path) -> None:
    parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def initialize(path: Path) -> LedgerStore:
    """Explicitly create a new ledger; never replace or adopt an existing file."""
    _validate_path(path)
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
        os.close(fd)
        _fsync_parent(path)
        connection = sqlite3.connect(path, isolation_level=None)
        try:
            _configure(connection, initializing=True)
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            schema_hex = SCHEMA_HASH.hex()
            connection.executescript(
                f"BEGIN IMMEDIATE;\n{_DDL}\nINSERT INTO ledger_schema VALUES(1,{SCHEMA_VERSION},X'{schema_hex}');\nCOMMIT;"
            )
        finally:
            connection.close()
        _fsync_parent(path)
    except (LedgerError, OSError, sqlite3.Error) as exc:
        raise LedgerError("ledger initialization failed") from exc
    return open_verified(path)


def open_verified(path: Path) -> LedgerStore:
    """Open only after complete structural, integrity, and event-chain verification."""
    connection = _connect(path)
    try:
        connection.execute("BEGIN")
        _verify(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return LedgerStore(path)


def _verify(connection: sqlite3.Connection) -> None:
    try:
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise LedgerError("foreign ledger application id")
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise LedgerError("unsupported ledger version")
        if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
            raise LedgerError("unsafe journal mode")
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise LedgerError("ledger integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise LedgerError("ledger foreign key check failed")
        expected = sqlite3.connect(":memory:")
        try:
            expected.executescript(_DDL)
            expected_rows = expected.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
            ).fetchall()
        finally:
            expected.close()
        actual_rows = connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
        if actual_rows != expected_rows:
            raise LedgerError("ledger schema drift")
        if connection.execute("SELECT schema_version,schema_hash FROM ledger_schema WHERE singleton=1").fetchone() != (SCHEMA_VERSION, SCHEMA_HASH):
            raise LedgerError("ledger schema metadata mismatch")
        previous = _ZERO_HASH
        expected_number = 1
        for number, kind, domain_id, payload, stored_previous, event_hash, created_ns in connection.execute(
            "SELECT event_number,event_type,domain_id,payload,previous_hash,event_hash,created_ns FROM events ORDER BY event_number"
        ):
            if (
                number != expected_number
                or stored_previous != previous
                or event_hash != _event_hash(number, kind, domain_id, payload, previous, created_ns)
            ):
                raise LedgerError("ledger event chain invalid")
            _verify_domain_event(connection, kind, domain_id, payload, created_ns)
            previous = event_hash
            expected_number += 1
        for operation, replay_key, record_hash, request, response, event_number in connection.execute(
            "SELECT operation,replay_key,record_hash,request,response,event_number FROM replay_records"
        ):
            if (
                record_hash != _record_hash(operation, replay_key, request, response)
                or not _valid_replay_record(
                    connection, operation, request, response, event_number
                )
            ):
                raise LedgerError("ledger replay record invalid")
        if connection.execute("SELECT count(*) FROM replay_records").fetchone()[0] != connection.execute(
            "SELECT count(*) FROM events"
        ).fetchone()[0]:
            raise LedgerError("ledger event/replay pairing invalid")
        for checkout_id, creation_blob, creation_hash, next_sequence in connection.execute(
            "SELECT checkout_id,creation_blob,creation_hash,next_sequence FROM checkouts"
        ):
            if hashlib.sha256(creation_blob).digest() != creation_hash:
                raise LedgerError("ledger checkout creation record invalid")
            maximum = connection.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM generations WHERE checkout_id=?", (checkout_id,)
            ).fetchone()[0]
            if next_sequence != maximum + 1:
                raise LedgerError("ledger checkout sequence invalid")
        for table, kind in (
            ("checkouts", "CHECKOUT_CREATED"),
            ("generations", "PREPARED_OPEN_COMMITTED"),
            ("quarantine_intents", "QUARANTINE_INTENT_RECORDED"),
        ):
            if connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] != connection.execute(
                "SELECT count(*) FROM events WHERE event_type=?", (kind,)
            ).fetchone()[0]:
                raise LedgerError("ledger contains an unpaired domain record or event")
        _verify_v1_history(connection)
    except sqlite3.Error as exc:
        raise LedgerError("ledger verification failed") from exc


def _verify_v1_history(connection: sqlite3.Connection) -> None:
    """Reject schema-valid histories that no V1 writer can produce."""
    checkout_events = dict(
        connection.execute(
            "SELECT domain_id,event_number FROM events WHERE event_type='CHECKOUT_CREATED'"
        )
    )
    generation_events = dict(
        connection.execute(
            "SELECT domain_id,event_number FROM events "
            "WHERE event_type='PREPARED_OPEN_COMMITTED'"
        )
    )
    intent_events = dict(
        connection.execute(
            "SELECT domain_id,event_number FROM events "
            "WHERE event_type='QUARANTINE_INTENT_RECORDED'"
        )
    )
    sequences: dict[bytes, list[int]] = {}
    for generation_id, checkout_id, sequence, state in connection.execute(
        "SELECT generation_id,checkout_id,sequence,state FROM generations"
    ):
        if (
            state != "OPEN"
            or checkout_id not in checkout_events
            or generation_id not in generation_events
            or checkout_events[checkout_id] >= generation_events[generation_id]
        ):
            raise LedgerError("ledger V1 generation history invalid")
        sequences.setdefault(checkout_id, []).append(sequence)
    for checkout_id, next_sequence in connection.execute(
        "SELECT checkout_id,next_sequence FROM checkouts"
    ):
        ordered = sorted(sequences.get(checkout_id, []))
        if next_sequence != len(ordered) + 1 or any(
            sequence != expected for expected, sequence in enumerate(ordered, 1)
        ):
            raise LedgerError("ledger V1 generation sequence invalid")
    for intent_id, generation_id, state in connection.execute(
        "SELECT q.intent_id,q.generation_id,g.state FROM quarantine_intents q "
        "JOIN generations g ON g.generation_id=q.generation_id"
    ):
        if (
            state != "OPEN"
            or generation_id not in generation_events
            or intent_id not in intent_events
            or generation_events[generation_id] >= intent_events[intent_id]
        ):
            raise LedgerError("ledger V1 quarantine history invalid")


def _event_hash(number: int, kind: str, domain_id: bytes, payload: bytes, previous: bytes, created_ns: int) -> bytes:
    fields = [str(number).encode(), kind.encode(), domain_id, payload, previous, str(created_ns).encode()]
    return hashlib.sha256(b"CWL:event:v1\0" + b"".join(len(value).to_bytes(8, "big") + value for value in fields)).digest()


def _record_hash(operation: str, replay_key: bytes, request: bytes, response: bytes) -> bytes:
    fields = [operation.encode(), replay_key, request, response]
    return hashlib.sha256(b"CWL:replay:v1\0" + b"".join(len(value).to_bytes(8, "big") + value for value in fields)).digest()


def _bound_request(operation: str, request: bytes, *fields: bytes) -> bytes:
    values = [operation.encode(), request, *fields]
    return b"CWL:request:v1\0" + b"".join(
        len(value).to_bytes(8, "big") + value for value in values
    )


def _parse_bound_request(request: bytes) -> list[bytes] | None:
    prefix = b"CWL:request:v1\0"
    if not request.startswith(prefix):
        return None
    fields: list[bytes] = []
    offset = len(prefix)
    while offset < len(request):
        if len(request) - offset < 8:
            return None
        length = int.from_bytes(request[offset : offset + 8], "big")
        offset += 8
        if length > 1024 * 1024 or len(request) - offset < length:
            return None
        fields.append(request[offset : offset + length])
        offset += length
    return fields


def _response(result: dict[str, object]) -> bytes:
    return json.dumps(
        {
            **result,
            "authorizes_readiness": False,
            "is_receipt": False,
            "is_ack": False,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _valid_replay_record(
    connection: sqlite3.Connection,
    operation: str,
    request: bytes,
    response: bytes,
    event_number: int,
) -> bool:
    event = connection.execute(
        "SELECT event_type,domain_id FROM events WHERE event_number=?", (event_number,)
    ).fetchone()
    fields = _parse_bound_request(request)
    if event is None or fields is None or not fields or fields[0] != operation.encode():
        return False
    if operation == "create_checkout" and event[0] == "CHECKOUT_CREATED" and len(fields) == 3:
        row = connection.execute(
            "SELECT creation_blob FROM checkouts WHERE checkout_id=?", (event[1],)
        ).fetchone()
        result = {"checkout_id": event[1].hex()}
        valid_fields = row is not None and fields[2] == row[0]
    elif (
        operation == "commit_prepared_open"
        and event[0] == "PREPARED_OPEN_COMMITTED"
        and len(fields) == 4
    ):
        row = connection.execute(
            "SELECT checkout_id,sequence,state,open_blob FROM generations WHERE generation_id=?",
            (event[1],),
        ).fetchone()
        valid_fields = (
            row is not None
            and fields[2] == row[0]
            and fields[3] == row[3]
            and row[2] == "OPEN"
        )
        result = {
            "generation_id": event[1].hex(),
            "sequence": None if row is None else row[1],
            "state": "OPEN",
            "caller_prepared": True,
        }
    elif (
        operation == "record_quarantine_intent"
        and event[0] == "QUARANTINE_INTENT_RECORDED"
        and len(fields) == 4
    ):
        row = connection.execute(
            "SELECT generation_id,reason FROM quarantine_intents WHERE intent_id=?", (event[1],)
        ).fetchone()
        valid_fields = row is not None and fields[2] == row[0] and fields[3] == row[1]
        result = {
            "intent_id": event[1].hex(),
            "generation_id": "" if row is None else row[0].hex(),
            "state": "OPEN",
            "recovery": "FENCE_REQUIRED",
        }
    else:
        return False
    return valid_fields and len(fields[1]) >= 1 and response == _response(result)


def _verify_domain_event(
    connection: sqlite3.Connection, kind: str, domain_id: bytes, payload: bytes, created_ns: int
) -> None:
    if kind == "CHECKOUT_CREATED":
        row = connection.execute(
            "SELECT creation_blob,created_ns FROM checkouts WHERE checkout_id=?", (domain_id,)
        ).fetchone()
        valid = row is not None and row[1] == created_ns and payload == hashlib.sha256(row[0]).digest()
    elif kind == "PREPARED_OPEN_COMMITTED":
        row = connection.execute(
            "SELECT checkout_id,sequence,open_blob,created_ns FROM generations WHERE generation_id=?",
            (domain_id,),
        ).fetchone()
        valid = (
            row is not None
            and row[3] == created_ns
            and payload == row[0] + row[1].to_bytes(8, "big") + hashlib.sha256(row[2]).digest()
        )
    elif kind == "QUARANTINE_INTENT_RECORDED":
        row = connection.execute(
            "SELECT generation_id,reason,created_ns FROM quarantine_intents WHERE intent_id=?", (domain_id,)
        ).fetchone()
        valid = row is not None and row[2] == created_ns and payload == row[0] + hashlib.sha256(row[1]).digest()
    else:
        valid = False
    if not valid:
        raise LedgerError("ledger domain record does not match event")


class LedgerStore:
    """A path handle; every operation uses a newly verified short-lived connection."""

    def __init__(self, path: Path, *, clock_ns: Callable[[], int] | None = None) -> None:
        self.path = path
        self._clock_ns = clock_ns or time.time_ns

    @staticmethod
    def _bytes(value: bytes, field: str) -> bytes:
        if not isinstance(value, bytes) or not 1 <= len(value) <= 1024 * 1024:
            raise LedgerError(f"{field} must be 1..1048576 bytes")
        return value

    def _now(self) -> int:
        value = self._clock_ns()
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise LedgerError("ledger clock returned an invalid timestamp")
        return value

    def _write(
        self,
        operation: str,
        replay_key: bytes,
        request: bytes,
        action: Callable[[sqlite3.Connection], tuple[dict[str, object], int]],
    ) -> bytes:
        replay_key = self._bytes(replay_key, "replay_key")
        request = self._bytes(request, "request")
        _validate_path(self.path)
        connection = _connect(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            _verify(connection)
            found = connection.execute("SELECT request,response FROM replay_records WHERE operation=? AND replay_key=?", (operation, replay_key)).fetchone()
            if found:
                if found[0] != request:
                    raise LedgerConflict("replay key request conflict")
                connection.commit()
                return found[1]
            result, event_number = action(connection)
            response = _response(result)
            connection.execute(
                "INSERT INTO replay_records VALUES(?,?,?,?,?,?)",
                (
                    operation,
                    replay_key,
                    _record_hash(operation, replay_key, request, response),
                    request,
                    response,
                    event_number,
                ),
            )
            _verify(connection)
            connection.commit()
            return response
        except LedgerConflict:
            connection.rollback()
            raise
        except LedgerError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise LedgerError("atomic ledger operation failed") from exc
        finally:
            connection.close()

    @staticmethod
    def _append(
        connection: sqlite3.Connection,
        kind: str,
        domain_id: bytes,
        payload: bytes,
        created_ns: int,
    ) -> int:
        row = connection.execute("SELECT event_number,event_hash FROM events ORDER BY event_number DESC LIMIT 1").fetchone()
        number, previous = (1, _ZERO_HASH) if row is None else (row[0] + 1, row[1])
        digest = _event_hash(number, kind, domain_id, payload, previous, created_ns)
        connection.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?)", (number, kind, domain_id, payload, previous, digest, created_ns))
        return number

    def create_checkout(self, *, replay_key: bytes, request: bytes, creation_blob: bytes) -> bytes:
        creation_blob = self._bytes(creation_blob, "creation_blob")
        request = self._bytes(request, "request")
        request = _bound_request("create_checkout", request, creation_blob)

        def action(connection: sqlite3.Connection) -> tuple[dict[str, object], int]:
            created_ns = self._now()
            checkout_id = secrets.token_bytes(32)
            connection.execute(
                "INSERT INTO checkouts VALUES(?,?,?,?,?)",
                (checkout_id, creation_blob, hashlib.sha256(creation_blob).digest(), 1, created_ns),
            )
            event_number = self._append(
                connection,
                "CHECKOUT_CREATED",
                checkout_id,
                hashlib.sha256(creation_blob).digest(),
                created_ns,
            )
            return {"checkout_id": checkout_id.hex()}, event_number

        return self._write("create_checkout", replay_key, request, action)

    def commit_prepared_open(
        self, *, checkout_id: bytes, open_blob: bytes, replay_key: bytes, request: bytes
    ) -> bytes:
        open_blob = self._bytes(open_blob, "open_blob")
        request = self._bytes(request, "request")
        if not isinstance(checkout_id, bytes) or len(checkout_id) != 32:
            raise LedgerError("checkout_id must be 32 bytes")
        request = _bound_request("commit_prepared_open", request, checkout_id, open_blob)

        def action(connection: sqlite3.Connection) -> tuple[dict[str, object], int]:
            created_ns = self._now()
            row = connection.execute("SELECT next_sequence FROM checkouts WHERE checkout_id=?", (checkout_id,)).fetchone()
            if row is None:
                raise sqlite3.IntegrityError("unknown checkout")
            generation_id, sequence = secrets.token_bytes(32), row[0]
            connection.execute("INSERT INTO generations VALUES(?,?,?,?,?,?)", (generation_id, checkout_id, sequence, "OPEN", open_blob, created_ns))
            connection.execute("UPDATE checkouts SET next_sequence=? WHERE checkout_id=?", (sequence + 1, checkout_id))
            payload = checkout_id + sequence.to_bytes(8, "big") + hashlib.sha256(open_blob).digest()
            event_number = self._append(
                connection, "PREPARED_OPEN_COMMITTED", generation_id, payload, created_ns
            )
            return {
                "generation_id": generation_id.hex(),
                "sequence": sequence,
                "state": "OPEN",
                "caller_prepared": True,
            }, event_number

        return self._write("commit_prepared_open", replay_key, request, action)

    def record_quarantine_intent(
        self, *, generation_id: bytes, reason: bytes, replay_key: bytes, request: bytes
    ) -> bytes:
        reason = self._bytes(reason, "reason")
        request = self._bytes(request, "request")
        if not isinstance(generation_id, bytes) or len(generation_id) != 32:
            raise LedgerError("generation_id must be 32 bytes")
        request = _bound_request("record_quarantine_intent", request, generation_id, reason)

        def action(connection: sqlite3.Connection) -> tuple[dict[str, object], int]:
            created_ns = self._now()
            row = connection.execute("SELECT state FROM generations WHERE generation_id=?", (generation_id,)).fetchone()
            if row != ("OPEN",):
                raise sqlite3.IntegrityError("quarantine intent requires OPEN generation")
            intent_id = secrets.token_bytes(32)
            connection.execute("INSERT INTO quarantine_intents VALUES(?,?,?,?)", (intent_id, generation_id, reason, created_ns))
            event_number = self._append(
                connection,
                "QUARANTINE_INTENT_RECORDED",
                intent_id,
                generation_id + hashlib.sha256(reason).digest(),
                created_ns,
            )
            return {
                "intent_id": intent_id.hex(),
                "generation_id": generation_id.hex(),
                "state": "OPEN",
                "recovery": "FENCE_REQUIRED",
            }, event_number

        return self._write("record_quarantine_intent", replay_key, request, action)

    def recovery_snapshot(self) -> bytes:
        _validate_path(self.path)
        connection = _connect(self.path)
        try:
            connection.execute("BEGIN")
            _verify(connection)
            generations = connection.execute(
                "SELECT hex(g.generation_id),hex(g.checkout_id),g.sequence,EXISTS(SELECT 1 FROM quarantine_intents q WHERE q.generation_id=g.generation_id) FROM generations g WHERE g.state='OPEN' ORDER BY g.checkout_id,g.sequence"
            ).fetchall()
            document = {"authorizes_readiness": False, "is_receipt": False, "is_ack": False, "open_generations": [
                {"generation_id": row[0].lower(), "checkout_id": row[1].lower(), "sequence": row[2], "state": "OPEN", "recovery": "FENCE_REQUIRED" if row[3] else "OPEN_UNATTESTED"} for row in generations
            ]}
            connection.commit()
            return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
