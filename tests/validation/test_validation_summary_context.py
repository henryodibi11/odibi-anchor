"""Bundled tests for validation_summary_context.

Run inline via importlib (workspace __pycache__ limitation).
All tests use pandas DataFrames for portability.
"""

import json
import pandas as pd
import numpy as np

from odibi_anchor.validation.validation_summary_context import (
    validation_summary_context,
    render_validation_report,
)


# -------------------------------------------------------------------
# Basic functionality
# -------------------------------------------------------------------


def test_all_rules_pass():
    """No failures — promotion is safe."""
    df = pd.DataFrame({"id": [1, 2, 3], "name": ["a", "b", "c"]})
    rules = [{"type": "not_null", "columns": ["id", "name"]}]
    ctx = validation_summary_context(df, rules)
    assert ctx["metrics"]["is_promotion_safe"] is True
    assert ctx["metrics"]["rules_failed"] == 0
    assert ctx["metrics"]["rows_with_any_failure"] == 0
    assert ctx["blockers"] == []


def test_not_null_failure():
    """Null values trigger not_null failure."""
    df = pd.DataFrame({"id": [1, None, 3], "name": ["a", "b", "c"]})
    rules = [{"type": "not_null", "columns": ["id"]}]
    ctx = validation_summary_context(df, rules)
    assert ctx["rules"][0]["passed"] is False
    assert ctx["rules"][0]["failed_count"] == 1
    assert ctx["metrics"]["is_promotion_safe"] is False


def test_unique_failure():
    """Duplicate rows trigger unique failure."""
    df = pd.DataFrame({"id": [1, 2, 2, 3], "val": [10, 20, 30, 40]})
    rules = [{"type": "unique", "columns": ["id"]}]
    ctx = validation_summary_context(df, rules)
    assert ctx["rules"][0]["passed"] is False
    assert ctx["rules"][0]["failed_count"] == 2  # both dups
    assert ctx["metrics"]["is_promotion_safe"] is False


def test_range_failure():
    """Out-of-range values trigger range failure."""
    df = pd.DataFrame({"amount": [10, -5, 200, 50]})
    rules = [{"type": "range", "column": "amount", "min": 0,
              "max": 100}]
    ctx = validation_summary_context(df, rules)
    assert ctx["rules"][0]["failed_count"] == 2
    assert ctx["rules"][0]["severity"] == "warning"


def test_range_min_only():
    """Range with only min bound."""
    df = pd.DataFrame({"amount": [-1, 0, 10]})
    rules = [{"type": "range", "column": "amount", "min": 0}]
    ctx = validation_summary_context(df, rules)
    assert ctx["rules"][0]["failed_count"] == 1


def test_range_max_only():
    """Range with only max bound."""
    df = pd.DataFrame({"amount": [50, 100, 150]})
    rules = [{"type": "range", "column": "amount", "max": 100}]
    ctx = validation_summary_context(df, rules)
    assert ctx["rules"][0]["failed_count"] == 1


def test_accepted_values_failure():
    """Invalid values trigger accepted_values failure."""
    df = pd.DataFrame({"status": ["active", "pending", "invalid"]})
    rules = [{"type": "accepted_values", "column": "status",
              "values": ["active", "pending"]}]
    ctx = validation_summary_context(df, rules)
    assert ctx["rules"][0]["failed_count"] == 1


def test_regex_failure():
    """Non-matching values trigger regex failure."""
    df = pd.DataFrame({"email": ["a@b.com", "bad", "c@d.org"]})
    rules = [{"type": "regex", "column": "email",
              "pattern": r"^.+@.+\..+$"}]
    ctx = validation_summary_context(df, rules)
    assert ctx["rules"][0]["failed_count"] == 1


def test_custom_sql_failure():
    """Custom condition failure."""
    df = pd.DataFrame({"amount": [10, -5, 20]})
    rules = [{"type": "custom_sql", "condition": "amount > 0"}]
    ctx = validation_summary_context(df, rules)
    assert ctx["rules"][0]["failed_count"] == 1


def test_custom_sql_invalid_expression():
    """Invalid custom_sql expression doesn't crash."""
    df = pd.DataFrame({"x": [1, 2, 3]})
    rules = [{"type": "custom_sql",
              "condition": "INVALID SYNTAX !!!"}]
    ctx = validation_summary_context(df, rules)
    # Should not crash — treats as passing
    assert ctx["rules"][0]["passed"] is True


