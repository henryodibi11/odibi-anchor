"""Modes are contracts: one mode list, and a contract derived from the profile."""

import pytest

from odibi_anchor.planning._task_profile import _LEGACY_DEFAULTS, normalize_task_profile
from odibi_anchor.planning.mode_contracts import contract_for
from odibi_anchor.planning.task_execution_context import task_execution_context

# docs/guides/agent-first-workflows.md, "Modes are contracts".
RIGOR_TABLE = {
    "direct": (1, False, [], "self review allowed (unless high risk)"),
    "compact": (2, True, [], "self review allowed (unless high risk)"),
    "full": (4, True, ["adversarial"], "independent review required"),
}


@pytest.mark.parametrize("mode", sorted(_LEGACY_DEFAULTS))
def test_task_execution_context_accepts_every_profile_mode(mode):
    """data, etl, refresh, reconciliation and spec_creation were rejected as unsupported."""
    context = task_execution_context(task="Change the orders pipeline configuration.", mode=mode)
    assert context["kind"] == "task_execution_context"


def test_pipeline_mode_profile():
    assert "pipeline" in _LEGACY_DEFAULTS
    profile = normalize_task_profile(legacy_mode="pipeline")
    assert (profile.work_type, profile.execution_mode, profile.risk, profile.rigor) == (
        "change", "source_change", "medium", "compact")
    assert profile.domains == ("data", "pipeline")
    assert profile.traits == frozenset({"pipeline-config"})


@pytest.mark.parametrize("rigor", sorted(RIGOR_TABLE))
def test_review_requirement_follows_the_rigor_table(rigor):
    reviews, distinct, angles, delivery = RIGOR_TABLE[rigor]
    review = contract_for("implementation", profile={"rigor": rigor, "risk": "medium"})["review_requirement"]
    assert review["plan_reviews_before_owner_approval"] == reviews
    assert review["distinct_angles"] is distinct
    assert review["required_angles"] == angles
    assert review["delivery_review"] == delivery
    assert review["self_review_allowed"] is (rigor != "full")
    high_risk = contract_for("implementation", profile={"rigor": rigor, "risk": "high"})
    assert high_risk["review_requirement"]["self_review_allowed"] is False


@pytest.mark.parametrize("mode", sorted(_LEGACY_DEFAULTS))
@pytest.mark.parametrize("execution", [None, "read_only", "artifact_only", "source_change", "data_change"])
def test_artifact_sha256_is_accepted_only_for_artifact_only(mode, execution):
    profile = {"execution_mode": execution} if execution else None
    contract = contract_for(mode, profile=profile)
    assert contract["mode"] == mode
    assert set(contract["accepted_criteria"]) >= {"pytest", "command", "review"}
    assert ("artifact_sha256" in contract["accepted_criteria"]) is (contract["execution_mode"] == "artifact_only")


def test_workflow_requirement_matches_task_admission():
    """Lightweight artifacts and data-only or read-only work are not workflow-enrolled."""
    required = {mode: contract_for(mode)["workflow_required"] for mode in _LEGACY_DEFAULTS}
    assert required["documentation"] is False  # artifact_only, low risk, direct rigor
    assert required["planning"] is True  # substantive artifact
    assert required["implementation"] is True and required["pipeline"] is True
    assert required["data"] is False and required["analysis"] is False
    planning = contract_for("planning")
    assert {"workflow_id", "trust_domain"} <= set(planning["required_task_fields"])
    assert "workflow_id" not in contract_for("analysis")["required_task_fields"]


def test_planning_contract_names_records_and_owner_touchpoints():
    contract = contract_for("planning")
    records = [entry["record"] for entry in contract["records"]]
    assert records[:5] == ["problem", "spec", "work_items", "work_item_reviews", "plan_approval"]
    assert "depends_on" in contract["records"][2]["how"]
    assert contract["owner_touchpoints"] == {
        "plan_approval": True, "delivery_approval": True, "lessons": True, "blockers": True,
    }
    implementation = contract_for("implementation")
    plan = next(entry for entry in implementation["records"] if entry["record"] == "workflow_plan")
    assert plan["fields"] == ["source_paths", "criteria"]
    assert implementation["owner_touchpoints"]["plan_approval"] is False


def test_contract_follows_an_accepted_profile_and_notes_documentation_in_a_repository():
    profile = normalize_task_profile(legacy_mode="documentation", execution_mode="source_change")
    contract = contract_for(None, profile=profile)
    assert contract["mode"] == "documentation" and contract["execution_mode"] == "source_change"
    assert contract["workflow_required"] is True
    assert contract_for(None, profile=profile.to_dict()) == contract
    assert any("source_change" in note for note in contract_for("documentation")["notes"])
    assert any("command" in note for note in contract_for("pipeline")["notes"])


def test_unknown_mode_is_rejected_with_the_mode_list():
    with pytest.raises(ValueError, match="pipeline"):
        contract_for("bogus")
