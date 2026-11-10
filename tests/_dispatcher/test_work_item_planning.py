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
    # These sparse historical fixtures exercise the explicit legacy readiness contract.
    if args and args[0] == "request_plan_approval":
        kwargs.setdefault("readiness_version", 1)
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
    assert {"review_id", "angle", "verdict", "findings", "reviewer_ref", "fingerprint", "recorded_at"} <= set(entry)
    assert entry["subject_type"] == "plan" and entry["reviewer_authentication"] == "none"
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
        f'anchor("work_item", "request_plan_approval", work_items=["{item_id}"], rigor="direct", readiness_version=1)'
    )
    assert 'provider="chat_in_session"' in operations["complete_in_session"]
    assert PENDING["approval_response"] in operations["complete_in_session"]


def test_spec_changed_while_owner_responds_is_not_recorded_as_approved(tmp_path, monkeypatch):
    spec_action(tmp_path, "create", name="PLAN", content="# Reviewed\n", specs_dir=tmp_path / "specs", output_format="dict")
    item_id = call(tmp_path, "create", title="T", outcome="O")["work_item_id"]
    review(tmp_path, item_id, "problem")

    def respond(**kwargs):
        (tmp_path / "specs/PLAN_SPEC.md").write_text("# Changed during wait\n")
        return dict(APPROVED)

    monkeypatch.setattr("odibi_anchor.owner_approval.request_owner_approval", respond)
    with pytest.raises(ValueError, match="spec changed while approval was pending"):
        call(tmp_path, "request_plan_approval", work_items=[item_id], rigor="direct", spec="PLAN")
    assert call(tmp_path, "show", item_id)["plan_approvals"] == []


def test_missing_spec_is_a_readiness_problem(tmp_path, owner):
    item_id = call(tmp_path, "create", title="T", outcome="O")["work_item_id"]
    review(tmp_path, item_id, "problem")
    with pytest.raises(ValueError, match=r"spec MISSING_SPEC\.md does not exist"):
        call(tmp_path, "request_plan_approval", work_items=[item_id], spec="missing", rigor="direct")
    assert owner["calls"] == []


@pytest.mark.parametrize("status", ["approved", "pending"])
def test_open_minor_findings_are_visible_without_blocking_approval(tmp_path, owner, status):
    owner["response"] = APPROVED if status == "approved" else PENDING
    item_id = call(tmp_path, "create", title="T", outcome="O")["work_item_id"]
    finding = {"summary": "Consider a follow-up example", "status": "open", "severity": "minor"}
    recorded = review(tmp_path, item_id, "problem", findings=[finding])
    shown = call(tmp_path, "show", item_id)
    assert shown["reviews"][0]["findings"] == [finding]
    markdown = work_item_action(tmp_path, "show", item_id)
    assert finding["summary"] in markdown and "minor" in markdown and "open" in markdown

    result = call(tmp_path, "request_plan_approval", work_items=[item_id], rigor="direct")

    assert result["status"] == status
    expected = [{"work_item_id": item_id, "review_id": recorded["review"]["review_id"],
                 "angle": "problem", **finding}]
    assert result["open_minor_findings"] == expected
    assert owner["calls"][0]["subject"]["open_minor_findings"] == expected
    assert finding["summary"] in owner["calls"][0]["prompt"]
    rendered = work_item_module.render_work_item_result("request_plan_approval", result)
    assert finding["summary"] in rendered and "minor" in rendered


@pytest.mark.parametrize("severity", [None, "blocking"])
def test_open_findings_default_to_blocking(tmp_path, owner, severity):
    item_id = call(tmp_path, "create", title="T", outcome="O")["work_item_id"]
    finding = {"summary": "Missing edge case", "status": "open"}
    if severity is not None:
        finding["severity"] = severity
    review(tmp_path, item_id, "problem", findings=[finding])
    with pytest.raises(ValueError, match="Missing edge case"):
        call(tmp_path, "request_plan_approval", work_items=[item_id], rigor="direct")
    assert owner["calls"] == []


@pytest.mark.parametrize("verdict", ["changes_requested", "reject"])
def test_minor_findings_do_not_override_blocking_verdict(tmp_path, owner, verdict):
    item_id = call(tmp_path, "create", title="T", outcome="O")["work_item_id"]
    review(tmp_path, item_id, "problem", verdict=verdict,
           findings=[{"summary": "Small note", "status": "open", "severity": "minor"}])
    review(tmp_path, item_id, "workplan")
    with pytest.raises(ValueError, match=f"verdict is {verdict}"):
        call(tmp_path, "request_plan_approval", work_items=[item_id], rigor="direct")
    assert owner["calls"] == []