# -------------------------------------------------------------------
# Severity and promotion
# -------------------------------------------------------------------


def test_severity_defaults():
    """Default severity: not_null=blocker, range=warning."""
    df = pd.DataFrame({"id": [None], "amount": [-1]})
    rules = [
        {"type": "not_null", "columns": ["id"]},
        {"type": "range", "column": "amount", "min": 0},
    ]
    ctx = validation_summary_context(df, rules)
    assert ctx["rules"][0]["severity"] == "blocker"
    assert ctx["rules"][1]["severity"] == "warning"


def test_severity_map_override():
    """Severity map overrides defaults."""
    df = pd.DataFrame({"amount": [-1]})
    rules = [{"type": "range", "column": "amount", "min": 0}]
    ctx = validation_summary_context(
        df, rules,
        severity_map={"range:amount": "blocker"},
    )
    assert ctx["rules"][0]["severity"] == "blocker"
    assert ctx["metrics"]["is_promotion_safe"] is False


def test_severity_map_broad_key():
    """Broad severity key (type only) applies to all columns."""
    df = pd.DataFrame({"a": [-1], "b": [-1]})
    rules = [
        {"type": "range", "column": "a", "min": 0},
        {"type": "range", "column": "b", "min": 0},
    ]
    ctx = validation_summary_context(
        df, rules,
        severity_map={"range": "info"},
    )
    assert all(r["severity"] == "info" for r in ctx["rules"])


def test_warnings_only_is_promotion_safe():
    """Only warning failures — promotion is still safe."""
    df = pd.DataFrame({"amount": [-1]})
    rules = [{"type": "range", "column": "amount", "min": 0}]
    ctx = validation_summary_context(df, rules)
    assert ctx["metrics"]["is_promotion_safe"] is True
    assert ctx["metrics"]["warning_count"] == 1


# -------------------------------------------------------------------
# Edge cases
# -------------------------------------------------------------------


def test_empty_dataframe():
    """Empty DataFrame — rules pass vacuously."""
    df = pd.DataFrame({"id": pd.Series([], dtype="float64")})
    rules = [{"type": "not_null", "columns": ["id"]}]
    ctx = validation_summary_context(df, rules)
    assert ctx["rules"][0]["passed"] is True
    assert ctx["metrics"]["total_rows"] == 0
    # Should have empty_dataframe finding
    finding_types = [f["check_type"] for f in ctx["findings"]]
    assert "empty_dataframe" in finding_types


def test_column_not_in_dataframe():
    """Rule references non-existent column — passes silently."""
    df = pd.DataFrame({"x": [1, 2, 3]})
    rules = [{"type": "not_null", "columns": ["missing_col"]}]
    ctx = validation_summary_context(df, rules)
    assert ctx["rules"][0]["passed"] is True


def test_multiple_rules_mixed():
    """Multiple rules with mixed pass/fail."""
    df = pd.DataFrame({
        "id": [1, 2, 3],
        "name": ["a", None, "c"],
        "amount": [10, 20, -5],
    })
    rules = [
        {"type": "not_null", "columns": ["id"]},
        {"type": "not_null", "columns": ["name"]},
        {"type": "range", "column": "amount", "min": 0},
    ]
    ctx = validation_summary_context(df, rules)
    assert ctx["metrics"]["rules_passed"] == 1
    assert ctx["metrics"]["rules_failed"] == 2


# -------------------------------------------------------------------
# Output format and rendering
# -------------------------------------------------------------------


def test_output_format_dict():
    """Default output is dict."""
    df = pd.DataFrame({"id": [1]})
    rules = [{"type": "not_null", "columns": ["id"]}]
    ctx = validation_summary_context(df, rules)
    assert isinstance(ctx, dict)
    assert ctx["kind"] == "validation_summary_context"


def test_output_format_markdown():
    """Markdown output is string."""
    df = pd.DataFrame({"id": [1, None]})
    rules = [{"type": "not_null", "columns": ["id"]}]
    result = validation_summary_context(
        df, rules, output_format="markdown"
    )
    assert isinstance(result, str)
    assert "# Validation:" in result
    assert "## Metrics" in result


