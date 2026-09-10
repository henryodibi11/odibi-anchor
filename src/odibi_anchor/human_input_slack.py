"""Slack Web API transport for portable human-input requests."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any
from urllib import error, parse, request

from odibi_anchor.human_input import (
    HumanInputConfigurationError,
    HumanInputDeliveryError,
    HumanInputReply,
    HumanInputRequest,
    TemporaryHumanInputTransportError,
)

SLACK_API_BASE_URL = "https://slack.com/api"
_TRANSIENT_SLACK_ERRORS = {
    "fatal_error",
    "internal_error",
    "ratelimited",
    "request_timeout",
    "service_unavailable",
}


class _NoRedirectHandler(request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


_urlopen = request.build_opener(_NoRedirectHandler()).open


def _decode_slack_text(value: str) -> str:
    return value.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


class SlackHumanInputTransport:
    """Deliver questions and collect authenticated threaded replies through Slack."""

    name = "slack"

    def __init__(
        self,
        *,
        bot_token: str,
        channel_id: str,
        allowed_user_id: str,
        api_base_url: str = SLACK_API_BASE_URL,
        request_timeout_seconds: float = 30,
    ) -> None:
        values = {
            "bot_token": bot_token,
            "channel_id": channel_id,
            "allowed_user_id": allowed_user_id,
        }
        for name, value in values.items():
            if not isinstance(value, str) or not value.strip():
                raise HumanInputConfigurationError(f"{name} must be a non-empty string")
        if not re.fullmatch(r"[CG][A-Z0-9]+", channel_id.strip()):
            raise HumanInputConfigurationError("channel_id must be a Slack channel ID")
        if not re.fullmatch(r"[UW][A-Z0-9]+", allowed_user_id.strip()):
            raise HumanInputConfigurationError("allowed_user_id must be a Slack member ID")
        if not api_base_url.startswith("https://"):
            raise HumanInputConfigurationError("api_base_url must use HTTPS")
        if request_timeout_seconds <= 0:
            raise HumanInputConfigurationError("request_timeout_seconds must be greater than 0")
        self._bot_token = bot_token.strip()
        self.channel_id = channel_id.strip()
        self.allowed_user_id = allowed_user_id.strip()
        self._api_base_url = api_base_url.rstrip("/")
        self._request_timeout_seconds = request_timeout_seconds

    @classmethod
    def from_environment(cls) -> SlackHumanInputTransport:
        """Create the Slack transport from Odibi Anchor environment variables."""
        variables = {
            "bot_token": "ANCHOR_SLACK_BOT_TOKEN",
            "channel_id": "ANCHOR_SLACK_CHANNEL_ID",
            "allowed_user_id": "ANCHOR_SLACK_USER_ID",
        }
        missing = [environment for environment in variables.values() if not os.environ.get(environment)]
        if missing:
            raise HumanInputConfigurationError(
                "Missing Slack configuration: " + ", ".join(sorted(missing))
            )
        return cls(**{name: os.environ[environment] for name, environment in variables.items()})

    def deliver(self, human_request: HumanInputRequest) -> str:
        self._validate_channel()
        deadline = datetime.fromtimestamp(human_request.deadline_at, timezone.utc).isoformat()
        text = (
            f"<@{self.allowed_user_id}> :question: *Odibi Anchor needs your input*\n\n"
            f"{human_request.message}\n\n"
            "Reply in this message's thread. The first reply from the configured user wins.\n"
            f"Request: `{human_request.request_id}`\nDeadline: `{deadline}`"
        )
        return self._post_message(text, human_request.request_id)

    def notify(self, message: str, notification_id: str) -> str:
        """Deliver one terminal agent notification without waiting for a reply."""
        self._validate_channel()
        text = (
            f"<@{self.allowed_user_id}> :bell: *Odibi Anchor agent finished*\n\n"
            f"{message}\n\nNotification: `{notification_id}`"
        )
        return self._post_message(text, notification_id)

    def _validate_channel(self) -> None:
        """Require a private, workspace-only Slack channel before posting."""
        conversation = self._api_call(
            "conversations.info", {"channel": self.channel_id}, method="GET"
        ).get("channel")
        if (
            not isinstance(conversation, dict)
            or conversation.get("id") != self.channel_id
            or conversation.get("is_channel") is not True
            or conversation.get("is_private") is not True
            or conversation.get("is_ext_shared") is True
            or conversation.get("is_org_shared") is True
        ):
            raise HumanInputConfigurationError(
                "ANCHOR_SLACK_CHANNEL_ID must identify a private, workspace-only channel"
            )

    def _post_message(self, text: str, client_message_id: str) -> str:
        """Post one message and return its opaque channel/timestamp reference."""
        result = self._api_call(
            "chat.postMessage",
            {
                "channel": self.channel_id,
                "text": text,
                "client_msg_id": client_message_id,
                "unfurl_links": False,
                "unfurl_media": False,
            },
        )
        timestamp = result.get("ts")
        channel = result.get("channel")
        if not isinstance(timestamp, str) or not timestamp or channel != self.channel_id:
            raise HumanInputDeliveryError("Slack returned an invalid delivery confirmation")
        return f"{self.channel_id}:{timestamp}"

    def replies(self, transport_ref: str) -> list[HumanInputReply]:
        try:
            channel, thread_timestamp = transport_ref.split(":", 1)
            float(thread_timestamp)
        except (TypeError, ValueError) as exc:
            raise HumanInputDeliveryError("Invalid Slack delivery reference") from exc
        if channel != self.channel_id:
            raise HumanInputDeliveryError("Slack delivery reference does not match the configured channel")
        replies: list[HumanInputReply] = []
        cursor = ""
        while True:
            payload = {"channel": channel, "ts": thread_timestamp, "limit": 100}
            if cursor:
                payload["cursor"] = cursor
            result = self._api_call("conversations.replies", payload, method="GET")
            messages = result.get("messages")
            if not isinstance(messages, list):
                raise HumanInputDeliveryError("Slack returned an invalid replies response")
            for message in messages:
                if not isinstance(message, dict):
                    continue
                message_id = message.get("ts")
                user_id = message.get("user")
                text = message.get("text")
                if (
                    message_id == thread_timestamp
                    or user_id != self.allowed_user_id
                    or not isinstance(message_id, str)
                    or not isinstance(text, str)
                ):
                    continue
                try:
                    received_at = float(message_id)
                    edited = message.get("edited")
                    if edited is not None:
                        if not isinstance(edited, dict) or not isinstance(edited.get("ts"), str):
                            continue
                        received_at = max(received_at, float(edited["ts"]))
                except ValueError:
                    continue
                replies.append(
                    HumanInputReply(
                        text=_decode_slack_text(text),
                        user_id=user_id,
                        message_id=f"{channel}:{message_id}",
                        received_at=received_at,
                    )
                )
            response_metadata = result.get("response_metadata", {})
            if not isinstance(response_metadata, dict):
                raise HumanInputDeliveryError("Slack returned invalid pagination metadata")
            cursor = response_metadata.get("next_cursor", "")
            if not isinstance(cursor, str) or not cursor:
                break
        return replies

    def _api_call(self, method_name: str, payload: dict[str, Any], *, method: str = "POST") -> dict[str, Any]:
        url = f"{self._api_base_url}/{method_name}"
        body: bytes | None
        headers = {"Authorization": f"Bearer {self._bot_token}"}
        if method == "GET":
            url = f"{url}?{parse.urlencode(payload)}"
            body = None
        else:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        slack_request = request.Request(url, data=body, headers=headers, method=method)
        try:
            with _urlopen(slack_request, timeout=self._request_timeout_seconds) as response:
                document = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After")
                try:
                    retry_seconds = float(retry_after) if retry_after is not None else None
                except ValueError:
                    retry_seconds = None
                raise TemporaryHumanInputTransportError(
                    "Slack rate limited response polling", retry_after_seconds=retry_seconds
                ) from exc
            if 500 <= exc.code < 600:
                raise TemporaryHumanInputTransportError(
                    f"Slack temporarily returned HTTP {exc.code}"
                ) from exc
            raise HumanInputDeliveryError(f"Slack returned HTTP {exc.code}") from exc
        except (error.URLError, TimeoutError) as exc:
            raise TemporaryHumanInputTransportError("Slack is temporarily unreachable") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HumanInputDeliveryError("Slack returned an invalid JSON response") from exc
        if not isinstance(document, dict):
            raise HumanInputDeliveryError("Slack returned an invalid API response")
        if document.get("ok") is not True:
            slack_error = document.get("error", "unknown_error")
            if slack_error in _TRANSIENT_SLACK_ERRORS:
                raise TemporaryHumanInputTransportError(
                    f"Slack temporarily rejected the request: {slack_error}"
                )
            raise HumanInputDeliveryError(f"Slack API rejected the request: {slack_error}")
        return document


__all__ = ["SlackHumanInputTransport"]
