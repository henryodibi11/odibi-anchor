# diff — Architecture

**Module:** `tables/diff_ops.py` (1,971 lines)
**Entry point:** `diff_tables_by_key(old_df, new_df, keys=[...])`

---

## System Overview

```
old_df + new_df
       │
       ▼
  [1] Validate inputs
      (engine check, key validation, column check)
       │
       ▼
  [2] Outer merge on keys
      (pandas: merge with suffixes; spark: full outer join)
       │
       ▼
  [3] Classify each row
      added / removed / changed / unchanged
       │
       ├── changed → [4] Per-column breakdown
       │                  null→value / value→null / value≠value
       │
       ▼
  [5] Compute metrics + pct thresholds
       │
       ▼
  [6] Generate merge_expr SQL
      (scoped to changed columns only)
       │
       ▼
  [7] Build context dict → render or return
```

---

## Change Categories

| Category | Condition | What it means |
|---|---|---|
| `added` | Key in new_df only | New row arrived since last snapshot |
| `removed` | Key in old_df only | Row disappeared since last snapshot |
| `changed` | Key in both, at least one column differs | Existing row was updated |
| `unchanged` | Key in both, all columns identical | No change |

### Per-column sub-types (changed rows only)

| Sub-type | Condition | Suggested action |
|---|---|---|
| `null_to_value` | old=null, new=value | Verify intentional backfill |
| `value_to_null` | old=value, new=null | Investigate data loss |
| `value_changed` | Both non-null, values differ | Run `coerce_check` to classify root cause |

---

## Design Decisions

**Outer join on keys, not set operations.**
Using an outer join preserves full row context for sampling and MERGE generation. Pure set operations on key columns would require a separate join for evidence gathering.

**Safety threshold on added/removed %.**
`status="error"` fires when added or removed row count exceeds a configurable threshold. This prevents a bad source file from silently replacing your entire table. The threshold is intentionally conservative.

**merge_expr scoped to changed columns.**
The generated SQL only updates columns that actually changed — not all columns. This minimizes write amplification in Delta MERGE INTO operations.

**NaN-aware comparison.**
Two NaN values are treated as equal (not different). This matches SQL NULL semantics: `NULL = NULL` is treated as "unchanged" rather than "changed."

**Dual-engine (pandas + Spark), same output contract.**
The output dict structure is identical regardless of engine. The pandas backend is complete; the Spark backend follows the same interface.

---

## Internal Structure

```
tables/
└── diff_ops.py
    ├── diff_tables_by_key()        ← public entry point
    ├── _diff_tables_by_key_pandas() ← pandas implementation
    ├── _diff_tables_by_key_spark()  ← spark implementation
    ├── _classify_row_pandas()       ← added/removed/changed/unchanged
    ├── _compare_column_pair()       ← null→value / value→null / value≠value
    ├── _build_merge_expr()          ← SQL MERGE INTO generator
    └── render_diff_report()         ← markdown renderer
```

---

## Anchor Standard Output Contract

```python
ctx = {
    "kind":     "diff_tables_by_key",
    "subject":  "old_df vs new_df",          # or custom label
    "engine":   "pandas",                     # or "spark"
    "status":   "ok",                         # "ok" or "error"
    "summary":  "1 added, 0 removed, 1 changed, 1 unchanged",
    "metrics": {
        "old_row_count":        3,
        "new_row_count":        3,
        "row_count_delta":      0,
        "added_key_count":      1,
        "removed_key_count":    1,
        "changed_key_count":    1,
        "unchanged_key_count":  1,
        "changed_cell_count":   1,
        "added_key_pct":        0.333,
        "removed_key_pct":      0.333,
        "changed_key_pct":      0.333,
        "changed_column_counts": [
            {
                "column":           "status",
                "changed_key_count": 1,
                "null_to_value":     0,
                "value_to_null":     0,
                "value_changed":     1,
            }
        ],
    },
    "findings":               [...],
    "risks":                  [...],
    "samples": {
        "added_rows":   [...],   # up to sample_limit rows
        "removed_rows": [...],
        "changed_rows": [...],   # includes before/after values per column
    },
    "suggested_next_actions": [...],
    "merge_expr":             "MERGE INTO target USING source ON ...",  # status=ok only
}
```

---

## Integration

```
schema_diff          diff          coerce_check
(column changes?) → (row changes?) → (why did values change?)
                          │
                          └──→ case_file (show me the actual rows)
```

Run `schema_diff` first when you suspect column additions or type changes. Only run `diff` when schemas match or you've accounted for the differences. After `diff`, run `coerce_check` on columns where `value_changed > 0`.
