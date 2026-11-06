"""Focused security and compatibility tests for the transport adapter."""
import pytest

from odibi_anchor._dispatcher._request_adapter import (
    MAX_NESTING_DEPTH,
    MAX_REQUEST_BYTES,
    RequestError,
    error_information,
    execute_request,
    normalize_request,
    redact_message,
)
from odibi_anchor._recovery import attach_recovery, dispatcher_operation


def test_bounds_size_and_depth():
    with pytest.raises(RequestError, match="bytes"):
        normalize_request(b'{"action":"x","value":"' + b"x" * MAX_REQUEST_BYTES + b'"}')
    value = "leaf"
    for _ in range(MAX_NESTING_DEPTH + 1):
        value = [value]
    with pytest.raises(RequestError, match="nesting"):
        normalize_request({"action": "x", "args": value})


@pytest.mark.parametrize(
    "payload,message",
    [
        ({}, "action"),
        ({"action": "x", "schema_version": 2}, "schema_version"),
        ({"action": "x", "args": {}}, "args"),
        ({"action": "x", "kwargs": []}, "kwargs"),
        ({"action": "x", "arg1": 1}, "contiguous"),
        ({"action": "x", "args": [1], "arg0": 1}, "duplicate positional"),
    ],
)
def test_schema_rejections(payload, message):
    with pytest.raises(RequestError, match=message):
        normalize_request(payload)


@pytest.mark.parametrize(
    "payload",
    [
        '{"action":"x","action":"y"}',
        '{"action":"x","args":null}',
        '{"action":"x","kwargs":null}',
        '{"action":"x","arg00":0}',
        '{"action":"x","arg0":0,"arg00":1}',
        '{"action":"x","value":NaN}',
        '{"action":"x","value":Infinity}',
    ],
)
def test_strict_json_and_positional_rejections(payload):
    with pytest.raises(RequestError):
        normalize_request(payload)


@pytest.mark.parametrize("payload", [None, 1, 1.2, [], object()])
def test_wrong_runtime_request_types_are_rejected(payload):
    with pytest.raises(RequestError, match="request must"):
        normalize_request(payload)


def test_legacy_order_and_resolver_delegation():
    seen = []

    def resolver(value, location):
        seen.append(location)
        return f"resolved:{value}"

    request = normalize_request(
        {"action": "x", "arg1": "b", "arg0": "a", "named": "c"}, resolver=resolver
    )
    assert request.args == ("resolved:a", "resolved:b")
    assert request.kwargs == {"named": "resolved:c"}
    assert seen == ["args[0]", "args[1]", "kwargs.named"]


def test_redaction_is_stable_and_execution_does_not_catch_base_exception():
    assert error_information(ValueError("token=abc password: xyz")) == {
        "type": "ValueError",
        "message": "token=<redacted> password=<redacted>",
    }
    request = normalize_request({"action": "x"})
    assert execute_request(lambda *_a, **_k: (_ for _ in ()).throw(ValueError("secret=q")), request) == {
        "ok": False,
        "error": {"type": "ValueError", "message": "secret=<redacted>"},
    }
    with pytest.raises(KeyboardInterrupt):
        execute_request(lambda *_a, **_k: (_ for _ in ()).throw(KeyboardInterrupt()), request)


def test_structured_recovery_is_additive_redacted_and_backward_compatible():
    operation = dispatcher_operation(
        "reject", "memory-1", reason="reject the unpromoted candidate",
    )
    exc = attach_recovery(
        ValueError("memory has no promotion event to withdraw"),
        error_code="memory_candidate_not_promoted",
        context={"memory_id": "memory-1", "status": "candidate", "token": "secret-value"},
        next_operations=[operation],
    )

    assert type(exc) is ValueError
    assert str(exc) == "memory has no promotion event to withdraw"
    error = error_information(exc)
    assert error["type"] == "ValueError"
    assert error["message"] == "memory has no promotion event to withdraw"
    assert error["error_code"] == "memory_candidate_not_promoted"
    assert error["context"] == {
        "memory_id": "memory-1", "status": "candidate", "token": "<redacted>",
    }
    assert error["next_operation"] == error["next_operations"][0]
    assert error["copy_ready"] == "anchor('reject', 'memory-1')"
    assert error["requires_owner"] is False
    assert error["retry_safety"] == "idempotent"


