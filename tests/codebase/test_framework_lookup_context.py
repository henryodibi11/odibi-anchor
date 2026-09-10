"""Tests for framework lookup context."""

import ast
import json
import os
import tempfile
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from odibi_anchor.codebase.framework_lookup_context import (
    framework_lookup_context as run_lookup,
    render_framework_lookup_report as run_render_report,
    _build_registry_from_source,
    _parse_init_exports,
    _parse_function_info,
    _score_match,
    _build_signature,
    _get_docstring_summary,
    _extract_parameters,
    _extract_example,
    _parse_docstring_params,
    _generate_keywords,
    _extract_docstring_keywords,
    _clear_cache,
    _extract_all_names,
    _load_json_index,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear module-level cache before each test."""
    _clear_cache()
    yield
    _clear_cache()


@pytest.fixture
def sample_framework(tmp_path):
    """A minimal framework-like package with multiple categories."""
    # transformers/
    transformers = tmp_path / "transformers"
    transformers.mkdir()
    (transformers / "__init__.py").write_text(
        '"""DataFrame transformers."""\n'
        "from .window_ops import deduplicate, window_calc\n"
        "from .column_ops import drop_columns, rename_columns\n\n"
        '__all__ = ["deduplicate", "window_calc", "drop_columns", "rename_columns"]\n',
        encoding="utf-8",
    )
    (transformers / "window_ops.py").write_text(
        'def deduplicate(\n'
        '    df,\n'
        '    keys: list[str],\n'
        '    order_by: str | None = None,\n'
        ') -> "Any":\n'
        '    """Remove duplicate rows using window-based ROW_NUMBER.\n'
        '\n'
        '    Args:\n'
        '        df: Input DataFrame (Spark or Pandas).\n'
        '        keys: Columns that define uniqueness (PARTITION BY).\n'
        '        order_by: SQL ORDER BY clause to determine which record to keep\n'
        '            (e.g., "updated_at DESC"). First row is kept.\n'
        '\n'
        '    Returns:\n'
        '        Deduplicated DataFrame.\n'
        '\n'
        '    Example:\n'
        '        >>> deduplicate(df, keys=["id"], order_by="updated_at DESC")\n'
        '    """\n'
        '    pass\n'
        '\n\n'
        'def window_calc(\n'
        '    df,\n'
        '    partition_by: list[str],\n'
        '    *,\n'
        '    function: str = "row_number",\n'
        '    order_by: str | None = None,\n'
        '    alias: str = "window_result",\n'
        '):\n'
        '    """Apply a window function over partitions.\n'
        '\n'
        '    Args:\n'
        '        df: Input DataFrame.\n'
        '        partition_by: Columns to partition by.\n'
        '        function: Window function name (row_number, rank, sum, etc.).\n'
        '        order_by: ORDER BY clause.\n'
        '        alias: Output column name.\n'
        '\n'
        '    Example:\n'
        '        >>> window_calc(df, ["dept"], function="rank", order_by="salary DESC")\n'
        '    """\n'
        '    pass\n',
        encoding="utf-8",
    )
    (transformers / "column_ops.py").write_text(
        'def drop_columns(df, columns: list[str]):\n'
        '    """Drop specified columns from a DataFrame.\n'
        '\n'
        '    Args:\n'
        '        df: Input DataFrame.\n'
        '        columns: Column names to drop.\n'
        '\n'
        '    Returns:\n'
        '        DataFrame without the specified columns.\n'
        '    """\n'
        '    pass\n'
        '\n\n'
        'def rename_columns(df, mapping: dict[str, str]):\n'
        '    """Rename columns using a mapping dict.\n'
        '\n'
        '    Args:\n'
        '        df: Input DataFrame.\n'
        '        mapping: Old name -> new name mapping.\n'
        '\n'
        '    Returns:\n'
        '        DataFrame with renamed columns.\n'
        '    """\n'
        '    pass\n',
        encoding="utf-8",
    )

    # validation/
    validation = tmp_path / "validation"
    validation.mkdir()
    (validation / "__init__.py").write_text(
        '"""Data quality validation."""\n'
        "from .fk import validate_fk\n"
        "from .profiler import profile\n"
        "from .contracts import validate, generate_schema_from_df\n\n"
        '__all__ = ["validate_fk", "profile", "validate", "generate_schema_from_df"]\n',
        encoding="utf-8",
    )
    (validation / "fk.py").write_text(
        'def validate_fk(\n'
        '    child_df,\n'
        '    parent_df,\n'
        '    *,\n'
        '    child_key: str,\n'
        '    parent_key: str,\n'
        '    label: str = "",\n'
        '):\n'
        '    """Validate foreign key relationship between two DataFrames.\n'
        '\n'
        '    Args:\n'
        '        child_df: Child DataFrame with FK column.\n'
        '        parent_df: Parent DataFrame with PK column.\n'
        '        child_key: Column name in child.\n'
        '        parent_key: Column name in parent.\n'
        '        label: Human label for the relationship.\n'
        '\n'
        '    Returns:\n'
        '        FKValidationResult with orphan details.\n'
        '\n'
        '    Example:\n'
        '        >>> validate_fk(orders, customers, child_key="cust_id", parent_key="id")\n'
        '    """\n'
        '    pass\n',
        encoding="utf-8",
    )
    (validation / "profiler.py").write_text(
        'def profile(df, *, sample_size: int = 10000):\n'
        '    """Profile a DataFrame — types, nulls, distributions.\n'
        '\n'
        '    Args:\n'
        '        df: Input DataFrame.\n'
        '        sample_size: Max rows to sample for profiling.\n'
        '\n'
        '    Returns:\n'
        '        ProfileResult with column-level stats.\n'
        '    """\n'
        '    pass\n',
        encoding="utf-8",
    )
    (validation / "contracts.py").write_text(
        'def validate(df, schema):\n'
        '    """Validate a DataFrame against a schema contract.\n'
        '\n'
        '    Args:\n'
        '        df: Input DataFrame.\n'
        '        schema: Schema contract object.\n'
        '\n'
        '    Returns:\n'
        '        ValidationResult.\n'
        '    """\n'
        '    pass\n'
        '\n\n'
        'def generate_schema_from_df(df, *, layer: str = "silver"):\n'
        '    """Auto-generate a schema contract from a DataFrame.\n'
        '\n'
        '    Args:\n'
        '        df: Input DataFrame to inspect.\n'
        '        layer: Target layer (bronze/silver/gold) for default rules.\n'
        '\n'
        '    Returns:\n'
        '        Schema object.\n'
        '    """\n'
        '    pass\n',
        encoding="utf-8",
    )

    # patterns/
    patterns = tmp_path / "patterns"
    patterns.mkdir()
    (patterns / "__init__.py").write_text(
        '"""Data warehouse patterns."""\n'
        "from .merge import merge_into\n"
        "from .scd2 import scd2\n\n"
        '__all__ = ["merge_into", "scd2"]\n',
        encoding="utf-8",
    )
    (patterns / "merge.py").write_text(
        'def merge_into(\n'
        '    source_df,\n'
        '    target_table: str,\n'
        '    keys: list[str],\n'
        '    *,\n'
        '    spark=None,\n'
        '    mode: str = "upsert",\n'
        '):\n'
        '    """Merge source into target using key-based matching.\n'
        '\n'
        '    Args:\n'
        '        source_df: Source DataFrame.\n'
        '        target_table: Target table name (3-part UC name).\n'
        '        keys: Match keys.\n'
        '        spark: SparkSession.\n'
        '        mode: Merge mode (upsert, insert_only, full_replace).\n'
        '\n'
        '    Example:\n'
        '        >>> merge_into(df, "catalog.schema.table", keys=["id"], spark=spark)\n'
        '    """\n'
        '    pass\n',
        encoding="utf-8",
    )
    (patterns / "scd2.py").write_text(
        'def scd2(\n'
        '    source_df,\n'
        '    target_table: str,\n'
        '    keys: list[str],\n'
        '    *,\n'
        '    spark=None,\n'
        '    effective_date_col: str = "effective_date",\n'
        '):\n'
        '    """Apply SCD Type 2 logic — track history with effective/expiry dates.\n'
        '\n'
        '    Args:\n'
        '        source_df: Source DataFrame with current records.\n'
        '        target_table: Target table name.\n'
        '        keys: Business keys.\n'
        '        spark: SparkSession.\n'
        '        effective_date_col: Column name for effective date.\n'
        '\n'
        '    Example:\n'
        '        >>> scd2(df, "catalog.schema.dim_customer", keys=["customer_id"], spark=spark)\n'
        '    """\n'
        '    pass\n',
        encoding="utf-8",
    )

    # io/
    io_dir = tmp_path / "io"
    io_dir.mkdir()
    (io_dir / "__init__.py").write_text(
        '"""Framework I/O."""\n'
        "from .reader_provider import ReaderProvider\n"
        "from .writer_provider import SaverProvider\n\n"
        '__all__ = ["ReaderProvider", "SaverProvider"]\n',
        encoding="utf-8",
    )
    (io_dir / "reader_provider.py").write_text(
        'class ReaderProvider:\n'
        '    """Unified reader for Spark and Pandas with auto-detection.\n'
        '\n'
        '    Example:\n'
        '        >>> reader = ReaderProvider()\n'
        '        >>> df = reader.read_auto("/path/to/file.csv", spark=spark)\n'
        '    """\n'
        '    def __init__(self, *, default_engine: str = "spark"):\n'
        '        self.default_engine = default_engine\n'
        '\n'
        '    def read_auto(self, path: str, *, spark=None):\n'
        '        """Auto-detect format and read."""\n'
        '        pass\n',
        encoding="utf-8",
    )
    (io_dir / "writer_provider.py").write_text(
        'class SaverProvider:\n'
        '    """Unified writer for Spark and Pandas.\n'
        '\n'
        '    Example:\n'
        '        >>> saver = SaverProvider()\n'
        '        >>> saver.save(df=df, data_type="table", container="cat", path_prefix="sch", object_name="tbl")\n'
        '    """\n'
        '    def __init__(self):\n'
        '        pass\n'
        '\n'
        '    def save(self, *, df, data_type: str, container: str, path_prefix: str, object_name: str, spark=None, mode: str = "overwrite"):\n'
        '        """Save a DataFrame."""\n'
        '        pass\n',
        encoding="utf-8",
    )

    return tmp_path


