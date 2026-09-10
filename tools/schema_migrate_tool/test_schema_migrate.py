"""Tests for schema_migrate_tool.

Uses _load() pattern to bypass __init__.py chain.
Tests run with pandas-only — no Spark required.
"""
import importlib.util
import os
import sys
import types

if "odibi_anchor" not in sys.modules:
    # Provide stub for the package
    anchor_mod = types.ModuleType("odibi_anchor")
    sys.modules["odibi_anchor"] = anchor_mod

    # Stub _utils subpackage
    utils_mod = types.ModuleType("odibi_anchor._utils")
    sys.modules["odibi_anchor._utils"] = utils_mod
    anchor_mod._utils = utils_mod

import pytest


def _load(name, rel_path):
    """Import a single .py file, bypassing package __init__.py."""
    path = os.path.normpath(os.path.join(os.path.dirname(__file__), rel_path))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Load dependencies first
_contract = _load(
    "odibi_anchor._utils.contract",
    "../../src/odibi_anchor/_utils/contract.py",
)
sys.modules["odibi_anchor._utils.contract"] = _contract

_render = _load(
    "odibi_anchor._utils.render_utils",
    "../../src/odibi_anchor/_utils/render_utils.py",
)
sys.modules["odibi_anchor._utils.render_utils"] = _render

# Now load the module under test
_mod = _load("_schema_migrate", "./schema_migrate_impl.py")
schema_migrate_context = _mod.schema_migrate_context
render_schema_migrate_report = _mod.render_schema_migrate_report


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def basic_diff_ctx():
    """A schema_diff output with added, removed, type_changed, and renames."""
    return {
        "kind": "schema_diff_context",
        "subject": "old_df -> new_df",
        "summary": "3 added, 1 removed, 1 type changed",
        "metrics": {
            "added_count": 3,
            "removed_count": 1,
            "type_changed_count": 1,
            "has_changes": True,
        },
        "added": [
            {"column": "loyalty_tier", "dtype": "string", "position": 5},
            {"column": "signup_source", "dtype": "string", "position": 6},
            {"column": "lifetime_value", "dtype": "decimal(10,2)", "position": 7},
        ],
        "removed": [
            {"column": "legacy_flag", "dtype": "boolean", "position": 4},
        ],
        "type_changed": [
            {
                "column": "price",
                "old_dtype": "string",
                "new_dtype": "decimal(10,2)",
                "old_type_family": "string",
                "new_type_family": "numeric",
                "old_position": 3,
                "new_position": 3,
                "risk_level": "high",
                "fix_expr": ".withColumn('price', F.col('price').cast('decimal(10,2)'))",
                "fix_safe": False,
            }
        ],
        "likely_renames": [
            {
                "old_column": "cust_segment",
                "new_column": "customer_segment",
                "confidence": "high",
                "reason": "naming_convention_change",
                "dtype_match": True,
            }
        ],
        "findings": [],
        "risks": [],
        "samples": {},
        "suggested_next_actions": [],
    }


@pytest.fixture
def empty_diff_ctx():
    """A schema_diff output with no changes."""
    return {
        "kind": "schema_diff_context",
        "subject": "old_df -> new_df",
        "summary": "No changes",
        "metrics": {"has_changes": False},
        "added": [],
        "removed": [],
        "type_changed": [],
        "likely_renames": [],
        "findings": [],
        "risks": [],
        "samples": {},
        "suggested_next_actions": [],
    }


@pytest.fixture
def rename_candidates_ctx():
    """Schema diff with columns that look like renames by name similarity."""
    return {
        "kind": "schema_diff_context",
        "subject": "test",
        "summary": "test",
        "metrics": {"has_changes": True},
        "added": [
            {"column": "customer_email", "dtype": "string", "position": 2},
            {"column": "order_total", "dtype": "double", "position": 3},
        ],
        "removed": [
            {"column": "cust_email", "dtype": "string", "position": 2},
            {"column": "order_amount", "dtype": "double", "position": 3},
        ],
        "type_changed": [],
        "likely_renames": [],  # schema_diff didn't detect these
        "findings": [],
        "risks": [],
        "samples": {},
        "suggested_next_actions": [],
    }


# ============================================================================
# Basic functionality tests
# ============================================================================


