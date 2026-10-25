"""One owner-approval rule for plan approval, delivery, revocation and memory promotion.

The challenge is ``VERB <64 lowercase hex>``: the SHA-256 of the exact canonical subject.
Channels open together and the first valid reply wins:

- Slack (remote, authenticated) is posted once per challenge and polled for at most
  ``wait_seconds``; until a reply arrives the result is ``pending``.
- In-session chat (``provider="chat_in_session"``) or Databricks in-session completes the
  same challenge with the exact reply the owner typed, superseding an open Slack request.
- Without Slack the result is ``approval_required``: the owner answers in the session. A
  blocking local dialog is never opened here, so an agent's call never pops up a window.

A challenge is single use: once one channel records the exact reply, later replies on any
channel are refused and the same approved record is returned again.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from typing import Any

from odibi_anchor import human_input, human_input_owner
from odibi_anchor.human_input import (
    MAX_TIMEOUT_MINUTES,
    HumanInputConfigurationError,
    HumanInputError,
    HumanInputRequest,
    HumanInputTimeout,
)
from odibi_anchor.human_input_slack import slack_environment_variables

DEFAULT_WAIT_SECONDS = 45
MAX_WAIT_SECONDS = 120
DEFAULT_TIMEOUT_MINUTES = 30
CHAT_IN_SESSION = "chat_in_session"
DATABRICKS_IN_SESSION = "databricks_in_session"
VERBS = ("APPROVE", "REVOKE")
DECLINE = "DECLINE"
_LOCAL_WINDOWS_TRANSPORT = "local-windows-owner-presence"
ASSURANCE_BY_TRANSPORT = {
    "slack": "remote_authenticated_slack_identity",
    "local-windows-owner-presence": "interactive_local_windows_account_presence",
    "databricks-in-session-owner-assertion": "lower_assurance_single_user_databricks_in_session_assertion",
    "chat-in-session-owner-assertion": "lower_assurance_single_user_chat_session_assertion",
}
_SUPERSEDED_NOTICE = (
    "This request was answered in another channel. It is closed; later replies here are ignored."
)


class OwnerApprovalMismatch(HumanInputError):
    """A reply did not match the exact challenge, owner or transport. Nothing was approved.

    ``mismatches`` names each failed check without echoing the received reply text.
    """

    def __init__(self, approval_response: str, mismatches: list[dict[str, Any]]) -> None:
        self.approval_response = approval_response
        self.mismatches = mismatches
        failed = "; ".join(item["check"] for item in mismatches)
        super().__init__(f"owner reply does not match the exact challenge (failed: {failed})")


def challenge_for(subject: dict, verb: str = "APPROVE") -> tuple[str, str]:
    """Return ``(hex, "VERB <hex>")`` for the SHA-256 of the canonical subject JSON."""
    if verb not in VERBS:
        raise ValueError(f"verb must be one of {', '.join(VERBS)}")
    if not isinstance(subject, dict) or not subject:
        raise ValueError("subject must be a non-empty JSON object")
    encoded = json.dumps(subject, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    hex_digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return hex_digest, f"{verb} {hex_digest}"


def _text_mismatch(received: str, approval_response: str) -> list[dict[str, Any]]:
    if received == approval_response:
        return []
    return [{
        "check": "reply_text",
        "detail": "the reply must be exactly the challenge line, with nothing added or removed",
        "received_length": len(received), "expected_length": len(approval_response),
        "matches_after_trimming_whitespace": received.strip() == approval_response,
    }]


def reply_mismatches(record: Any, approval_response: str, *, expected_owner_id: str,
                     transport_name: str) -> list[dict[str, Any]]:
    """Name every check a reply record fails; an empty list means it is the exact approval."""
    received = record.response if isinstance(getattr(record, "response", None), str) else ""
    mismatches = _text_mismatch(received, approval_response)
    if getattr(record, "response_user_id", None) != expected_owner_id:
        mismatches.append({"check": "owner", "detail": "the reply came from a different user than the configured owner",
                           "expected_owner_id": expected_owner_id})
    if getattr(record, "transport", None) != transport_name:
        mismatches.append({"check": "transport", "detail": "the reply arrived through a different transport",
                           "expected_transport": transport_name})
    if not getattr(record, "request_id", None) or not getattr(record, "response_message_id", None):
        mismatches.append({"check": "message_ids", "detail": "the reply is missing its request or message id"})
    return mismatches


def _databricks_runtime() -> bool:
    return not human_input_owner._is_windows() and bool(os.environ.get("DATABRICKS_RUNTIME_VERSION"))


def _in_session_channels() -> list[str]:
    return [DATABRICKS_IN_SESSION, CHAT_IN_SESSION] if _databricks_runtime() else [CHAT_IN_SESSION]


def _expires_at(record: HumanInputRequest) -> str:
    return datetime.fromtimestamp(record.deadline_at, UTC).isoformat()


def _approved(record: Any, base: dict[str, Any], assurance: str | None) -> dict[str, Any]:
    assurance = assurance or ASSURANCE_BY_TRANSPORT.get(record.transport)
    if assurance is None:
        raise HumanInputConfigurationError(f"No owner assurance is defined for transport {record.transport!r}")
    return {"status": "approved", **base, "owner": record.response_user_id, "assurance": assurance,
            "transport": record.transport, "authority_ref": record.request_id,
            "response_message_id": record.response_message_id}


def _consumed(approval_response: str) -> HumanInputRequest | None:
    """Return the request that already consumed this challenge, if any."""
    for record in human_input.find_human_input_requests(approval_response):
        if record.status == "answered" and record.response == approval_response:
            return record
    return None


def _supersede(approval_response: str, *, except_request_id: str | None) -> None:
    """Close every other open request for this challenge; Slack gets a thread reply."""
    if not any(record.status in {"created", "pending"} and record.request_id != except_request_id
               for record in human_input.find_human_input_requests(approval_response)):
        return
    try:
        transport = human_input_owner.select_owner_approval_provider().transport
    except HumanInputError:
        transport = None
    human_input.supersede_human_input_requests(
        approval_response, reason=_SUPERSEDED_NOTICE, except_request_id=except_request_id,
        transport=transport if getattr(transport, "resumable", False) else None,
    )


def _validate(operation: str, prompt: str, provider: str | None, in_session_approval: str | None,
              wait_seconds: int, timeout_minutes: int) -> None:
    if not isinstance(operation, str) or not operation.strip():
        raise ValueError("operation must be a non-empty string")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    if provider not in (None, CHAT_IN_SESSION, DATABRICKS_IN_SESSION):
        raise ValueError("provider must be omitted, 'chat_in_session' or 'databricks_in_session'")
    if in_session_approval is not None and not isinstance(in_session_approval, str):
        raise ValueError("in_session_approval must be the exact challenge response string")
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= MAX_WAIT_SECONDS:
        raise ValueError(f"wait_seconds must be an integer from 0 to {MAX_WAIT_SECONDS}")
    if type(timeout_minutes) is not int or not 1 <= timeout_minutes <= MAX_TIMEOUT_MINUTES:
        raise ValueError(f"timeout_minutes must be an integer from 1 to {MAX_TIMEOUT_MINUTES}")


def request_owner_approval(
    *,
    operation: str,
    subject: dict,
    prompt: str,
    verb: str = "APPROVE",
    provider: str | None = None,
    in_session_approval: str | None = None,
    wait_seconds: int = DEFAULT_WAIT_SECONDS,
    timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES,
) -> dict:
    """Ask the owner to approve one exact subject without blocking for long.

    Returns a dict whose ``status`` is ``approved`` (with ``owner``, ``assurance``,
    ``transport``, ``authority_ref`` and ``response_message_id``), ``pending`` (a remote
    request is open), ``approval_required`` (only an in-session reply can answer),
    ``expired`` or ``declined``. Every result carries ``approval_response`` (the exact
    challenge line) and ``challenge_sha256``. Raises :class:`OwnerApprovalMismatch` for an
    in-session or local reply that is not the exact challenge from the owner.
    """
    _validate(operation, prompt, provider, in_session_approval, wait_seconds, timeout_minutes)
    challenge_sha256, approval_response = challenge_for(subject, verb)
    message = prompt.strip() + "\nReply exactly: " + approval_response
    base = {"approval_response": approval_response, "challenge_sha256": challenge_sha256}

    if provider is not None or in_session_approval is not None:
        if provider is None:
            if not _databricks_runtime():
                raise HumanInputConfigurationError(
                    "in_session_approval requires provider='chat_in_session' (or 'databricks_in_session' "
                    "on Databricks)"
                )
            provider = DATABRICKS_IN_SESSION
        if in_session_approval is None:
            return {"status": "approval_required", **base,
                    "channels": [provider], "expires_at": None}
        if in_session_approval != approval_response:
            raise OwnerApprovalMismatch(approval_response, _text_mismatch(in_session_approval, approval_response))
        consumed = _consumed(approval_response)
        if consumed is not None:
            return _approved(consumed, base, None)
        selected = human_input_owner.select_owner_approval_provider(
            provider=provider, in_session_approval=in_session_approval,
        )
        record = human_input.open_human_input_request(
            message, timeout_minutes, challenge=approval_response, operation=operation,
            owner_id=selected.expected_owner_id, transport=selected.transport,
        )
        record = human_input.poll_human_input_request(
            record.request_id, transport=selected.transport,
            accept=lambda reply: reply.text == approval_response,
        )
        if record.status != "answered":
            consumed = _consumed(approval_response)
            if consumed is None:
                raise HumanInputError(f"In-session approval could not be recorded (status {record.status})")
            return _approved(consumed, base, None)
        _supersede(approval_response, except_request_id=record.request_id)
        return _approved(record, base, selected.assurance)

    consumed = _consumed(approval_response)
    if consumed is not None:
        return _approved(consumed, base, None)
    try:
        selected = human_input_owner.select_owner_approval_provider()
    except HumanInputConfigurationError:
        if any(os.environ.get(name) for name in slack_environment_variables()):
            raise
        return {"status": "approval_required", **base, "channels": _in_session_channels(), "expires_at": None}

    if getattr(selected.transport, "resumable", False):
        record = human_input.open_human_input_request(
            message, timeout_minutes, challenge=approval_response, operation=operation,
            owner_id=selected.expected_owner_id, transport=selected.transport,
        )
        record = human_input.poll_human_input_request(
            record.request_id, transport=selected.transport, wait_seconds=wait_seconds,
            accept=lambda reply: reply.text.strip() in {approval_response, DECLINE},
        )
        if record.status == "answered" and record.response == approval_response:
            _supersede(approval_response, except_request_id=record.request_id)
            return _approved(record, base, selected.assurance)
        if record.status == "answered":
            return {"status": "declined", **base}
        if record.status == "expired":
            return {"status": "expired", **base}
        consumed = _consumed(approval_response)
        if consumed is not None:
            return _approved(consumed, base, None)
        if record.status != "pending":
            raise HumanInputError(f"Owner approval request ended as {record.status}")
        return {"status": "pending", **base, "channels": [selected.transport.name, *_in_session_channels()],
                "expires_at": _expires_at(record), "request_id": record.request_id}

    if selected.transport.name == _LOCAL_WINDOWS_TRANSPORT:
        # The local dialog would hold the agent's call and pop up on the desktop; without a
        # remote channel the owner answers in the session instead.
        return {"status": "approval_required", **base, "channels": _in_session_channels(), "expires_at": None}

    try:
        record = human_input.request_human_input_record(
            message, timeout_minutes=timeout_minutes, transport=selected.transport,
            challenge=approval_response, operation=operation, owner_id=selected.expected_owner_id,
        )
    except HumanInputTimeout:
        return {"status": "expired", **base}
    if record.response == DECLINE and record.response_user_id == selected.expected_owner_id:
        return {"status": "declined", **base}
    mismatches = reply_mismatches(record, approval_response, expected_owner_id=selected.expected_owner_id,
                                  transport_name=selected.transport.name)
    if mismatches:
        raise OwnerApprovalMismatch(approval_response, mismatches)
    _supersede(approval_response, except_request_id=record.request_id)
    return _approved(record, base, selected.assurance)


__all__ = [
    "ASSURANCE_BY_TRANSPORT",
    "CHAT_IN_SESSION",
    "DATABRICKS_IN_SESSION",
    "DEFAULT_TIMEOUT_MINUTES",
    "DEFAULT_WAIT_SECONDS",
    "OwnerApprovalMismatch",
    "challenge_for",
    "reply_mismatches",
    "request_owner_approval",
]