# ---------------------------------------------------------------------------
# Standard Contract Tests
# ---------------------------------------------------------------------------


class TestStandardContract:
    """Verify standard output contract (kind, subject, summary, metrics, etc.)."""

    def test_returns_dict_by_default(self, sample_framework):
        ctx = run_lookup("deduplicate", framework_root=str(sample_framework))
        assert isinstance(ctx, dict)

    def test_has_all_standard_fields(self, sample_framework):
        ctx = run_lookup("deduplicate", framework_root=str(sample_framework))
        required_keys = {
            "kind", "subject", "summary", "metrics",
            "findings", "risks", "samples", "suggested_next_actions",
        }
        assert required_keys.issubset(ctx.keys())

    def test_kind_is_correct(self, sample_framework):
        ctx = run_lookup("deduplicate", framework_root=str(sample_framework))
        assert ctx["kind"] == "framework_lookup_context"

    def test_has_matches_field(self, sample_framework):
        ctx = run_lookup("deduplicate", framework_root=str(sample_framework))
        assert "matches" in ctx
        assert isinstance(ctx["matches"], list)

    def test_metrics_structure(self, sample_framework):
        ctx = run_lookup("deduplicate", framework_root=str(sample_framework))
        m = ctx["metrics"]
        assert "query" in m
        assert "matches_found" in m
        assert "categories_searched" in m
        assert "registry_size" in m

    def test_output_format_markdown(self, sample_framework):
        result = run_lookup(
            "deduplicate",
            framework_root=str(sample_framework),
            output_format="markdown",
        )
        assert isinstance(result, str)
        assert "# Framework Lookup:" in result

    def test_invalid_output_format_raises(self, sample_framework):
        with pytest.raises(ValueError, match="output_format"):
            run_lookup("x", framework_root=str(sample_framework), output_format="xml")

    def test_samples_is_dict(self, sample_framework):
        ctx = run_lookup("deduplicate", framework_root=str(sample_framework))
        assert ctx["samples"] == {}

    def test_subject_truncation(self, sample_framework):
        long_query = "a" * 100
        ctx = run_lookup(long_query, framework_root=str(sample_framework))
        assert len(ctx["subject"]) <= 40