class TestBasicMigration:
    """Test core migration generation."""

    def test_returns_dict_by_default(self, basic_diff_ctx):
        result = schema_migrate_context(
            basic_diff_ctx, target_table="cat.sch.customers"
        )
        assert isinstance(result, dict)
        assert result["kind"] == "schema_migrate"

    def test_contract_keys_present(self, basic_diff_ctx):
        result = schema_migrate_context(
            basic_diff_ctx, target_table="cat.sch.customers"
        )
        required = {"kind", "subject", "summary", "metrics", "findings", "risks",
                    "samples", "suggested_next_actions"}
        assert required.issubset(set(result.keys()))

    def test_subject_is_target_table(self, basic_diff_ctx):
        result = schema_migrate_context(
            basic_diff_ctx, target_table="analytics.gold.customers"
        )
        assert result["subject"] == "analytics.gold.customers"

    def test_metrics_counts(self, basic_diff_ctx):
        result = schema_migrate_context(
            basic_diff_ctx, target_table="cat.sch.tbl"
        )
        m = result["metrics"]
        assert m["columns_added"] == 3
        assert m["columns_retyped"] == 1
        assert m["columns_renamed"] == 1
        assert m["unsafe_casts"] == 1
        assert m["safe_casts"] == 0
        # removed=1 but it's the rename, so final_removed=0
        # Actually legacy_flag is removed, cust_segment is in renames
        # cust_segment isn't in removed[] — it's only in likely_renames
        # So final_removed has legacy_flag
        assert m["columns_removed"] == 1

    def test_dry_run_in_summary(self, basic_diff_ctx):
        result = schema_migrate_context(
            basic_diff_ctx, target_table="cat.sch.tbl"
        )
        assert "Dry run" in result["summary"]

    def test_markdown_output(self, basic_diff_ctx):
        result = schema_migrate_context(
            basic_diff_ctx, target_table="cat.sch.tbl", output_format="markdown"
        )
        assert isinstance(result, str)
        assert "Schema Migration" in result
        assert "cat.sch.tbl" in result


class TestAddColumns:
    """Test ADD COLUMN DDL generation."""

    def test_add_column_ddl_generated(self, basic_diff_ctx):
        result = schema_migrate_context(
            basic_diff_ctx, target_table="cat.sch.tbl"
        )
        plan = result["migration_plan"]
        adds = plan["add_columns"]
        assert len(adds) == 3

    def test_string_column_null_default(self, basic_diff_ctx):
        result = schema_migrate_context(
            basic_diff_ctx, target_table="cat.sch.tbl"
        )
        adds = result["migration_plan"]["add_columns"]
        loyalty = next(a for a in adds if a["column"] == "loyalty_tier")
        assert loyalty["default"] == "NULL"
        assert "ADD COLUMN" in loyalty["ddl"]
        assert "loyalty_tier" in loyalty["ddl"]

    def test_decimal_column_zero_default(self, basic_diff_ctx):
        result = schema_migrate_context(
            basic_diff_ctx, target_table="cat.sch.tbl"
        )
        adds = result["migration_plan"]["add_columns"]
        ltv = next(a for a in adds if a["column"] == "lifetime_value")
        assert ltv["default"] == "0"
        assert "DEFAULT 0" in ltv["ddl"]

    def test_ddl_uses_target_table(self, basic_diff_ctx):
        result = schema_migrate_context(
            basic_diff_ctx, target_table="prod.analytics.fact_orders"
        )
        adds = result["migration_plan"]["add_columns"]
        for a in adds:
            assert "prod.analytics.fact_orders" in a["ddl"]


