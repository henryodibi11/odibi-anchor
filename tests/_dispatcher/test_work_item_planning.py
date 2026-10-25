"""Planning records on work items: dependencies, reviews, next, and owner plan approval."""

import hashlib
import sys
import types
from pathlib import Path
from typing import Any

import pytest

import odibi_anchor._dispatcher._work_item as work_item_module
from odibi_anchor._dispatcher._spec import spec_action
from odibi_anchor._dispatcher._work_item import work_item_action

APPROVED = {
    "status": "approved",
    "approval_response": "APPROVE " + "a" * 64,
    "challenge_sha256": "a" * 64,
    "owner": "owner@example.com",
    "assurance": "chat_in_session",
    "transport": "chat_in_session",
    "authority_ref": "owner-approval:1",
    "response_message_id": "m1",
}
PENDING = {
    "status": "pending",
    "approval_response": "APPROVE " + "b" * 64,
    "challenge_sha256": "b" * 64,
    "channels": ["slack"],
    "expires_at": "2026-10-25T12:30:00+00:00",
}


def call(root, *args, **kwargs) -> dict[str, Any]:
    result = work_item_action(root, *args, output_format="dict", **kwargs)
    assert isinstance(result, dict)
    return result


def review(root, item_id, angle, verdict="approve", findings=None):
    current = call(root, "show", item_id)["fingerprint"]
    return call(
        root, "review", item_id, angle=angle, verdict=verdict, findings=findings or [],
        reviewer_ref=f"task-{angle}", expected_fingerprint=current,
    )


@pytest.fixture
def owner(monkeypatch):
    state: dict[str, Any] = {"response": APPROVED, "calls": []}

    def fake(**kwargs):
        state["calls"].append(kwargs)
        return dict(state["response"])

    try:
        import odibi_anchor.owner_approval as module
    except ImportError:
        module = types.ModuleType("odibi_anchor.owner_approval")
        monkeypatch.setitem(sys.modules, "odibi_anchor.owner_approval", module)
    monkeypatch.setattr(module, "request_owner_approval", fake, raising=False)
    return state


def test_legacy_file_without_planning_keys_parses_with_same_fingerprint(tmp_path):
    item = call(tmp_path, "create", title="T", outcome="O", context="C")
    path = Path(item["artifact_path"])
    legacy = "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.startswith(("depends_on:", "reviews:", "plan_approvals:"))
    )
    path.write_text(legacy, encoding="utf-8")
    before = path.read_bytes()
    shown = call(tmp_path, "show", item["work_item_id"])
    assert shown["fingerprint"] == item["fingerprint"]
    assert (shown["depends_on"], shown["reviews"], shown["plan_approvals"]) == ([], [], [])
    assert path.read_bytes() == before
    other = call(tmp_path, "create", title="Dep", outcome="O")
    updated = call(tmp_path, "update", item["work_item_id"], depends_on=[other["work_item_id"]])
    assert updated["write_performed"] and updated["fingerprint"] == item["fingerprint"]
    assert f'depends_on: ["{other["work_item_id"]}"]' in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "value,match",
    [("WI-2026-0001", "must be a list"), (["bad"], "WI-YYYY-NNNN"), (["WI-2026-0005", "wi-2026-0005"], "unique")],
)
def test_depends_on_format_is_validated(tmp_path, value, match):
    with pytest.raises(ValueError, match=match):
        call(tmp_path, "create", title="T", outcome="O", depends_on=value)


def test_self_dependency_is_rejected(tmp_path):
    item = call(tmp_path, "create", title="T", outcome="O")
    with pytest.raises(ValueError, match="itself"):
        call(tmp_path, "update", item["work_item_id"], depends_on=[item["work_item_id"]])


def test_next_orders_ready_items_and_reports_blocked_and_unknown_dependencies(tmp_path):
    first = call(tmp_path, "create", title="First", outcome="O")["work_item_id"]
    second = call(tmp_path, "create", title="Second", outcome="O", depends_on=[first])["work_item_id"]
    third = call(tmp_path, "create", title="Third", outcome="O", depends_on=["WI-2001-0001"])["work_item_id"]
    result = call(tmp_path, "next")
    assert result["write_performed"] is False
    assert [x["work_item_id"] for x in result["ready"]] == [first]
    assert result["ready"][0]["plan_approval"]["approved"] is False
    blocked = {x["work_item_id"]: x for x in result["blocked"]}
    assert blocked[second]["unmet_dependencies"] == [first]
    assert blocked[third]["unknown_dependencies"] == ["WI-2001-0001"]
    call(tmp_path, "close", first, outcome="completed", implementation_disposition="implemented")
    assert [x["work_item_id"] for x in call(tmp_path, "next")["ready"]] == [second]
    assert "Next work items" in work_item_action(tmp_path, "next")


