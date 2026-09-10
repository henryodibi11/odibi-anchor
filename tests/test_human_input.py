"""Focused tests for the portable human-input lifecycle and Slack transport."""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from urllib import error

import pytest

from odibi_anchor import (
    HumanInputConfigurationError,
    HumanInputDeliveryError,
    HumanInputTimeout,
    get_human_input_request,
    notify_human,
    request_human_input,
    request_human_input_record,
)
from odibi_anchor.human_input import (
    HumanInputReply,
    TemporaryHumanInputTransportError,
    _request_human_input,
    _RequestStore,
)
from odibi_anchor.human_input_slack import SlackHumanInputTransport


class FakeClock:
    def __init__(self, value=1_000.0):
        self.value = value
        self.sleeps = []

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


class FakeTransport:
    name = "fake"

    def __init__(self, batches=None, delivery_error=None):
        self.batches = list(batches or [])
        self.delivery_error = delivery_error
        self.request = None
        self.polls = 0

    def deliver(self, request):
        self.request = request
        if self.delivery_error:
            raise self.delivery_error
        return "fake:root"

    def replies(self, _transport_ref):
        self.polls += 1
        return self.batches.pop(0) if self.batches else []


class FakeNotificationTransport:
    name = "fake"

    def __init__(self, delivery_error=None, transport_ref="fake:notification"):
        self.delivery_error = delivery_error
        self.transport_ref = transport_ref
        self.notifications = []

    def notify(self, message, notification_id):
        self.notifications.append((message, notification_id))
        if self.delivery_error:
            raise self.delivery_error
        return self.transport_ref


def reply(text, *, message_id="1001.0", received_at=1_001.0):
    return HumanInputReply(
        text=text,
        user_id="UOWNER",
        message_id=message_id,
        received_at=received_at,
    )


def test_request_returns_first_valid_response_and_persists_timing(tmp_path):
    clock = FakeClock()
    transport = FakeTransport(
        batches=[[], [reply("  first answer  "), reply("second", message_id="1002.0", received_at=1_002.0)]]
    )
    state = tmp_path / "private" / "human-input.sqlite3"

    result = _request_human_input(
        "Choose a path",
        timeout_minutes=1,
        poll_interval_seconds=1,
        state_path=state,
        transport=transport,
        clock=clock,
        monotonic=clock,
        sleep=clock.sleep,
    )

    assert result == "first answer"
    record = get_human_input_request(transport.request.request_id, state_path=state)
    assert record.status == "answered"
    assert record.transport == "fake"
    assert record.transport_ref == "fake:root"
    assert record.response_user_id == "UOWNER"
    assert record.response_latency_seconds == 1
    assert stat_mode(state) == 0o600
    assert stat_mode(state.parent) == 0o700


def test_request_record_returns_authenticated_durable_response(tmp_path):
    transport = FakeTransport(batches=[[reply("approved")]])
    state = tmp_path / "human-input.sqlite3"

    record = request_human_input_record(
        "Authorize exact action", state_path=state, transport=transport,
        poll_interval_seconds=0.01,
    )

    assert record == get_human_input_request(record.request_id, state_path=state)
    assert record.status == "answered"
    assert record.response == "approved"
    assert record.response_user_id == "UOWNER"
    assert record.response_message_id == "1001.0"


def test_timeout_expires_durably_and_stops_polling(tmp_path):
    clock = FakeClock()
    transport = FakeTransport()
    state = tmp_path / "human-input.sqlite3"

    with pytest.raises(HumanInputTimeout) as caught:
        _request_human_input(
            "Respond quickly",
            timeout_minutes=0.05,
            poll_interval_seconds=2,
            state_path=state,
            transport=transport,
            clock=clock,
            monotonic=clock,
            sleep=clock.sleep,
        )

    assert caught.value.request_id == transport.request.request_id
    record = get_human_input_request(caught.value.request_id, state_path=state)
    assert record.status == "expired"
    assert record.response is None
    assert clock.sleeps == [2, 1]
    assert transport.polls == 3


