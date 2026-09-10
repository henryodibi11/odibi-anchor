from __future__ import annotations

import hashlib
import importlib
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from odibi_anchor._governance_ledger import _store

LedgerConflict = _store.LedgerConflict
LedgerError = _store.LedgerError
initialize = _store.initialize
open_verified = _store.open_verified


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    os.chmod(tmp_path, 0o700)
    return tmp_path / "authority.sqlite3"


def _document(value: bytes) -> dict[str, object]:
    return json.loads(value)


def _store_at(path: Path) -> _store.LedgerStore:
    return _store.LedgerStore(path, clock_ns=iter(range(1, 100)).__next__)


def _restore_triggers(connection: sqlite3.Connection, triggers: dict[str, str]) -> None:
    for sql in triggers.values():
        connection.execute(sql)


def _trigger_sql(connection: sqlite3.Connection, *names: str) -> dict[str, str]:
    rows = dict(
        connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'")
    )
    assert all(name in rows for name in names)
    return {name: rows[name] for name in names}


def _open_generation(store: _store.LedgerStore) -> tuple[bytes, bytes]:
    checkout = _document(
        store.create_checkout(replay_key=b"c", request=b"c", creation_blob=b"creation")
    )
    checkout_id = bytes.fromhex(checkout["checkout_id"])  # type: ignore[arg-type]
    opened = _document(
        store.commit_prepared_open(
            checkout_id=checkout_id,
            open_blob=b"prepared",
            replay_key=b"o",
            request=b"open",
        )
    )
    return checkout_id, bytes.fromhex(opened["generation_id"])  # type: ignore[arg-type]


def test_import_and_missing_inspection_create_nothing(ledger_path: Path) -> None:
    sys.modules.pop("odibi_anchor._governance_ledger", None)
    importlib.import_module("odibi_anchor._governance_ledger")
    assert not ledger_path.exists()
    with pytest.raises(LedgerError):
        open_verified(ledger_path)
    assert not ledger_path.exists()


def test_refuses_unsafe_paths_and_foreign_database(tmp_path: Path, ledger_path: Path) -> None:
    with pytest.raises(LedgerError):
        initialize(Path("relative.db"))
    os.chmod(tmp_path, 0o755)
    with pytest.raises(LedgerError):
        initialize(ledger_path)
    os.chmod(tmp_path, 0o700)
    sqlite3.connect(ledger_path).close()
    os.chmod(ledger_path, 0o600)
    with pytest.raises(LedgerError):
        open_verified(ledger_path)


@pytest.mark.parametrize("name", ["authority?.sqlite3", "authority#.sqlite3", "authority%.sqlite3"])
def test_uri_metacharacters_open_only_the_validated_file(tmp_path: Path, name: str) -> None:
    os.chmod(tmp_path, 0o700)
    selected = tmp_path / name
    initialize(selected)
    store = _store_at(selected)
    store.create_checkout(replay_key=b"c", request=b"c", creation_blob=b"creation")
    with sqlite3.connect(selected) as connection:
        assert connection.execute("SELECT count(*) FROM checkouts").fetchone() == (1,)
    alias = tmp_path / name.split("?", 1)[0].split("#", 1)[0]
    if alias != selected:
        assert not alias.exists()


def test_exact_initialization_and_reopen_pragmas(ledger_path: Path) -> None:
    initialize(ledger_path)
    assert ledger_path.stat().st_mode & 0o777 == 0o600
    open_verified(ledger_path)
    connection = sqlite3.connect(ledger_path)
    try:
        assert connection.execute("PRAGMA application_id").fetchone() == (0x43574C31,)
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert connection.execute("SELECT length(schema_hash) FROM ledger_schema").fetchone() == (32,)
    finally:
        connection.close()


