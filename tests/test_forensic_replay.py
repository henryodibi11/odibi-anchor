import sqlite3

import pytest

from odibi_anchor._forensic_replay import (
    EventConflict,
    ForensicJournal,
    ForensicJournalError,
    build_context_package,
    inspect,
)


def test_ordering_idempotency_conflict_and_isolation(tmp_path):
    journal = ForensicJournal(tmp_path / "journal.db")
    first = journal.append("task-a", "start", {"action_id": "a"}, event_id="one", occurred_at="2026-01-01T00:00:00Z")
    assert journal.append("task-a", "start", {"action_id": "a"}, event_id="one")["event_hash"] == first["event_hash"]
    journal.append("task-a", "completed", {"action_id": "a"}, event_id="two")
    journal.append("task-b", "evidence", {"result": 1}, event_id="one")
    assert [e["sequence"] for e in journal.inspect("task-a")] == [1, 2]
    assert len(journal.inspect("task-b")) == 1
    with pytest.raises(EventConflict):
        journal.append("task-a", "failed", {}, event_id="one")


def test_tamper_detection_and_append_only(tmp_path):
    path = tmp_path / "journal.db"
    journal = ForensicJournal(path)
    journal.append("task", "evidence", {"proof": "ok"}, event_id="e")
    with sqlite3.connect(path) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE forensic_events SET payload_json='{}'")
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER forensic_no_update")
        connection.execute("UPDATE forensic_events SET event_hash=?", ("f" * 64,))
    with pytest.raises(ForensicJournalError, match=r"schema checksum|chain invalid"):
        journal.verify("task")


def test_redaction_and_rejection(tmp_path):
    journal = ForensicJournal(tmp_path / "journal.db")
    event = journal.append("task", "evidence", {"token": "secret", "path": "/home/me/private", "url": "https://x.test/a?X-Amz-Signature=secret"}, event_id="e")
    assert event["payload"] == {"path": "[REDACTED_PATH]", "token": "[REDACTED]", "url": "https://x.test/a[REDACTED_URL]"}
    with pytest.raises(ForensicJournalError, match="private key"):
        journal.append("task", "evidence", {"text": "-----BEGIN PRIVATE KEY-----"}, event_id="bad")
    nested = {}
    cursor = nested
    for _ in range(10):
        cursor["x"] = {}
        cursor = cursor["x"]
    with pytest.raises(ForensicJournalError, match="nesting"):
        journal.append("task", "evidence", nested, event_id="deep")


@pytest.mark.parametrize("secret", [
    "github_pat_abcdefghijklmnopqrstuvwxyz123456",
    "ghp_abcdefghijklmnopqrstuvwxyz123456",
    "xoxb-1234567890-abcdefghijklmnopqrstuvwxyz",
    "AKIAABCDEFGHIJKLMNOP",
    "eyJabcdefghijk.abcdefghijklmnop.abcdefghijklmnop",
])
def test_unlabelled_tokens_are_redacted(tmp_path, secret):
    if secret.startswith("[REDACTED:slack-"):
        secret = "xo" + "xb-1234567890-abcdefghijklmnopqrstuvwxyz"
    elif secret.startswith("[REDACTED:aws-"):
        secret = "AK" + "IA" + "A" * 16
    journal = ForensicJournal(tmp_path / "journal.db")
    event = journal.append("task", "evidence", {"detail": secret}, event_id="e")
    assert event["payload"]["detail"] == "[REDACTED]"


@pytest.mark.parametrize("secret", [
    "sk" + "-proj-" + "a" * 32,
    "sk" + "_live_" + "a" * 24,
    "gl" + "pat-" + "a" * 24,
    "da" + "pi" + "a1" * 16,
])
def test_additional_provider_tokens_are_redacted(tmp_path, secret):
    journal = ForensicJournal(tmp_path / "journal.db")
    event = journal.append("task", "evidence", {"detail": secret}, event_id="e")
    assert event["payload"]["detail"] == "[REDACTED]"


def test_database_url_credentials_are_redacted(tmp_path):
    journal = ForensicJournal(tmp_path / "journal.db")
    event = journal.append(
        "task", "evidence", {"detail": "postgresql://alice:secret@db.example/data"},
        event_id="e",
    )
    assert event["payload"]["detail"] == "postgresql://db.example/data[REDACTED_URL]"


def test_deterministic_package_and_incomplete_action(tmp_path):
    journal = ForensicJournal(tmp_path / "journal.db")
    journal.append("task", "start", {"action_id": "build", "repository": "repo"}, event_id="s", occurred_at="2026-01-01T00:00:00Z")
    first = journal.context_package("task")
    assert first == journal.context_package("task")
    assert first["unavailable_evidence"] == [{"kind": "incomplete_action", "action_id": "build", "reason": "no completed or failed event recorded"}]
    journal.append("task", "unavailable", {"kind": "artifact", "reason": "expired"}, event_id="u")
    assert journal.context_package("task")["unavailable_evidence"][0]["event_type"] == "unavailable"


def test_read_only_helpers_do_not_create_an_absent_journal(tmp_path):
    path = tmp_path / "missing.db"
    assert inspect(path, "task") == []
    package = build_context_package(path, "task")
    assert package["verification"]["event_count"] == 0
    assert package["unavailable_evidence"] == [
        {"kind": "task_journal", "reason": "no events recorded"}
    ]
    assert not path.exists()
