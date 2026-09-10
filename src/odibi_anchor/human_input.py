"""Portable, durable human-input requests for synchronous Python callers."""
from __future__ import annotations

import contextlib
import os
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

MAX_MESSAGE_LENGTH = 3_000
MAX_RESPONSE_LENGTH = 10_000
MAX_TIMEOUT_MINUTES = 7 * 24 * 60
DEFAULT_POLL_INTERVAL_SECONDS = 5.0


class HumanInputError(RuntimeError):
    """Base error for a human-input request that cannot complete safely."""


class HumanInputConfigurationError(HumanInputError):
    """Required human-input configuration is missing or invalid."""


class HumanInputDeliveryError(HumanInputError):
    """The request could not be delivered or checked reliably."""


class HumanInputTimeout(HumanInputError):
    """No valid response arrived before the request deadline."""

    def __init__(self, request_id: str, timeout_minutes: float) -> None:
        self.request_id = request_id
        self.timeout_minutes = timeout_minutes
        super().__init__(
            f"Human-input request {request_id} timed out after {timeout_minutes:g} minutes"
        )


@dataclass(frozen=True)
class HumanInputReply:
    """One transport-authenticated candidate response."""

    text: str
    user_id: str
    message_id: str
    received_at: float


@dataclass(frozen=True)
class HumanInputRequest:
    """Durable state for one human-input request."""

    request_id: str
    message: str
    status: str
    created_at: float
    deadline_at: float
    delivered_at: float | None
    transport: str
    transport_ref: str | None
    response: str | None
    response_user_id: str | None
    response_message_id: str | None
    responded_at: float | None
    last_error: str | None

    @property
    def response_latency_seconds(self) -> float | None:
        """Return delivery-to-response latency when both timestamps are known."""
        if self.delivered_at is None or self.responded_at is None:
            return None
        return max(0.0, self.responded_at - self.delivered_at)


class HumanInputTransport(Protocol):
    """Narrow boundary between the request lifecycle and one communication channel."""

    name: str

    def deliver(self, request: HumanInputRequest) -> str:
        """Deliver a request and return an opaque transport reference."""

    def replies(self, transport_ref: str) -> Sequence[HumanInputReply]:
        """Return candidate replies for a previously delivered request."""


class HumanNotificationTransport(Protocol):
    """Narrow boundary for one-way human notifications."""

    name: str

    def notify(self, message: str, notification_id: str) -> str:
        """Deliver one notification and return an opaque transport reference."""