# ---------------------------------------------------------------------------
# Exact Match Tests
# ---------------------------------------------------------------------------


class TestExactMatch:
    """Test exact function name matching."""

    def test_exact_name_match_deduplicate(self, sample_framework):
        ctx = run_lookup("deduplicate", framework_root=str(sample_framework))
        assert ctx["matches"]
        assert ctx["matches"][0]["function"] == "deduplicate"
        assert ctx["matches"][0]["relevance_score"] >= 0.9

    def test_exact_name_match_validate_fk(self, sample_framework):
        ctx = run_lookup("validate_fk", framework_root=str(sample_framework))
        assert ctx["matches"]
        assert ctx["matches"][0]["function"] == "validate_fk"

    def test_exact_name_returns_correct_import(self, sample_framework):
        ctx = run_lookup("merge_into", framework_root=str(sample_framework))
        assert ctx["matches"]
        m = ctx["matches"][0]
        pkg = sample_framework.name
        assert m["import_statement"] == f"from {pkg}.patterns import merge_into"

    def test_exact_name_returns_signature(self, sample_framework):
        ctx = run_lookup("deduplicate", framework_root=str(sample_framework))
        m = ctx["matches"][0]
        assert "keys" in m["signature"]
        assert "order_by" in m["signature"]

    def test_exact_name_returns_docstring(self, sample_framework):
        ctx = run_lookup("deduplicate", framework_root=str(sample_framework))
        m = ctx["matches"][0]
        assert "duplicate" in m["docstring_summary"].lower()

    def test_exact_name_returns_parameters(self, sample_framework):
        ctx = run_lookup("deduplicate", framework_root=str(sample_framework))
        m = ctx["matches"][0]
        param_names = [p["name"] for p in m["parameters"]]
        assert "df" in param_names
        assert "keys" in param_names

    def test_exact_name_returns_example(self, sample_framework):
        ctx = run_lookup("deduplicate", framework_root=str(sample_framework))
        m = ctx["matches"][0]
        assert "deduplicate(" in m["example"]

    def test_class_lookup(self, sample_framework):
        ctx = run_lookup("ReaderProvider", framework_root=str(sample_framework))
        assert ctx["matches"]
        m = ctx["matches"][0]
        assert m["function"] == "ReaderProvider"
        assert m["module"] == f"{sample_framework.name}.io"


