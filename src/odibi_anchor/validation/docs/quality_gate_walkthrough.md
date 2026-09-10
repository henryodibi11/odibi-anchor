# quality_gate_context — Code Walkthrough

## Module Overview

`quality_gate_context` performs pre-write structural safety checks with a binary **write-safety decision** (`is_write_safe`). It runs up to 7 check types, **auto-skips** checks when required inputs are missing, and generates paste-ready fix code using the caller's `df_name` variable.

**Architecture:** 4-file module decomposition
- `quality_gate_context.py` — 373-line orchestrator
- `_quality_gate_helpers.py` — threshold merging, check resolution, result building
- `_quality_gate_checks_pandas.py` — pandas engine checks
- `_quality_gate_checks_spark.py` — Spark engine checks

**Check types:** `dup_keys`, `null_keys`, `schema`, `row_count`, `completeness`, `freshness`, `anomaly`

**Source:** `src/odibi_anchor/validation/` (373 lines orchestrator + 3 helpers)

---

## Worked Example 1: Duplicate Key Check Fails

**Input:** DataFrame with 1000 rows, `keys=["order_id"]`, 3 duplicate keys.

```python
# _check_dup_keys_pandas(df, keys=["order_id"], sample_limit=10)

# Step 1: Group and count
groups = df.groupby(["order_id"]).size()
# groups with size > 1: 3 groups (sizes: 2, 3, 2) → 7 excess rows

# Step 2: Build result
status   = "FAIL"
severity = "blocker"
excess   = 7  # (2-1) + (3-1) + (2-1)

# Step 3: Fix expression (using df_name parameter)
fix_expr = 'df = df.drop_duplicates(subset=["order_id"], keep="last")'

# Step 4: Sample top 3 worst offenders
samples = groups[groups > 1].sort_values(ascending=False).head(3)
```

**Output:**
```python
{
    "is_write_safe": False,
    "failed_checks": [{
        "check":        "dup_keys",
        "status":       "FAIL",
        "severity":     "blocker",
        "excess_rows":  7,
        "fix_expr":     'df = df.drop_duplicates(subset=["order_id"], keep="last")'
    }],
    "recommendation": "Not safe to write — 1 blocker check failed"
}
```

`is_write_safe: False` — any blocker check failure blocks the write.

---

## Worked Example 2: All Checks Pass

**Input:** DataFrame with 1000 rows, `keys=["id"]`, `target_schema` provided.

```python
# _resolve_checks(checks=None, keys=["id"], target_schema=<df>)
# → all 5 applicable checks are included

# Results:
# dup_keys:     0 duplicates                    → PASS
# null_keys:    0 null keys                     → PASS
# schema:       all columns present and typed   → PASS
# row_count:    1000 > _DEFAULT_THRESHOLDS["row_count"] (1) → PASS
# completeness: 99.5% > threshold (95%)          → PASS
```

**Output:**
```python
{
    "is_write_safe":   True,
    "checks_run":      5,
    "checks_skipped":  0,
    "recommendation":  "Safe to write — all 5 checks passed"
}
```

All checks passing at once is the happy path. The orchestrator collects results from `_quality_gate_checks_pandas._run_all_checks()` and sets `is_write_safe = True` when no blocker failed.

---

## Worked Example 3: Auto-Skip When Inputs Missing

**Input:** DataFrame with 1000 rows, `keys=None`, `target_schema=None`.

```python
# _resolve_checks(checks=None, keys=None, target_schema=None)
# Step 1: keys is None → skip dup_keys, null_keys (both require keys)
# Step 2: target_schema is None → skip schema
# Step 3: Only run: [row_count, completeness]
```

**Output:**
```python
{
    "checks_run":     2,
    "checks_skipped": 3,
    "skipped_reason": {
        "dup_keys":  "keys not provided",
        "null_keys": "keys not provided",
        "schema":    "target_schema not provided"
    }
}
```

Auto-skip prevents false failures — callers don’t need to know which checks require which inputs. The `_resolve_checks()` function handles all routing logic in one place.

---

## Python Patterns

| Pattern | Location | Purpose |
|---|---|---|
| Module decomposition | 4 files | Orchestrator delegates to engine-specific check modules; swapping engines is transparent to callers |
| `_ALL_CHECKS` tuple | `_quality_gate_helpers.py` | Defines canonical check order; ensures consistent iteration and reporting |
| `_DEFAULT_THRESHOLDS` dict | Helper module | `{"completeness": 0.95, "row_count": 1}` — merged with `thresholds=` kwarg via `_merge_thresholds` |
| `_merge_thresholds()` | Private helper | Caller’s `thresholds=` wins on collision; defaults fill gaps. No mutation of the defaults dict. |
| `df_name` param | Public API | Fix expressions use the caller’s variable name: `'df = df.drop_duplicates(...)'` where `df` = `df_name` |
| `_resolve_checks()` | Private helper | Filters `_ALL_CHECKS` based on available inputs — single responsibility, no conditionals in the orchestrator |