def test_output_format_invalid():
    """Invalid output_format raises ValueError."""
    df = pd.DataFrame({"id": [1]})
    rules = [{"type": "not_null", "columns": ["id"]}]
    try:
        validation_summary_context(
            df, rules, output_format="xml"
        )
        assert False, "Should have raised"
    except ValueError as e:
        assert "output_format" in str(e)


def test_render_validation_report_standalone():
    """Standalone renderer works on dict output."""
    df = pd.DataFrame({"id": [1, None], "amount": [-1, 10]})
    rules = [
        {"type": "not_null", "columns": ["id"]},
        {"type": "range", "column": "amount", "min": 0},
    ]
    ctx = validation_summary_context(df, rules)
    report = render_validation_report(ctx)
    assert "# Validation:" in report
    assert "## Failed Rules" in report
    assert "Blockers" in report


def test_render_with_show_samples():
    """Renderer includes sample failures when requested."""
    df = pd.DataFrame({"id": [1, None, None]})
    rules = [{"type": "not_null", "columns": ["id"]}]
    ctx = validation_summary_context(df, rules, sample_failures=2)
    report = render_validation_report(ctx, show_samples=True)
    assert "### Samples:" in report


def test_render_invalid_ctx():
    """Renderer raises on missing keys."""
    try:
        render_validation_report({"foo": "bar"})
        assert False
    except ValueError as e:
        assert "missing required keys" in str(e)


# -------------------------------------------------------------------
# JSON serialization
# -------------------------------------------------------------------


def test_json_serializable():
    """Output is fully JSON-serializable."""
    df = pd.DataFrame({
        "id": [1, None, 3],
        "amount": [10.0, np.nan, 50.0],
    })
    rules = [
        {"type": "not_null", "columns": ["id"]},
        {"type": "range", "column": "amount", "min": 0},
    ]
    ctx = validation_summary_context(df, rules)
    dumped = json.dumps(ctx, indent=2)
    assert "validation_summary_context" in dumped


# -------------------------------------------------------------------
# Sample failures
# -------------------------------------------------------------------


def test_sample_failures_collected():
    """Failed rules include sample failing rows."""
    df = pd.DataFrame({"id": [1, None, None, None, None, None]})
    rules = [{"type": "not_null", "columns": ["id"]}]
    ctx = validation_summary_context(df, rules, sample_failures=3)
    samples = ctx["rules"][0]["sample_failures"]
    assert len(samples) == 3
    assert all(s["id"] is None for s in samples)


def test_sample_failures_zero():
    """sample_failures=0 skips collection."""
    df = pd.DataFrame({"id": [None, None]})
    rules = [{"type": "not_null", "columns": ["id"]}]
    ctx = validation_summary_context(df, rules, sample_failures=0)
    assert ctx["rules"][0]["sample_failures"] == []


# -------------------------------------------------------------------
# Validation errors
# -------------------------------------------------------------------


def test_empty_rules_raises():
    """Empty rules list raises ValueError."""
    df = pd.DataFrame({"id": [1]})
    try:
        validation_summary_context(df, rules=[])
        assert False
    except ValueError as e:
        assert "non-empty" in str(e)


def test_invalid_engine_raises():
    """Invalid engine raises ValueError."""
    df = pd.DataFrame({"id": [1]})
    rules = [{"type": "not_null", "columns": ["id"]}]
    try:
        validation_summary_context(
            df, rules, engine="polars"
        )
        assert False
    except ValueError as e:
        assert "engine" in str(e)


# -------------------------------------------------------------------
# New rule types (VALIDATE_DEEPEN_SPEC)
# -------------------------------------------------------------------


def test_row_count_max_passes():
    """row_count_max passes when row count within limit."""
    df = pd.DataFrame({"id": [1, 2, 3]})
    rules = [{"type": "row_count_max", "max": 100}]
    ctx = validation_summary_context(df, rules)
    assert ctx["rules"][0]["passed"] is True
    assert ctx["rules"][0]["rule_type"] == "row_count_max"
    assert ctx["metrics"]["is_promotion_safe"] is True


def test_row_count_max_fails():
    """row_count_max fails when row count exceeds limit."""
    df = pd.DataFrame({"id": [1, 2, 3, 4, 5]})
    rules = [{"type": "row_count_max", "max": 3}]
    ctx = validation_summary_context(df, rules)
    assert ctx["rules"][0]["passed"] is False
    assert "row count 5" in ctx["rules"][0]["detail"]