def test_every_operation_rechecks_file_identity_policy(ledger_path: Path) -> None:
    initialize(ledger_path)
    store = _store_at(ledger_path)
    os.chmod(ledger_path, 0o644)
    with pytest.raises(LedgerError, match="private"):
        store.recovery_snapshot()
    with pytest.raises(LedgerError, match="private"):
        store.create_checkout(replay_key=b"c", request=b"c", creation_blob=b"creation")
    os.chmod(ledger_path, 0o600)
    hardlink = ledger_path.with_name("authority-hardlink.sqlite3")
    os.link(ledger_path, hardlink)
    with pytest.raises(LedgerError, match="singly-linked"):
        store.recovery_snapshot()
    hardlink.unlink()
    real_ledger = ledger_path.with_name("authority-real.sqlite3")
    ledger_path.rename(real_ledger)
    ledger_path.symlink_to(real_ledger.name)
    with pytest.raises(LedgerError, match="private"):
        store.recovery_snapshot()
    with pytest.raises(LedgerError, match="private"):
        store.create_checkout(replay_key=b"c", request=b"c", creation_blob=b"creation")


def test_atomic_create_prepared_open_replay_and_quarantine(ledger_path: Path) -> None:
    initialize(ledger_path)
    store = _store_at(ledger_path)
    checkout_response = store.create_checkout(
        replay_key=b"checkout-1", request=b"create", creation_blob=b"host creation evidence"
    )
    assert store.create_checkout(
        replay_key=b"checkout-1", request=b"create", creation_blob=b"host creation evidence"
    ) == checkout_response
    with pytest.raises(LedgerConflict):
        store.create_checkout(
            replay_key=b"checkout-1", request=b"create", creation_blob=b"different creation"
        )
    with pytest.raises(LedgerConflict):
        store.create_checkout(
            replay_key=b"checkout-1", request=b"different", creation_blob=b"host creation evidence"
        )
    checkout_id = bytes.fromhex(_document(checkout_response)["checkout_id"])  # type: ignore[arg-type]

    open_response = store.commit_prepared_open(
        checkout_id=checkout_id, open_blob=b"untrusted caller bytes", replay_key=b"open-1", request=b"open"
    )
    assert store.commit_prepared_open(
        checkout_id=checkout_id,
        open_blob=b"untrusted caller bytes",
        replay_key=b"open-1",
        request=b"open",
    ) == open_response
    with pytest.raises(LedgerConflict):
        store.commit_prepared_open(
            checkout_id=checkout_id,
            open_blob=b"different caller bytes",
            replay_key=b"open-1",
            request=b"open",
        )
    opened = _document(open_response)
    assert opened["sequence"] == 1
    assert opened["state"] == "OPEN"
    assert opened["caller_prepared"] is True
    assert opened["authorizes_readiness"] is False
    generation_id = bytes.fromhex(opened["generation_id"])  # type: ignore[arg-type]

    quarantine_response = store.record_quarantine_intent(
        generation_id=generation_id,
        reason=b"suspicion",
        replay_key=b"q-1",
        request=b"quarantine",
    )
    quarantine = _document(quarantine_response)
    assert store.record_quarantine_intent(
        generation_id=generation_id,
        reason=b"suspicion",
        replay_key=b"q-1",
        request=b"quarantine",
    ) == quarantine_response
    with pytest.raises(LedgerConflict):
        store.record_quarantine_intent(
            generation_id=generation_id,
            reason=b"different reason",
            replay_key=b"q-1",
            request=b"quarantine",
        )
    assert quarantine["state"] == "OPEN"
    assert quarantine["recovery"] == "FENCE_REQUIRED"
    snapshot = _document(store.recovery_snapshot())
    assert snapshot["authorizes_readiness"] is False
    assert snapshot["open_generations"][0]["recovery"] == "FENCE_REQUIRED"  # type: ignore[index]

    connection = sqlite3.connect(ledger_path)
    try:
        assert connection.execute("SELECT next_sequence FROM checkouts").fetchone() == (2,)
        assert connection.execute("SELECT state FROM generations").fetchone() == ("OPEN",)
        assert connection.execute("SELECT count(*) FROM events").fetchone() == (3,)
        assert connection.execute("SELECT count(*) FROM replay_records").fetchone() == (3,)
    finally:
        connection.close()


