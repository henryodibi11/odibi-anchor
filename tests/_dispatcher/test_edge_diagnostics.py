"""Edge failures name what went wrong and the next step instead of costing agents a guess."""
from __future__ import annotations

from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from odibi_anchor._dispatcher import _workflow_delivery as delivery
from odibi_anchor.codebase._workflow import WorkflowError
from tests._dispatcher.test_workflow_runtime import advance
from tests._dispatcher.test_workflow_runtime import runtime as workflow_runtime

CHALLENGE = "APPROVE sha256:" + "a" * 64


@pytest.fixture
def runtime(tmp_path, monkeypatch, request):
    return workflow_runtime.__wrapped__(tmp_path, monkeypatch, request)


def _provider():
    return SimpleNamespace(expected_owner_id="U-OWNER", transport=SimpleNamespace(name="slack"))


def _reply(text, *, user="U-OWNER", transport="slack", request_id="r-1", message_id="m-1"):
    return SimpleNamespace(response=text, response_user_id=user, transport=transport,
                           request_id=request_id, response_message_id=message_id)


def _check(response):
    delivery._require_exact_response(
        response, {"approval_response": CHALLENGE}, _provider(),
        operation="request_delivery_approval", workflow_id="wf_x", generation=6,
    )


def test_exact_reply_from_the_owner_is_accepted():
    _check(_reply(CHALLENGE))


def test_rejected_approval_names_each_failed_check_without_echoing_the_reply():
    with pytest.raises(WorkflowError) as raised:
        _check(_reply(CHALLENGE + " ", user="U-SOMEONE-ELSE"))

    error = raised.value
    assert str(error).startswith("human response does not match exact challenge and owner")
    assert error.code == "authority_required"
    assert error.error_code == "delivery_approval_mismatch"  # type: ignore[attr-defined]
    mismatches = {item["check"]: item for item in error.context["mismatches"]}  # type: ignore[attr-defined]
    assert set(mismatches) == {"reply_text", "owner"}
    assert mismatches["reply_text"]["matches_after_trimming_whitespace"] is True
    assert CHALLENGE + " " not in repr(error.context)  # type: ignore[attr-defined]
    assert error.next_operation["requires_owner"] is True  # type: ignore[attr-defined]


def test_readback_http_failure_reports_status_endpoint_and_retry_after(monkeypatch):
    class Opener:
        def open(self, req, timeout):
            raise HTTPError(req.full_url, 503, "Service Unavailable", {"Retry-After": "30"}, None)

    monkeypatch.setattr(delivery.request, "build_opener", lambda *handlers: Opener())

    with pytest.raises(WorkflowError) as raised:
        delivery._get_json("github", "/repos/owner/repo/releases/tags/v1")

    error = raised.value
    assert str(error).startswith("github readback unavailable (HTTPError 503)")
    assert error.context == {  # type: ignore[attr-defined]
        "service": "github", "endpoint": "https://api.github.com/repos/owner/repo/releases/tags/v1",
        "http_status": 503, "retry_after": "30", "error_type": "HTTPError",
    }


def test_module_function_called_as_an_action_points_to_the_import(runtime):
    anchor = runtime[0]

    with pytest.raises(ValueError, match="Unknown anchor\\(\\) action: 'doctor'") as raised:
        anchor("doctor", output_format="dict")

    assert raised.value.error_code == "module_function_not_action"  # type: ignore[attr-defined]
    assert "from odibi_anchor.startup import doctor" in str(raised.value)


def test_stale_plan_refusal_gives_the_rework_order(runtime):
    anchor, _home, _target, draft = runtime
    advance(anchor, "accept_plan")
    advance(anchor, "replan", plan={**draft["plan"], "goal": "A different plan"}, reason="Changed requirements")

    with pytest.raises(RuntimeError, match="plan changed; establish a fresh task") as raised:
        advance(anchor, "implemented")

    assert "with a clean worktree open a fresh producer" in str(raised.value)
    assert "Rework after review" in anchor("help", "workflow", output_format="dict")