class TestTypeCasts:
    """Test type cast expression generation."""

    def test_unsafe_cast_uses_try_cast(self, basic_diff_ctx):
        result = schema_migrate_context(
            basic_diff_ctx, target_table="cat.sch.tbl"
        )
        casts = result["migration_plan"]["type_casts"]
        assert len(casts) == 1
        price_cast = casts[0]
        assert price_cast["safe"] is False
        assert "TRY_CAST" in price_cast["sql_expr"]
        assert "TRY_CAST" in price_cast["spark"]

    def test_safe_cast_uses_cast(self):
        """Test widening cast (int→bigint) uses regular CAST."""
        ctx = {
            "kind": "schema_diff_context",
            "subject": "test",
            "summary": "test",
            "metrics": {"has_changes": True},
            "added": [],
            "removed": [],
            "type_changed": [
                {
                    "column": "count",
                    "old_dtype": "int",
                    "new_dtype": "bigint",
                    "old_type_family": "numeric",
                    "new_type_family": "numeric",
                    "old_position": 0,
                    "new_position": 0,
                    "risk_level": "low",
                    "fix_expr": "",
                    "fix_safe": True,
                }
            ],
            "likely_renames": [],
            "findings": [],
            "risks": [],
            "samples": {},
            "suggested_next_actions": [],
        }
        result = schema_migrate_context(ctx, target_table="cat.sch.tbl")
        casts = result["migration_plan"]["type_casts"]
        assert casts[0]["safe"] is True
        assert "CAST(" in casts[0]["sql_expr"]
        assert "TRY_CAST" not in casts[0]["sql_expr"]

    def test_risk_generated_for_unsafe(self, basic_diff_ctx):
        result = schema_migrate_context(
            basic_diff_ctx, target_table="cat.sch.tbl"
        )
        assert any("TRY_CAST" in r for r in result["risks"])


class TestRenames:
    """Test rename detection and DDL generation."""

    def test_schema_diff_renames_used(self, basic_diff_ctx):
        result = schema_migrate_context(
            basic_diff_ctx, target_table="cat.sch.tbl"
        )
        renames = result["migration_plan"]["renames"]
        assert len(renames) == 1
        assert renames[0]["from_column"] == "cust_segment"
        assert renames[0]["to_column"] == "customer_segment"

    def test_rename_ddl_valid(self, basic_diff_ctx):
        result = schema_migrate_context(
            basic_diff_ctx, target_table="cat.sch.tbl"
        )
        renames = result["migration_plan"]["renames"]
        assert "RENAME COLUMN" in renames[0]["ddl"]
        assert "cust_segment" in renames[0]["ddl"]
        assert "customer_segment" in renames[0]["ddl"]

    def test_enhanced_rename_detection(self, rename_candidates_ctx):
        result = schema_migrate_context(
            rename_candidates_ctx,
            target_table="cat.sch.tbl",
            rename_threshold=0.7,
        )
        renames = result["migration_plan"]["renames"]
        # cust_email → customer_email should be detected
        rename_pairs = {(r["from_column"], r["to_column"]) for r in renames}
        assert ("cust_email", "customer_email") in rename_pairs

    def test_threshold_too_high_no_match(self, rename_candidates_ctx):
        result = schema_migrate_context(
            rename_candidates_ctx,
            target_table="cat.sch.tbl",
            rename_threshold=0.99,
        )
        renames = result["migration_plan"]["renames"]
        # Very high threshold should not match
        assert len(renames) == 0

    def test_renamed_columns_excluded_from_add_remove(self, basic_diff_ctx):
        """Columns in likely_renames should not appear in add_columns or drops."""
        result = schema_migrate_context(
            basic_diff_ctx, target_table="cat.sch.tbl"
        )
        adds = result["migration_plan"]["add_columns"]
        add_names = [a["column"] for a in adds]
        # customer_segment is the renamed column's new name — it shouldn't be in adds
        assert "customer_segment" not in add_names


class TestDropColumns:
    """Test DROP COLUMN behavior."""

    def test_drop_disabled_by_default(self, basic_diff_ctx):
        result = schema_migrate_context(
            basic_diff_ctx, target_table="cat.sch.tbl"
        )
        drops = result["migration_plan"]["drops"]
        if drops:
            assert drops[0]["action"] == "warning_only"
            assert "WARNING" in drops[0]["ddl"]
            assert "--" in drops[0]["ddl"]

    def test_drop_enabled(self, basic_diff_ctx):
        result = schema_migrate_context(
            basic_diff_ctx, target_table="cat.sch.tbl", drop_columns=True
        )
        drops = result["migration_plan"]["drops"]
        if drops:
            assert drops[0]["action"] == "drop"
            assert "DROP COLUMN" in drops[0]["ddl"]
            assert not drops[0]["ddl"].startswith("--")


