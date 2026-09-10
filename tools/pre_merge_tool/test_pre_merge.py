"""Formal pytest suite for pre_merge tool.

Tests cover:
- Standard contract shape
- Duplicate key detection
- NULL key detection
- Schema compatibility matrix
- Type compatibility classification
- Merge behavior prediction (INSERT-only, UPDATE-only, MIXED)
- Source batch ratio warnings
- Markdown output format
- Edge cases (empty DataFrames, single-key, composite keys)
"""
import sys
import os
import pytest
import pandas as pd

# Add tool path for direct import (relative to this file so it works in any
# environment, not just the original Databricks workspace).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pre_merge_impl import (
    _detect_engine,
    _normalize_type,
    _check_type_compatibility,
    _compare_schemas,
    _duplicate_keys_pandas,
    _null_keys_pandas,
    _key_overlap_pandas,
    pre_merge_context,
)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def clean_source():
    """Source with no issues — perfect for merge."""
    return pd.DataFrame({
        "id": [1, 2, 3, 4, 5],
        "name": ["Alice", "Bob", "Charlie", "Diana", "Eve"],
        "value": [100, 200, 300, 400, 500],
    })


@pytest.fixture
def target_table():
    """Target table with some existing records."""
    return pd.DataFrame({
        "id": [1, 2, 3, 10, 20, 30],
        "name": ["Alice_old", "Bob_old", "Charlie_old", "Frank", "Grace", "Heidi"],
        "value": [10, 20, 30, 1000, 2000, 3000],
    })


@pytest.fixture
def source_with_dupes():
    """Source with duplicate keys."""
    return pd.DataFrame({
        "id": [1, 1, 1, 2, 2, 3],
        "name": ["A", "B", "C", "D", "E", "F"],
        "value": [10, 20, 30, 40, 50, 60],
    })


@pytest.fixture
def source_with_nulls():
    """Source with NULL keys."""
    return pd.DataFrame({
        "id": [1, None, 3, None, 5],
        "name": ["A", "B", "C", "D", "E"],
        "value": [10, 20, 30, 40, 50],
    })


@pytest.fixture
def source_type_mismatch():
    """Source with type incompatibilities vs target."""
    return pd.DataFrame({
        "id": [1, 2, 3],
        "amount": ["100", "200", "not_a_number"],  # string, target expects int
        "status": [True, False, True],
    })


@pytest.fixture
def target_type_mismatch():
    """Target with types that don't match source."""
    return pd.DataFrame({
        "id": [10, 20],
        "amount": [1000, 2000],  # int64
        "status": [True, False],
    })


# ============================================================================
# _detect_engine tests
# ============================================================================

class TestDetectEngine:
    def test_pandas(self, clean_source):
        assert _detect_engine(clean_source) == "pandas"

    def test_unknown(self):
        assert _detect_engine([1, 2, 3]) == "unknown"

    def test_none(self):
        assert _detect_engine(None) == "unknown"


# ============================================================================
# _normalize_type tests
# ============================================================================

class TestNormalizeType:
    def test_basic_types(self):
        assert _normalize_type("int") == "int"
        assert _normalize_type("string") == "string"
        assert _normalize_type("double") == "double"

    def test_aliases(self):
        assert _normalize_type("integer") == "int"
        assert _normalize_type("bigint") == "long"
        assert _normalize_type("tinyint") == "byte"
        assert _normalize_type("smallint") == "short"
        assert _normalize_type("varchar") == "string"

    def test_strip_params(self):
        assert _normalize_type("decimal(10,2)") == "decimal"
        assert _normalize_type("varchar(100)") == "string"

    def test_pandas_types(self):
        assert _normalize_type("int64") == "long"
        assert _normalize_type("int32") == "int"
        assert _normalize_type("float64") == "double"
        assert _normalize_type("object") == "string"

    def test_case_insensitive(self):
        assert _normalize_type("INT") == "int"
        assert _normalize_type("String") == "string"


# ============================================================================
# _check_type_compatibility tests
# ============================================================================

