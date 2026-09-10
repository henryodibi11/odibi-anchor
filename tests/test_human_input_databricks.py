"""Tests for the lower-assurance Databricks in-session owner assertion."""
from __future__ import annotations

import time

import pytest

from odibi_anchor.human_input import (
    HumanInputConfigurationError,
    HumanInputDeliveryError,
    HumanInputRequest,
)
from odibi_anchor.human_input_databricks import DatabricksInSessionOwnerTransport
from odibi_anchor.human_input_owner import (
    databricks_in_session_preparation_available,
    owner_approval_provider_status,
    select_owner_approval_provider,
)

_CHALLENGE = "b" * 64


def _request() -> HumanInputRequest:
    now = time.time()
    return HumanInputRequest(
        request_id="request-db-1",
        message=f"Claim: exact.\n\nReply exactly: APPROVE {_CHALLENGE}",
        status="created",
        created_at=now,
        deadline_at=now + 60,
        delivered_at=None,
        transport="databricks-in-session-owner-assertion",
        transport_ref=None,
        response=None,
        response_user_id=None,
        response_message_id=None,
        responded_at=None,
        last_error=None,
    )


def test_databricks_transport_records_only_the_supplied_exact_response():
    response = f"APPROVE {_CHALLENGE}"
    transport = DatabricksInSessionOwnerTransport(response)

    reference = transport.deliver(_request())
    reply = transport.replies(reference)[0]

    assert reply.text == response
    assert reply.user_id == "single-user-databricks-session"
    assert reference.startswith("databricks-in-session:request-db-1:")


def test_databricks_transport_rejects_a_response_for_another_challenge():
    transport = DatabricksInSessionOwnerTransport(f"APPROVE {'c' * 64}")

    with pytest.raises(HumanInputDeliveryError, match="current exact challenge"):
        transport.deliver(_request())


def test_databricks_provider_is_explicitly_lower_assurance(monkeypatch):
    for name in ("ANCHOR_SLACK_BOT_TOKEN", "ANCHOR_SLACK_CHANNEL_ID", "ANCHOR_SLACK_USER_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABRICKS_RUNTIME_VERSION", "serverless")
    monkeypatch.setattr("odibi_anchor.human_input_owner._is_windows", lambda: False)

    assert databricks_in_session_preparation_available() is True
    provider = select_owner_approval_provider(
        in_session_approval=f"APPROVE {_CHALLENGE}",
    )

    assert provider.transport.name == "databricks-in-session-owner-assertion"
    assert provider.expected_owner_id == "single-user-databricks-session"
    assert provider.assurance == "lower_assurance_single_user_databricks_in_session_assertion"
    status = owner_approval_provider_status()
    assert status["available"] is True
    assert status["configured"] is False
    assert status["transport"] == "databricks-in-session-owner-assertion"


def test_explicit_databricks_provider_can_override_complete_slack(monkeypatch):
    monkeypatch.setenv("ANCHOR_SLACK_BOT_TOKEN", "token")
    monkeypatch.setenv("ANCHOR_SLACK_CHANNEL_ID", "C123")
    monkeypatch.setenv("ANCHOR_SLACK_USER_ID", "U123")
    monkeypatch.setenv("DATABRICKS_RUNTIME_VERSION", "serverless")
    monkeypatch.setattr("odibi_anchor.human_input_owner._is_windows", lambda: False)

    assert databricks_in_session_preparation_available(
        provider="databricks_in_session",
    ) is True
    selected = select_owner_approval_provider(
        provider="databricks_in_session",
        in_session_approval=f"APPROVE {_CHALLENGE}",
    )
    default = select_owner_approval_provider()

    assert selected.transport.name == "databricks-in-session-owner-assertion"
    assert default.transport.name == "slack"


@pytest.mark.parametrize("provider", ("slack", "automatic", ""))
def test_unsupported_explicit_provider_fails_closed(monkeypatch, provider):
    monkeypatch.setenv("DATABRICKS_RUNTIME_VERSION", "serverless")

    with pytest.raises(HumanInputConfigurationError, match="must be omitted or"):
        select_owner_approval_provider(
            provider=provider,
            in_session_approval=f"APPROVE {_CHALLENGE}",
        )


def test_explicit_databricks_provider_requires_databricks_runtime(monkeypatch):
    monkeypatch.delenv("DATABRICKS_RUNTIME_VERSION", raising=False)
    monkeypatch.setattr("odibi_anchor.human_input_owner._is_windows", lambda: False)

    with pytest.raises(HumanInputConfigurationError, match="requires a Databricks runtime"):
        select_owner_approval_provider(
            provider="databricks_in_session",
            in_session_approval=f"APPROVE {_CHALLENGE}",
        )


def test_non_databricks_linux_does_not_gain_an_in_session_lane(monkeypatch):
    for name in (
        "ANCHOR_SLACK_BOT_TOKEN", "ANCHOR_SLACK_CHANNEL_ID", "ANCHOR_SLACK_USER_ID",
        "DATABRICKS_RUNTIME_VERSION",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("odibi_anchor.human_input_owner._is_windows", lambda: False)

    assert databricks_in_session_preparation_available() is False
    with pytest.raises(HumanInputConfigurationError, match="requires complete Slack"):
        select_owner_approval_provider(in_session_approval=f"APPROVE {_CHALLENGE}")
    with pytest.raises(HumanInputDeliveryError):
        DatabricksInSessionOwnerTransport(f"APPROVE {_CHALLENGE}").replies("unknown")
