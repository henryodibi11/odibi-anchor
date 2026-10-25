"""Lower-assurance single-user approval assertion typed in the agent's chat session."""
from __future__ import annotations

import re
import time
from uuid import uuid4

from odibi_anchor.human_input import (
    HumanInputConfigurationError,
    HumanInputDeliveryError,
    HumanInputReply,
    HumanInputRequest,
)

_CHALLENGE_PATTERN = re.compile(r"(?:^|\n)Reply exactly: ((?:APPROVE|REVOKE) [0-9a-f]{64})$")


class ChatInSessionOwnerTransport:
    """Record the exact reply the host saw the owner type in the current chat session."""

    name = "chat-in-session-owner-assertion"
    owner_user_id = "single-user-chat-session"

    def __init__(self, response: str) -> None:
        if not isinstance(response, str) or not response.strip():
            raise HumanInputConfigurationError(
                "Chat in-session approval must be the non-empty exact challenge response"
            )
        self._response = response
        self._responses: dict[str, list[HumanInputReply]] = {}

    def deliver(self, request: HumanInputRequest) -> str:
        match = _CHALLENGE_PATTERN.search(request.message)
        if match is None or match.group(1) != self._response:
            raise HumanInputDeliveryError(
                "Chat in-session approval does not match the current exact challenge"
            )
        reference = f"chat-in-session:{request.request_id}:{uuid4()}"
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
            raise HumanInputDeliveryError("Unknown chat in-session owner approval reference") from exc


__all__ = ["ChatInSessionOwnerTransport"]
