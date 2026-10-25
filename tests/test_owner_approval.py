"""Owner approval: one challenge format, non-blocking remote requests, first valid reply wins."""
from __future__ import annotations

import importlib
import re
import sqlite3
import time

import pytest

human_input = human_input_owner = owner_approval = None


@pytest.fixture(autouse=True)
def _current_modules():
    """Bind the live modules: a bootstrap init in an earlier test purges odibi_anchor imports."""
    global human_input, human_input_owner, owner_approval
    human_input = importlib.import_module("odibi_anchor.human_input")
    human_input_owner = importlib.import_module("odibi_anchor.human_input_owner")
    owner_approval = importlib.import_module("odibi_anchor.owner_approval")


SUBJECT = {"workflow_id": "wf_1", "generation": 6, "candidate_sha256": "sha256:" + "c" * 64}


class FakeSlack:
    """A resumable remote transport whose thread replies the test controls."""

    name = "slack"
    resumable = True

    def __init__(self):
        self.delivered = []
        self.closed = []
        self.thread = []

    def deliver(self, request):
        self.delivered.append(request)
        return f"C123:{len(self.delivered)}.0"

    def replies(self, _transport_ref):
        return list(self.thread)

    def close(self, request, reason):
        self.closed.append((request.request_id, reason))
        return "C123:closed"


@pytest.fixture
def slack(tmp_path, monkeypatch):
    """Route provider selection to a fake Slack unless an in-session provider is explicit."""
    monkeypatch.setenv("ANCHOR_HUMAN_INPUT_STATE_PATH", str(tmp_path / "human-input.sqlite3"))
    transport = FakeSlack()
    real = human_input_owner.select_owner_approval_provider

    def select(*, provider=None, in_session_approval=None):
        if provider is not None:
            return real(provider=provider, in_session_approval=in_session_approval)
        return human_input_owner.OwnerApprovalProvider(
            transport=transport, expected_owner_id="UOWNER", assurance="remote_authenticated_slack_identity",
        )

    monkeypatch.setattr(human_input_owner, "select_owner_approval_provider", select)
    return transport


def request(**kwargs):
    options = {"operation": "request_delivery_approval", "subject": SUBJECT,
               "prompt": "Authorize this exact delivery.", "wait_seconds": 0}
    return owner_approval.request_owner_approval(**{**options, **kwargs})


def owner_reply(text, *, user="UOWNER", message_id="C123:2.0"):
    return human_input.HumanInputReply(text=text, user_id=user, message_id=message_id, received_at=time.time())


def test_challenge_is_bare_lowercase_hex_of_the_canonical_subject():
    hex_digest, response = owner_approval.challenge_for(SUBJECT)
    reordered = dict(reversed(list(SUBJECT.items())))

    assert re.fullmatch(r"[0-9a-f]{64}", hex_digest)
    assert response == f"APPROVE {hex_digest}"
    assert owner_approval.challenge_for(reordered) == (hex_digest, response)
    assert owner_approval.challenge_for(SUBJECT, "REVOKE") == (hex_digest, f"REVOKE {hex_digest}")
    with pytest.raises(ValueError, match="verb"):
        owner_approval.challenge_for(SUBJECT, "DELETE")


def test_chat_in_session_approval_is_exact_lower_assurance_and_single_use(slack):
    response = owner_approval.challenge_for(SUBJECT)[1]
    with pytest.raises(owner_approval.OwnerApprovalMismatch) as mismatch:
        request(provider="chat_in_session", in_session_approval=response + "\n")
    assert mismatch.value.mismatches[0]["matches_after_trimming_whitespace"] is True

    approved = request(provider="chat_in_session", in_session_approval=response)

    assert approved["status"] == "approved"
    assert approved["approval_response"] == response
    assert approved["owner"] == "single-user-chat-session"
    assert approved["assurance"] == "lower_assurance_single_user_chat_session_assertion"
    assert approved["transport"] == "chat-in-session-owner-assertion"
    assert approved["authority_ref"] and approved["response_message_id"]
    # Consumed: asking again returns the same reply and opens nothing remote.
    assert request() == approved
    assert slack.delivered == []


