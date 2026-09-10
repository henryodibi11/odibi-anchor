"""Tests for local owner-presence provider selection and Windows transport."""
from __future__ import annotations

import importlib
import time

import pytest

_CHALLENGE = "a" * 64


def _modules():
    return (
        importlib.import_module("odibi_anchor.human_input"),
        importlib.import_module("odibi_anchor.human_input_owner"),
        importlib.import_module("odibi_anchor.human_input_windows"),
    )


def _request():
    human_input, _, _ = _modules()
    now = time.time()
    return human_input.HumanInputRequest(
        request_id="request-1",
        message=f"Claim: keep exact evidence.\n\nReply exactly: APPROVE {_CHALLENGE}",
        status="created",
        created_at=now,
        deadline_at=now + 60,
        delivered_at=None,
        transport="local-windows-owner-presence",
        transport_ref=None,
        response=None,
        response_user_id=None,
        response_message_id=None,
        responded_at=None,
        last_error=None,
    )


@pytest.mark.parametrize(("dialog_result", "response"), [(6, f"APPROVE {_CHALLENGE}"), (7, "DECLINE")])
def test_local_windows_transport_records_explicit_yes_or_no(monkeypatch, dialog_result, response):
    _, _, windows = _modules()
    displayed = []
    monkeypatch.setattr(windows, "_message_box", lambda message, timeout: displayed.append((message, timeout)) or dialog_result)
    transport = windows.LocalWindowsOwnerTransport("windows-account:Henry")

    reference = transport.deliver(_request())
    replies = transport.replies(reference)

    assert replies[0].text == response
    assert replies[0].user_id == "windows-account:Henry"
    assert "keep exact evidence" in displayed[0][0]
    assert "personally approve" in displayed[0][0]
    assert 0 < displayed[0][1] <= 60_000


def test_local_windows_transport_timeout_grants_no_reply(monkeypatch):
    _, _, windows = _modules()
    monkeypatch.setattr(windows, "_message_box", lambda *_args: 32_000)
    transport = windows.LocalWindowsOwnerTransport("windows-account:Henry")

    reference = transport.deliver(_request())

    assert transport.replies(reference) == []


def test_local_windows_transport_rejects_unbound_or_unknown_requests():
    human_input, _, windows = _modules()
    transport = windows.LocalWindowsOwnerTransport("windows-account:Henry")
    request = _request()
    unbound = human_input.HumanInputRequest(**{**request.__dict__, "message": "Approve this"})

    with pytest.raises(human_input.HumanInputDeliveryError, match="no exact approval challenge"):
        transport.deliver(unbound)
    with pytest.raises(human_input.HumanInputDeliveryError, match="Unknown local owner"):
        transport.replies("forged")


def test_owner_provider_prefers_complete_slack_configuration(monkeypatch):
    _, owner, _ = _modules()
    monkeypatch.setenv("ANCHOR_SLACK_BOT_TOKEN", "token")
    monkeypatch.setenv("ANCHOR_SLACK_CHANNEL_ID", "C123")
    monkeypatch.setenv("ANCHOR_SLACK_USER_ID", "U123")

    provider = owner.select_owner_approval_provider()

    assert provider.transport.name == "slack"
    assert provider.expected_owner_id == "U123"
    assert provider.assurance == "remote_authenticated_slack_identity"


def test_owner_provider_uses_local_windows_presence_without_slack(monkeypatch):
    _, owner, windows = _modules()
    for name in ("ANCHOR_SLACK_BOT_TOKEN", "ANCHOR_SLACK_CHANNEL_ID", "ANCHOR_SLACK_USER_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(owner, "_is_windows", lambda: True)
    monkeypatch.setattr(windows, "_windows_username", lambda: "Henry")

    provider = owner.select_owner_approval_provider()

    assert provider.transport.name == "local-windows-owner-presence"
    assert provider.expected_owner_id == "windows-account:Henry"
    assert provider.assurance == "interactive_local_windows_account_presence"


def test_owner_provider_rejects_partial_slack_instead_of_silently_falling_back(monkeypatch):
    human_input, owner, _ = _modules()
    monkeypatch.setenv("ANCHOR_SLACK_USER_ID", "U123")
    monkeypatch.delenv("ANCHOR_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ANCHOR_SLACK_CHANNEL_ID", raising=False)

    with pytest.raises(human_input.HumanInputConfigurationError, match="Incomplete Slack"):
        owner.select_owner_approval_provider()


def test_owner_provider_does_not_downgrade_configured_slack(monkeypatch):
    human_input, owner, _ = _modules()
    monkeypatch.setenv("ANCHOR_SLACK_BOT_TOKEN", "token")
    monkeypatch.setenv("ANCHOR_SLACK_CHANNEL_ID", "C123")
    monkeypatch.setenv("ANCHOR_SLACK_USER_ID", "U123")

    with pytest.raises(
        human_input.HumanInputConfigurationError,
        match="cannot override configured Slack",
    ):
        owner.select_owner_approval_provider(
            in_session_approval=f"APPROVE {_CHALLENGE}",
        )