class TemporaryHumanInputTransportError(HumanInputDeliveryError):
    """A transient polling failure that may be retried before the deadline."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


def _default_state_path() -> Path:
    configured = os.environ.get("ANCHOR_HUMAN_INPUT_STATE_PATH")
    if configured:
        return Path(configured).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "odibi-anchor" / "human-input.sqlite3"


class _RequestStore:
    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path).expanduser() if path is not None else _default_state_path()
        if self.path.is_symlink():
            raise HumanInputConfigurationError("Human-input state path must not be a symbolic link")
        parent_existed = self.path.parent.exists()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not parent_existed and os.name == "posix":
            self.path.parent.chmod(0o700)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextlib.contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS human_input_requests (
                    request_id TEXT PRIMARY KEY,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    deadline_at REAL NOT NULL,
                    delivered_at REAL,
                    transport TEXT NOT NULL,
                    transport_ref TEXT,
                    response TEXT,
                    response_user_id TEXT,
                    response_message_id TEXT UNIQUE,
                    responded_at REAL,
                    last_error TEXT
                )
                """
            )
        if os.name == "posix":
            self.path.chmod(0o600)
            mode = self.path.stat().st_mode
            if not stat.S_ISREG(mode) or mode & 0o077:
                raise HumanInputConfigurationError("Human-input state database must be a private regular file")

    def create(self, message: str, timeout_minutes: float, transport: str, now: float) -> HumanInputRequest:
        request_id = str(uuid4())
        deadline_at = now + timeout_minutes * 60
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO human_input_requests (
                    request_id, message, status, created_at, deadline_at, transport
                ) VALUES (?, ?, 'created', ?, ?, ?)
                """,
                (request_id, message, now, deadline_at, transport),
            )
        return self.get(request_id)

    def get(self, request_id: str) -> HumanInputRequest:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM human_input_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown human-input request: {request_id}")
        return HumanInputRequest(**dict(row))

    def mark_delivered(self, request_id: str, transport_ref: str, now: float) -> HumanInputRequest:
        with self._connection() as connection:
            changed = connection.execute(
                """
                UPDATE human_input_requests
                SET status = 'pending', delivered_at = ?, transport_ref = ?, last_error = NULL
                WHERE request_id = ? AND status = 'created'
                """,
                (now, transport_ref, request_id),
            ).rowcount
        if changed != 1:
            raise HumanInputError(f"Request {request_id} is not awaiting delivery")
        return self.get(request_id)

    def record_poll_error(self, request_id: str, error: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE human_input_requests SET last_error = ? WHERE request_id = ? AND status = 'pending'",
                (error[:500], request_id),
            )

    def fail(self, request_id: str, error: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE human_input_requests SET status = 'failed', last_error = ?
                WHERE request_id = ? AND status IN ('created', 'pending')
                """,
                (error[:500], request_id),
            )

    def accept(self, request_id: str, reply: HumanInputReply) -> bool:
        text = reply.text.strip()
        if not text or len(text) > MAX_RESPONSE_LENGTH:
            return False
        try:
            with self._connection() as connection:
                changed = connection.execute(
                    """
                    UPDATE human_input_requests
                    SET status = 'answered', response = ?, response_user_id = ?,
                        response_message_id = ?, responded_at = ?, last_error = NULL
                    WHERE request_id = ? AND status = 'pending' AND deadline_at > ?
                    """,
                    (
                        text,
                        reply.user_id,
                        reply.message_id,
                        reply.received_at,
                        request_id,
                        reply.received_at,
                    ),
                ).rowcount
        except sqlite3.IntegrityError:
            return False
        return changed == 1

    def expire(self, request_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE human_input_requests SET status = 'expired'
                WHERE request_id = ? AND status = 'pending'
                """,
                (request_id,),
            )


def _validate_message(message: str) -> str:
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message must be a non-empty string")
    normalized = message.strip()
    if len(normalized) > MAX_MESSAGE_LENGTH:
        raise ValueError(f"message must not exceed {MAX_MESSAGE_LENGTH} characters")
    return normalized


def _validate_request(message: str, timeout_minutes: float, poll_interval_seconds: float) -> str:
    normalized = _validate_message(message)
    if isinstance(timeout_minutes, bool) or not isinstance(timeout_minutes, (int, float)):
        raise ValueError("timeout_minutes must be a number")
    if not 0 < timeout_minutes <= MAX_TIMEOUT_MINUTES:
        raise ValueError(f"timeout_minutes must be greater than 0 and at most {MAX_TIMEOUT_MINUTES}")
    if isinstance(poll_interval_seconds, bool) or not isinstance(poll_interval_seconds, (int, float)):
        raise ValueError("poll_interval_seconds must be a number")
    if not 0 < poll_interval_seconds <= 60:
        raise ValueError("poll_interval_seconds must be greater than 0 and at most 60")
    return normalized


def _default_transport() -> HumanInputTransport:
    from odibi_anchor.human_input_slack import SlackHumanInputTransport

    return SlackHumanInputTransport.from_environment()


def _default_notification_transport() -> HumanNotificationTransport:
    from odibi_anchor.human_input_slack import SlackHumanInputTransport

    return SlackHumanInputTransport.from_environment()


def _safe_error(exc: BaseException) -> str:
    return type(exc).__name__


def _request_human_input(
    message: str,
    timeout_minutes: float = 60,
    *,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    state_path: str | os.PathLike[str] | None = None,
    transport: HumanInputTransport | None = None,
    clock: Callable[[], float],
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    return_record: bool = False,
) -> str | HumanInputRequest:
    normalized = _validate_request(message, timeout_minutes, poll_interval_seconds)
    selected_transport = transport or _default_transport()
    if not isinstance(getattr(selected_transport, "name", None), str) or not selected_transport.name.strip():
        raise HumanInputConfigurationError("transport.name must be a non-empty string")
    store = _RequestStore(state_path)
    wait_deadline = monotonic() + timeout_minutes * 60
    request = store.create(normalized, timeout_minutes, selected_transport.name, clock())

    try:
        transport_ref = selected_transport.deliver(request)
        if not isinstance(transport_ref, str) or not transport_ref.strip():
            raise HumanInputDeliveryError("Transport returned an invalid delivery reference")
        request = store.mark_delivered(request.request_id, transport_ref, clock())
    except HumanInputError as exc:
        store.fail(request.request_id, _safe_error(exc))
        raise
    except Exception as exc:
        error = HumanInputDeliveryError(f"Could not deliver human-input request: {type(exc).__name__}")
        store.fail(request.request_id, _safe_error(exc))
        raise error from exc

    while True:
        retry_after = poll_interval_seconds
        try:
            replies = selected_transport.replies(transport_ref)
        except TemporaryHumanInputTransportError as exc:
            store.record_poll_error(request.request_id, _safe_error(exc))
            if exc.retry_after_seconds is not None:
                retry_after = max(retry_after, exc.retry_after_seconds)
        except HumanInputError as exc:
            store.fail(request.request_id, _safe_error(exc))
            raise
        except Exception as exc:
            error = HumanInputDeliveryError(f"Could not check human-input responses: {type(exc).__name__}")
            store.fail(request.request_id, _safe_error(exc))
            raise error from exc
        else:
            for reply in sorted(replies, key=lambda candidate: candidate.received_at):
                if store.accept(request.request_id, reply):
                    accepted = store.get(request.request_id)
                    assert accepted.response is not None
                    return accepted if return_record else accepted.response

        remaining = wait_deadline - monotonic()
        if remaining <= 0:
            store.expire(request.request_id)
            raise HumanInputTimeout(request.request_id, timeout_minutes)
        sleep(min(retry_after, remaining))


def request_human_input(
    message: str,
    timeout_minutes: float = 60,
    *,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    state_path: str | os.PathLike[str] | None = None,
    transport: HumanInputTransport | None = None,
) -> str:
    """Ask a human for input and synchronously return the first valid response.

    The caller must remain alive while this function waits. Request state is
    persisted for diagnosis and duplicate protection, but restoring a terminated
    caller is the responsibility of the caller's execution framework.
    """
    return _request_human_input(
        message,
        timeout_minutes,
        poll_interval_seconds=poll_interval_seconds,
        state_path=state_path,
        transport=transport,
        clock=time.time,
        monotonic=time.monotonic,
        sleep=time.sleep,
    )


def request_human_input_record(
    message: str,
    timeout_minutes: float = 60,
    *,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    state_path: str | os.PathLike[str] | None = None,
    transport: HumanInputTransport | None = None,
) -> HumanInputRequest:
    """Ask for input and return its durable authenticated request record."""
    result = _request_human_input(
        message,
        timeout_minutes,
        poll_interval_seconds=poll_interval_seconds,
        state_path=state_path,
        transport=transport,
        clock=time.time,
        monotonic=time.monotonic,
        sleep=time.sleep,
        return_record=True,
    )
    assert isinstance(result, HumanInputRequest)
    return result


def notify_human(
    message: str,
    *,
    transport: HumanNotificationTransport | None = None,
) -> str:
    """Deliver one human notification without waiting for a response."""
    normalized = _validate_message(message)
    selected_transport = transport or _default_notification_transport()
    if not isinstance(getattr(selected_transport, "name", None), str) or not selected_transport.name.strip():
        raise HumanInputConfigurationError("transport.name must be a non-empty string")
    try:
        transport_ref = selected_transport.notify(normalized, str(uuid4()))
    except HumanInputError:
        raise
    except Exception as exc:
        raise HumanInputDeliveryError(
            f"Could not deliver human notification: {type(exc).__name__}"
        ) from exc
    if not isinstance(transport_ref, str) or not transport_ref.strip():
        raise HumanInputDeliveryError("Transport returned an invalid notification reference")
    return transport_ref


def get_human_input_request(
    request_id: str, *, state_path: str | os.PathLike[str] | None = None
) -> HumanInputRequest:
    """Read durable status and timing for one human-input request."""
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("request_id must be a non-empty string")
    return _RequestStore(state_path).get(request_id.strip())


__all__ = [
    "HumanInputConfigurationError",
    "HumanInputDeliveryError",
    "HumanInputError",
    "HumanInputReply",
    "HumanInputRequest",
    "HumanInputTimeout",
    "HumanInputTransport",
    "HumanNotificationTransport",
    "TemporaryHumanInputTransportError",
    "get_human_input_request",
    "notify_human",
    "request_human_input",
    "request_human_input_record",
]