def test_reply_timestamp_after_deadline_is_rejected(tmp_path):
    clock = FakeClock()
    transport = FakeTransport(batches=[[reply("late", received_at=1_004.0)]])

    with pytest.raises(HumanInputTimeout):
        _request_human_input(
            "Deadline",
            timeout_minutes=0.05,
            poll_interval_seconds=3,
            state_path=tmp_path / "state.sqlite3",
            transport=transport,
            clock=clock,
            monotonic=clock,
            sleep=clock.sleep,
        )


def test_transient_polling_error_retries_and_retains_no_error_after_success(tmp_path):
    clock = FakeClock()

    class TransientTransport(FakeTransport):
        def replies(self, transport_ref):
            if self.polls == 0:
                self.polls += 1
                raise TemporaryHumanInputTransportError("temporary", retry_after_seconds=2)
            return [reply("recovered", received_at=clock())]

    transport = TransientTransport()
    state = tmp_path / "state.sqlite3"

    result = _request_human_input(
        "Retry",
        timeout_minutes=1,
        poll_interval_seconds=1,
        state_path=state,
        transport=transport,
        clock=clock,
        monotonic=clock,
        sleep=clock.sleep,
    )

    assert result == "recovered"
    assert clock.sleeps == [2]
    assert get_human_input_request(transport.request.request_id, state_path=state).last_error is None


def test_delivery_failure_is_recorded_without_leaking_exception_detail(tmp_path):
    transport = FakeTransport(delivery_error=RuntimeError("secret detail"))
    state = tmp_path / "state.sqlite3"

    with pytest.raises(HumanInputDeliveryError, match="RuntimeError"):
        request_human_input("Question", state_path=state, transport=transport)

    record = get_human_input_request(transport.request.request_id, state_path=state)
    assert record.status == "failed"
    assert "secret detail" not in (record.last_error or "")


def test_atomic_acceptance_is_first_response_wins(tmp_path):
    store = _RequestStore(tmp_path / "state.sqlite3")
    request = store.create("Question", 1, "fake", 1_000)
    store.mark_delivered(request.request_id, "fake:root", 1_000)

    assert store.accept(request.request_id, reply("first")) is True
    assert store.accept(
        request.request_id,
        reply("second", message_id="1002.0", received_at=1_002),
    ) is False
    assert store.get(request.request_id).response == "first"


def test_concurrent_acceptance_has_one_winner(tmp_path):
    store = _RequestStore(tmp_path / "state.sqlite3")
    request = store.create("Question", 1, "fake", 1_000)
    store.mark_delivered(request.request_id, "fake:root", 1_000)
    candidates = [
        reply(f"answer-{index}", message_id=f"100{index}.0", received_at=1_001 + index)
        for index in range(8)
    ]

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda candidate: store.accept(request.request_id, candidate), candidates))

    assert results.count(True) == 1
    assert store.get(request.request_id).response in {candidate.text for candidate in candidates}


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"message": ""}, "non-empty"),
        ({"message": "x", "timeout_minutes": 0}, "greater than 0"),
        ({"message": "x", "poll_interval_seconds": 0}, "greater than 0"),
    ],
)
def test_public_input_validation_precedes_state_creation(tmp_path, kwargs, match):
    with pytest.raises(ValueError, match=match):
        request_human_input(state_path=tmp_path / "state.sqlite3", transport=FakeTransport(), **kwargs)
    assert not (tmp_path / "state.sqlite3").exists()


def test_notification_posts_once_without_request_state(tmp_path, monkeypatch):
    transport = FakeNotificationTransport()
    monkeypatch.setenv("ANCHOR_HUMAN_INPUT_STATE_PATH", str(tmp_path / "state.sqlite3"))

    result = notify_human("  Task completed  ", transport=transport)

    assert result == "fake:notification"
    assert len(transport.notifications) == 1
    message, notification_id = transport.notifications[0]
    assert message == "Task completed"
    assert notification_id
    assert not (tmp_path / "state.sqlite3").exists()