def test_redacts_realistic_authorization_and_quoted_multiline_assignments():
    message = (
        'Authorization: Bearer abc.def\nAuthorization=Basic dXNlcjpwYXNz\n'
        "'Authorization' = 'Bearer quoted.token'\n"
        '{"api_key" : "super \'secret\'"}\n\'password\': \'hunter2\''
    )
    redacted = redact_message(message)
    assert "abc.def" not in redacted
    assert "dXNlcjpwYXNz" not in redacted
    assert "quoted.token" not in redacted
    assert "super 'secret'" not in redacted
    assert "hunter2" not in redacted
    assert redacted.count("<redacted>") == 5


def test_pathological_raw_json_is_translated_to_request_error():
    deeply_nested = '{"action":"x","value":' + "[" * 2_000 + "0" + "]" * 2_000 + "}"
    with pytest.raises(RequestError):
        normalize_request(deeply_nested)
    with pytest.raises(RequestError, match="integer"):
        normalize_request('{"action":"x","value":' + "9" * 1_001 + "}")
    with pytest.raises(RequestError, match="contiguous"):
        normalize_request('{"action":"x","arg' + "9" * 100_000 + '":0}')


def test_direct_python_keeps_raw_results_and_exceptions(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / "project"
    project.mkdir()
    from odibi_anchor.bootstrap import init

    anchor, _, _ = init(root=str(project), output_format="dict")

    assert isinstance(anchor("help", output_format="dict"), str)
    with pytest.raises(ValueError, match=r"Unknown anchor\(\) action"):
        anchor("not-an-action")


def test_workflow_error_code_is_exported_and_explicit_error_code_wins():
    from odibi_anchor.codebase._workflow import WorkflowError

    error = error_information(WorkflowError("review_required", "synthetic review missing"))
    assert error == {"type": "WorkflowError", "message": "synthetic review missing",
                     "error_code": "review_required"}
    annotated = attach_recovery(WorkflowError("wrong_authority", "bound elsewhere"),
                                error_code="workflow_binding_mismatch", context={})
    assert error_information(annotated)["error_code"] == "workflow_binding_mismatch"


def test_other_exception_code_attributes_are_not_error_codes():
    import io
    from urllib.error import HTTPError

    from odibi_anchor._governance_sidecar._protocol import EnvelopeError

    class CodedError(RuntimeError):
        code = "not_a_workflow_code"

    for exc in (
        HTTPError("https://example.invalid", 503, "unavailable", {}, io.BytesIO()),
        EnvelopeError("bad_envelope"),
        CodedError("synthetic"),
    ):
        assert "error_code" not in error_information(exc), exc


def test_workflow_error_identity_survives_package_reimport_and_envelopes_agree():
    from odibi_anchor._dispatcher._envelope import build_failure_envelope, build_operation_failure_envelope

    # Bootstrap re-imports the package, so a WorkflowError may come from another module object.
    reimported = type("WorkflowError", (RuntimeError,), {"__module__": "odibi_anchor.codebase._workflow"})
    exc = reimported("synthetic")
    exc.code = "stale_plan"
    impostor = type("WorkflowError", (RuntimeError,), {"__module__": "elsewhere"})("synthetic")
    impostor.code = "stale_plan"

    assert error_information(exc)["error_code"] == "stale_plan"
    assert "error_code" not in error_information(impostor)
    assert build_failure_envelope("workflow", exc)["error"]["error_code"] == "stale_plan"
    assert build_operation_failure_envelope("workflow.review", exc, mutating=False)["error"]["error_code"] == "stale_plan"
    assert build_failure_envelope("workflow", impostor)["error"]["error_code"] is None
    annotated = attach_recovery(exc, error_code="explicit_code", context={})
    assert build_failure_envelope("workflow", annotated)["error"]["error_code"] == "explicit_code"
