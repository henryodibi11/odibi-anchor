#!/usr/bin/env python
"""Verify all context generators produce valid, JSON-serializable output.

Runs each generator with minimal inputs and checks:
1. Returns a dict (or str for markdown mode)
2. Contains expected standard keys (kind, subject, summary, metrics)
3. JSON-serializable
4. No exceptions on clean data
5. No exceptions on edge cases (empty df, single row, all nulls)

Usage: python scripts/verify_outputs.py
"""
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

import pandas as pd
from odibi_anchor.planning.task_execution_context import task_execution_context, quick_context
from odibi_anchor.validation.quality_gate_context import quality_gate_context
from odibi_anchor.validation.validation_summary_context import validation_summary_context
from odibi_anchor.validation.duplicate_key_context import duplicate_key_context
from odibi_anchor.tables.diff_ops import diff_tables_by_key
from odibi_anchor.tables.schema_diff_context import schema_diff_context
from odibi_anchor.tables.table_contract_summary import table_contract_summary


def check_output(name: str, result, expect_keys: set = None):
    """Validate a generator's output."""
    errors = []

    if not isinstance(result, dict):
        errors.append(f"Expected dict, got {type(result).__name__}")
        return errors

    if expect_keys:
        missing = expect_keys - set(result.keys())
        if missing:
            errors.append(f"Missing keys: {missing}")

    try:
        json.dumps(result, default=str)
    except (TypeError, ValueError) as e:
        errors.append(f"Not JSON-serializable: {e}")

    return errors


def main():
    """Verify tool outputs match expected contract structure."""
    from odibi_anchor._dispatcher._tool_wrappers import _profile_table_context

    def verified_profile(df, *, subject: str, row_count: int, columns: set[str]):
        result = _profile_table_context(df, subject=subject)
        metrics = result.get("metrics", {})
        actual_columns = result.get("column_profiles")
        if (
            result.get("kind") != "profile_table"
            or result.get("subject") != subject
            or metrics.get("row_count") != row_count
            or metrics.get("column_count") != len(columns)
            or not isinstance(actual_columns, dict)
            or set(actual_columns) != columns
        ):
            raise ValueError(f"profile_table returned an unsuccessful contract: {result}")
        return result

    print("=" * 60)
    print("VERIFY: Context generator output contracts")
    print("=" * 60)

    # Test data
    df = pd.DataFrame({"id": list(range(20)), "val": list(range(20)), "name": ["a"] * 20})
    df_empty = pd.DataFrame({"id": pd.Series([], dtype="int64"), "val": pd.Series([], dtype="int64")})
    df_nulls = pd.DataFrame({"id": [1, 2, 3], "val": [None, None, None]})
    df2 = pd.DataFrame({"id": list(range(15, 25)), "val": list(range(100, 110)), "name": ["b"] * 10})

    standard_keys = {"kind", "subject", "summary", "metrics"}
    total_checks = 0
    total_errors = 0

    tests = [
        # (name, callable, expected_keys)
        ("task_execution_context (basic)",
         lambda: task_execution_context(task="test", goal="verify"),
         {"status", "readiness", "plan", "metadata"}),

        ("task_execution_context (full)",
         lambda: task_execution_context(task="test", goal="verify", mode="implementation",
                                        constraints=["rule"], acceptance_criteria=["done"]),
         {"status", "readiness", "plan", "metadata"}),

        ("quick_context",
         lambda: quick_context("test"),
         None),  # returns str

        ("quality_gate_context (clean)",
         lambda: quality_gate_context(df, keys=["id"]),
         standard_keys | {"checks"}),

        ("quality_gate_context (empty)",
         lambda: quality_gate_context(df_empty, keys=["id"]),
         standard_keys | {"checks"}),

        ("quality_gate_context (nulls)",
         lambda: quality_gate_context(df_nulls, keys=["id"]),
         standard_keys | {"checks"}),

        ("quality_gate_context (markdown)",
         lambda: quality_gate_context(df, keys=["id"], output_format="markdown"),
         None),  # returns str

        ("duplicate_key_context (clean)",
         lambda: duplicate_key_context(df, keys=["id"]),
         {"kind", "subject", "summary"}),

        ("duplicate_key_context (dupes)",
         lambda: duplicate_key_context(
             pd.DataFrame({"id": [1, 1, 2], "val": [10, 20, 30]}), keys=["id"]),
         {"kind", "subject", "summary"}),

        ("validation_summary_context",
         lambda: validation_summary_context(df, rules=[{"column": "val", "check": "not_null"}]),
         standard_keys),

        ("schema_diff_context (same)",
         lambda: schema_diff_context(df, df),
         {"kind", "subject"}),

        ("schema_diff_context (different)",
         lambda: schema_diff_context(df, df2),
         {"kind", "subject"}),

        ("table_contract_summary",
         lambda: table_contract_summary(df),
         standard_keys),

        ("table_contract_summary (empty)",
         lambda: table_contract_summary(df_empty),
         standard_keys),

        ("profile_table",
         lambda: verified_profile(
             df, subject="verification", row_count=20, columns={"id", "val", "name"}
         ),
         standard_keys | {"column_profiles"}),

        ("profile_table (single row)",
         lambda: verified_profile(
             pd.DataFrame({"id": [1], "val": [42]}),
             subject="single-row",
             row_count=1,
             columns={"id", "val"},
         ),
         standard_keys | {"column_profiles"}),

        ("diff_tables_by_key",
         lambda: diff_tables_by_key(df[["id", "val"]].head(10), df2[["id", "val"]].head(10), keys=["id"]),
         {"kind", "subject", "status"}),
    ]

    for name, fn, expect_keys in tests:
        total_checks += 1
        try:
            result = fn()

            # String outputs (quick_context, markdown mode)
            if expect_keys is None:
                if isinstance(result, str) and len(result) > 0:
                    print(f"  ✓ {name} → str ({len(result)} chars)")
                else:
                    print(f"  ✗ {name} → unexpected: {type(result)}")
                    total_errors += 1
                continue

            errors = check_output(name, result, expect_keys)
            if errors:
                print(f"  ✗ {name}")
                for e in errors:
                    print(f"      {e}")
                total_errors += 1
            else:
                print(f"  ✓ {name} → dict ({len(result)} keys)")

        except Exception as e:
            print(f"  ✗ {name} → EXCEPTION: {type(e).__name__}: {e}")
            total_errors += 1

    print(f"\n{'='*60}")
    print(f"Results: {total_checks - total_errors}/{total_checks} passed")
    if total_errors == 0:
        print("\n✅ ALL OUTPUT CONTRACTS VERIFIED")
        return 0
    else:
        print(f"\n❌ {total_errors} FAILURE(S)")
        return 1


if __name__ == "__main__":
    sys.exit(main())