def test_invalid_finding_severity_is_refused(tmp_path):
    item_id = call(tmp_path, "create", title="T", outcome="O")["work_item_id"]
    with pytest.raises(ValueError, match="severity must be blocking or minor"):
        review(tmp_path, item_id, "problem",
               findings=[{"summary": "Note", "status": "open", "severity": "trivial"}])


def test_legacy_finding_without_severity_remains_blocking(tmp_path):
    item_id = call(tmp_path, "create", title="T", outcome="O")["work_item_id"]
    review(tmp_path, item_id, "problem", findings=[{"summary": "Legacy defect", "status": "open"}])
    record = call(tmp_path, "show", item_id)["record"]
    del record["reviews"][0]["findings"][0]["severity"]
    problems = work_item_module._plan_readiness_problems(tmp_path, [(item_id, record)], "direct")
    assert any("Legacy defect" in problem for problem in problems)


def test_review_help_documents_severity_and_verdict_rules():
    from odibi_anchor._dispatcher._dispatch_table import build_help_text

    help_text = build_help_text("work_item", None)
    for text in ("severity", "blocking (default)", "minor", "approving reviews", "changes_requested", "reject"):
        assert text in help_text


def test_planning_keys_are_optional_metadata_outside_material():
    planning = {"depends_on", "reviews", "plan_approvals"}
    assert planning <= work_item_module.OPTIONAL_META
    assert not planning & set(work_item_module._MATERIAL)
    assert not planning & work_item_module.REQUIRED_META

def test_real_owner_approval_completes_plan_in_session_without_a_remote_channel(tmp_path, monkeypatch):
    for name in ("ANCHOR_SLACK_BOT_TOKEN", "ANCHOR_SLACK_CHANNEL_ID", "ANCHOR_SLACK_USER_ID",
                 "CW_SLACK_BOT_TOKEN", "CW_SLACK_CHANNEL_ID", "CW_SLACK_USER_ID", "DATABRICKS_RUNTIME_VERSION"):
        monkeypatch.delenv(name, raising=False)
    item_id = call(tmp_path, "create", title="T", outcome="O")["work_item_id"]
    review(tmp_path, item_id, "problem")

    asked = call(tmp_path, "request_plan_approval", work_items=[item_id], rigor="direct")

    assert asked["status"] == "approval_required" and asked["write_performed"] is False
    reply = asked["owner_approval"]["approval_response"]
    assert reply.startswith("APPROVE ") and len(reply) == len("APPROVE ") + 64
    approved = call(tmp_path, "request_plan_approval", work_items=[item_id], rigor="direct",
                    provider="chat_in_session", in_session_approval=reply)
    assert approved["status"] == "approved"
    stored = call(tmp_path, "show", item_id)["record"]["plan_approvals"][0]
    assert stored["assurance"] == "lower_assurance_single_user_chat_session_assertion"
    assert call(tmp_path, "next")["ready"][0]["plan_approval"]["approved"] is True


def _completed_item_with_approved_plan(root, owner):
    from odibi_anchor._dispatcher._problem import problem_action

    problem_action(root, "create", title="Exports are slow", output_format="dict")
    problem_action(root, "create", title="Imports are slow", output_format="dict")
    spec_action(root, "create", name="PLAN", content="# Plan\n", specs_dir=Path(root) / "specs", output_format="dict")
    item_id = call(
        root, "create", title="Fix exports", outcome="Exports finish in time", scope="export path",
        acceptance_criteria=["Exports finish"], problem_record="PRB-2026-0002",
        # Recorded before review so closing leaves the reviewed fingerprint current.
        implementation_disposition="implemented",
    )["work_item_id"]
    reviewed = review(root, item_id, "problem")
    call(root, "request_plan_approval", work_items=[item_id], rigor="direct",
         provider="chat_in_session", in_session_approval=APPROVED["approval_response"])
    closed = call(root, "close", item_id, outcome="completed")
    return item_id, reviewed["review_id"], closed


