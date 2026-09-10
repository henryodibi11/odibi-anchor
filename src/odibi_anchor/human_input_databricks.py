"""Lower-assurance single-user approval assertion for Databricks sessions."""
from __future__ import annotations

import time
from uuid import uuid4

from odibi_anchor.human_input import (
    HumanInputConfigurationError,
    HumanInputDeliveryError,
    HumanInputReply,
    HumanInputRequest,
)


class DatabricksInSessionOwnerTransport:
    """Record an exact response that the owner supplied in the current Genie session."""

    name = "databricks-in-session-owner-assertion"
    owner_user_id = "single-user-databricks-session"

    def __init__(self, response: str) -> None:
        if not isinstance(response, str) or not response.strip():
            raise HumanInputConfigurationError(
                "Databricks in-session approval must be a non-empty exact challenge response"
            )
        self._response = response.strip()
        self._responses: dict[str, list[HumanInputReply]] = {}

    def deliver(self, request: HumanInputRequest) -> str:
        if self._response not in request.message:
            raise HumanInputDeliveryError(
                "Databricks in-session approval does not match the current exact challenge"
            )
        reference = f"databricks-in-session:{request.request_id}:{uuid4()}"
        self._responses[reference] = [
            HumanInputReply(
                text=self._response,
                user_id=self.owner_user_id,
                message_id=f"{reference}:response",
                received_at=time.time(),
            )
        ]
        return reference

    def replies(self, transport_ref: str) -> list[HumanInputReply]:
        try:
            return list(self._responses[transport_ref])
        except KeyError as exc:
            raise HumanInputDeliveryError(
                "Unknown Databricks in-session owner approval reference"
            ) from exc


__all__ = ["DatabricksInSessionOwnerTransport"]
