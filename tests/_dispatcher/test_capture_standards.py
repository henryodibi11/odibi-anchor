"""Contracts for universal capture routing and task projection."""

from types import SimpleNamespace

from odibi_anchor._dispatcher._capture_standards import (
    capture_standards_contract,
    project_capture_guidance,
    render_capture_standards_markdown,
)
from odibi_anchor.planning import render_task_execution_report


def test_capture_contract_is_complete_and_truth_preserving() -> None:
    contract = capture_standards_contract()

    assert contract["version"] == "1.1"
    assert contract["scope"] == "all_tasks_and_managed_projects"
    assert "never create implementation authority" in contract["authority_rule"]
    assert "unavailable" in contract["truth_rule"]
    assert "search bounded memory" in contract["memory_rule"]
    assert "independent" in contract["memory_rule"]
    assert set(contract["common_fields"]) == {
        "context", "observation", "evidence", "interpretation", "uncertainty",
        "consequence", "next_authority", "provenance",
    }
    assert {route["record"] for route in contract["routes"]} == {
        "task_context", "journal", "observation", "evidence", "problem", "decision", "spec",
        "work_item", "memory_candidate", "lesson", "trail", "skill_or_reference",
        "source_record", "notebook", "verification_result", "snapshot", "handoff",
        "terminal_return",
    }
    assert all(route["required"] for route in contract["routes"])
    memory_route = next(route for route in contract["routes"] if route["record"] == "memory_candidate")
    assert {"project_or_trust_scope", "bounded_overlap_result"}.issubset(memory_route["required"])
    assert set(contract["references"]) == {"routing", "standards", "examples"}


def test_task_projection_is_bounded_deterministic_and_profile_aware() -> None:
    contract = capture_standards_contract()
    profile = SimpleNamespace(execution_mode="source_change", work_type="change")

    first = project_capture_guidance(
        contract,
        task_text="Investigate and fix the source contract.",
        profile=profile,
    )
    second = project_capture_guidance(
        contract,
        task_text="Investigate and fix the source contract.",
        profile=profile,
    )

    assert first == second
    assert first["memory_rule"] == contract["memory_rule"]
    selected = {route["record"] for route in first["routes"]}
    assert {"task_context", "journal", "observation", "evidence", "problem", "spec",
            "work_item", "verification_result", "memory_candidate",
            "terminal_return"}.issubset(selected)
    assert "lesson" not in selected
    assert len(first["routes"]) < len(contract["routes"])


def test_full_markdown_render_preserves_every_contract_dimension() -> None:
    contract = capture_standards_contract()

    rendered = render_capture_standards_markdown(contract)

    assert "**Scope:** `all_tasks_and_managed_projects`" in rendered
    assert contract["memory_rule"] in rendered
    assert "**Common fields:**" in rendered
    assert "Do not use for:" in rendered
    assert "Required:" in rendered
    assert "### Offline references" in rendered
    assert all(route["record"] in rendered for route in contract["routes"])
    assert all(path in rendered for path in contract["references"].values())


def test_task_projection_can_include_existing_artifact_kind() -> None:
    guidance = project_capture_guidance(
        capture_standards_contract(),
        task_text="Continue the bounded task.",
        profile=SimpleNamespace(execution_mode="read_only", work_type="analysis"),
        artifacts=({"kind": "decision"},),
    )

    assert "decision" in {route["record"] for route in guidance["routes"]}


def test_task_projection_accepts_runtime_artifact_entries() -> None:
    guidance = project_capture_guidance(
        capture_standards_contract(),
        task_text="Continue the bounded task.",
        profile=SimpleNamespace(execution_mode="read_only", work_type="analysis"),
        artifacts=(SimpleNamespace(kind="notebook"),),
    )

    assert "notebook" in {route["record"] for route in guidance["routes"]}


def test_task_renderer_exposes_capture_guidance() -> None:
    guidance = project_capture_guidance(
        capture_standards_contract(),
        task_text="Record an evidence-backed decision.",
        profile=SimpleNamespace(execution_mode="read_only", work_type="analysis"),
    )
    rendered = render_task_execution_report({
        "kind": "task_execution_context",
        "subject": "capture",
        "summary": "Capture correctly.",
        "status": "ready",
        "capture_guidance": guidance,
    })

    assert "## Capture Guidance v1.1" in rendered
    assert "`decision`" in rendered
    assert "Required:" in rendered
