"""Tests for diagnose_empty_context tool.

Covers all 6 diagnostic checks:
1. Row count funnel
2. Key overlap analysis
3. Filter boundary analysis
4. All-NULL column detection
5. Type mismatch detection
6. Cause ranking and suggestions
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

# Load module using file path (tools are standalone)
_TOOL_DIR = Path(__file__).parent
_spec = importlib.util.spec_from_file_location(
    "diagnose_empty_impl", _TOOL_DIR / "diagnose_empty_impl.py"
)
assert _spec is not None
assert _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

diagnose_empty_context = _mod.diagnose_empty_context
_detect_engine = _mod._detect_engine
_build_row_count_funnel = _mod._build_row_count_funnel
_key_overlap_pandas = _mod._key_overlap_pandas
_filter_boundary_analysis = _mod._filter_boundary_analysis
_all_null_columns = _mod._all_null_columns
_type_mismatch_detection = _mod._type_mismatch_detection
_rank_causes = _mod._rank_causes
_classify_value_match = getattr(_mod, "_classify_value_match", None)


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def source_df():
    """Source DataFrame with project data."""
    return pd.DataFrame({
        "project_id": ["P1", "P2", "P3", "P4", "P5"],
        "name": ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"],
        "capacity_mw": [100, 200, 300, 400, 500],
    })


@pytest.fixture
def dim_df():
    """Dimension DataFrame with overlapping keys."""
    return pd.DataFrame({
        "project_id": ["P1", "P2", "P3", "P6", "P7"],
        "market": ["PJM", "ERCOT", "MISO", "SPP", "CAISO"],
        "state": ["TX", "TX", "IL", "OK", "CA"],
    })


@pytest.fixture
def no_overlap_df():
    """DataFrame with zero key overlap with source_df."""
    return pd.DataFrame({
        "project_id": ["X1", "X2", "X3"],
        "market": ["PJM", "ERCOT", "MISO"],
    })


@pytest.fixture
def empty_result():
    """Empty result DataFrame."""
    return pd.DataFrame({
        "project_id": pd.Series([], dtype="object"),
        "name": pd.Series([], dtype="object"),
        "capacity_mw": pd.Series([], dtype="float64"),
        "market": pd.Series([], dtype="object"),
    })


@pytest.fixture
def type_mismatch_df():
    """DataFrame with integer project_id (vs string in source)."""
    return pd.DataFrame({
        "project_id": [1, 2, 3],
        "cost": [1000, 2000, 3000],
    })


@pytest.fixture
def all_null_df():
    """DataFrame with an all-NULL key column."""
    return pd.DataFrame({
        "project_id": [None, None, None],
        "market": ["PJM", "ERCOT", "MISO"],
    })


# ============================================================================
# Test: Engine detection
# ============================================================================


class TestEngineDetection:
    def test_pandas_detection(self, source_df):
        assert _detect_engine(source_df) == "pandas"

    def test_unknown_detection(self):
        assert _detect_engine("not a dataframe") == "unknown"

    def test_dict_unknown(self):
        assert _detect_engine({"a": 1}) == "unknown"


# ============================================================================
# Test: Row count funnel
# ============================================================================


class TestRowCountFunnel:
    def test_basic_funnel(self, source_df, dim_df, empty_result):
        funnel = _build_row_count_funnel(
            empty_result,
            {"source": source_df, "dim": dim_df},
            "pandas",
        )
        assert funnel["source"]["count"] == 5
        assert funnel["dim"]["count"] == 5
        assert funnel["__result__"]["count"] == 0
        assert funnel["source"]["columns"] == 3
        assert funnel["dim"]["columns"] == 3

    def test_single_upstream(self, source_df, empty_result):
        funnel = _build_row_count_funnel(
            empty_result,
            {"source": source_df},
            "pandas",
        )
        assert "source" in funnel
        assert "__result__" in funnel
        assert len(funnel) == 2


# ============================================================================
# Test: Key overlap analysis
# ============================================================================


class TestKeyOverlap:
    def test_partial_overlap(self, source_df, dim_df):
        result = _key_overlap_pandas(
            source_df, dim_df, ["project_id"], "source", "dim", 10
        )
        assert result["distinct_a"] == 5
        assert result["distinct_b"] == 5
        assert result["overlap_count"] == 3  # P1, P2, P3
        assert result["only_in_a_count"] == 2  # P4, P5
        assert result["only_in_b_count"] == 2  # P6, P7

    def test_zero_overlap(self, source_df, no_overlap_df):
        result = _key_overlap_pandas(
            source_df, no_overlap_df, ["project_id"], "source", "no_overlap", 10
        )
        assert result["overlap_count"] == 0
        assert result["overlap_pct"] == 0.0
        assert result["only_in_a_count"] == 5
        assert result["only_in_b_count"] == 3

    def test_full_overlap(self):
        df_a = pd.DataFrame({"id": [1, 2, 3]})
        df_b = pd.DataFrame({"id": [1, 2, 3]})
        result = _key_overlap_pandas(df_a, df_b, ["id"], "a", "b", 10)
        assert result["overlap_count"] == 3
        assert result["overlap_pct"] == 1.0
        assert result["only_in_a_count"] == 0
        assert result["only_in_b_count"] == 0

    def test_composite_key_overlap(self):
        df_a = pd.DataFrame({"id": [1, 1, 2], "date": ["A", "B", "A"]})
        df_b = pd.DataFrame({"id": [1, 2, 2], "date": ["A", "A", "B"]})
        result = _key_overlap_pandas(df_a, df_b, ["id", "date"], "a", "b", 10)
        # (1,A) and (2,A) overlap; (1,B) only in a; (2,B) only in b
        assert result["overlap_count"] == 2
        assert result["only_in_a_count"] == 1
        assert result["only_in_b_count"] == 1


# ============================================================================
# Test: Filter boundary analysis
# ============================================================================


class TestFilterBoundary:
    def test_filter_kills_all(self, source_df):
        result = _filter_boundary_analysis(
            {"source": source_df},
            "capacity_mw > 9000",
            10,
        )
        assert result["per_upstream"]["source"]["all_fail"] is True
        assert result["per_upstream"]["source"]["passing"] == 0

    def test_filter_passes_some(self, source_df):
        result = _filter_boundary_analysis(
            {"source": source_df},
            "capacity_mw > 200",
            10,
        )
        assert result["per_upstream"]["source"]["all_fail"] is False
        assert result["per_upstream"]["source"]["passing"] == 3  # 300, 400, 500

    def test_filter_passes_all(self, source_df):
        result = _filter_boundary_analysis(
            {"source": source_df},
            "capacity_mw > 0",
            10,
        )
        assert result["per_upstream"]["source"]["passing"] == 5
        assert result["per_upstream"]["source"]["all_fail"] is False

    def test_invalid_filter_expr(self, source_df):
        result = _filter_boundary_analysis(
            {"source": source_df},
            "nonexistent_col > 100",
            10,
        )
        # Should capture error, not crash
        assert result["per_upstream"]["source"]["error"] is not None

    def test_empty_upstream_filter(self):
        empty = pd.DataFrame({"x": pd.Series([], dtype="int64")})
        result = _filter_boundary_analysis({"empty": empty}, "x > 0", 10)
        assert result["per_upstream"]["empty"]["total"] == 0
        assert result["per_upstream"]["empty"]["all_fail"] is True


# ============================================================================
# Test: All-NULL column detection
# ============================================================================


class TestAllNullColumns:
    def test_no_null_columns(self, source_df):
        result = _all_null_columns({"source": source_df})
        assert result["source"] == []

    def test_all_null_key(self, all_null_df):
        result = _all_null_columns({"src": all_null_df})
        assert "project_id" in result["src"]
        assert "market" not in result["src"]

    def test_mixed_nulls(self):
        df = pd.DataFrame({
            "a": [None, None, None],
            "b": [1, None, 3],
            "c": [None, None, None],
        })
        result = _all_null_columns({"mixed": df})
        assert sorted(result["mixed"]) == ["a", "c"]

    def test_empty_dataframe(self):
        df = pd.DataFrame({"a": pd.Series([], dtype="object"), "b": pd.Series([], dtype="int64")})
        result = _all_null_columns({"empty": df})
        # Empty df — all columns are vacuously null
        assert set(result["empty"]) == {"a", "b"}


# ============================================================================
# Test: Type mismatch detection
# ============================================================================


class TestTypeMismatch:
    def test_string_vs_int(self, source_df, type_mismatch_df):
        result = _type_mismatch_detection(
            {"source": source_df, "costs": type_mismatch_df},
            ["project_id"],
        )
        assert len(result) == 1
        assert result[0]["key_column"] == "project_id"
        assert "object" in result[0]["types_by_source"]["source"]
        assert "int" in result[0]["types_by_source"]["costs"]

    def test_same_types_no_mismatch(self, source_df, dim_df):
        result = _type_mismatch_detection(
            {"source": source_df, "dim": dim_df},
            ["project_id"],
        )
        assert len(result) == 0

    def test_float_vs_int_not_flagged(self):
        """float64 (e.g. a nullable key) vs int64 coerce in joins — not a
        mismatch. Regression: previously raw dtype compare flagged this."""
        float_df = pd.DataFrame({"id": [100.0, 101.0, None]})  # null -> float64
        int_df = pd.DataFrame({"id": [100, 101]})  # int64
        result = _type_mismatch_detection(
            {"source": float_df, "dim": int_df},
            ["id"],
        )
        assert result == []

    def test_key_not_in_all_upstreams(self, source_df):
        other = pd.DataFrame({"other_id": [1, 2], "val": ["a", "b"]})
        result = _type_mismatch_detection(
            {"source": source_df, "other": other},
            ["project_id"],
        )
        # other doesn't have project_id, so only source has it — no mismatch
        assert len(result) == 0


# ============================================================================
# Test: Cause ranking
# ============================================================================


class TestCauseRanking:
    def test_type_mismatch_highest_priority(self):
        funnel = {"a": {"count": 10}, "b": {"count": 10}, "__result__": {"count": 0}}
        type_mismatches = [{
            "key_column": "id",
            "types_by_source": {"a": "object", "b": "int64"},
            "unique_types": ["int64", "object"],
        }]
        stage, cause, _ = _rank_causes(
            funnel, [], None, {"a": [], "b": []}, type_mismatches, ["id"]
        )
        assert cause == "key_type_mismatch"
        assert "id" in stage

    def test_zero_overlap_second_priority(self):
        funnel = {"a": {"count": 10}, "b": {"count": 10}, "__result__": {"count": 0}}
        overlaps = [{
            "pair": "a↔b",
            "common_keys": ["id"],
            "distinct_a": 10,
            "distinct_b": 10,
            "overlap_count": 0,
            "overlap_pct": 0.0,
            "only_in_a_count": 10,
            "only_in_b_count": 10,
            "sample_only_a": [],
            "sample_only_b": [],
        }]
        stage, cause, _ = _rank_causes(
            funnel, overlaps, None, {"a": [], "b": []}, [], ["id"]
        )
        assert cause == "zero_key_overlap"

    def test_filter_kills_all_third_priority(self):
        funnel = {"src": {"count": 100}, "__result__": {"count": 0}}
        filter_analysis = {
            "filter_expr": "x > 9000",
            "per_upstream": {
                "src": {"total": 100, "passing": 0, "all_fail": True, "error": None}
            },
        }
        stage, cause, _ = _rank_causes(
            funnel, [], filter_analysis, {"src": []}, [], []
        )
        assert cause == "filter_kills_all"

    def test_null_keys_fourth_priority(self):
        funnel = {"src": {"count": 10}, "__result__": {"count": 0}}
        null_columns = {"src": ["id", "name"]}
        stage, cause, _ = _rank_causes(
            funnel, [], None, null_columns, [], ["id"]
        )
        assert cause == "key_columns_all_null"

    def test_empty_upstream(self):
        funnel = {"src": {"count": 0}, "__result__": {"count": 0}}
        stage, cause, _ = _rank_causes(
            funnel, [], None, {"src": []}, [], []
        )
        assert cause == "empty_upstream"

    def test_unknown_cause(self):
        funnel = {"src": {"count": 10}, "__result__": {"count": 0}}
        stage, cause, findings = _rank_causes(
            funnel, [], None, {"src": []}, [], []
        )
        assert cause == "undetermined"
        assert len(findings) > 0


# ============================================================================
# Test: Main entry point — full integration
# ============================================================================


class TestDiagnoseEmptyContext:
    def test_basic_contract_shape(self, empty_result, source_df, dim_df):
        result = diagnose_empty_context(
            result_df=empty_result,
            upstreams={"source": source_df, "dim": dim_df},
            keys=["project_id"],
        )
        # Standard contract keys
        assert result["kind"] == "diagnose_empty"
        assert "subject" in result
        assert "summary" in result
        assert isinstance(result["metrics"], dict)
        assert isinstance(result["findings"], list)
        assert isinstance(result["risks"], list)
        assert isinstance(result["samples"], dict)
        assert isinstance(result["suggested_next_actions"], list)
        assert "funnel" in result

    def test_zero_overlap_detection(self, empty_result, source_df, no_overlap_df):
        result = diagnose_empty_context(
            result_df=empty_result,
            upstreams={"source": source_df, "no_overlap": no_overlap_df},
            keys=["project_id"],
        )
        assert result["metrics"]["dropout_cause"] == "zero_key_overlap"
        assert "zero_key_overlap" in result["metrics"]["dropout_cause"]

    def test_type_mismatch_detection(
        self, empty_result, source_df, type_mismatch_df
    ):
        result = diagnose_empty_context(
            result_df=empty_result,
            upstreams={"source": source_df, "costs": type_mismatch_df},
            keys=["project_id"],
        )
        assert result["metrics"]["dropout_cause"] == "key_type_mismatch"
        assert "type_mismatches" in result["samples"]

    def test_filter_kills_all(self, empty_result, source_df):
        result = diagnose_empty_context(
            result_df=empty_result,
            upstreams={"source": source_df},
            keys=["project_id"],
            filter_expr="capacity_mw > 9000",
        )
        assert result["metrics"]["dropout_cause"] == "filter_kills_all"

    def test_null_key_detection(self, empty_result, all_null_df):
        result = diagnose_empty_context(
            result_df=empty_result,
            upstreams={"src": all_null_df},
            keys=["project_id"],
        )
        assert result["metrics"]["dropout_cause"] == "key_columns_all_null"

    def test_markdown_output(self, empty_result, source_df, no_overlap_df):
        result = diagnose_empty_context(
            result_df=empty_result,
            upstreams={"source": source_df, "no_overlap": no_overlap_df},
            keys=["project_id"],
            output_format="markdown",
        )
        assert isinstance(result, str)
        assert "# Empty Output Diagnosis" in result
        assert "Row Count Funnel" in result
        assert "Suggested Next Actions" in result

    def test_positional_args(self, empty_result, source_df):
        """Test that positional args work (result_df, upstreams)."""
        result = diagnose_empty_context(
            empty_result,
            {"source": source_df},
            keys=["project_id"],
            filter_expr="capacity_mw > 9000",
        )
        assert result["kind"] == "diagnose_empty"
        assert result["metrics"]["result_count"] == 0

    def test_no_keys_provided(self, empty_result, source_df):
        """Should work without keys — skips overlap and type mismatch checks."""
        result = diagnose_empty_context(
            result_df=empty_result,
            upstreams={"source": source_df},
        )
        assert result["kind"] == "diagnose_empty"
        # No key overlap or type mismatch since no keys
        assert "key_overlaps" not in result["samples"]
        assert "type_mismatches" not in result["samples"]

    def test_single_upstream_no_overlap_check(self, empty_result, source_df):
        """With single upstream, key overlap is skipped (needs 2+ DataFrames)."""
        result = diagnose_empty_context(
            result_df=empty_result,
            upstreams={"source": source_df},
            keys=["project_id"],
        )
        # Key overlap only runs with 2+ upstreams
        assert "key_overlaps" not in result["samples"]

    def test_raises_on_missing_result(self):
        with pytest.raises(ValueError, match="result_df is required"):
            diagnose_empty_context(result_df=None, upstreams={"a": pd.DataFrame()})

    def test_raises_on_empty_upstreams(self, empty_result):
        with pytest.raises(ValueError, match="upstreams dict is required"):
            diagnose_empty_context(result_df=empty_result, upstreams={})

    def test_raises_on_invalid_output_format(self, empty_result, source_df):
        with pytest.raises(ValueError, match="output_format"):
            diagnose_empty_context(
                result_df=empty_result,
                upstreams={"source": source_df},
                output_format="invalid",
            )

    def test_suggestions_are_actionable(self, empty_result, source_df, no_overlap_df):
        result = diagnose_empty_context(
            result_df=empty_result,
            upstreams={"source": source_df, "no_overlap": no_overlap_df},
            keys=["project_id"],
        )
        assert len(result["suggested_next_actions"]) > 0
        # Suggestions should be strings
        for s in result["suggested_next_actions"]:
            assert isinstance(s, str)
            assert len(s) > 10

    def test_custom_subject(self, empty_result, source_df):
        result = diagnose_empty_context(
            result_df=empty_result,
            upstreams={"source": source_df},
            subject="my_pipeline_output",
        )
        assert result["subject"] == "my_pipeline_output"

    def test_non_empty_result(self, source_df, dim_df):
        """Even a non-empty result works — reports counts and any issues found."""
        result = diagnose_empty_context(
            result_df=source_df,  # 5 rows, not empty
            upstreams={"dim": dim_df},
            keys=["project_id"],
        )
        assert result["metrics"]["result_count"] == 5
        assert result["kind"] == "diagnose_empty"