@pytest.mark.parametrize(
    "message,match",
    [
        ("", "non-empty"),
        ("x" * 3_001, "must not exceed"),
    ],
)
def test_notification_validates_before_delivery(message, match):
    transport = FakeNotificationTransport()

    with pytest.raises(ValueError, match=match):
        notify_human(message, transport=transport)

    assert transport.notifications == []


def test_notification_wraps_unexpected_error_without_detail():
    transport = FakeNotificationTransport(delivery_error=RuntimeError("secret detail"))

    with pytest.raises(HumanInputDeliveryError, match="RuntimeError") as caught:
        notify_human("Finished", transport=transport)

    assert "secret detail" not in str(caught.value)


def test_notification_rejects_invalid_transport_reference():
    with pytest.raises(HumanInputDeliveryError, match="invalid notification reference"):
        notify_human("Finished", transport=FakeNotificationTransport(transport_ref=""))


def test_slack_environment_configuration(monkeypatch):
    for variable in ("ANCHOR_SLACK_BOT_TOKEN", "ANCHOR_SLACK_CHANNEL_ID", "ANCHOR_SLACK_USER_ID"):
        monkeypatch.delenv(variable, raising=False)
    with pytest.raises(HumanInputConfigurationError) as caught:
        SlackHumanInputTransport.from_environment()
    assert "ANCHOR_SLACK_BOT_TOKEN" in str(caught.value)
    assert "xoxb" not in str(caught.value)


def test_slack_posts_question_and_accepts_only_configured_user(monkeypatch):
    calls = []
    responses = [
        {
            "ok": True,
            "channel": {"id": "C123", "is_channel": True, "is_private": True},
        },
        {"ok": True, "channel": "C123", "ts": "1700000000.100"},
        {
            "ok": True,
            "messages": [
                {"user": "BBOT", "text": "root", "ts": "1700000000.100"},
                {"user": "UOTHER", "text": "wrong", "ts": "1700000001.100"},
                {"user": "UOWNER", "text": "yes &amp; proceed", "ts": "1700000002.100"},
            ],
        },
    ]

    def fake_urlopen(slack_request, timeout):
        calls.append((slack_request, timeout))
        return FakeHTTPResponse(responses.pop(0))

    monkeypatch.setitem(SlackHumanInputTransport._api_call.__globals__, "_urlopen", fake_urlopen)
    transport = SlackHumanInputTransport(
        bot_token="xoxb-secret",
        channel_id="C123",
        allowed_user_id="UOWNER",
    )
    human_request = make_request()

    transport_ref = transport.deliver(human_request)
    replies = transport.replies(transport_ref)

    assert transport_ref == "C123:1700000000.100"
    assert [candidate.text for candidate in replies] == ["yes & proceed"]
    posted = json.loads(calls[1][0].data.decode("utf-8"))
    assert posted["client_msg_id"] == human_request.request_id
    assert "Question from test" in posted["text"]
    assert "<@UOWNER>" in posted["text"]
    assert calls[0][0].get_header("Authorization") == "Bearer xoxb-secret"
    assert "xoxb-secret" not in calls[2][0].full_url


def test_slack_rejects_public_channel_before_posting(monkeypatch):
    calls = []

    def fake_urlopen(slack_request, timeout):
        calls.append((slack_request, timeout))
        return FakeHTTPResponse(
            {"ok": True, "channel": {"id": "C123", "is_channel": True, "is_private": False}}
        )

    monkeypatch.setitem(SlackHumanInputTransport._api_call.__globals__, "_urlopen", fake_urlopen)
    transport = SlackHumanInputTransport(
        bot_token="token", channel_id="C123", allowed_user_id="UOWNER"
    )

    with pytest.raises(HumanInputConfigurationError, match="private"):
        transport.deliver(make_request())
    assert len(calls) == 1
    assert calls[0][0].full_url.startswith("https://slack.com/api/conversations.info")