# ---------------------------------------------------------------------------
# Fuzzy Match Tests
# ---------------------------------------------------------------------------


class TestFuzzyMatch:
    """Test fuzzy/keyword matching."""

    def test_natural_language_query(self, sample_framework):
        ctx = run_lookup(
            "deduplicate keeping latest row",
            framework_root=str(sample_framework),
        )
        assert ctx["matches"]
        # deduplicate should be top match
        assert ctx["matches"][0]["function"] == "deduplicate"

    def test_partial_name_match(self, sample_framework):
        ctx = run_lookup("dedup", framework_root=str(sample_framework))
        assert ctx["matches"]
        assert ctx["matches"][0]["function"] == "deduplicate"

    def test_keyword_match_from_docstring(self, sample_framework):
        ctx = run_lookup("foreign key validation", framework_root=str(sample_framework))
        assert ctx["matches"]
        # validate_fk should match due to docstring keywords
        func_names = [m["function"] for m in ctx["matches"]]
        assert "validate_fk" in func_names

    def test_merge_query(self, sample_framework):
        ctx = run_lookup("merge source into target", framework_root=str(sample_framework))
        assert ctx["matches"]
        func_names = [m["function"] for m in ctx["matches"]]
        assert "merge_into" in func_names

    def test_scd2_query(self, sample_framework):
        ctx = run_lookup("slowly changing dimension type 2", framework_root=str(sample_framework))
        assert ctx["matches"]
        func_names = [m["function"] for m in ctx["matches"]]
        assert "scd2" in func_names

    def test_no_match_returns_empty(self, sample_framework):
        ctx = run_lookup("zzqxwv_zzqxwv_zzqxwv", framework_root=str(sample_framework))
        assert ctx["matches"] == []
        assert ctx["metrics"]["matches_found"] == 0