def test_transition_and_append_only_triggers(ledger_path: Path) -> None:
    initialize(ledger_path)
    store = _store_at(ledger_path)
    checkout = _document(
        store.create_checkout(replay_key=b"c", request=b"c", creation_blob=b"creation")
    )
    checkout_id = bytes.fromhex(checkout["checkout_id"])  # type: ignore[arg-type]
    opened = _document(store.commit_prepared_open(
        checkout_id=checkout_id, open_blob=b"blob", replay_key=b"o", request=b"o"
    ))
    generation_id = bytes.fromhex(opened["generation_id"])  # type: ignore[arg-type]
    connection = sqlite3.connect(ledger_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE generations SET state='BOUND'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM events")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO generations VALUES(?,?,?,?,?,?)", (os.urandom(32), checkout_id, 2, "QUARANTINED", b"x", 3)
            )
        assert connection.execute("SELECT state FROM generations WHERE generation_id=?", (generation_id,)).fetchone() == ("OPEN",)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE ledger_schema SET schema_hash=zeroblob(32)",
        "UPDATE replay_records SET response=X'00'",
        "DROP TRIGGER events_no_update",
    ],
)
def test_schema_event_or_record_tampering_fails_closed(ledger_path: Path, statement: str) -> None:
    initialize(ledger_path)
    store = _store_at(ledger_path)
    store.create_checkout(replay_key=b"c", request=b"c", creation_blob=b"creation")
    connection = sqlite3.connect(ledger_path)
    try:
        if statement.startswith("UPDATE replay_records"):
            connection.execute("DROP TRIGGER replay_no_update")
            connection.execute(statement)
            connection.execute(
                "CREATE TRIGGER replay_no_update BEFORE UPDATE ON replay_records "
                "BEGIN SELECT RAISE(ABORT,'replay records are immutable'); END"
            )
        else:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(LedgerError):
        open_verified(ledger_path)


def test_sqlite_managed_analyze_schema_drift_fails_closed(ledger_path: Path) -> None:
    initialize(ledger_path)
    connection = sqlite3.connect(ledger_path)
    try:
        connection.execute("ANALYZE")
        connection.commit()
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='sqlite_stat1'"
        ).fetchone() == (1,)
    finally:
        connection.close()
    with pytest.raises(LedgerError, match="schema drift"):
        open_verified(ledger_path)


def test_failed_open_is_atomically_rolled_back(ledger_path: Path) -> None:
    initialize(ledger_path)
    store = _store_at(ledger_path)
    with pytest.raises(LedgerError):
        store.commit_prepared_open(
            checkout_id=os.urandom(32), open_blob=b"blob", replay_key=b"bad", request=b"bad"
        )
    connection = sqlite3.connect(ledger_path)
    try:
        assert connection.execute("SELECT count(*) FROM generations").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM events").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM replay_records").fetchone() == (0,)
    finally:
        connection.close()


def test_competing_writer_fails_closed_without_partial_state(ledger_path: Path) -> None:
    initialize(ledger_path)
    store = _store_at(ledger_path)
    holder = sqlite3.connect(ledger_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(LedgerError, match="atomic ledger operation failed"):
            store.create_checkout(replay_key=b"c", request=b"c", creation_blob=b"creation")
        assert holder.execute("SELECT count(*) FROM checkouts").fetchone() == (0,)
        assert holder.execute("SELECT count(*) FROM events").fetchone() == (0,)
        assert holder.execute("SELECT count(*) FROM replay_records").fetchone() == (0,)
    finally:
        holder.rollback()
        holder.close()
    response = store.create_checkout(
        replay_key=b"c", request=b"c", creation_blob=b"creation"
    )
    assert _document(response)["authorizes_readiness"] is False


def test_post_write_verification_rolls_back_writer_impossible_history(ledger_path: Path) -> None:
    initialize(ledger_path)
    store = _store_at(ledger_path)

    def malformed_action(connection: sqlite3.Connection) -> tuple[dict[str, object], int]:
        checkout_id = bytes(32)
        creation_blob = b"malformed creation"
        connection.execute(
            "INSERT INTO checkouts VALUES(?,?,?,?,?)",
            (
                checkout_id,
                creation_blob,
                hashlib.sha256(creation_blob).digest(),
                1,
                1,
            ),
        )
        event_number = store._append(
            connection, "UNKNOWN_EVENT", checkout_id, b"payload", 1
        )
        return {"malformed": True}, event_number

    with pytest.raises(LedgerError, match="domain record does not match event"):
        store._write("malformed", b"key", b"request", malformed_action)
    with sqlite3.connect(ledger_path) as connection:
        assert connection.execute("SELECT count(*) FROM checkouts").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM events").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM replay_records").fetchone() == (0,)