def test_amend_corrects_links_on_a_completed_item_and_reports_invalidated_bindings(tmp_path, owner):
    item_id, review_id, closed = _completed_item_with_approved_plan(tmp_path, owner)
    path = Path(closed["artifact_path"])
    approval_id = closed["plan_approval"]["approval_id"]
    assert approval_id
    revision = call(tmp_path, "show", item_id)["record"]["revision"]

    amended = call(
        tmp_path, "amend", item_id, problem_record="prb-2026-0001", specification="plan",
        pr_url="https://github.com/org/repo/pull/7", reason={"why": "audit found the wrong Problem link"},
    )

    assert amended["amended"] is True and amended["status"] == "completed"
    shown = call(tmp_path, "show", item_id)
    record = shown["record"]
    assert (record["problem_record"], record["specification"], record["pr_url"]) == (
        "PRB-2026-0001", "PLAN", "https://github.com/org/repo/pull/7",
    )
    assert (record["title"], record["outcome"], record["scope"], record["acceptance_criteria"]) == (
        "Fix exports", "Exports finish in time", "export path", ["Exports finish"],
    )
    assert record["implementation_disposition"] == "implemented"
    assert shown["fingerprint"] != closed["fingerprint"]
    entry = record["revisions"][-1]
    assert entry["revision"] == record["revision"] == revision + 1
    assert entry["change"].startswith("Metadata amended after completed")
    amendment = entry["amendment"]
    assert amendment["reason"] == {"why": "audit found the wrong Problem link"}
    assert amendment["fields"]["problem_record"] == {"before": "PRB-2026-0002", "after": "PRB-2026-0001"}
    assert amendment["fingerprint_before"] == closed["fingerprint"]
    assert amendment["fingerprint_after"] == shown["fingerprint"]
    assert amendment["invalidated"] == {
        "reviews": [review_id], "plan_approvals": [approval_id], "publication_approvals": [],
    }
    assert [r["stale"] for r in shown["reviews"]] == [True]
    assert shown["plan_approval"] == {"approved": False, "approval_id": None, "stale_approval_ids": [approval_id]}

    before = path.read_bytes()
    replay = call(
        tmp_path, "amend", item_id, problem_record="PRB-2026-0001", specification="PLAN",
        pr_url="https://github.com/org/repo/pull/7", reason={"why": "audit found the wrong Problem link"},
    )
    assert replay["amended"] is False and replay["write_performed"] is False
    assert path.read_bytes() == before
    cleared = call(tmp_path, "amend", item_id, pr_url="", reason={"why": "PR link belonged elsewhere"})
    assert cleared["amendment"]["invalidated"] == {"reviews": [], "plan_approvals": [], "publication_approvals": []}
    assert call(tmp_path, "show", item_id)["record"]["pr_url"] == ""


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"scope": "wider", "reason": {"why": "x"}}, "scope cannot change after the item is completed"),
        ({"outcome": "other", "reason": {"why": "x"}}, "outcome cannot change"),
        ({"acceptance_criteria": ["other"], "reason": {"why": "x"}}, "acceptance_criteria cannot change"),
        ({"implementation_disposition": "deferred", "reason": {"why": "x"}}, "implementation_disposition cannot"),
        ({"status": "draft", "reason": {"why": "x"}}, "status cannot change"),
        ({"reason": {"why": "x"}}, "requires at least one of"),
        ({"pr_url": "https://x.test/1"}, "reason as a non-empty JSON object"),
        ({"pr_url": "https://x.test/1", "reason": "text"}, "reason as a non-empty JSON object"),
        ({"pr_url": "https://x.test/1", "reason": {}}, "reason as a non-empty JSON object"),
        ({"problem_record": "PRB-2026-0099", "reason": {"why": "x"}}, "PRB-2026-0099 does not exist"),
        ({"problem_record": "the export problem", "reason": {"why": "x"}}, "must match PRB-YYYY-NNNN"),
        ({"specification": "MISSING", "reason": {"why": "x"}}, "MISSING_SPEC.md does not exist"),
        ({"pr_url": "http://x.test/1", "reason": {"why": "x"}}, "https URL"),
    ],
)
def test_amend_refuses_delivery_content_and_invalid_links_without_writing(tmp_path, owner, changes, match):
    item_id, _, closed = _completed_item_with_approved_plan(tmp_path, owner)
    path = Path(closed["artifact_path"])
    before = path.read_bytes()
    with pytest.raises(ValueError, match=match):
        call(tmp_path, "amend", item_id, **changes)
    assert path.read_bytes() == before


def test_amend_applies_only_to_terminal_items_and_update_points_to_it(tmp_path):
    item_id = call(tmp_path, "create", title="T", outcome="O")["work_item_id"]
    with pytest.raises(ValueError, match="use update for a draft item"):
        call(tmp_path, "amend", item_id, pr_url="https://x.test/1", reason={"why": "x"})
    call(tmp_path, "close", item_id, outcome="cancelled", implementation_disposition="rejected")
    with pytest.raises(ValueError, match=r'cancelled work items cannot be updated; correct link metadata with anchor\("work_item", "amend"'):
        call(tmp_path, "update", item_id, pr_url="https://x.test/1")
    amended = call(tmp_path, "amend", item_id, pr_url="https://x.test/1", reason={"why": "record the closing PR"})
    shown = call(tmp_path, "show", item_id)
    assert amended["status"] == shown["status"] == "cancelled"
    assert shown["record"]["pr_url"] == "https://x.test/1"
