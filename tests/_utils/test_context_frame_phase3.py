"""Tests for Context Frame Phase 3 — contextual suggestions + tool frame wiring."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone
from typing import Any

import pytest

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from odibi_anchor._utils._context_frame import (
    ContextFrame, Finding, Risk, MemoryContext, DataContext,
)


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

def _frame_with_profile_finding(subject: str, column: str, msg: str) -> ContextFrame:
    """Frame with a profiler finding containing null anomaly for a specific column."""
    frame = ContextFrame.new(project="test")
    frame.findings.append(Finding(
        source="profile_table",
        category="quality",
        severity="medium",
        message=msg,
        subject=f"{subject}.{column}",
        evidence={},
        timestamp=datetime.now(timezone.utc),
    ))
    return frame


def _frame_with_memory_gotcha(subject: str) -> ContextFrame:
    """Frame with a memory gotcha registered for the given subject."""
    frame = ContextFrame.new(project="test")
    frame.memory_context.loaded_memories.append({
        "type": "gotcha",
        "content": f"Known issue for {subject}: date column has timezone offset bugs",
        "tags_list": [f"file:{subject}"],
    })
    return frame


def _frame_with_profile(subject: str) -> ContextFrame:
    """Frame whose data_context already has a profile for subject."""
    frame = ContextFrame.new(project="test")
    frame.data_context.tables_profiled[subject] = {"grain": ["id"], "row_count": 1000}
    return frame


def _frame_with_high_risks(count: int = 3) -> ContextFrame:
    """Frame with N high-severity risks."""
    frame = ContextFrame.new(project="test")
    for i in range(count):
        frame.risks.append(Risk(
            source="test",
            severity="high",
            message=f"High risk #{i+1}",
            mitigation="",
            timestamp=datetime.now(timezone.utc),
        ))
    return frame


# ---------------------------------------------------------------------------
# Import _build_contextual_suggestions from its extracted module (_dispatcher._frame)
# ---------------------------------------------------------------------------

def _load_build_contextual_suggestions():
    """Import _build_contextual_suggestions from its extracted module."""
    from odibi_anchor._dispatcher._frame import _build_contextual_suggestions
    return _build_contextual_suggestions


# ---------------------------------------------------------------------------
# Tests: _build_contextual_suggestions
# ---------------------------------------------------------------------------

class TestBuildContextualSuggestions:
    """Test the dispatcher-level contextual suggestion injector."""

    @pytest.fixture
    def build_fn(self):
        return _load_build_contextual_suggestions()

    def test_empty_frame_returns_empty(self, build_fn):
        """No findings/risks → no contextual suggestions."""
        frame = ContextFrame.new(project="test")
        result = {"kind": "exploration_context", "suggested_next_actions": []}
        suggestions = build_fn(frame, result)
        assert suggestions == []

    def test_null_finding_suggests_microscope(self, build_fn):
        """Profiler null anomaly → suggests microscope with column name."""
        frame = _frame_with_profile_finding("orders", "created_date", "15% null rate on created_date")
        result = {"kind": "exploration_context", "subject": "orders", "suggested_next_actions": []}
        suggestions = build_fn(frame, result)
        microscope_sugs = [s for s in suggestions if "microscope" in s.lower()]
        assert microscope_sugs, f"Expected microscope suggestion, got: {suggestions}"
        assert "created_date" in microscope_sugs[0]

    def test_high_risk_accumulation_suggests_gate(self, build_fn):
        """3+ high-severity risks → MUST: anchor('gate') suggestion."""
        frame = _frame_with_high_risks(3)
        result = {"kind": "profile_table", "subject": "orders", "suggested_next_actions": []}
        suggestions = build_fn(frame, result)
        gate_sugs = [s for s in suggestions if "gate" in s.lower()]
        assert gate_sugs, f"Expected gate suggestion for 3 high risks, got: {suggestions}"

    def test_no_duplicate_suggestions(self, build_fn):
        """Existing suggestions are not duplicated."""
        frame = _frame_with_profile_finding("orders", "amount", "null on amount column")
        existing_sug = "SHOULD: anchor('microscope', df, 'amount') — profiler found null anomaly"
        result = {
            "kind": "exploration_context",
            "subject": "orders",
            "suggested_next_actions": [existing_sug],
        }
        suggestions = build_fn(frame, result)
        all_sug = result["suggested_next_actions"] + suggestions
        amount_sugs = [s for s in all_sug if "amount" in s and "microscope" in s]
        assert len(amount_sugs) == 1, f"Duplicate suggestion found: {amount_sugs}"

    def test_never_raises_on_broken_frame(self, build_fn):
        """Broken frame object → empty list, no exception."""
        frame = MagicMock()
        frame.findings = None  # Will cause AttributeError
        result = {"suggested_next_actions": []}
        suggestions = build_fn(frame, result)
        assert suggestions == []

    def test_memory_gotcha_for_changed_file(self, build_fn):
        """Memory gotcha for a changed file → MUST review warning."""
        frame = ContextFrame.new(project="test")
        frame.code_context.files_changed.add("src/lib/transform.py")
        frame.memory_context.loaded_memories.append({
            "type": "gotcha",
            "content": "timezone bug in date handling in src/lib/transform.py",
            "tags_list": ["file:src/lib/transform.py"],
        })
        result = {"kind": "safe", "subject": "src/lib/transform.py", "suggested_next_actions": []}
        suggestions = build_fn(frame, result)
        gotcha_sugs = [s for s in suggestions if "gotcha" in s.lower() or "memory" in s.lower()]
        assert gotcha_sugs, f"Expected memory gotcha suggestion, got: {suggestions}"


# NOTE: exploration_context and dataset_profile_context were consolidated into
# the table_profiler tool (anchor("profile_table")). Their frame= parameter tests
# were removed along with the modules.


# ---------------------------------------------------------------------------
# Tests: MemoryContext.has_gotcha_for / get_gotchas_for
# ---------------------------------------------------------------------------

class TestMemoryContextGotchaHelpers:
    """Test has_gotcha_for / get_gotchas_for on MemoryContext."""

    def test_has_gotcha_for_returns_true(self):
        mc = MemoryContext()
        mc.loaded_memories.append({"type": "gotcha", "content": "date bug in src/lib/transform.py"})
        assert mc.has_gotcha_for("src/lib/transform.py")

    def test_has_gotcha_for_returns_false(self):
        mc = MemoryContext()
        assert not mc.has_gotcha_for("nonexistent.py")

    def test_get_gotchas_for_returns_list(self):
        mc = MemoryContext()
        mc.loaded_memories.append({"type": "gotcha", "content": "bug1 in orders table"})
        mc.loaded_memories.append({"type": "gotcha", "content": "bug2 in orders table"})
        result = mc.get_gotchas_for("orders")
        assert len(result) == 2
        assert all("orders" in r for r in result)

    def test_get_gotchas_for_missing_returns_empty(self):
        mc = MemoryContext()
        result = mc.get_gotchas_for("missing")
        assert result == []


# ---------------------------------------------------------------------------
# Tests: DataContext.has_profile
# ---------------------------------------------------------------------------

class TestDataContextHasProfile:
    """Test has_profile on DataContext."""

    def test_has_profile_true(self):
        dc = DataContext()
        dc.tables_profiled["orders"] = {"row_count": 100}
        assert dc.has_profile("orders")

    def test_has_profile_false(self):
        dc = DataContext()
        assert not dc.has_profile("nonexistent")