def test_row_count_min_passes():
    """row_count_min passes when row count meets minimum."""
    df = pd.DataFrame({"id": [1, 2, 3]})
    rules = [{"type": "row_count_min", "min": 1}]
    ctx = validation_summary_context(df, rules)
    assert ctx["rules"][0]["passed"] is True


def test_row_count_min_fails():
    """row_count_min fails when row count below minimum."""
    df = pd.DataFrame({"id": [1, 2]})
    rules = [{"type": "row_count_min", "min": 5}]
    ctx = validation_summary_context(df, rules)
    assert ctx["rules"][0]["passed"] is False
    assert "< min" in ctx["rules"][0]["detail"]


def test_row_count_range_passes():
    """row_count_range passes when within bounds."""
    df = pd.DataFrame({"id": range(50)})
    rules = [{"type": "row_count_range", "min": 10, "max": 100}]
    ctx = validation_summary_context(df, rules)
    assert ctx["rules"][0]["passed"] is True


def test_row_count_range_fails_below():
    """row_count_range fails when below min."""
    df = pd.DataFrame({"id": [1, 2]})
    rules = [{"type": "row_count_range", "min": 10, "max": 100}]
    ctx = validation_summary_context(df, rules)
    assert ctx["rules"][0]["passed"] is False


def test_row_count_range_fails_above():
    """row_count_range fails when above max."""
    df = pd.DataFrame({"id": range(200)})
    rules = [{"type": "row_count_range", "min": 10, "max": 100}]
    ctx = validation_summary_context(df, rules)
    assert ctx["rules"][0]["passed"] is False


def test_referential_integrity_passes():
    """referential_integrity passes when all values in reference set."""
    df = pd.DataFrame({"status": ["active", "pending", "active"]})
    rules = [{"type": "referential_integrity", "column": "status",
              "reference_values": ["active", "pending", "closed"]}]
    ctx = validation_summary_context(df, rules)
    assert ctx["rules"][0]["passed"] is True


def test_referential_integrity_fails():
    """referential_integrity fails when values outside reference set."""
    df = pd.DataFrame({"status": ["active", "INVALID", "pending", "UNKNOWN"]})
    rules = [{"type": "referential_integrity", "column": "status",
              "reference_values": ["active", "pending", "closed"]}]
    ctx = validation_summary_context(df, rules)
    assert ctx["rules"][0]["passed"] is False
    assert ctx["rules"][0]["failed_count"] == 2


def test_referential_integrity_nulls_pass():
    """referential_integrity allows null values (only checks non-null)."""
    df = pd.DataFrame({"status": ["active", None, None]})
    rules = [{"type": "referential_integrity", "column": "status",
              "reference_values": ["active", "pending"]}]
    ctx = validation_summary_context(df, rules)
    assert ctx["rules"][0]["passed"] is True


def test_referential_integrity_empty_reference():
    """referential_integrity with empty reference_values passes (rule skipped)."""
    df = pd.DataFrame({"status": ["active", "pending"]})
    rules = [{"type": "referential_integrity", "column": "status",
              "reference_values": []}]
    ctx = validation_summary_context(df, rules)
    assert ctx["rules"][0]["passed"] is True


def test_referential_integrity_missing_column():
    """referential_integrity with missing column passes (column not in df)."""
    df = pd.DataFrame({"name": ["a", "b"]})
    rules = [{"type": "referential_integrity", "column": "status",
              "reference_values": ["active"]}]
    ctx = validation_summary_context(df, rules)
    assert ctx["rules"][0]["passed"] is True


def test_not_stale_passes():
    """not_stale passes when max date is recent."""
    from datetime import datetime, timedelta
    recent = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    df = pd.DataFrame({"updated_at": [recent, "2020-01-01"]})
    rules = [{"type": "not_stale", "column": "updated_at", "max_age_days": 7}]
    ctx = validation_summary_context(df, rules)
    assert ctx["rules"][0]["passed"] is True


def test_not_stale_fails():
    """not_stale fails when max date is too old."""
    df = pd.DataFrame({"updated_at": ["2020-01-01", "2020-06-15"]})
    rules = [{"type": "not_stale", "column": "updated_at", "max_age_days": 7}]
    ctx = validation_summary_context(df, rules)
    assert ctx["rules"][0]["passed"] is False