class TestFullScripts:
    """Test full DDL and PySpark script generation."""

    def test_full_ddl_in_samples(self, basic_diff_ctx):
        result = schema_migrate_context(
            basic_diff_ctx, target_table="cat.sch.tbl"
        )
        assert "full_ddl" in result["samples"]
        ddl = result["samples"]["full_ddl"]
        assert "ALTER TABLE" in ddl
        assert "cat.sch.tbl" in ddl

    def test_full_pyspark_in_samples(self, basic_diff_ctx):
        result = schema_migrate_context(
            basic_diff_ctx, target_table="cat.sch.tbl"
        )
        assert "full_pyspark" in result["samples"]
        pyspark = result["samples"]["full_pyspark"]
        assert "from pyspark.sql import functions as F" in pyspark

    def test_ddl_includes_rename(self, basic_diff_ctx):
        result = schema_migrate_context(
            basic_diff_ctx, target_table="cat.sch.tbl"
        )
        ddl = result["samples"]["full_ddl"]
        assert "RENAME COLUMN" in ddl

    def test_pyspark_includes_try_cast(self, basic_diff_ctx):
        result = schema_migrate_context(
            basic_diff_ctx, target_table="cat.sch.tbl"
        )
        pyspark = result["samples"]["full_pyspark"]
        assert "TRY_CAST" in pyspark


class TestEdgeCases:
    """Test edge cases and validation."""

    def test_empty_diff_no_operations(self, empty_diff_ctx):
        result = schema_migrate_context(
            empty_diff_ctx, target_table="cat.sch.tbl"
        )
        assert result["metrics"]["total_operations"] == 0
        assert "No operations" in result["summary"]

    def test_missing_ctx_raises(self):
        with pytest.raises(ValueError, match="schema_diff_ctx is required"):
            schema_migrate_context(target_table="cat.sch.tbl")

    def test_missing_target_raises(self, basic_diff_ctx):
        with pytest.raises(ValueError, match="target_table is required"):
            schema_migrate_context(basic_diff_ctx)

    def test_invalid_ctx_type_raises(self):
        with pytest.raises(ValueError, match="must be a dict"):
            schema_migrate_context("not_a_dict", target_table="cat.sch.tbl")

    def test_invalid_output_format_raises(self, basic_diff_ctx):
        with pytest.raises(ValueError, match="output_format"):
            schema_migrate_context(
                basic_diff_ctx, target_table="cat.sch.tbl", output_format="xml"
            )

    def test_positional_args_work(self, basic_diff_ctx):
        """schema_migrate_context(ctx, table) should work without kwargs."""
        result = schema_migrate_context(basic_diff_ctx, "cat.sch.tbl")
        assert result["kind"] == "schema_migrate"
        assert result["subject"] == "cat.sch.tbl"


class TestRenderer:
    """Test markdown renderer."""

    def test_renderer_produces_string(self, basic_diff_ctx):
        result = schema_migrate_context(
            basic_diff_ctx, target_table="cat.sch.tbl", output_format="dict"
        )
        md = render_schema_migrate_report(result)
        assert isinstance(md, str)
        assert len(md) > 100

    def test_renderer_includes_ddl_block(self, basic_diff_ctx):
        result = schema_migrate_context(
            basic_diff_ctx, target_table="cat.sch.tbl", output_format="dict"
        )
        md = render_schema_migrate_report(result)
        assert "```sql" in md
        assert "```python" in md

    def test_renderer_includes_tables(self, basic_diff_ctx):
        result = schema_migrate_context(
            basic_diff_ctx, target_table="cat.sch.tbl", output_format="dict"
        )
        md = render_schema_migrate_report(result)
        assert "| Column |" in md or "| Old Name |" in md


class TestSafeCastMapping:
    """Test the safe cast mapping directly."""

    def test_widening_is_safe(self):
        assert _mod._is_safe_cast("int", "bigint") is True
        assert _mod._is_safe_cast("float", "double") is True
        assert _mod._is_safe_cast("date", "timestamp") is True

    def test_narrowing_is_unsafe(self):
        assert _mod._is_safe_cast("string", "int") is False
        assert _mod._is_safe_cast("double", "int") is False
        assert _mod._is_safe_cast("bigint", "int") is False

    def test_same_type_is_safe(self):
        assert _mod._is_safe_cast("string", "string") is True
        assert _mod._is_safe_cast("int", "int") is True

    def test_unknown_pair_is_unsafe(self):
        # Unknown types default to unsafe
        assert _mod._is_safe_cast("struct", "array") is False


