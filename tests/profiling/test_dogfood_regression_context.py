"""Tests for dogfood_regression_context."""

import json
import os
import time
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from odibi_anchor.profiling.dogfood_regression_context import (
    dogfood_regression_context,
    render_dogfood_regression_report,
    _slugify,
    _compute_diffs,
    _classify_change,
    _load_baseline,
    _save_baseline,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_output():
    """A minimal valid tool output."""
    return {
        "kind": "exploration_context",
        "subject": "silver.queue_positions",
        "summary": "Explored table with 5000 rows.",
        "metrics": {
            "row_count": 5000,
            "null_pct": 2.5,
            "completeness_pct": 97.5,
            "uniqueness_pct": 85.0,
        },
        "findings": ["Found 5 columns with >10% nulls."],
        "risks": ["High null rate in cod_date column."],
    }


@pytest.fixture
def baseline_dir(tmp_path):
    """A temporary baseline directory."""
    bd = tmp_path / ".dogfood_baselines"
    bd.mkdir()
    return bd


# ---------------------------------------------------------------------------
# Output Contract Tests
# ---------------------------------------------------------------------------

class TestOutputContract:
    """Test that output follows standard contract."""

    def test_has_required_keys(self, sample_output, tmp_path):
        ctx = dogfood_regression_context(sample_output, root=str(tmp_path))
        required = {"kind", "subject", "summary", "metrics", "findings", "risks",
                    "samples", "suggested_next_actions"}
        assert required.issubset(set(ctx.keys()))

    def test_kind_is_correct(self, sample_output, tmp_path):
        ctx = dogfood_regression_context(sample_output, root=str(tmp_path))
        assert ctx["kind"] == "dogfood_regression_context"

    def test_subject_includes_tool_kind(self, sample_output, tmp_path):
        ctx = dogfood_regression_context(sample_output, root=str(tmp_path))
        assert "exploration_context" in ctx["subject"]
        assert "silver.queue_positions" in ctx["subject"]

    def test_has_diffs_field(self, sample_output, tmp_path):
        ctx = dogfood_regression_context(sample_output, root=str(tmp_path))
        assert "diffs" in ctx
        assert isinstance(ctx["diffs"], list)

    def test_has_baseline_path_field(self, sample_output, tmp_path):
        ctx = dogfood_regression_context(sample_output, root=str(tmp_path))
        assert "baseline_path" in ctx

    def test_learning_guidance_does_not_force_memory_save(self, sample_output, tmp_path):
        ctx = dogfood_regression_context(sample_output, root=str(tmp_path))
        assert isinstance(ctx, dict)
        actions = "\n".join(ctx["suggested_next_actions"])
        assert 'anchor("save"' not in actions
        assert "nothing_reusable_learned" in actions


# ---------------------------------------------------------------------------
# Baseline Storage Tests
# ---------------------------------------------------------------------------

class TestBaselineStorage:
    """Test baseline save/load cycle."""

    def test_save_creates_file(self, sample_output, tmp_path):
        dogfood_regression_context(
            sample_output, save_as_baseline=True, root=str(tmp_path)
        )
        baseline_dir = tmp_path / ".dogfood_baselines"
        assert baseline_dir.exists()
        files = list(baseline_dir.glob("*.json"))
        assert len(files) == 1

    def test_save_then_load_roundtrip(self, sample_output, tmp_path):
        # Save
        dogfood_regression_context(
            sample_output, save_as_baseline=True, root=str(tmp_path)
        )
        # Load (compare same data)
        ctx = dogfood_regression_context(sample_output, root=str(tmp_path))
        assert ctx["metrics"]["regressions_count"] == 0
        assert ctx["metrics"]["improvements_count"] == 0
        assert len(ctx["diffs"]) == 0

    def test_no_baseline_suggests_save(self, sample_output, tmp_path):
        ctx = dogfood_regression_context(sample_output, root=str(tmp_path))
        assert "save_as_baseline=True" in ctx["summary"]
        assert any("save_as_baseline" in a for a in ctx["suggested_next_actions"])

    def test_baseline_metadata_has_timestamp(self, sample_output, tmp_path):
        dogfood_regression_context(
            sample_output, save_as_baseline=True, root=str(tmp_path)
        )
        baseline_dir = tmp_path / ".dogfood_baselines"
        files = list(baseline_dir.glob("*.json"))
        with open(files[0]) as f:
            data = json.load(f)
        assert "saved_at" in data
        assert "saved_at_epoch" in data
        assert "output" in data


# ---------------------------------------------------------------------------
# Diff Detection Tests
# ---------------------------------------------------------------------------

class TestDiffDetection:
    """Test diff computation and classification."""

    def test_detects_metric_improvement(self, sample_output, tmp_path):
        # Save baseline
        dogfood_regression_context(
            sample_output, save_as_baseline=True, root=str(tmp_path)
        )
        # Improve coverage_gaps_count (lower is better for this metric)
        improved = dict(sample_output)
        improved["metrics"] = dict(sample_output["metrics"])
        improved["metrics"]["null_pct"] = 1.0  # Improved from 2.5
        
        ctx = dogfood_regression_context(improved, root=str(tmp_path))
        assert ctx["metrics"]["improvements_count"] >= 1
        assert any(d["verdict"] == "improvement" for d in ctx["diffs"])

    def test_detects_metric_regression(self, sample_output, tmp_path):
        # Save baseline
        dogfood_regression_context(
            sample_output, save_as_baseline=True, root=str(tmp_path)
        )
        # Regress: increase null_pct
        regressed = dict(sample_output)
        regressed["metrics"] = dict(sample_output["metrics"])
        regressed["metrics"]["null_pct"] = 15.0  # Regressed from 2.5
        
        ctx = dogfood_regression_context(regressed, root=str(tmp_path))
        assert ctx["metrics"]["regressions_count"] >= 1
        assert any(d["verdict"] == "regression" for d in ctx["diffs"])

    def test_detects_risk_improvement(self, sample_output, tmp_path):
        # Save baseline with risks
        dogfood_regression_context(
            sample_output, save_as_baseline=True, root=str(tmp_path)
        )
        # Remove risks = improvement
        improved = dict(sample_output)
        improved["risks"] = []
        
        ctx = dogfood_regression_context(improved, root=str(tmp_path))
        assert any(
            d["verdict"] == "improvement" and "risk" in d["field"].lower()
            for d in ctx["diffs"]
        )

    def test_no_diffs_when_identical(self, sample_output, tmp_path):
        dogfood_regression_context(
            sample_output, save_as_baseline=True, root=str(tmp_path)
        )
        ctx = dogfood_regression_context(sample_output, root=str(tmp_path))
        assert len(ctx["diffs"]) == 0
        assert ctx["metrics"]["improvements_count"] == 0
        assert ctx["metrics"]["regressions_count"] == 0


# ---------------------------------------------------------------------------
# Format and Validation
# ---------------------------------------------------------------------------

class TestFormatAndValidation:
    """Test output format and input validation."""

    def test_output_format_dict(self, sample_output, tmp_path):
        ctx = dogfood_regression_context(
            sample_output, output_format="dict", root=str(tmp_path)
        )
        assert isinstance(ctx, dict)

    def test_output_format_markdown(self, sample_output, tmp_path):
        md = dogfood_regression_context(
            sample_output, output_format="markdown", root=str(tmp_path)
        )
        assert isinstance(md, str)
        assert "Dog-Food Regression:" in md

    def test_invalid_format_raises(self, sample_output, tmp_path):
        with pytest.raises(ValueError, match="output_format"):
            dogfood_regression_context(
                sample_output, output_format="xml", root=str(tmp_path)
            )

    def test_non_dict_input_raises(self, tmp_path):
        with pytest.raises(TypeError, match="must be a dict"):
            dogfood_regression_context("not a dict", root=str(tmp_path))

    def test_missing_kind_raises(self, tmp_path):
        with pytest.raises(ValueError, match="kind"):
            dogfood_regression_context({"subject": "x"}, root=str(tmp_path))


# ---------------------------------------------------------------------------
# Render Function
# ---------------------------------------------------------------------------

class TestRenderFunction:
    """Test render_dogfood_regression_report."""

    def test_render_contains_summary(self, sample_output, tmp_path):
        ctx = dogfood_regression_context(sample_output, root=str(tmp_path))
        md = render_dogfood_regression_report(ctx)
        assert "Dog-Food Regression:" in md
        assert ctx["summary"] in md

    def test_render_contains_metrics(self, sample_output, tmp_path):
        ctx = dogfood_regression_context(sample_output, root=str(tmp_path))
        md = render_dogfood_regression_report(ctx)
        assert "| Metric | Value |" in md

    def test_render_shows_diffs(self, sample_output, tmp_path):
        dogfood_regression_context(
            sample_output, save_as_baseline=True, root=str(tmp_path)
        )
        improved = dict(sample_output)
        improved["metrics"] = dict(sample_output["metrics"])
        improved["metrics"]["null_pct"] = 0.5
        ctx = dogfood_regression_context(improved, root=str(tmp_path))
        md = render_dogfood_regression_report(ctx)
        assert "Diffs" in md


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

class TestHelperFunctions:
    """Test internal helpers."""

    def test_slugify_basic(self):
        assert _slugify("hello_world") == "hello_world"

    def test_slugify_with_special_chars(self):
        result = _slugify("exploration_context::silver.queue_positions")
        assert "." not in result
        assert ":" not in result
        assert result == "exploration_context_silver_queue_positions"

    def test_slugify_removes_double_underscores(self):
        result = _slugify("a__b___c")
        assert "__" not in result

    def test_classify_change_higher_is_better(self):
        # completeness_pct: higher is better
        assert _classify_change("metrics.completeness_pct", 90.0, 95.0) == "improvement"
        assert _classify_change("metrics.completeness_pct", 95.0, 90.0) == "regression"
        assert _classify_change("metrics.completeness_pct", 90.0, 90.0) == "unchanged"

    def test_classify_change_lower_is_better(self):
        # null_pct: lower is better
        assert _classify_change("metrics.null_pct", 5.0, 2.0) == "improvement"
        assert _classify_change("metrics.null_pct", 2.0, 5.0) == "regression"

    def test_compute_diffs_empty_when_equal(self):
        output = {"metrics": {"a": 1}, "findings": ["x"], "risks": []}
        assert _compute_diffs(output, output) == []

    def test_load_baseline_returns_none_for_missing(self, tmp_path):
        result = _load_baseline(tmp_path / "nonexistent.json")
        assert result is None