# ---------------------------------------------------------------------------
# Category Filter Tests
# ---------------------------------------------------------------------------


class TestCategoryFilter:
    """Test category filtering."""

    def test_filter_to_transformers(self, sample_framework):
        ctx = run_lookup(
            "deduplicate",
            framework_root=str(sample_framework),
            category="transformers",
        )
        assert ctx["matches"]
        assert all(m["module"] == f"{sample_framework.name}.transformers" for m in ctx["matches"])
        assert ctx["metrics"]["categories_searched"] == 1

    def test_filter_to_validation(self, sample_framework):
        ctx = run_lookup(
            "validate",
            framework_root=str(sample_framework),
            category="validation",
        )
        assert ctx["matches"]
        assert all(m["module"] == f"{sample_framework.name}.validation" for m in ctx["matches"])

    def test_filter_excludes_other_categories(self, sample_framework):
        ctx = run_lookup(
            "deduplicate",
            framework_root=str(sample_framework),
            category="validation",
        )
        # deduplicate is in transformers, not validation
        func_names = [m["function"] for m in ctx["matches"]]
        assert "deduplicate" not in func_names

    def test_no_category_searches_all(self, sample_framework):
        ctx = run_lookup("", framework_root=str(sample_framework))
        assert ctx["metrics"]["categories_searched"] >= 3


# ---------------------------------------------------------------------------
# Max Results Tests
# ---------------------------------------------------------------------------


class TestMaxResults:
    """Test max_results parameter."""

    def test_default_max_results(self, sample_framework):
        ctx = run_lookup("columns", framework_root=str(sample_framework))
        assert len(ctx["matches"]) <= 5

    def test_custom_max_results(self, sample_framework):
        ctx = run_lookup(
            "columns",
            framework_root=str(sample_framework),
            max_results=2,
        )
        assert len(ctx["matches"]) <= 2

    def test_max_results_one(self, sample_framework):
        ctx = run_lookup(
            "deduplicate",
            framework_root=str(sample_framework),
            max_results=1,
        )
        assert len(ctx["matches"]) == 1


# ---------------------------------------------------------------------------
# Include Source Tests
# ---------------------------------------------------------------------------


class TestIncludeSource:
    """Test include_source parameter."""

    def test_source_excluded_by_default(self, sample_framework):
        ctx = run_lookup("deduplicate", framework_root=str(sample_framework))
        assert "source" not in ctx["matches"][0]

    def test_source_included_when_requested(self, sample_framework):
        ctx = run_lookup(
            "deduplicate",
            framework_root=str(sample_framework),
            include_source=True,
        )
        assert "source" in ctx["matches"][0]
        assert "def deduplicate" in ctx["matches"][0]["source"]


# ---------------------------------------------------------------------------
# Render Tests
# ---------------------------------------------------------------------------


class TestRender:
    """Test markdown rendering."""

    def test_render_contains_function_name(self, sample_framework):
        ctx = run_lookup("deduplicate", framework_root=str(sample_framework))
        md = run_render_report(ctx)
        assert "deduplicate" in md

    def test_render_contains_import(self, sample_framework):
        ctx = run_lookup("deduplicate", framework_root=str(sample_framework))
        md = run_render_report(ctx)
        assert f"from {sample_framework.name}.transformers import deduplicate" in md

    def test_render_contains_metrics(self, sample_framework):
        ctx = run_lookup("deduplicate", framework_root=str(sample_framework))
        md = run_render_report(ctx)
        assert "matches_found" in md

    def test_render_no_matches(self, sample_framework):
        ctx = run_lookup("xyzzy", framework_root=str(sample_framework))
        md = run_render_report(ctx)
        assert "No matching functions" in md


# ---------------------------------------------------------------------------
# JSON Index Tests
# ---------------------------------------------------------------------------