class TestTypeDefaults:
    """Test type-appropriate defaults."""

    def test_string_default_null(self):
        assert _mod._type_default("string") == "NULL"
        assert _mod._type_default("STRING") == "NULL"

    def test_numeric_defaults_zero(self):
        assert _mod._type_default("int") == "0"
        assert _mod._type_default("bigint") == "0"
        assert _mod._type_default("decimal(10,2)") == "0"
        assert _mod._type_default("double") == "0"

    def test_boolean_default_false(self):
        assert _mod._type_default("boolean") == "false"

    def test_complex_types_null(self):
        assert _mod._type_default("array<string>") == "NULL"
        assert _mod._type_default("map<string,int>") == "NULL"
        assert _mod._type_default("struct<a:int,b:string>") == "NULL"

    def test_unknown_type_null(self):
        assert _mod._type_default("some_custom_type") == "NULL"



class TestPandasTypeNormalization:
    """Test that pandas dtype names are properly normalized."""

    def test_int64_to_float64_is_safe(self):
        """int64 → float64 is int → double widening — should be safe."""
        assert _mod._is_safe_cast("int64", "float64") is True

    def test_int64_to_bigint_is_same_type(self):
        """int64 normalizes to bigint — same type cast is safe."""
        assert _mod._is_safe_cast("int64", "bigint") is True

    def test_object_to_int64_is_unsafe(self):
        """object (string) → int64 (bigint) is lossy."""
        assert _mod._is_safe_cast("object", "int64") is False

    def test_float64_to_int64_is_unsafe(self):
        """float64 (double) → int64 (bigint) is narrowing."""
        assert _mod._is_safe_cast("float64", "int64") is False

    def test_object_to_datetime64_is_unsafe(self):
        """object (string) → datetime64[ns] (timestamp) is lossy."""
        assert _mod._is_safe_cast("object", "datetime64[ns]") is False

    def test_type_default_object_is_null(self):
        """Pandas 'object' maps to string → default NULL."""
        assert _mod._type_default("object") == "NULL"

    def test_type_default_int64_is_zero(self):
        """Pandas 'int64' maps to bigint → default 0."""
        assert _mod._type_default("int64") == "0"

    def test_type_default_float64_is_zero(self):
        """Pandas 'float64' maps to double → default 0."""
        assert _mod._type_default("float64") == "0"

    def test_type_default_bool_is_false(self):
        """Pandas 'bool' maps to boolean → default false."""
        assert _mod._type_default("bool") == "false"

    def test_type_default_datetime64_is_null(self):
        """Pandas 'datetime64[ns]' maps to timestamp → default NULL."""
        assert _mod._type_default("datetime64[ns]") == "NULL"

    def test_ddl_uses_sql_types_not_pandas(self):
        """DDL output should use SQL type names, not pandas."""
        ctx = {
            "kind": "schema_diff_context", "subject": "test", "summary": "test",
            "metrics": {"has_changes": True},
            "added": [
                {"column": "name", "dtype": "object", "position": 0},
                {"column": "score", "dtype": "float64", "position": 1},
                {"column": "count", "dtype": "int64", "position": 2},
                {"column": "active", "dtype": "bool", "position": 3},
                {"column": "created", "dtype": "datetime64[ns]", "position": 4},
            ],
            "removed": [], "type_changed": [], "likely_renames": [],
            "findings": [], "risks": [], "samples": {}, "suggested_next_actions": [],
        }
        result = schema_migrate_context(ctx, target_table="cat.sch.tbl")
        adds = result["migration_plan"]["add_columns"]

        name_col = next(a for a in adds if a["column"] == "name")
        assert "STRING" in name_col["ddl"]
        assert "object" not in name_col["ddl"]

        score_col = next(a for a in adds if a["column"] == "score")
        assert "DOUBLE" in score_col["ddl"]
        assert "float64" not in score_col["ddl"]

        count_col = next(a for a in adds if a["column"] == "count")
        assert "BIGINT" in count_col["ddl"]
        assert "int64" not in count_col["ddl"]

        active_col = next(a for a in adds if a["column"] == "active")
        assert "BOOLEAN" in active_col["ddl"]
        assert active_col["default"] == "false"

        created_col = next(a for a in adds if a["column"] == "created")
        assert "TIMESTAMP" in created_col["ddl"]
        assert "datetime64" not in created_col["ddl"]

    def test_cast_expr_uses_sql_types(self):
        """Cast expressions should use SQL type names."""
        ctx = {
            "kind": "schema_diff_context", "subject": "test", "summary": "test",
            "metrics": {"has_changes": True},
            "added": [], "removed": [],
            "type_changed": [
                {
                    "column": "amount",
                    "old_dtype": "int64",
                    "new_dtype": "float64",
                    "old_type_family": "numeric",
                    "new_type_family": "numeric",
                    "old_position": 0,
                    "new_position": 0,
                    "risk_level": "low",
                    "fix_expr": "",
                    "fix_safe": True,
                }
            ],
            "likely_renames": [],
            "findings": [], "risks": [], "samples": {}, "suggested_next_actions": [],
        }
        result = schema_migrate_context(ctx, target_table="cat.sch.tbl")
        casts = result["migration_plan"]["type_casts"]
        assert len(casts) == 1
        # int64→float64 normalizes to bigint→double which is safe (widening)
        assert casts[0]["safe"] is True
        assert "DOUBLE" in casts[0]["sql_expr"]
        assert "float64" not in casts[0]["sql_expr"]

    def test_normalize_preserves_spark_types(self):
        """Spark-native types should pass through unchanged."""
        assert _mod._normalize_type("string") == "string"
        assert _mod._normalize_type("int") == "int"
        assert _mod._normalize_type("bigint") == "bigint"
        assert _mod._normalize_type("decimal(10,2)") == "decimal"
        assert _mod._normalize_type("timestamp") == "timestamp"

    def test_normalize_for_ddl_preserves_precision(self):
        """DDL normalization preserves precision like decimal(10,2)."""
        assert _mod._normalize_type_for_ddl("decimal(10,2)") == "decimal(10,2)"
        assert _mod._normalize_type_for_ddl("int64") == "BIGINT"
        assert _mod._normalize_type_for_ddl("object") == "STRING"
        assert _mod._normalize_type_for_ddl("float64") == "DOUBLE"
        assert _mod._normalize_type_for_ddl("datetime64[ns]") == "TIMESTAMP"

    def test_nullable_pandas_int(self):
        """Nullable pandas Int64 (capital I) normalizes correctly."""
        # Capital I means nullable int in pandas
        assert _mod._is_safe_cast("Int64", "float64") is True
        assert _mod._type_default("Int64") == "0"