class TestTypeCompatibility:
    def test_same_type(self):
        result = _check_type_compatibility("int", "int")
        assert result["compatible"] is True
        assert result["risk"] == "none"

    def test_safe_upcast(self):
        result = _check_type_compatibility("int", "long")
        assert result["compatible"] is True
        assert result["risk"] == "none"

    def test_dangerous_cast(self):
        result = _check_type_compatibility("string", "int")
        assert result["compatible"] is False
        assert result["risk"] == "high"

    def test_risky_but_possible(self):
        result = _check_type_compatibility("double", "decimal")
        assert result["compatible"] is True
        assert result["risk"] == "medium"

    def test_string_to_date_fails(self):
        result = _check_type_compatibility("string", "date")
        assert result["compatible"] is False
        assert result["risk"] == "high"

    def test_string_to_decimal_fails(self):
        result = _check_type_compatibility("string", "decimal(18,2)")
        assert result["compatible"] is False
        assert result["risk"] == "high"

    def test_date_to_timestamp_safe(self):
        result = _check_type_compatibility("date", "timestamp")
        assert result["compatible"] is True
        assert result["risk"] == "none"

    def test_unknown_pair_warns(self):
        result = _check_type_compatibility("array", "map")
        assert result["compatible"] is True
        assert result["risk"] == "medium"
        assert "unknown type pair" in result["note"]


# ============================================================================
# _compare_schemas tests
# ============================================================================

class TestCompareSchemas:
    def test_identical_schemas(self):
        schema = {"id": "int", "name": "string"}
        result = _compare_schemas(schema, schema)
        assert result["schema_compatible"] is True
        assert result["columns_added"] == []
        assert result["columns_removed"] == []
        assert result["columns_type_mismatch"] == []

    def test_added_columns(self):
        source = {"id": "int", "name": "string", "new_col": "double"}
        target = {"id": "int", "name": "string"}
        result = _compare_schemas(source, target)
        assert result["columns_added"] == ["new_col"]
        assert result["schema_compatible"] is True

    def test_removed_columns(self):
        source = {"id": "int"}
        target = {"id": "int", "name": "string"}
        result = _compare_schemas(source, target)
        assert result["columns_removed"] == ["name"]

    def test_type_mismatch(self):
        source = {"id": "int", "amount": "string"}
        target = {"id": "int", "amount": "int"}
        result = _compare_schemas(source, target)
        assert result["schema_compatible"] is False
        assert len(result["columns_type_mismatch"]) == 1
        assert result["columns_type_mismatch"][0]["column"] == "amount"


# ============================================================================
# _duplicate_keys_pandas tests
# ============================================================================

class TestDuplicateKeysPandas:
    def test_no_duplicates(self, clean_source):
        result = _duplicate_keys_pandas(clean_source, ["id"], sample_limit=5)
        assert result["count"] == 0
        assert result["samples"] == []
        assert result["total_excess_rows"] == 0

    def test_with_duplicates(self, source_with_dupes):
        result = _duplicate_keys_pandas(source_with_dupes, ["id"], sample_limit=5)
        assert result["count"] == 2  # id=1 (3x) and id=2 (2x)
        assert len(result["samples"]) == 2
        # Sorted by frequency — id=1 should be first (3 occurrences)
        assert result["samples"][0]["_duplicate_count"] == 3
        assert result["samples"][0]["id"] == 1
        assert result["total_excess_rows"] == 3  # (3-1) + (2-1) = 3

    def test_composite_keys(self):
        df = pd.DataFrame({
            "a": [1, 1, 2, 2],
            "b": ["x", "x", "y", "z"],
            "v": [10, 20, 30, 40],
        })
        result = _duplicate_keys_pandas(df, ["a", "b"], sample_limit=5)
        assert result["count"] == 1  # (1, "x") is duplicated
        assert result["samples"][0]["a"] == 1
        assert result["samples"][0]["b"] == "x"


# ============================================================================
# _null_keys_pandas tests
# ============================================================================

class TestNullKeysPandas:
    def test_no_nulls(self, clean_source):
        result = _null_keys_pandas(clean_source, ["id"])
        assert result["total_null_key_rows"] == 0
        assert result["null_pct"] == 0.0

    def test_with_nulls(self, source_with_nulls):
        result = _null_keys_pandas(source_with_nulls, ["id"])
        assert result["total_null_key_rows"] == 2
        assert result["per_column"]["id"]["null_count"] == 2

    def test_composite_keys_any_null(self):
        df = pd.DataFrame({
            "a": [1, None, 3],
            "b": ["x", "y", None],
            "v": [10, 20, 30],
        })
        result = _null_keys_pandas(df, ["a", "b"])
        assert result["total_null_key_rows"] == 2  # row 1 (a=None) and row 2 (b=None)