@pytest.mark.parametrize("sequence", [2, 2**62])
def test_semantic_verifier_rejects_cryptographically_consistent_sequence_gap(
    ledger_path: Path, monkeypatch: pytest.MonkeyPatch, sequence: int
) -> None:
    initialize(ledger_path)
    store = _store_at(ledger_path)
    checkout_id, generation_id = _open_generation(store)
    with sqlite3.connect(ledger_path) as connection:
        triggers = _trigger_sql(
            connection,
            "generations_no_transition",
            "events_no_update",
            "replay_no_update",
        )
        for name in triggers:
            connection.execute(f"DROP TRIGGER {name}")
        connection.execute(
            "UPDATE generations SET sequence=? WHERE generation_id=?", (sequence, generation_id)
        )
        connection.execute(
            "UPDATE checkouts SET next_sequence=? WHERE checkout_id=?",
            (sequence + 1, checkout_id),
        )
        event_number, kind, domain_id, previous, created_ns = connection.execute(
            "SELECT event_number,event_type,domain_id,previous_hash,created_ns "
            "FROM events WHERE event_type='PREPARED_OPEN_COMMITTED'"
        ).fetchone()
        payload = (
            checkout_id
            + sequence.to_bytes(8, "big")
            + hashlib.sha256(b"prepared").digest()
        )
        connection.execute(
            "UPDATE events SET payload=?,event_hash=? WHERE event_number=?",
            (
                payload,
                _store._event_hash(
                    event_number, kind, domain_id, payload, previous, created_ns
                ),
                event_number,
            ),
        )
        operation, replay_key, request, response = connection.execute(
            "SELECT operation,replay_key,request,response FROM replay_records "
            "WHERE operation='commit_prepared_open'"
        ).fetchone()
        response_document = _document(response)
        response_document["sequence"] = sequence
        response = json.dumps(response_document, sort_keys=True, separators=(",", ":")).encode()
        connection.execute(
            "UPDATE replay_records SET response=?,record_hash=? "
            "WHERE operation='commit_prepared_open'",
            (
                response,
                _store._record_hash(operation, replay_key, request, response),
            ),
        )
        _restore_triggers(connection, triggers)
        connection.commit()

    verifier = _store._verify_v1_history
    monkeypatch.setattr(_store, "_verify_v1_history", lambda connection: None)
    open_verified(ledger_path)
    monkeypatch.setattr(_store, "_verify_v1_history", verifier)
    with pytest.raises(LedgerError, match="generation sequence invalid"):
        open_verified(ledger_path)


def test_semantic_verifier_directly_rejects_non_open_generation(ledger_path: Path) -> None:
    initialize(ledger_path)
    store = _store_at(ledger_path)
    _, generation_id = _open_generation(store)
    with sqlite3.connect(ledger_path) as connection:
        trigger = _trigger_sql(connection, "generations_no_transition")
        connection.execute("DROP TRIGGER generations_no_transition")
        connection.execute(
            "UPDATE generations SET state='CLOSED' WHERE generation_id=?", (generation_id,)
        )
        with pytest.raises(LedgerError, match="generation history invalid"):
            _store._verify_v1_history(connection)
        _restore_triggers(connection, trigger)