class TestJsonIndex:
    """Test pre-built JSON index loading."""

    def test_loads_valid_index(self, sample_framework):
        index = {
            "generated_at": "2025-01-15T10:00:00Z",
            "framework_version": "2.0.0",
            "functions": [
                {
                    "name": "test_func",
                    "module": "odibi.transformers",
                    "import": "from odibi.transformers import test_func",
                    "signature": "test_func(df, keys)",
                    "docstring": "A test function.",
                    "parameters": [
                        {"name": "df", "type": "DataFrame", "description": "Input"},
                    ],
                    "category": "transformers",
                    "keywords": ["test", "func"],
                    "example": "test_func(df, ['id'])",
                }
            ],
        }
        index_path = sample_framework / ".api_index.json"
        index_path.write_text(json.dumps(index))

        ctx = run_lookup("test_func", framework_root=str(sample_framework))
        assert ctx["matches"]
        assert ctx["matches"][0]["function"] == "test_func"

    def test_falls_back_on_invalid_json(self, sample_framework):
        index_path = sample_framework / ".api_index.json"
        index_path.write_text("not valid json{{{")

        # Should fall back to live parsing
        ctx = run_lookup("deduplicate", framework_root=str(sample_framework))
        assert ctx["matches"]
        assert ctx["matches"][0]["function"] == "deduplicate"

    def test_falls_back_on_empty_functions(self, sample_framework):
        index = {"generated_at": "2025-01-15", "functions": []}
        index_path = sample_framework / ".api_index.json"
        index_path.write_text(json.dumps(index))

        # Should fall back to live parsing
        ctx = run_lookup("deduplicate", framework_root=str(sample_framework))
        assert ctx["matches"]


# ---------------------------------------------------------------------------
# Cache Tests
# ---------------------------------------------------------------------------


class TestCache:
    """Test registry caching behavior."""

    def test_cache_reused_on_second_call(self, sample_framework):
        ctx1 = run_lookup("deduplicate", framework_root=str(sample_framework))
        ctx2 = run_lookup("merge_into", framework_root=str(sample_framework))
        # Both should work (cache hit on second)
        assert ctx1["matches"]
        assert ctx2["matches"]

    def test_cache_invalidated_on_mtime_change(self, sample_framework):
        ctx1 = run_lookup("deduplicate", framework_root=str(sample_framework))
        assert ctx1["matches"]

        # Modify an __init__.py to change mtime
        init = sample_framework / "transformers" / "__init__.py"
        content = init.read_text()
        # Touch the file (change mtime)
        import time
        time.sleep(0.01)
        init.write_text(content + "\n# modified\n")

        ctx2 = run_lookup("deduplicate", framework_root=str(sample_framework))
        assert ctx2["matches"]


# ---------------------------------------------------------------------------
# Registry Building Tests
# ---------------------------------------------------------------------------


class TestRegistryBuilding:
    """Test internal registry construction."""

    def test_builds_from_all_categories(self, sample_framework):
        registry = _build_registry_from_source(sample_framework)
        categories = {e["category"] for e in registry}
        assert "transformers" in categories
        assert "validation" in categories
        assert "patterns" in categories
        assert "io" in categories

    def test_registry_entry_has_required_fields(self, sample_framework):
        registry = _build_registry_from_source(sample_framework)
        for entry in registry:
            assert "name" in entry
            assert "module" in entry
            assert "import_statement" in entry
            assert "signature" in entry
            assert "category" in entry

    def test_registry_size_matches_exports(self, sample_framework):
        registry = _build_registry_from_source(sample_framework)
        # 4 transformers + 4 validation + 2 patterns + 2 io = 12
        assert len(registry) == 12

    def test_nonexistent_root_returns_empty(self, tmp_path):
        registry = _build_registry_from_source(tmp_path / "nonexistent")
        assert registry == []


# ---------------------------------------------------------------------------
# Scoring Tests
# ---------------------------------------------------------------------------