# ============================================================================
# _key_overlap_pandas tests
# ============================================================================

class TestKeyOverlapPandas:
    def test_mixed_merge(self, clean_source, target_table):
        result = _key_overlap_pandas(clean_source, target_table, ["id"])
        assert result["merge_behavior"] == "MIXED"
        assert result["overlap_count"] == 3  # ids 1, 2, 3
        assert result["insert_count"] == 2  # ids 4, 5
        assert result["update_count"] == 3

    def test_insert_only(self):
        source = pd.DataFrame({"id": [100, 200, 300]})
        target = pd.DataFrame({"id": [1, 2, 3]})
        result = _key_overlap_pandas(source, target, ["id"])
        assert result["merge_behavior"] == "INSERT-only"
        assert result["insert_count"] == 3
        assert result["update_count"] == 0

    def test_update_only(self):
        source = pd.DataFrame({"id": [1, 2, 3]})
        target = pd.DataFrame({"id": [1, 2, 3, 4, 5]})
        result = _key_overlap_pandas(source, target, ["id"])
        assert result["merge_behavior"] == "UPDATE-only"
        assert result["update_count"] == 3
        assert result["insert_count"] == 0


# ============================================================================
# pre_merge_context (public entry point) tests
# ============================================================================

class TestPreMergeContext:
    def test_clean_merge_returns_contract(self, clean_source, target_table):
        result = pre_merge_context(clean_source, target_table, keys=["id"], output_format="dict")
        # Standard contract keys
        assert result["kind"] == "pre_merge"
        assert "subject" in result
        assert "summary" in result
        assert "metrics" in result
        assert "findings" in result
        assert "risks" in result
        assert "samples" in result
        assert "suggested_next_actions" in result

    def test_clean_merge_is_safe(self, clean_source, target_table):
        result = pre_merge_context(clean_source, target_table, keys=["id"], output_format="dict")
        assert "OK" in result["summary"]
        assert result["metrics"]["source_duplicate_key_count"] == 0
        assert result["metrics"]["source_null_key_count"] == 0
        assert result["metrics"]["schema_compatible"] is True
        assert result["metrics"]["merge_behavior"] == "MIXED"

    def test_duplicates_block_merge(self, source_with_dupes, target_table):
        result = pre_merge_context(source_with_dupes, target_table, keys=["id"], output_format="dict")
        assert "BLOCKED" in result["summary"]
        assert result["metrics"]["source_duplicate_key_count"] == 2
        assert any("MERGE WILL FAIL" in r for r in result["risks"])
        assert any("Deduplicate" in a for a in result["suggested_next_actions"])

    def test_null_keys_warned(self, source_with_nulls, target_table):
        result = pre_merge_context(source_with_nulls, target_table, keys=["id"], output_format="dict")
        assert result["metrics"]["source_null_key_count"] == 2
        assert any("NULL merge keys" in r for r in result["risks"])

    def test_type_mismatch_detected(self, source_type_mismatch, target_type_mismatch):
        result = pre_merge_context(
            source_type_mismatch, target_type_mismatch,
            keys=["id"], output_format="dict"
        )
        assert result["metrics"]["schema_compatible"] is False
        assert len(result["metrics"]["columns_type_mismatch"]) > 0
        assert any("Type mismatch" in r for r in result["risks"])

    def test_batch_ratio_warning(self):
        """Source much larger than target triggers warning."""
        source = pd.DataFrame({"id": range(1000), "v": range(1000)})
        target = pd.DataFrame({"id": range(100), "v": range(100)})
        result = pre_merge_context(source, target, keys=["id"], output_format="dict")
        assert result["metrics"]["source_target_ratio"] == 10.0
        assert any("larger than target" in r for r in result["risks"])

    def test_markdown_output(self, clean_source, target_table):
        result = pre_merge_context(clean_source, target_table, keys=["id"], output_format="markdown")
        assert isinstance(result, str)
        assert "Pre-Merge Validation" in result
        assert "Metrics" in result

    def test_kwargs_style(self, clean_source, target_table):
        """Test calling with all keyword arguments."""
        result = pre_merge_context(
            source_df=clean_source,
            target=target_table,
            keys=["id"],
            output_format="dict",
        )
        assert result["kind"] == "pre_merge"

    def test_missing_source_raises(self, target_table):
        with pytest.raises(ValueError, match="source_df is required"):
            pre_merge_context(target=target_table, keys=["id"])

    def test_missing_target_raises(self, clean_source):
        with pytest.raises(ValueError, match="target is required"):
            pre_merge_context(clean_source, keys=["id"])

    def test_missing_keys_raises(self, clean_source, target_table):
        with pytest.raises(ValueError, match="keys is required"):
            pre_merge_context(clean_source, target_table)

    def test_invalid_key_column_raises(self, clean_source, target_table):
        with pytest.raises(ValueError, match="not found in source"):
            pre_merge_context(clean_source, target_table, keys=["nonexistent"])

    def test_composite_keys(self):
        source = pd.DataFrame({
            "project_id": [1, 1, 2],
            "queue_date": ["2024-01-01", "2024-01-02", "2024-01-01"],
            "value": [10, 20, 30],
        })
        target = pd.DataFrame({
            "project_id": [1, 2],
            "queue_date": ["2024-01-01", "2024-01-01"],
            "value": [100, 200],
        })
        result = pre_merge_context(
            source, target,
            keys=["project_id", "queue_date"],
            output_format="dict",
        )
        assert result["kind"] == "pre_merge"
        assert result["metrics"]["merge_behavior"] == "MIXED"
        assert result["metrics"]["overlap_count"] == 2
        assert result["metrics"]["insert_count"] == 1

    def test_insert_only_prediction(self):
        source = pd.DataFrame({"id": [100, 200], "v": [1, 2]})
        target = pd.DataFrame({"id": [1, 2, 3], "v": [10, 20, 30]})
        result = pre_merge_context(source, target, keys=["id"], output_format="dict")
        assert result["metrics"]["merge_behavior"] == "INSERT-only"

    def test_update_only_prediction(self):
        source = pd.DataFrame({"id": [1, 2], "v": [99, 88]})
        target = pd.DataFrame({"id": [1, 2, 3], "v": [10, 20, 30]})
        result = pre_merge_context(source, target, keys=["id"], output_format="dict")
        assert result["metrics"]["merge_behavior"] == "UPDATE-only"

    def test_suggested_actions_executable(self, source_with_dupes, target_table):
        """Suggested actions should contain code-like strings."""
        result = pre_merge_context(source_with_dupes, target_table, keys=["id"], output_format="dict")
        assert any("dropDuplicates" in a for a in result["suggested_next_actions"])

    def test_string_key_auto_wraps(self, clean_source, target_table):
        """Single string key gets auto-wrapped into list."""
        result = pre_merge_context(clean_source, target_table, keys="id", output_format="dict")
        assert result["kind"] == "pre_merge"


# ============================================================================
# Edge cases
# ============================================================================

class TestEdgeCases:
    def test_empty_source(self, target_table):
        source = pd.DataFrame({"id": pd.Series([], dtype="int64"), "name": pd.Series([], dtype="str"), "value": pd.Series([], dtype="int64")})
        result = pre_merge_context(source, target_table, keys=["id"], output_format="dict")
        assert result["metrics"]["source_count"] == 0
        assert result["metrics"]["merge_behavior"] == "EMPTY"

    def test_empty_target(self, clean_source):
        target = pd.DataFrame({"id": pd.Series([], dtype="int64"), "name": pd.Series([], dtype="str"), "value": pd.Series([], dtype="int64")})
        result = pre_merge_context(clean_source, target, keys=["id"], output_format="dict")
        assert result["metrics"]["target_count"] == 0
        assert result["metrics"]["merge_behavior"] == "INSERT-only"

    def test_single_row(self):
        source = pd.DataFrame({"id": [1], "v": [10]})
        target = pd.DataFrame({"id": [1], "v": [99]})
        result = pre_merge_context(source, target, keys=["id"], output_format="dict")
        assert result["metrics"]["merge_behavior"] == "UPDATE-only"
        assert result["metrics"]["source_count"] == 1