def test_slack_pending_is_idempotent_then_chat_supersedes_and_late_slack_reply_is_ignored(slack):
    response = owner_approval.challenge_for(SUBJECT)[1]

    first = request()
    second = request()

    assert first["status"] == second["status"] == "pending"
    assert first["channels"] == ["slack", "chat_in_session"]
    assert first["request_id"] == second["request_id"]
    assert first["expires_at"]
    assert len(slack.delivered) == 1
    assert slack.delivered[0].message.endswith("\nReply exactly: " + response)
    slack.thread = [owner_reply("looks fine"), owner_reply(response, user="USOMEONE", message_id="C123:3.0")]
    assert request()["status"] == "pending"

    approved = request(provider="chat_in_session", in_session_approval=response)

    assert approved["status"] == "approved" and approved["transport"] == "chat-in-session-owner-assertion"
    assert slack.closed == [(first["request_id"], owner_approval._SUPERSEDED_NOTICE)]
    slack.thread = [owner_reply(response)]
    late = human_input.poll_human_input_request(first["request_id"], transport=slack)
    assert late.status == "superseded" and late.response is None
    assert request() == approved
    assert len(slack.delivered) == 1


@pytest.mark.parametrize(("text", "status"), [("  {response}\n", "approved"), ("DECLINE", "declined")])
def test_first_valid_slack_reply_decides(slack, text, status):
    response = owner_approval.challenge_for(SUBJECT)[1]
    slack.thread = [owner_reply(text.format(response=response))]

    result = request()

    assert result["status"] == status
    if status == "approved":
        assert result["owner"] == "UOWNER"
        assert result["assurance"] == "remote_authenticated_slack_identity"
        assert result["transport"] == "slack"


def test_pending_slack_request_expires_after_its_deadline(slack, tmp_path):
    pending = request()
    with sqlite3.connect(tmp_path / "human-input.sqlite3") as connection:
        connection.execute("UPDATE human_input_requests SET deadline_at = ? WHERE request_id = ?",
                           (time.time() - 1, pending["request_id"]))

    expired = request()

    assert expired == {"status": "expired", "approval_response": pending["approval_response"],
                       "challenge_sha256": pending["challenge_sha256"]}
    assert human_input.get_human_input_request(pending["request_id"]).status == "expired"
    # A later request opens a fresh remote request.
    assert request()["request_id"] != pending["request_id"]
    assert len(slack.delivered) == 2


def test_no_remote_channel_requires_the_in_session_reply(tmp_path, monkeypatch):
    monkeypatch.setenv("ANCHOR_HUMAN_INPUT_STATE_PATH", str(tmp_path / "human-input.sqlite3"))
    monkeypatch.setattr(human_input_owner, "_is_windows", lambda: False)
    monkeypatch.delenv("DATABRICKS_RUNTIME_VERSION", raising=False)

    result = request()

    assert result["status"] == "approval_required"
    assert result["channels"] == ["chat_in_session"]
    monkeypatch.setenv("DATABRICKS_RUNTIME_VERSION", "serverless")
    assert request()["channels"] == ["databricks_in_session", "chat_in_session"]


def test_store_migrates_an_existing_database_additively(tmp_path):
    path = tmp_path / "human-input.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE human_input_requests (request_id TEXT PRIMARY KEY, message TEXT NOT NULL, "
            "status TEXT NOT NULL, created_at REAL NOT NULL, deadline_at REAL NOT NULL, delivered_at REAL, "
            "transport TEXT NOT NULL, transport_ref TEXT, response TEXT, response_user_id TEXT, "
            "response_message_id TEXT UNIQUE, responded_at REAL, last_error TEXT)"
        )
        connection.execute(
            "INSERT INTO human_input_requests VALUES ('old', 'Question', 'answered', 1, 2, 1, 'slack', "
            "'C1:1.0', 'yes', 'U1', 'C1:2.0', 1.5, NULL)"
        )

    store = human_input._RequestStore(path)

    old = store.get("old")
    assert old.response == "yes" and old.challenge is None and old.owner_id is None
    created = store.create("Question", 1, "slack", time.time(), challenge="APPROVE " + "a" * 64,
                           operation="test", owner_id="U1")
    assert store.find("APPROVE " + "a" * 64) == [created]