def test_not_stale_all_null():
    """not_stale fails when all values are null."""
    df = pd.DataFrame({"updated_at": [None, None, None]})
    rules = [{"type": "not_stale", "column": "updated_at", "max_age_days": 30}]
    ctx = validation_summary_context(df, rules)
    assert ctx["rules"][0]["passed"] is False


def test_not_stale_missing_column():
    """not_stale with missing column passes (column not in df)."""
    df = pd.DataFrame({"name": ["a", "b"]})
    rules = [{"type": "not_stale", "column": "event_date", "max_age_days": 7}]
    ctx = validation_summary_context(df, rules)
    assert ctx["rules"][0]["passed"] is True


def test_from_manifest_generates_rules():
    """from_manifest generates rules from .anchor_manifest.json."""
    import json, os, tempfile
    manifest = {
        "project_name": "test",
        "constraints": {
            "sensitive_columns": ["customer_id", "email"],
            "max_table_rows_local": 100000
        }
    }
    tmpdir = tempfile.mkdtemp()
    try:
        with open(os.path.join(tmpdir, ".anchor_manifest.json"), "w") as f:
            json.dump(manifest, f)
        df = pd.DataFrame({
            "customer_id": [1, 2, 3],
            "email": ["a@b.com", "c@d.com", "e@f.com"],
            "other": ["x", "y", "z"],
        })
        ctx = validation_summary_context(df, from_manifest=True, root=tmpdir)
        assert ctx["metrics"]["manifest_rules_generated"] == 3  # 2 not_null + 1 row_count_max
        assert ctx["metrics"]["rules_evaluated"] == 3
        assert ctx["metrics"]["is_promotion_safe"] is True
        # All manifest rules tagged
        for r in ctx["rules"]:
            assert r.get("source") == "manifest"
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_from_manifest_merge_explicit_wins():
    """Explicit rules override manifest rules on type+column conflict."""
    import json, os, tempfile
    manifest = {
        "project_name": "test",
        "constraints": {
            "sensitive_columns": ["email"],
            "max_table_rows_local": 50000
        }
    }
    tmpdir = tempfile.mkdtemp()
    try:
        with open(os.path.join(tmpdir, ".anchor_manifest.json"), "w") as f:
            json.dump(manifest, f)
        df = pd.DataFrame({"email": [None, "a@b.com"], "val": [1, 2]})
        # Explicit not_null on email with blocker severity
        ctx = validation_summary_context(
            df,
            rules=[{"type": "not_null", "columns": ["email"], "severity": "blocker"}],
            from_manifest=True,
            root=tmpdir,
        )
        # Should have: explicit not_null:email + manifest row_count_max (no duplicate email rule)
        email_rules = [r for r in ctx["rules"] if "email" in r.get("rule_id", "")]
        assert len(email_rules) == 1
        assert email_rules[0]["source"] == "explicit"
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_from_manifest_no_manifest_raises():
    """from_manifest=True with no manifest file raises ValueError."""
    import tempfile
    tmpdir = tempfile.mkdtemp()
    try:
        df = pd.DataFrame({"id": [1, 2, 3]})
        try:
            validation_summary_context(df, from_manifest=True, root=tmpdir)
            assert False, "Should have raised ValueError"
        except ValueError:
            pass  # Expected
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_from_manifest_regex_pattern():
    """from_manifest matches sensitive_columns with regex patterns."""
    import json, os, tempfile
    manifest = {
        "project_name": "test",
        "constraints": {
            "sensitive_columns": ["ssn_.*"],  # regex
        }
    }
    tmpdir = tempfile.mkdtemp()
    try:
        with open(os.path.join(tmpdir, ".anchor_manifest.json"), "w") as f:
            json.dump(manifest, f)
        df = pd.DataFrame({
            "ssn_primary": ["123", "456"],
            "ssn_secondary": ["789", "012"],
            "name": ["A", "B"],
        })
        ctx = validation_summary_context(df, from_manifest=True, root=tmpdir)
        # Should match ssn_primary and ssn_secondary
        not_null_rules = [r for r in ctx["rules"] if r["rule_type"] == "not_null"]
        matched_cols = set()
        for r in not_null_rules:
            matched_cols.update(r.get("columns", []))
        assert "ssn_primary" in matched_cols
        assert "ssn_secondary" in matched_cols
        assert "name" not in matched_cols
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_frame_persistence():
    """Frame receives validation results after validate runs."""
    from odibi_anchor._utils._context_frame import ContextFrame
    frame = ContextFrame(session_id="test", project="test", manifest={})
    df = pd.DataFrame({"id": [1, None, 3]})
    rules = [{"type": "not_null", "columns": ["id"]}]
    ctx = validation_summary_context(df, rules, subject="my_table", frame=frame)
    # Check data_context
    val_data = frame.data_context.tables_profiled.get("my_table", {}).get("validation", {})
    assert val_data["rules_evaluated"] == 1
    assert val_data["rules_failed"] == 1
    assert val_data["is_promotion_safe"] is False
    assert val_data["blocker_count"] == 1
    assert "timestamp" in val_data


