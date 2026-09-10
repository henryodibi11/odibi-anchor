"""Provider selection for governed owner-memory authority."""
from __future__ import annotations

import os
from dataclasses import dataclass

from odibi_anchor.human_input import (
    HumanInputConfigurationError,
    HumanInputTransport,
)

_SLACK_VARIABLES = (
    "ANCHOR_SLACK_BOT_TOKEN",
    "ANCHOR_SLACK_CHANNEL_ID",
    "ANCHOR_SLACK_USER_ID",
)
_DATABRICKS_IN_SESSION_PROVIDER = "databricks_in_session"


@dataclass(frozen=True)
class OwnerApprovalProvider:
    """Selected transport plus the identity and assurance it establishes."""

    transport: HumanInputTransport
    expected_owner_id: str
    assurance: str


def _is_windows() -> bool:
    return os.name == "nt"


def databricks_in_session_preparation_available(*, provider: str | None = None) -> bool:
    """Return whether Databricks can prepare an exact in-session challenge."""
    if provider is not None and provider != _DATABRICKS_IN_SESSION_PROVIDER:
        raise HumanInputConfigurationError(
            "Owner approval provider must be omitted or 'databricks_in_session'"
        )
    return (
        not _is_windows()
        and bool(os.environ.get("DATABRICKS_RUNTIME_VERSION"))
        and (
            provider == _DATABRICKS_IN_SESSION_PROVIDER
            or not any(os.environ.get(name) for name in _SLACK_VARIABLES)
        )
    )


def select_owner_approval_provider(
    *, provider: str | None = None, in_session_approval: str | None = None,
) -> OwnerApprovalProvider:
    """Prefer Slack, then an explicit local Windows or Databricks owner action."""
    if provider is not None and provider != _DATABRICKS_IN_SESSION_PROVIDER:
        raise HumanInputConfigurationError(
            "Owner approval provider must be omitted or 'databricks_in_session'"
        )
    # PRE-CODE GATE: provider selection grants no authority; the exact bound
    # challenge must still be supplied through the selected transport.
    if provider == _DATABRICKS_IN_SESSION_PROVIDER:
        if _is_windows() or not os.environ.get("DATABRICKS_RUNTIME_VERSION"):
            raise HumanInputConfigurationError(
                "The databricks_in_session owner approval provider requires a Databricks runtime"
            )
        if in_session_approval is None:
            raise HumanInputConfigurationError(
                "Databricks owner promotion requires the exact prepared in-session challenge response"
            )
        from odibi_anchor.human_input_databricks import DatabricksInSessionOwnerTransport

        transport = DatabricksInSessionOwnerTransport(in_session_approval)
        return OwnerApprovalProvider(
            transport=transport,
            expected_owner_id=transport.owner_user_id,
            assurance="lower_assurance_single_user_databricks_in_session_assertion",
        )
    configured = tuple(bool(os.environ.get(name)) for name in _SLACK_VARIABLES)
    if any(configured):
        if not all(configured):
            missing = [
                name for name, present in zip(_SLACK_VARIABLES, configured, strict=True)
                if not present
            ]
            raise HumanInputConfigurationError(
                "Incomplete Slack owner approval configuration: " + ", ".join(missing)
            )
        if in_session_approval is not None:
            raise HumanInputConfigurationError(
                "Databricks in-session approval cannot override configured Slack owner identity"
            )
        from odibi_anchor.human_input_slack import SlackHumanInputTransport

        transport = SlackHumanInputTransport.from_environment()
        return OwnerApprovalProvider(
            transport=transport,
            expected_owner_id=transport.allowed_user_id,
            assurance="remote_authenticated_slack_identity",
        )
    if _is_windows():
        from odibi_anchor.human_input_windows import LocalWindowsOwnerTransport

        transport = LocalWindowsOwnerTransport.from_current_session()
        return OwnerApprovalProvider(
            transport=transport,
            expected_owner_id=transport.owner_user_id,
            assurance="interactive_local_windows_account_presence",
        )
    if os.environ.get("DATABRICKS_RUNTIME_VERSION"):
        if in_session_approval is None:
            raise HumanInputConfigurationError(
                "Databricks owner promotion requires the exact prepared in-session challenge response"
            )
        from odibi_anchor.human_input_databricks import DatabricksInSessionOwnerTransport

        transport = DatabricksInSessionOwnerTransport(in_session_approval)
        return OwnerApprovalProvider(
            transport=transport,
            expected_owner_id=transport.owner_user_id,
            assurance="lower_assurance_single_user_databricks_in_session_assertion",
        )
    raise HumanInputConfigurationError(
        "Owner promotion requires complete Slack configuration, an interactive Windows session, "
        "or an explicit single-user Databricks in-session approval"
    )


def owner_approval_provider_status() -> dict[str, object]:
    """Report provider capability without exposing credentials or opening a prompt."""
    if databricks_in_session_preparation_available():
        return {
            "available": True,
            "configured": False,
            "transport": "databricks-in-session-owner-assertion",
            "assurance": "lower_assurance_single_user_databricks_in_session_assertion",
            "reason": "An exact challenge and a separate explicit in-session response are required",
        }
    try:
        provider = select_owner_approval_provider()
    except HumanInputConfigurationError as exc:
        return {
            "available": False,
            "configured": False,
            "transport": None,
            "assurance": None,
            "reason": str(exc),
        }
    return {
        "available": True,
        "configured": True,
        "transport": provider.transport.name,
        "assurance": provider.assurance,
        "reason": None,
    }


__all__ = [
    "OwnerApprovalProvider",
    "databricks_in_session_preparation_available",
    "owner_approval_provider_status",
    "select_owner_approval_provider",
]