def test_review_is_bound_to_fingerprint_and_goes_stale_after_update(tmp_path):
    item_id = call(tmp_path, "create", title="T", outcome="O")["work_item_id"]
    recorded = review(tmp_path, item_id, "workplan", findings=[{"summary": "ok", "status": "resolved"}])
    entry = recorded["review"]
    assert entry["review_id"].startswith("WIR-") and entry["fingerprint"] == recorded["fingerprint"]
    assert set(entry) == {"review_id", "angle", "verdict", "findings", "reviewer_ref", "fingerprint", "recorded_at"}
    assert call(tmp_path, "show", item_id)["reviews"][0]["stale"] is False
    call(tmp_path, "update", item_id, scope="changed")
    assert call(tmp_path, "show", item_id)["reviews"][0]["stale"] is True
    assert call(tmp_path, "list")["work_items"][0]["reviews"][0]["stale"] is True


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"expected_fingerprint": "sha256:old"}, "fingerprint"),
        ({"angle": "Adversarial"}, "kebab-case"),
        ({"verdict": "maybe"}, "verdict"),
        ({"findings": [{"summary": "x", "status": "pending"}]}, "open or resolved"),
        ({"reviewer_ref": " "}, "nonempty"),
    ],
)
def test_review_rejects_invalid_input(tmp_path, overrides, match):
    item = call(tmp_path, "create", title="T", outcome="O")
    values = {
        "angle": "problem", "verdict": "approve", "findings": [], "reviewer_ref": "task-1",
        "expected_fingerprint": item["fingerprint"], **overrides,
    }
    with pytest.raises(ValueError, match=match):
        call(tmp_path, "review", item["work_item_id"], **values)


def test_closed_items_refuse_update_and_review(tmp_path):
    item = call(tmp_path, "create", title="T", outcome="O")
    closed = call(tmp_path, "close", item["work_item_id"], outcome="cancelled", implementation_disposition="rejected")
    with pytest.raises(ValueError, match="cancelled work items cannot be updated"):
        call(tmp_path, "update", item["work_item_id"], scope="x")
    with pytest.raises(ValueError, match="closed"):
        call(
            tmp_path, "review", item["work_item_id"], angle="problem", verdict="approve",
            reviewer_ref="task-1", expected_fingerprint=closed["fingerprint"],
        )


@pytest.mark.parametrize(
    "rigor,expected",
    [
        ("direct", ["direct rigor needs 1"]),
        ("compact", ["compact rigor needs 2", "found 1 (problem)"]),
        ("full", ["full rigor needs 4", "needs an approving current adversarial review"]),
    ],
)
def test_readiness_refusal_lists_every_problem_without_contacting_owner(tmp_path, owner, rigor, expected):
    outside = call(tmp_path, "create", title="Outside", outcome="O")["work_item_id"]
    first = call(tmp_path, "create", title="First", outcome="O")["work_item_id"]
    second = call(tmp_path, "create", title="Second", outcome="O", depends_on=[outside])["work_item_id"]
    review(tmp_path, first, "problem", findings=[{"summary": "missing edge case", "status": "open"}])
    with pytest.raises(ValueError) as caught:
        call(tmp_path, "request_plan_approval", work_items=[first, second], rigor=rigor)
    message = str(caught.value)
    assert "missing edge case" in message
    assert f"{second}: dependency {outside} is draft" in message
    assert all(text in message for text in expected)
    assert f"{second}: {rigor} rigor needs" in message
    assert owner["calls"] == []


def test_readiness_requires_dependency_order_and_respects_blocking_verdicts(tmp_path, owner):
    first = call(tmp_path, "create", title="First", outcome="O")["work_item_id"]
    second = call(tmp_path, "create", title="Second", outcome="O", depends_on=[first])["work_item_id"]
    review(tmp_path, first, "problem")
    review(tmp_path, second, "problem", verdict="reject")
    review(tmp_path, second, "workplan")
    with pytest.raises(ValueError) as caught:
        call(tmp_path, "request_plan_approval", work_items=[second, first], rigor="direct")
    message = str(caught.value)
    assert f"{second}: dependency {first} must come earlier" in message
    assert "verdict is reject" in message
    review(tmp_path, second, "problem")
    approved = call(tmp_path, "request_plan_approval", work_items=[first, second], rigor="direct")
    assert approved["status"] == "approved"