class TestScoring:
    """Test relevance scoring logic."""

    def test_exact_match_scores_highest(self):
        entry = {
            "name": "deduplicate",
            "keywords": ["dedup", "duplicate"],
            "docstring_full": "Remove duplicate rows.",
            "parameters": [{"name": "df"}, {"name": "keys"}],
            "category": "transformers",
        }
        score = _score_match("deduplicate", entry)
        assert score >= 0.9

    def test_substring_match_scores_high(self):
        entry = {
            "name": "deduplicate",
            "keywords": ["dedup"],
            "docstring_full": "Remove duplicate rows.",
            "parameters": [{"name": "df"}],
            "category": "transformers",
        }
        score = _score_match("dedup", entry)
        assert score >= 0.5

    def test_keyword_match_scores_medium(self):
        entry = {
            "name": "validate_fk",
            "keywords": ["foreign", "key", "fk"],
            "docstring_full": "Validate foreign key relationship.",
            "parameters": [{"name": "child_df"}],
            "category": "validation",
        }
        score = _score_match("foreign key", entry)
        assert score > 0.0

    def test_no_match_scores_zero(self):
        entry = {
            "name": "deduplicate",
            "keywords": ["dedup"],
            "docstring_full": "Remove duplicate rows.",
            "parameters": [{"name": "df"}],
            "category": "transformers",
        }
        score = _score_match("xyzzy_nothing", entry)
        assert score == 0.0

    def test_score_capped_at_one(self):
        entry = {
            "name": "deduplicate",
            "keywords": ["deduplicate", "dedup", "duplicate"],
            "docstring_full": "deduplicate rows deduplicate",
            "parameters": [{"name": "deduplicate"}],
            "category": "transformers",
        }
        score = _score_match("deduplicate transformers", entry)
        assert score <= 1.0


# ---------------------------------------------------------------------------
# Helper Function Tests
# ---------------------------------------------------------------------------


class TestHelpers:
    """Test internal helper functions."""

    def test_build_signature_simple(self, tmp_path):
        code = "def foo(a, b, c=1): pass"
        tree = ast.parse(code)
        func = tree.body[0]
        sig = _build_signature(func)
        assert sig == "foo(a, b, c=1)"

    def test_build_signature_keyword_only(self, tmp_path):
        code = "def bar(x, *, y=None, z='abc'): pass"
        tree = ast.parse(code)
        func = tree.body[0]
        sig = _build_signature(func)
        assert "x" in sig
        assert "y=None" in sig
        assert "z='abc'" in sig
        assert "*" in sig

    def test_build_signature_with_annotations(self, tmp_path):
        code = "def baz(df, keys: list[str], *, mode: str = 'fast'): pass"
        tree = ast.parse(code)
        func = tree.body[0]
        sig = _build_signature(func)
        assert "keys: list[str]" in sig
        assert "mode: str" in sig

    def test_get_docstring_summary(self):
        doc = "Remove duplicate rows.\n\nMore details here."
        assert _get_docstring_summary(doc) == "Remove duplicate rows."

    def test_get_docstring_summary_multiline(self):
        doc = "Short summary\nwith continuation"
        result = _get_docstring_summary(doc)
        assert "Short summary" in result

    def test_get_docstring_summary_empty(self):
        assert _get_docstring_summary("") == ""

    def test_parse_docstring_params(self):
        doc = (
            "Some function.\n\n"
            "Args:\n"
            "    df: Input DataFrame.\n"
            "    keys: Columns to use.\n"
            "\n"
            "Returns:\n"
            "    Result.\n"
        )
        params = _parse_docstring_params(doc)
        assert params["df"] == "Input DataFrame."
        assert params["keys"] == "Columns to use."

    def test_extract_example_doctest(self):
        doc = (
            "Do something.\n\n"
            "Example:\n"
            "    >>> foo(x, y=1)\n"
        )
        assert _extract_example(doc) == "foo(x, y=1)"

    def test_extract_example_empty(self):
        doc = "No example here."
        assert _extract_example(doc) == ""

    def test_generate_keywords(self):
        kw = _generate_keywords("validate_fk")
        assert "validate_fk" in kw
        assert "validate" in kw
        assert "fk" in kw

    def test_generate_keywords_camelcase(self):
        kw = _generate_keywords("ReaderProvider")
        assert "readerprovider" in kw

    def test_extract_all_names(self):
        code = '__all__ = ["foo", "bar", "baz"]'
        tree = ast.parse(code)
        names = _extract_all_names(tree)
        assert names == ["foo", "bar", "baz"]

    def test_extract_all_names_missing(self):
        code = "x = 1"
        tree = ast.parse(code)
        assert _extract_all_names(tree) == []


