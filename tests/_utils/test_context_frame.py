"""Tests for _context_frame.py — ContextFrame, FrameRecorder, sub-contexts.

Tests:
    - Frame creation via ContextFrame.new()
    - FrameRecorder.is_recordable() detection
    - Finding extraction from StandardContract result
    - Risk extraction from StandardContract result
    - Non-StandardContract results are silently skipped
    - Action log grows with each record() call
    - to_dict() / from_dict() round-trip serialization
    - Section queries via frame.get_section()
    - Keyword search via frame.search_findings()
    - Sub-context updates (DataContext, CodeContext, MemoryContext, ComplianceContext)
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path


# ─── Module loading helpers ───────────────────────────────────────────────────

def _load_module(name: str, path: Path):
    """Load a module from an absolute path bypassing __init__.py chain."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_CW_SRC = Path(__file__).parent.parent.parent / "src"

_cf_module = _load_module(
    "odibi_anchor._utils._context_frame",
    _CW_SRC / "odibi_anchor" / "_utils" / "_context_frame.py",
)

ContextFrame = _cf_module.ContextFrame
FrameRecorder = _cf_module.FrameRecorder
Finding = _cf_module.Finding
Risk = _cf_module.Risk
Decision = _cf_module.Decision
Action = _cf_module.Action
DataContext = _cf_module.DataContext
CodeContext = _cf_module.CodeContext
MemoryContext = _cf_module.MemoryContext
ComplianceContext = _cf_module.ComplianceContext


# ─── Fixtures / helpers ───────────────────────────────────────────────────────

def _make_contract(
    kind: str = "test_tool",
    subject: str = "test.table",
    findings: list | None = None,
    risks: list | None = None,
    metrics: dict | None = None,
) -> dict:
    """Build a minimal StandardContract-shaped dict for testing."""
    return {
        "kind": kind,
        "subject": subject,
        "summary": f"{kind} summary",
        "metrics": metrics or {"row_count": 100},
        "findings": findings or [],
        "risks": risks or [],
        "samples": {},
        "suggested_next_actions": [],
    }


# ─── Tests: ContextFrame.new() ────────────────────────────────────────────────

def test_frame_creation_defaults():
    """ContextFrame.new() with defaults produces empty, valid frame."""
    frame = ContextFrame.new(project="test-project")

    assert frame.project == "test-project"
    assert frame.session_id  # non-empty
    assert len(frame.session_id) == 8  # 8-char UUID prefix
    assert frame.manifest is None
    assert frame.findings == []
    assert frame.risks == []
    assert frame.decisions == []
    assert frame.actions == []
    assert isinstance(frame.data_context, DataContext)
    assert isinstance(frame.code_context, CodeContext)
    assert isinstance(frame.memory_context, MemoryContext)
    assert isinstance(frame.compliance, ComplianceContext)
    assert isinstance(frame.created, datetime)
    assert isinstance(frame.last_updated, datetime)


def test_frame_creation_with_manifest():
    """ContextFrame.new() with manifest stores it."""
    manifest = {"project": {"name": "my-project"}, "version": "1.0"}
    frame = ContextFrame.new(session_id="abc12345", project="my-project", manifest=manifest)

    assert frame.session_id == "abc12345"
    assert frame.manifest == manifest


# ─── Tests: record() — finding extraction ─────────────────────────────────────

def test_record_extracts_findings():
    """record() extracts findings from a StandardContract result."""
    frame = ContextFrame.new(project="p")
    result = _make_contract(
        kind="dataset_profile",
        subject="catalog.schema.events",
        findings=["15% null rate on created_date", "duplicate key found on id"],
    )
    count = frame.record("profile_table", result)

    assert count == 2
    assert len(frame.findings) == 2
    assert frame.findings[0].source == "profile_table"
    assert frame.findings[0].subject == "catalog.schema.events"
    assert "null" in frame.findings[0].message
    assert frame.findings[0].category == "data_quality"  # inferred from kind=dataset_profile


def test_record_extracts_risks():
    """record() extracts risks from a StandardContract result."""
    frame = ContextFrame.new(project="p")
    result = _make_contract(
        risks=["MUST: Run anchor('preflight') before gate", "High null rate on join key"],
    )
    frame.record("status", result)

    assert len(frame.risks) == 2
    assert frame.risks[0].source == "status"
    assert "preflight" in frame.risks[0].message
    assert frame.risks[0].severity == "high"   # MUST: prefix → high


def test_record_non_dict_is_safe():
    """record() with a non-dict result (e.g. markdown string) silently skips."""
    frame = ContextFrame.new(project="p")
    count = frame.record("status", "# Markdown output\nsome content")
    assert count == 0
    assert frame.findings == []
    assert frame.risks == []
    assert frame.actions == []  # No action logged for non-dict results


def test_record_non_contract_dict_is_safe():
    """record() with a dict missing standard keys still doesn't crash."""
    frame = ContextFrame.new(project="p")
    count = frame.record("unknown_tool", {"arbitrary_key": "value", "nested": {"a": 1}})
    assert count == 0
    assert frame.findings == []
    assert len(frame.actions) == 1  # Action IS logged — it is a dict


def test_record_action_log_grows():
    """Every dict-returning tool call adds to the actions timeline."""
    frame = ContextFrame.new(project="p")

    frame.record("tool_a", _make_contract(findings=["f1"]))
    frame.record("tool_b", _make_contract(findings=["f2", "f3"]))
    frame.record("tool_c", _make_contract())  # No findings

    assert len(frame.actions) == 3
    assert frame.actions[0].tool == "tool_a"
    assert frame.actions[0].findings_produced == 1
    assert frame.actions[1].tool == "tool_b"
    assert frame.actions[1].findings_produced == 2
    assert frame.actions[2].tool == "tool_c"
    assert frame.actions[2].findings_produced == 0