def test_slack_posts_one_way_notification(monkeypatch):
    calls = []
    responses = [
        {
            "ok": True,
            "channel": {"id": "C123", "is_channel": True, "is_private": True},
        },
        {"ok": True, "channel": "C123", "ts": "1700000000.200"},
    ]

    def fake_urlopen(slack_request, timeout):
        calls.append((slack_request, timeout))
        return FakeHTTPResponse(responses.pop(0))

    monkeypatch.setitem(SlackHumanInputTransport._api_call.__globals__, "_urlopen", fake_urlopen)
    transport = SlackHumanInputTransport(
        bot_token="xoxb-secret", channel_id="C123", allowed_user_id="UOWNER"
    )

    transport_ref = transport.notify("Task complete", "notification-id")

    assert transport_ref == "C123:1700000000.200"
    assert len(calls) == 2
    posted = json.loads(calls[1][0].data.decode("utf-8"))
    assert posted["client_msg_id"] == "notification-id"
    assert "Task complete" in posted["text"]
    assert "<@UOWNER>" in posted["text"]
    assert "Reply in this message's thread" not in posted["text"]


def test_slack_paginates_and_uses_edit_time_without_over_decoding(monkeypatch):
    responses = [
        {
            "ok": True,
            "messages": [{"user": "BBOT", "text": "root", "ts": "1700000000.100"}],
            "response_metadata": {"next_cursor": "page-two"},
        },
        {
            "ok": True,
            "messages": [
                {
                    "user": "UOWNER",
                    "text": "Use &copy; &amp; &lt;safe&gt;",
                    "ts": "1700000001.100",
                    "edited": {"ts": "1700000003.100", "user": "UOWNER"},
                }
            ],
            "response_metadata": {"next_cursor": ""},
        },
    ]
    calls = []

    def fake_urlopen(slack_request, timeout):
        calls.append((slack_request, timeout))
        return FakeHTTPResponse(responses.pop(0))

    monkeypatch.setitem(SlackHumanInputTransport._api_call.__globals__, "_urlopen", fake_urlopen)
    transport = SlackHumanInputTransport(
        bot_token="token", channel_id="C123", allowed_user_id="UOWNER"
    )

    replies = transport.replies("C123:1700000000.100")

    assert len(calls) == 2
    assert "cursor=page-two" in calls[1][0].full_url
    assert replies[0].text == "Use &copy; & <safe>"
    assert replies[0].received_at == 1_700_000_003.1


def test_slack_application_transient_error_is_retryable(monkeypatch):
    monkeypatch.setitem(
        SlackHumanInputTransport._api_call.__globals__,
        "_urlopen",
        lambda *_args, **_kwargs: FakeHTTPResponse({"ok": False, "error": "internal_error"}),
    )
    transport = SlackHumanInputTransport(
        bot_token="token", channel_id="C123", allowed_user_id="UOWNER"
    )

    with pytest.raises(TemporaryHumanInputTransportError):
        transport.replies("C123:1700000000.100")


def test_slack_rate_limit_is_temporary(monkeypatch):
    headers = Message()
    headers["Retry-After"] = "7"

    def rate_limited(*_args, **_kwargs):
        raise error.HTTPError("https://slack.com/api/x", 429, "rate", headers, None)

    monkeypatch.setitem(SlackHumanInputTransport._api_call.__globals__, "_urlopen", rate_limited)
    transport = SlackHumanInputTransport(
        bot_token="token", channel_id="C123", allowed_user_id="U123"
    )
    with pytest.raises(TemporaryHumanInputTransportError) as caught:
        transport.replies("C123:1700000000.100")
    assert caught.value.retry_after_seconds == 7


def make_request():
    from odibi_anchor.human_input import HumanInputRequest

    return HumanInputRequest(
        request_id="00000000-0000-0000-0000-000000000001",
        message="Question from test",
        status="created",
        created_at=1_700_000_000,
        deadline_at=1_700_003_600,
        delivered_at=None,
        transport="slack",
        transport_ref=None,
        response=None,
        response_user_id=None,
        response_message_id=None,
        responded_at=None,
        last_error=None,
    )


class FakeHTTPResponse:
    def __init__(self, document):
        self.body = json.dumps(document).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.body


def stat_mode(path):
    return os.stat(path).st_mode & 0o777
