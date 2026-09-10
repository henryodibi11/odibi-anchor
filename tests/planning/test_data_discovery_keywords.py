"""Focused text signals route shift-left data tools by canonical anchor action name."""
from odibi_anchor.planning._task_builders import _recommend_context_generators
from odibi_anchor.planning._task_profile import normalize_task_profile
from odibi_anchor.planning.context_selection import select_context


def _names_for(task):
    plan = select_context(
        normalize_task_profile(legacy_mode="data", task_text=task),
        task_text=task,
    )
    recs = _recommend_context_generators(context_plan=plan)
    return {r["name"] for r in recs}


def test_join_routes_to_pre_join():
    assert "pre_join" in _names_for("I need to join orders and customers")


def test_merge_into_routes_to_pre_merge():
    assert "pre_merge" in _names_for("merge into the silver target with upsert")


def test_empty_routes_to_diagnose_empty():
    assert "diagnose_empty" in _names_for("my result has zero rows")


def test_coerce_routes_to_coerce_check():
    assert "coerce_check" in _names_for("values differ by whitespace and case mismatch")


def test_lineage_routes_to_trace_row():
    assert "trace_row" in _names_for("trace value lineage for this wrong value")