@pytest.mark.parametrize(
    "event_order",
    [
        ("PREPARED_OPEN_COMMITTED", "CHECKOUT_CREATED", "QUARANTINE_INTENT_RECORDED"),
        ("CHECKOUT_CREATED", "QUARANTINE_INTENT_RECORDED", "PREPARED_OPEN_COMMITTED"),
    ],
)
def test_semantic_verifier_rejects_cryptographically_consistent_dependency_reordering(
    ledger_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event_order: tuple[str, str, str],
) -> None:
    initialize(ledger_path)
    store = _store_at(ledger_path)
    _, generation_id = _open_generation(store)
    store.record_quarantine_intent(
        generation_id=generation_id,
        reason=b"reason",
        replay_key=b"q",
        request=b"quarantine",
    )
    with sqlite3.connect(ledger_path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        triggers = _trigger_sql(connection, "events_no_delete", "replay_no_delete")
        for name in triggers:
            connection.execute(f"DROP TRIGGER {name}")
        events = {
            row[1]: row
            for row in connection.execute(
                "SELECT event_number,event_type,domain_id,payload,previous_hash,event_hash,created_ns "
                "FROM events"
            )
        }
        replay_rows = connection.execute(
            "SELECT operation,replay_key,record_hash,request,response,event_number "
            "FROM replay_records"
        ).fetchall()
        connection.execute("DELETE FROM replay_records")
        connection.execute("DELETE FROM events")
        previous = bytes(32)
        event_numbers: dict[str, int] = {}
        for number, kind in enumerate(event_order, 1):
            _, _, domain_id, payload, _, _, created_ns = events[kind]
            event_hash = _store._event_hash(
                number, kind, domain_id, payload, previous, created_ns
            )
            connection.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?)",
                (number, kind, domain_id, payload, previous, event_hash, created_ns),
            )
            event_numbers[kind] = number
            previous = event_hash
        operation_events = {
            "create_checkout": "CHECKOUT_CREATED",
            "commit_prepared_open": "PREPARED_OPEN_COMMITTED",
            "record_quarantine_intent": "QUARANTINE_INTENT_RECORDED",
        }
        for operation, replay_key, record_hash, request, response, _ in replay_rows:
            connection.execute(
                "INSERT INTO replay_records VALUES(?,?,?,?,?,?)",
                (
                    operation,
                    replay_key,
                    record_hash,
                    request,
                    response,
                    event_numbers[operation_events[operation]],
                ),
            )
        _restore_triggers(connection, triggers)
        connection.commit()

    verifier = _store._verify_v1_history
    monkeypatch.setattr(_store, "_verify_v1_history", lambda connection: None)
    open_verified(ledger_path)
    monkeypatch.setattr(_store, "_verify_v1_history", verifier)
    with pytest.raises(LedgerError, match="history invalid"):
        open_verified(ledger_path)


def test_event_number_not_clock_is_the_monotonic_ordering_fact(ledger_path: Path) -> None:
    initialize(ledger_path)
    store = _store.LedgerStore(ledger_path, clock_ns=iter((3, 2, 1)).__next__)
    _, generation_id = _open_generation(store)
    store.record_quarantine_intent(
        generation_id=generation_id,
        reason=b"reason",
        replay_key=b"q",
        request=b"quarantine",
    )
    reopened = open_verified(ledger_path)
    snapshot = _document(reopened.recovery_snapshot())
    assert snapshot["open_generations"][0]["recovery"] == "FENCE_REQUIRED"  # type: ignore[index]


def test_quarantine_intent_persists_and_checkout_reuse_remains_denied(ledger_path: Path) -> None:
    initialize(ledger_path)
    store = _store_at(ledger_path)
    checkout_id, generation_id = _open_generation(store)
    store.record_quarantine_intent(
        generation_id=generation_id,
        reason=b"reason",
        replay_key=b"q",
        request=b"quarantine",
    )
    reopened = open_verified(ledger_path)
    snapshot = _document(reopened.recovery_snapshot())
    assert snapshot["open_generations"][0]["recovery"] == "FENCE_REQUIRED"  # type: ignore[index]
    with pytest.raises(LedgerError, match="atomic ledger operation failed"):
        reopened.commit_prepared_open(
            checkout_id=checkout_id,
            open_blob=b"second",
            replay_key=b"second-open",
            request=b"second-open",
        )
    with sqlite3.connect(ledger_path) as connection:
        assert connection.execute("SELECT count(*) FROM generations").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM events").fetchone() == (3,)


