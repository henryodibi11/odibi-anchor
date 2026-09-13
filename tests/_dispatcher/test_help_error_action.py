"""H/AXI: dispatcher help must direct callers to real, documented actions.

anchor() dispatches 'known_error', not 'error'. The intent map previously suggested
'error', which an agent could not call.
"""
from pathlib import Path

from odibi_anchor._dispatcher._dispatch_table import (
    _INTENT_MAP,
    ACTION_GROUPS,
    DISPATCH_SIGS,
    PUBLIC_ACTION_METADATA,
    build_help_text,
)
from odibi_anchor._dispatcher._effects import BUILTIN_ACTION_NAMES


def test_no_intent_references_error_action():
    for intent, entry in _INTENT_MAP.items():
        use = entry.get("use", [])
        assert "error" not in use, f"intent {intent!r} references non-existent 'error' action"


def test_known_error_present_in_action_groups():
    flat = {a for actions in ACTION_GROUPS.values() for a in actions}
    assert "known_error" in flat
    assert "error" not in flat


def test_canonical_public_action_metadata_covers_runtime_and_packaged_tools_once():
    grouped = [action for actions in ACTION_GROUPS.values() for action in actions]
    metadata = set(PUBLIC_ACTION_METADATA)

    assert len(grouped) == len(set(grouped))
    assert set(grouped) == metadata == set(DISPATCH_SIGS)
    assert {
        name for name, entry in PUBLIC_ACTION_METADATA.items() if entry.kind == "builtin"
    } == set(BUILTIN_ACTION_NAMES)
    assert {
        name for name, entry in PUBLIC_ACTION_METADATA.items()
        if entry.kind == "registered_tool"
    } == {"coerce_fix", "diagnose_empty", "explain_row", "pre_join", "pre_merge"}
    for action in ("apply_sql", "help", "orient", "quick", "skill_loaded", "sync", "task_adoption", "task_rebind"):
        assert action in metadata


def test_memory_help_documents_lifecycle_subcommand_contracts():
    help_text = build_help_text("memory")

    assert "final relevance-ranked matches" in help_text
    for field in ("total_matches", "returned_count", "offset", "next_offset", "has_more"):
        assert f"`{field}`" in help_text
    for subcommand in ("apply", "disposition", "evaluate", "diagnostics", "promotion"):
        assert f"`{subcommand}`:" in help_text
    assert "JSON objects, not strings" in help_text
    assert 'all_pending=True' in help_text
    assert 'request_owner_activation' in help_text
    assert 'provider="databricks_in_session"' in help_text
    assert 'anchor("reject", "<memory_id>")' in help_text
    assert "in_session_approval" in help_text


def test_task_help_documents_bounded_memory_limit():
    help_text = build_help_text("task")

    assert "`memory_limit`" in help_text
    assert "1 through 20" in help_text
    assert "default 5" in help_text


def test_learning_help_documents_subcommand_contracts():
    help_text = build_help_text("learning")

    for subcommand in ("capture", "assess", "safe_stop"):
        assert f"`{subcommand}`:" in help_text
    assert "reusable_practice" in help_text
    assert "non-empty list of objects" in help_text
    assert "reference_type" in help_text
    assert "observed_at" in help_text
    assert "nothing_reusable_learned" in help_text
    assert "unavailable_evidence" in help_text
    assert 'status="blocked"' in help_text


def test_legacy_counters_are_documented_as_deprecated_non_authoritative_compatibility_fields():
    root = Path(__file__).parents[2]
    reference = (root / ".assistant/references/odibi-anchor/actions.md").read_text(
        encoding="utf-8",
    )

    assert "Legacy counters are deprecated non-authoritative compatibility fields" in reference
    for name in ("use_count", "surface_count", "applied_count", "sessions_seen"):
        assert f"`{name}`" in reference