def test_frame_findings_for_blockers():
    """Frame findings populated for blocker failures."""
    from odibi_anchor._utils._context_frame import ContextFrame
    frame = ContextFrame(session_id="test", project="test", manifest={})
    df = pd.DataFrame({"id": [1, None, 3], "key": [1, 1, 2]})
    rules = [
        {"type": "not_null", "columns": ["id"]},  # blocker — fails
        {"type": "unique", "columns": ["key"]},    # blocker — fails (1 is duped)
    ]
    ctx = validation_summary_context(df, rules, subject="test_tbl", frame=frame)
    assert len(frame.findings) == 2
    assert all(f.source == "validate" for f in frame.findings)
    assert all(f.severity == "critical" for f in frame.findings)
    assert all("test_tbl" in f.message for f in frame.findings)


def test_frame_no_findings_when_safe():
    """No findings added when validation passes (promotion safe)."""
    from odibi_anchor._utils._context_frame import ContextFrame
    frame = ContextFrame(session_id="test", project="test", manifest={})
    df = pd.DataFrame({"id": [1, 2, 3]})
    rules = [{"type": "not_null", "columns": ["id"]}]
    ctx = validation_summary_context(df, rules, subject="clean_tbl", frame=frame)
    assert len(frame.findings) == 0
    assert ctx["metrics"]["is_promotion_safe"] is True


def test_source_tag_on_explicit_rules():
    """Explicit rules are tagged with source='explicit'."""
    df = pd.DataFrame({"id": [1, 2, 3]})
    rules = [{"type": "not_null", "columns": ["id"]}]
    ctx = validation_summary_context(df, rules)
    assert ctx["rules"][0].get("source") == "explicit"


def test_new_rule_types_used_metric():
    """new_rule_types_used metric tracks new rule types."""
    df = pd.DataFrame({"id": [1, 2, 3], "status": ["a", "b", "c"]})
    rules = [
        {"type": "row_count_min", "min": 1},
        {"type": "referential_integrity", "column": "status",
         "reference_values": ["a", "b", "c"]},
        {"type": "not_null", "columns": ["id"]},
    ]
    ctx = validation_summary_context(df, rules)
    new_types = set(ctx["metrics"]["new_rule_types_used"])
    assert "row_count_min" in new_types
    assert "referential_integrity" in new_types
    assert "not_null" not in new_types  # not a new type


def test_mixed_old_and_new_rules():
    """Old and new rule types work together in one call."""
    from datetime import datetime, timedelta
    recent = (datetime.now() - timedelta(hours=12)).strftime("%Y-%m-%d %H:%M:%S")
    df = pd.DataFrame({
        "id": [1, 2, 3],
        "status": ["active", "pending", "active"],
        "updated_at": [recent, recent, recent],
    })
    rules = [
        {"type": "not_null", "columns": ["id"]},
        {"type": "row_count_range", "min": 1, "max": 1000},
        {"type": "referential_integrity", "column": "status",
         "reference_values": ["active", "pending", "closed"]},
        {"type": "not_stale", "column": "updated_at", "max_age_days": 7},
    ]
    ctx = validation_summary_context(df, rules)
    assert ctx["metrics"]["rules_evaluated"] == 4
    assert ctx["metrics"]["rules_passed"] == 4
    assert ctx["metrics"]["is_promotion_safe"] is True