def test_permission_granting_replay_response_fails_closed(ledger_path: Path) -> None:
    initialize(ledger_path)
    store = _store_at(ledger_path)
    store.create_checkout(replay_key=b"c", request=b"c", creation_blob=b"creation")
    connection = sqlite3.connect(ledger_path)
    try:
        operation, replay_key, request, event_number = connection.execute(
            "SELECT operation,replay_key,request,event_number FROM replay_records"
        ).fetchone()
        response = json.dumps(
            {
                "authorizes_readiness": True,
                "checkout_id": connection.execute("SELECT hex(checkout_id) FROM checkouts").fetchone()[
                    0
                ].lower(),
                "is_ack": False,
                "is_receipt": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        connection.execute("DROP TRIGGER replay_no_update")
        connection.execute(
            "UPDATE replay_records SET response=?,record_hash=?",
            (response, _store._record_hash(operation, replay_key, request, response)),
        )
        connection.execute(
            "CREATE TRIGGER replay_no_update BEFORE UPDATE ON replay_records "
            "BEGIN SELECT RAISE(ABORT,'replay records are immutable'); END"
        )
        connection.commit()
        assert event_number == 1
    finally:
        connection.close()
    with pytest.raises(LedgerError, match="replay record invalid"):
        open_verified(ledger_path)


@pytest.mark.parametrize(
    "tamper",
    ["false_state", "wrong_sequence", "extra_field", "malformed_request", "wrong_checkout"],
)
def test_exact_prepared_open_replay_contract_fails_closed(
    ledger_path: Path, tamper: str
) -> None:
    initialize(ledger_path)
    store = _store_at(ledger_path)
    checkout = _document(
        store.create_checkout(replay_key=b"c", request=b"c", creation_blob=b"creation")
    )
    checkout_id = bytes.fromhex(checkout["checkout_id"])  # type: ignore[arg-type]
    store.commit_prepared_open(
        checkout_id=checkout_id,
        open_blob=b"prepared",
        replay_key=b"o",
        request=b"open",
    )
    connection = sqlite3.connect(ledger_path)
    try:
        operation, replay_key, request, response = connection.execute(
            "SELECT operation,replay_key,request,response FROM replay_records "
            "WHERE operation='commit_prepared_open'"
        ).fetchone()
        document = json.loads(response)
        if tamper == "false_state":
            document["state"] = "QUARANTINED"
            response = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        elif tamper == "wrong_sequence":
            document["sequence"] = 99
            response = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        elif tamper == "extra_field":
            document["unexpected"] = True
            response = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        elif tamper == "malformed_request":
            request = b"not-a-bound-request"
        else:
            request = _store._bound_request(
                "commit_prepared_open", b"open", bytes(32), b"prepared"
            )
        connection.execute("DROP TRIGGER replay_no_update")
        connection.execute(
            "UPDATE replay_records SET request=?,response=?,record_hash=? "
            "WHERE operation='commit_prepared_open'",
            (
                request,
                response,
                _store._record_hash(operation, replay_key, request, response),
            ),
        )
        connection.execute(
            "CREATE TRIGGER replay_no_update BEFORE UPDATE ON replay_records "
            "BEGIN SELECT RAISE(ABORT,'replay records are immutable'); END"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(LedgerError, match="replay record invalid"):
        open_verified(ledger_path)
    with pytest.raises(LedgerError, match="replay record invalid"):
        store.commit_prepared_open(
            checkout_id=checkout_id,
            open_blob=b"prepared",
            replay_key=b"o",
            request=b"open",
        )


def test_package_has_no_governance_runtime_imports() -> None:
    package = Path(__file__).parents[2] / "src/odibi_anchor/_governance_ledger"
    source = "\n".join(path.read_text() for path in package.glob("*.py"))
    for forbidden in (
        "_governance_protocol",
        "_governance_sidecar",
        "_governance_host_probe",
        "bootstrap",
        "_kernel",
        "socket",
        "subprocess",
        "cryptography",
    ):
        assert forbidden not in source
