"""Protocol, lifecycle, and process tests for the foundational sidecar kernel."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from odibi_anchor._governance_sidecar._crypto import create_process_identity
from odibi_anchor._governance_sidecar._framing import FramingError, read_frame, write_frame
from odibi_anchor._governance_sidecar._kernel import Kernel
from odibi_anchor._governance_sidecar._protocol import EnvelopeError, validate_envelope
from odibi_anchor._governance_sidecar._state import KernelState, Lifecycle


def envelope(request_id: str, operation: str, payload: object = None) -> dict[str, object]:
    return {
        "protocol_version": 1,
        "request_id": request_id,
        "operation": operation,
        "thread_id": "amp thread \N{GREEK SMALL LETTER ALPHA}",
        "payload": {} if payload is None else payload,
    }


def test_handshake_has_exact_fresh_key_material() -> None:
    first = create_process_identity()
    second = create_process_identity()
    expected = {
        "handshake_version",
        "sidecar_instance_id",
        "challenge",
        "closure_public_key_base64",
        "closure_public_key_digest",
        "issued_at_ms",
    }
    assert set(first.handshake) == expected
    assert first.handshake["challenge"] != second.handshake["challenge"]
    assert first.handshake["sidecar_instance_id"] != second.handshake["sidecar_instance_id"]
    challenge = base64.b64decode(str(first.handshake["challenge"]), validate=True)
    public = base64.b64decode(str(first.handshake["closure_public_key_base64"]), validate=True)
    assert len(challenge) == len(public) == 32
    assert base64.b64encode(public).decode() == first.handshake["closure_public_key_base64"]
    assert hashlib.sha256(public).hexdigest() == first.handshake["closure_public_key_digest"]
    assert "private" not in json.dumps(first.handshake)
    signature = first._private_key.sign(b"closure-receipt-test")
    Ed25519PublicKey.from_public_bytes(public).verify(signature, b"closure-receipt-test")


@pytest.mark.parametrize("raw", [b"\n", b"{}\r\n", b"{}", b"{\xff}\n", b'{"x":1,"x":2}\n', b'{"x":NaN}\n'])
def test_framing_rejects_invalid_input(raw: bytes) -> None:
    with pytest.raises(FramingError):
        read_frame(io.BytesIO(raw))


def test_framing_exact_limit_and_strict_output() -> None:
    raw = b'{"x":"' + b"a" * (1_048_576 - 8) + b'"}\n'
    assert len(raw) - 1 == 1_048_576
    assert read_frame(io.BytesIO(raw))["x"]
    with pytest.raises(FramingError):
        read_frame(io.BytesIO(raw[:-1] + b"a\n"))
    output = io.BytesIO()
    write_frame(output, {"ok": True})
    assert output.getvalue() == b'{"ok":true}\n'
    with pytest.raises(FramingError):
        write_frame(io.BytesIO(), {"number": float("nan")})


def test_framing_completes_short_writes_and_rejects_zero_progress() -> None:
    class ShortWriter(io.BytesIO):
        def write(self, value: bytes) -> int:
            return super().write(value[:2])

    class StalledWriter(io.BytesIO):
        def write(self, value: bytes) -> int:
            return 0

    output = ShortWriter()
    write_frame(output, {"ok": True})
    assert output.getvalue() == b'{"ok":true}\n'
    with pytest.raises(FramingError, match="did not complete"):
        write_frame(StalledWriter(), {"ok": True})


def test_protocol_validation_and_safe_correlation() -> None:
    assert validate_envelope(envelope("request-1", "execute")).thread_id == "amp thread \N{GREEK SMALL LETTER ALPHA}"
    decomposed = "thread-e\N{COMBINING ACUTE ACCENT}"
    assert validate_envelope({**envelope("request-nfc", "execute"), "thread_id": decomposed}).thread_id == "thread-é"
    with pytest.raises(EnvelopeError) as unsupported:
        validate_envelope({**envelope("request-2", "execute"), "protocol_version": 2})
    assert unsupported.value.code == "unsupported_version" and unsupported.value.correlatable
    with pytest.raises(EnvelopeError) as uncorrelated:
        validate_envelope({**envelope("bad request", "execute"), "extra": 1})
    assert not uncorrelated.value.correlatable
    for invalid_thread in ("", "thread\n", "x" * 513, chr(0xD800)):
        with pytest.raises(EnvelopeError, match="invalid_request"):
            validate_envelope({**envelope("request-thread", "execute"), "thread_id": invalid_thread})


def test_kernel_consumes_ids_and_only_shutdown_succeeds() -> None:
    kernel = Kernel()
    denied = kernel.handle(envelope("one", "bootstrap"))
    assert denied["error"]["code"] == "invalid_request"
    assert kernel.handle(envelope("one", "execute"))["error"]["code"] == "duplicate_request"
    assert kernel.handle(envelope("two", "authorize_tool_call"))["error"]["code"] == "not_bootstrapped"
    assert kernel.handle(envelope("unknown", "future_operation"))["error"]["code"] == "invalid_request"
    assert kernel.handle(envelope("unknown", "execute"))["error"]["code"] == "duplicate_request"
    malformed_shutdown = kernel.handle(envelope("three", "shutdown", {"extra": True}))
    assert malformed_shutdown["error"]["code"] == "invalid_request"
    closed = kernel.handle(envelope("four", "shutdown"))
    assert closed["ok"] is True
    assert closed["result"] == {
        "revoked_pre_allow_authorities": 0,
        "unresolved_allowed_calls": 0,
        "closed": True,
    }
    assert kernel.shutdown


def test_kernel_redacts_internal_errors_and_lifecycle_is_one_way(monkeypatch) -> None:
    kernel = Kernel()

    def fail(_envelope):
        raise ValueError("token=must-not-escape")

    monkeypatch.setattr(kernel, "_dispatch_locked", fail)
    internal = kernel.handle(envelope("internal", "execute"))
    assert internal["error"] == {
        "code": "internal_error",
        "message": "The sidecar could not process the request.",
    }
    assert "must-not-escape" not in json.dumps(internal)

    state = KernelState()
    state.transition(Lifecycle.READY_UNLEASED)
    state.transition(Lifecycle.LEASED)
    state.transition(Lifecycle.CLOSING)
    state.transition(Lifecycle.SHUTDOWN)
    with pytest.raises(RuntimeError, match="illegal"):
        state.transition(Lifecycle.UNBOOTSTRAPPED)


def test_module_handshake_shutdown_and_exit() -> None:
    request = json.dumps(envelope("shutdown-1", "shutdown"), separators=(",", ":")) + "\n"
    result = subprocess.run(
        [sys.executable, "-m", "odibi_anchor._governance_sidecar"],
        input=request,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0 and result.stderr == ""
    lines = result.stdout.splitlines()
    assert len(lines) == 2
    assert len(json.loads(lines[0])) == 6
    assert json.loads(lines[1])["result"]["closed"] is True


def test_module_framing_failure_is_terminal_without_response() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "odibi_anchor._governance_sidecar"],
        input="\n",
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode != 0
    assert len(result.stdout.splitlines()) == 1


def test_module_rejects_escaped_surrogate_then_remains_synchronized() -> None:
    invalid = json.dumps({**envelope("invalid-thread", "execute"), "thread_id": chr(0xD800)})
    shutdown = json.dumps(envelope("shutdown-after-invalid", "shutdown"))
    result = subprocess.run(
        [sys.executable, "-m", "odibi_anchor._governance_sidecar"],
        input=f"{invalid}\n{shutdown}\n",
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0 and result.stderr == ""
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(lines[0]) == 6
    assert lines[1]["error"] == {"code": "invalid_request", "message": "The request is invalid."}
    assert lines[2]["result"]["closed"] is True