class TestSparkTypeNormalization:
    """Test that Spark Python repr type names are properly normalized."""

    # --- _normalize_type (for safe-cast lookup) ---

    def test_integer_type(self):
        assert _mod._normalize_type("IntegerType()") == "int"

    def test_long_type(self):
        assert _mod._normalize_type("LongType()") == "bigint"

    def test_string_type(self):
        assert _mod._normalize_type("StringType()") == "string"

    def test_double_type(self):
        assert _mod._normalize_type("DoubleType()") == "double"

    def test_float_type(self):
        assert _mod._normalize_type("FloatType()") == "float"

    def test_boolean_type(self):
        assert _mod._normalize_type("BooleanType()") == "boolean"

    def test_date_type(self):
        assert _mod._normalize_type("DateType()") == "date"

    def test_timestamp_type(self):
        assert _mod._normalize_type("TimestampType()") == "timestamp"

    def test_decimal_type(self):
        assert _mod._normalize_type("DecimalType(10,2)") == "decimal"

    def test_short_type(self):
        assert _mod._normalize_type("ShortType()") == "short"

    def test_byte_type(self):
        assert _mod._normalize_type("ByteType()") == "byte"

    # --- Safe cast detection with Spark reprs ---

    def test_integer_to_long_is_safe(self):
        assert _mod._is_safe_cast("IntegerType()", "LongType()") is True

    def test_float_to_double_is_safe(self):
        assert _mod._is_safe_cast("FloatType()", "DoubleType()") is True

    def test_date_to_timestamp_is_safe(self):
        assert _mod._is_safe_cast("DateType()", "TimestampType()") is True

    def test_int_to_double_is_safe(self):
        assert _mod._is_safe_cast("IntegerType()", "DoubleType()") is True

    def test_string_to_decimal_is_unsafe(self):
        assert _mod._is_safe_cast("StringType()", "DecimalType(10,2)") is False

    def test_double_to_integer_is_unsafe(self):
        assert _mod._is_safe_cast("DoubleType()", "IntegerType()") is False

    # --- _normalize_type_for_ddl (for SQL output) ---

    def test_ddl_string_type(self):
        assert _mod._normalize_type_for_ddl("StringType()") == "STRING"

    def test_ddl_integer_type(self):
        assert _mod._normalize_type_for_ddl("IntegerType()") == "INT"

    def test_ddl_long_type(self):
        assert _mod._normalize_type_for_ddl("LongType()") == "BIGINT"

    def test_ddl_double_type(self):
        assert _mod._normalize_type_for_ddl("DoubleType()") == "DOUBLE"

    def test_ddl_decimal_with_precision(self):
        assert _mod._normalize_type_for_ddl("DecimalType(10,2)") == "DECIMAL(10,2)"

    def test_ddl_timestamp_type(self):
        assert _mod._normalize_type_for_ddl("TimestampType()") == "TIMESTAMP"

    def test_ddl_boolean_type(self):
        assert _mod._normalize_type_for_ddl("BooleanType()") == "BOOLEAN"

    # --- Complex types ---

    def test_ddl_array_type(self):
        result = _mod._normalize_type_for_ddl("ArrayType(StringType(), True)")
        assert result == "ARRAY<STRING>"

    def test_ddl_map_type(self):
        result = _mod._normalize_type_for_ddl("MapType(StringType(), IntegerType(), True)")
        assert result == "MAP<STRING,INT>"

    def test_normalize_array_base(self):
        assert _mod._normalize_type("ArrayType(StringType(), True)") == "array"

    def test_normalize_map_base(self):
        assert _mod._normalize_type("MapType(StringType(), IntegerType(), True)") == "map"

    # --- Type defaults with Spark reprs ---

    def test_type_default_string_type(self):
        assert _mod._type_default("StringType()") == "NULL"

    def test_type_default_integer_type(self):
        assert _mod._type_default("IntegerType()") == "0"

    def test_type_default_boolean_type(self):
        assert _mod._type_default("BooleanType()") == "false"

    def test_type_default_double_type(self):
        assert _mod._type_default("DoubleType()") == "0"

    # --- Full integration: DDL output uses SQL types ---

    def test_full_migration_spark_types(self):
        """Full schema_migrate with Spark repr types produces valid SQL."""
        ctx = {
            "kind": "schema_diff_context", "subject": "test", "summary": "test",
            "metrics": {"has_changes": True},
            "added": [
                {"column": "email", "dtype": "StringType()", "position": 5},
                {"column": "score", "dtype": "DoubleType()", "position": 6},
                {"column": "count", "dtype": "IntegerType()", "position": 7},
            ],
            "removed": [],
            "type_changed": [
                {
                    "column": "amount",
                    "old_dtype": "IntegerType()",
                    "new_dtype": "LongType()",
                    "old_type_family": "numeric",
                    "new_type_family": "numeric",
                    "old_position": 2,
                    "new_position": 2,
                    "risk_level": "low",
                    "fix_expr": "",
                    "fix_safe": True,
                },
                {
                    "column": "price",
                    "old_dtype": "StringType()",
                    "new_dtype": "DecimalType(10,2)",
                    "old_type_family": "string",
                    "new_type_family": "numeric",
                    "old_position": 3,
                    "new_position": 3,
                    "risk_level": "high",
                    "fix_expr": "",
                    "fix_safe": False,
                },
            ],
            "likely_renames": [],
            "findings": [], "risks": [], "samples": {}, "suggested_next_actions": [],
        }
        result = schema_migrate_context(ctx, target_table="prod.sales.orders")

        # Verify safe cast detection
        casts = result["migration_plan"]["type_casts"]
        amount_cast = next(c for c in casts if c["column"] == "amount")
        price_cast = next(c for c in casts if c["column"] == "price")
        assert amount_cast["safe"] is True
        assert price_cast["safe"] is False

        # Verify DDL uses SQL types
        assert "BIGINT" in amount_cast["sql_expr"]
        assert "IntegerType" not in amount_cast["sql_expr"]
        assert "DECIMAL(10,2)" in price_cast["sql_expr"]
        assert "StringType" not in price_cast["sql_expr"]

        # Verify ADD COLUMN DDL
        adds = result["migration_plan"]["add_columns"]
        email_add = next(a for a in adds if a["column"] == "email")
        assert "STRING" in email_add["ddl"]
        assert "StringType" not in email_add["ddl"]

        score_add = next(a for a in adds if a["column"] == "score")
        assert "DOUBLE" in score_add["ddl"]
        assert "DoubleType" not in score_add["ddl"]

        count_add = next(a for a in adds if a["column"] == "count")
        assert "INT" in count_add["ddl"]
        assert "IntegerType" not in count_add["ddl"]