# ─── Tests: frame is append-only ─────────────────────────────────────────────

def test_record_is_append_only():
    """Findings grow monotonically — record() never removes existing findings."""
    frame = ContextFrame.new(project="p")
    frame.record("tool_a", _make_contract(findings=["first finding"]))

    first_count = len(frame.findings)
    frame.record("tool_b", _make_contract(findings=["second finding"]))

    assert len(frame.findings) == first_count + 1
    assert frame.findings[0].message == "first finding"  # original preserved


# ─── Tests: serialization ─────────────────────────────────────────────────────

def test_to_dict_is_serializable():
    """to_dict() produces a JSON-compatible snapshot without non-serializable types."""
    import json

    frame = ContextFrame.new(session_id="abc12345", project="test")
    frame.record("profile_table", _make_contract(
        kind="dataset_profile",
        subject="my.table",
        findings=["3% null rate"],
        risks=["low freshness"],
        metrics={"row_count": 500, "null_rate": 0.03},
    ))

    d = frame.to_dict()
    # Must be JSON-serializable
    serialized = json.dumps(d, default=str)
    assert serialized  # non-empty

    assert d["session_id"] == "abc12345"
    assert d["project"] == "test"
    assert d["total_findings"] == 1
    assert d["total_risks"] == 1
    assert d["total_actions"] == 1
    assert "data_quality" in d["findings_by_category"]


def test_from_dict_round_trip():
    """from_dict(to_dict()) round-trips all key fields."""
    frame = ContextFrame.new(session_id="xyz99999", project="round-trip")
    frame.record("microscope", _make_contract(
        kind="microscope_result",
        subject="my.table.created_date",
        findings=["weekend gap pattern detected"],
    ))

    d = frame.to_dict()
    restored = ContextFrame.from_dict(d)

    assert restored.session_id == frame.session_id
    assert restored.project == frame.project
    assert len(restored.findings) == len(frame.findings)
    assert restored.findings[0].message == frame.findings[0].message
    assert restored.findings[0].source == frame.findings[0].source
    assert len(restored.actions) == len(frame.actions)


# ─── Tests: querying ─────────────────────────────────────────────────────────

def test_search_findings():
    """search_findings() returns findings whose message matches the keyword."""
    frame = ContextFrame.new(project="p")
    frame.record("tool", _make_contract(findings=[
        "15% null rate on created_date",
        "duplicate key found",
        "null values in join column id",
    ]))

    matched = frame.search_findings("null")
    assert len(matched) == 2
    assert all("null" in f.message.lower() for f in matched)


def test_findings_by_category():
    """findings_by_category() returns correct counts per category."""
    frame = ContextFrame.new(project="p")
    frame.record("profile_table", _make_contract(
        kind="dataset_profile",
        findings=["null issue", "null again"],
    ))
    frame.record("preflight", _make_contract(
        kind="preflight_context",
        findings=["naming violation"],
    ))

    cats = frame.findings_by_category()
    assert cats.get("data_quality", 0) == 2
    assert cats.get("convention", 0) == 1


# ─── Tests: sub-context updates ──────────────────────────────────────────────

def test_data_context_updated_by_profiler():
    """DataContext.tables_profiled is populated when profiler result is recorded."""
    frame = ContextFrame.new(project="p")
    frame.record("profile_table", _make_contract(
        kind="dataset_profile",
        subject="catalog.schema.orders",
        metrics={"row_count": 1000, "data_quality_score": 0.85, "grain": ["order_id"]},
    ))

    assert frame.data_context.has_profile("catalog.schema.orders")
    profile = frame.data_context.get_profile("catalog.schema.orders")
    assert profile["row_count"] == 1000
    assert frame.data_context.data_quality_scores["catalog.schema.orders"] == 0.85
    assert frame.data_context.grain_detected["catalog.schema.orders"] == ["order_id"]


def test_compliance_context_updated_by_status():
    """ComplianceContext.gates_passed is updated when status result is recorded."""
    frame = ContextFrame.new(project="p")
    frame.record("status", _make_contract(kind="session_status"))

    assert "status" in frame.compliance.gates_passed


def test_memory_context_updated_by_known_bad():
    """MemoryContext.known_bad_checked is set when known_bad result is recorded."""
    frame = ContextFrame.new(project="p")
    frame.record("known_bad", _make_contract(kind="known_bad_context"))

    assert frame.memory_context.known_bad_checked is True


# ─── Tests: FrameRecorder ─────────────────────────────────────────────────────

def test_frame_recorder_is_recordable():
    """FrameRecorder.is_recordable() correctly identifies StandardContract dicts."""
    recorder = FrameRecorder()

    assert recorder.is_recordable({"kind": "test", "findings": []}) is True
    assert recorder.is_recordable({"findings": ["x"]}) is True
    assert recorder.is_recordable({"risks": ["r"]}) is True
    assert recorder.is_recordable("markdown string") is False
    assert recorder.is_recordable(42) is False
    assert recorder.is_recordable(None) is False
    assert recorder.is_recordable({"no_standard_keys": True}) is False


def test_frame_recorder_delegates_to_frame():
    """FrameRecorder.record() delegates to frame.record() correctly."""
    frame = ContextFrame.new(project="p")
    recorder = FrameRecorder()

    result = _make_contract(findings=["test finding"])
    count = recorder.record(frame, "test_action", result)

    assert count == 1
    assert len(frame.findings) == 1
    assert frame.findings[0].source == "test_action"