# ---------------------------------------------------------------------------
# Suggested Next Actions Tests
# ---------------------------------------------------------------------------


class TestSuggestedActions:
    """Test suggested_next_actions content."""

    def test_has_import_suggestion_on_match(self, sample_framework):
        ctx = run_lookup("deduplicate", framework_root=str(sample_framework))
        actions = ctx["suggested_next_actions"]
        assert any("import" in a for a in actions)

    def test_has_manual_suggestion_on_no_match(self, sample_framework):
        ctx = run_lookup("xyzzy_nothing", framework_root=str(sample_framework))
        actions = ctx["suggested_next_actions"]
        assert any("manual" in a.lower() or "PRE-CODE GATE" in a for a in actions)

    def test_has_quality_gate_reminder(self, sample_framework):
        ctx = run_lookup("deduplicate", framework_root=str(sample_framework))
        actions = ctx["suggested_next_actions"]
        assert any("quality" in a.lower() for a in actions)


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_empty_query(self, sample_framework):
        # Should not crash, may return low-relevance or no results
        ctx = run_lookup("", framework_root=str(sample_framework))
        assert isinstance(ctx, dict)
        assert ctx["kind"] == "framework_lookup_context"

    def test_nonexistent_framework_root(self, tmp_path):
        ctx = run_lookup("deduplicate", framework_root=str(tmp_path / "nonexistent"))
        assert ctx["matches"] == []
        assert ctx["metrics"]["registry_size"] == 0

    def test_category_case_insensitive(self, sample_framework):
        ctx = run_lookup(
            "deduplicate",
            framework_root=str(sample_framework),
            category="Transformers",
        )
        assert ctx["matches"]
        assert ctx["matches"][0]["function"] == "deduplicate"

    def test_special_characters_in_query(self, sample_framework):
        ctx = run_lookup("(merge) [into] {table}", framework_root=str(sample_framework))
        assert isinstance(ctx, dict)
        # Should not crash

    def test_unicode_in_query(self, sample_framework):
        ctx = run_lookup("données dédupliquées", framework_root=str(sample_framework))
        assert isinstance(ctx, dict)


# ---------------------------------------------------------------------------
# Integration: Real Framework (if available)
# ---------------------------------------------------------------------------


class TestRealFramework:
    """Integration tests against the actual framework installation.

    These tests are skipped if the framework directory doesn't exist.
    """

    FRAMEWORK_ROOT = os.environ.get("FRAMEWORK_ROOT", "")

    @pytest.fixture(autouse=True)
    def skip_if_no_framework(self):
        if not self.FRAMEWORK_ROOT or not Path(self.FRAMEWORK_ROOT).is_dir():
            pytest.skip("Set FRAMEWORK_ROOT to run external framework integration tests")

    def test_deduplicate_lookup(self):
        ctx = run_lookup("deduplicate", framework_root=self.FRAMEWORK_ROOT)
        assert ctx["matches"]
        assert ctx["matches"][0]["function"] == "deduplicate"
        assert "keys" in ctx["matches"][0]["signature"]

    def test_validate_fk_lookup(self):
        ctx = run_lookup("validate foreign key", framework_root=self.FRAMEWORK_ROOT)
        assert ctx["matches"]
        func_names = [m["function"] for m in ctx["matches"]]
        assert "validate_fk" in func_names

    def test_merge_lookup(self):
        ctx = run_lookup("merge_into", framework_root=self.FRAMEWORK_ROOT)
        assert ctx["matches"]
        assert ctx["matches"][0]["function"] == "merge_into"

    def test_registry_size(self):
        ctx = run_lookup("", framework_root=self.FRAMEWORK_ROOT)
        # Should have 100+ functions indexed
        assert ctx["metrics"]["registry_size"] >= 50