def test_approved_plan_writes_approvals_bound_to_fingerprints_and_dependencies(tmp_path, owner):
    spec_action(tmp_path, "create", name="PLAN", content="# Plan\n", specs_dir=tmp_path / "specs", output_format="dict")
    first = call(tmp_path, "create", title="First", outcome="O")["work_item_id"]
    second = call(tmp_path, "create", title="Second", outcome="O", depends_on=[first])["work_item_id"]
    for item_id in (first, second):
        for angle in ("problem", "workplan", "conventions", "adversarial"):
            review(tmp_path, item_id, angle)
    result = call(
        tmp_path, "request_plan_approval", work_items=[first, second], spec="plan", rigor="full",
        provider="chat_in_session", in_session_approval=APPROVED["approval_response"],
    )
    assert result["status"] == "approved" and result["write_performed"] is True
    sent = owner["calls"][0]
    assert sent["provider"] == "chat_in_session" and "wait_seconds" not in sent
    subject = sent["subject"]
    assert subject["spec"] == {"name": "PLAN", "sha256": hashlib.sha256(b"# Plan\n").hexdigest()}
    assert [x["work_item_id"] for x in subject["work_items"]] == [first, second]
    assert subject["work_items"][1]["depends_on"] == [first]
    assert subject["rigor"] == "full"
    assert len(result["managed_writes"]) == 2
    for approval in result["plan_approvals"]:
        shown = call(tmp_path, "show", approval["work_item_id"])
        stored = shown["record"]["plan_approvals"][0]
        assert stored["approval_id"].startswith("WIP-")
        assert stored["fingerprint"] == shown["fingerprint"]
        assert stored["plan_sha256"] == result["plan_sha256"]
        assert (stored["owner"], stored["assurance"], stored["authority_ref"]) == (
            "owner@example.com", "chat_in_session", "owner-approval:1",
        )
        assert shown["plan_approval"]["approved"] is True
    assert call(tmp_path, "next")["ready"][0]["plan_approval"]["approved"] is True
    call(tmp_path, "update", first, scope="changed after approval")
    shown = call(tmp_path, "show", first)
    assert len(shown["plan_approvals"]) == 1 and shown["plan_approvals"][0]["stale"] is True
    assert call(tmp_path, "next")["ready"][0]["plan_approval"]["approved"] is False
    other = call(tmp_path, "create", title="Other", outcome="O")["work_item_id"]
    call(tmp_path, "update", second, depends_on=[first, other])
    assert call(tmp_path, "show", second)["plan_approval"]["approved"] is False


def test_pending_approval_writes_nothing_and_returns_next_operations(tmp_path, owner):
    owner["response"] = PENDING
    item_id = call(tmp_path, "create", title="T", outcome="O")["work_item_id"]
    review(tmp_path, item_id, "problem")
    path = tmp_path / "work_items" / f"{item_id}.md"
    before = path.read_bytes()
    result = call(tmp_path, "request_plan_approval", work_items=[item_id], rigor="direct", wait_seconds=5)
    assert path.read_bytes() == before
    assert result["status"] == "pending" and result["write_performed"] is False
    assert owner["calls"][0]["wait_seconds"] == 5
    operations = {x["purpose"]: x["operation"] for x in result["next_operations"]}
    assert operations["check_again"] == (
        f'anchor("work_item", "request_plan_approval", work_items=["{item_id}"], rigor="direct")'
    )
    assert 'provider="chat_in_session"' in operations["complete_in_session"]
    assert PENDING["approval_response"] in operations["complete_in_session"]


def test_missing_spec_is_a_readiness_problem(tmp_path, owner):
    item_id = call(tmp_path, "create", title="T", outcome="O")["work_item_id"]
    review(tmp_path, item_id, "problem")
    with pytest.raises(ValueError, match=r"spec MISSING_SPEC\.md does not exist"):
        call(tmp_path, "request_plan_approval", work_items=[item_id], spec="missing", rigor="direct")
    assert owner["calls"] == []


def test_planning_keys_are_optional_metadata_outside_material():
    planning = {"depends_on", "reviews", "plan_approvals"}
    assert planning <= work_item_module.OPTIONAL_META
    assert not planning & set(work_item_module._MATERIAL)
    assert not planning & work_item_module.REQUIRED_META
