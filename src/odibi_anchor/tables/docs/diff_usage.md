# diff — Full Usage Reference

---

## Function Signature

```python
diff_tables_by_key(
    old_df,
    new_df,
    keys: list[str],
    *,
    compare_columns: list[str] | None = None,
    subject: str | None = None,
    engine: str = "auto",
    sample_limit: int = 20,
    output_format: str = "dict",
) -> dict | str
```

---

## Parameters — Complete Reference

### `old_df` / `new_df`
The two DataFrames to compare. Both must use the same engine (both pandas or both Spark). Passing mixed types raises `ValueError`.

### `keys`
Required. List of column names that uniquely identify a row — the business grain. All key columns must exist in both DataFrames. Use multi-column keys for compound grains: `keys=["iso", "queue_number"]`.

### `compare_columns`
Optional. Restricts comparison to the listed non-key columns. When omitted, all common non-key columns are compared. Use when:
- You only care about specific fields changing (e.g. status and MW, not timestamps)
- One DataFrame has extra columns the other doesn't and you want to ignore them

### `subject`
Optional display label. Appears in the output header and summary. Good practice: `"queue_gold: 2026-06-04 vs 2026-06-11"`.

### `engine`
- `"auto"` (default) — detected from `old_df` type
- `"pandas"` — force pandas backend
- `"spark"` — force Spark backend

### `sample_limit`
Controls how many example rows appear in each sample section (`added_rows`, `removed_rows`, `changed_rows`). Also caps the per-row changed-column detail in `changed_rows`. Default 20.

### `output_format`
- `"dict"` (default) — returns structured dict
- `"markdown"` — returns formatted markdown string for display or LLM prompts

---

## Output Contract — Complete Reference

### Top-level keys

| Key | Type | Always present | Description |
|---|---|---|---|
| `kind` | str | Yes | `"diff_tables_by_key"` |
| `subject` | str | Yes | Display label |
| `engine` | str | Yes | `"pandas"` or `"spark"` |
| `status` | str | Yes | `"ok"` or `"error"` |
| `summary` | str | Yes | One-line verdict |
| `metrics` | dict | Yes | All counts and percentages |
| `findings` | list[str] | Yes | What was discovered |
| `risks` | list[str] | Yes | Anomalies worth investigating |
| `samples` | dict | Yes | Example rows per category |
| `suggested_next_actions` | list[str] | Yes | What to do next |
| `merge_expr` | str | Only when `status="ok"` | Ready-to-paste MERGE INTO SQL |

### `metrics` sub-keys

| Key | Description |
|---|---|
| `old_row_count` | Row count of old_df |
| `new_row_count` | Row count of new_df |
| `row_count_delta` | new - old (positive = grew, negative = shrank) |
| `added_key_count` | Keys in new_df only |
| `removed_key_count` | Keys in old_df only |
| `changed_key_count` | Keys in both with at least one value difference |
| `unchanged_key_count` | Keys in both with all values identical |
| `changed_cell_count` | Total individual cell changes across all changed rows |
| `added_key_pct` | `added_key_count / new_row_count` |
| `removed_key_pct` | `removed_key_count / old_row_count` |
| `changed_key_pct` | `changed_key_count / matched_key_count` |
| `changed_column_counts` | List — one entry per changed column (see below) |

### `changed_column_counts` entry

```python
{
    "column":           "Generic Queue Status",
    "changed_key_count": 3,     # rows where this column changed
    "null_to_value":     1,     # was null, now has value
    "value_to_null":     0,     # had value, now null
    "value_changed":     2,     # both non-null, values differ
}
```

Only `value_changed > 0` warrants a `coerce_check`. The other two are genuine data transitions.

### `samples` sub-keys

```python
ctx["samples"]["added_rows"]    # list[dict] — full row dicts for added keys
ctx["samples"]["removed_rows"]  # list[dict] — full row dicts for removed keys
ctx["samples"]["changed_rows"]  # list[dict] — each row has before/after per changed column
```

`changed_rows` example entry:
```python
{
    "Application ID": "27INR0557",
    "_changed_columns": ["Generic Queue Status", "Interconnection Size (MW)"],
    "Generic Queue Status__before": "Scoping",
    "Generic Queue Status__after":  "Facilities Study",
    "Interconnection Size (MW)__before": 50.0,
    "Interconnection Size (MW)__after":  1336.52,
}
```

---

## Common Patterns

### Threshold decision from metrics
```python
ctx = anchor("diff", previous_df, current_df, keys=["id"], output_format="dict")
m = ctx["metrics"]

if ctx["status"] == "error":
    raise ValueError(f"Anomalous diff — {m['removed_key_pct']:.1%} rows removed")

if m["changed_key_count"] > 0:
    changed_cols = [c["column"] for c in m["changed_column_counts"]
                    if c["value_changed"] > 0]
    coerce_ctx = anchor("coerce_check", previous_df, current_df,
                    keys=["id"], columns=changed_cols)
```

### Apply the generated MERGE SQL
```python
ctx = anchor("diff", old_df, new_df, keys=["id"], output_format="dict")
if ctx["status"] == "ok" and ctx["merge_expr"]:
    spark.sql(ctx["merge_expr"])
```

### Inspect a specific changed column
```python
changed = [r for r in ctx["samples"]["changed_rows"]
           if "Generic Queue Status" in r.get("_changed_columns", [])]
for row in changed:
    print(row["Application ID"],
          row["Generic Queue Status__before"], "→",
          row["Generic Queue Status__after"])
```

---

## Edge Cases

| Scenario | Behavior |
|---|---|
| Keys not unique in old_df | `ValueError` — keys must define the grain |
| Column in `compare_columns` missing from one DataFrame | That column is silently skipped |
| Both DataFrames identical | `changed=0, added=0, removed=0` — `merge_expr` is empty string |
| NaN vs NaN | Treated as equal (unchanged) — matches SQL NULL semantics |
| `status="error"` | Occurs when added or removed % exceeds safety threshold; `merge_expr` is absent |
| Large DataFrames with `sample_limit=20` | Only 20 example rows per category — full counts still accurate in `metrics` |

---

## Verification Workflow

After applying diff-driven changes, verify with a second diff:

```python
# Apply MERGE
spark.sql(ctx["merge_expr"])

# Reload and re-diff
updated_df = spark.table("target_table").toPandas()
verify_ctx = anchor("diff", current_df, updated_df, keys=["id"])
m2 = verify_ctx["metrics"]

assert m2["changed_key_count"] == 0, f"Still {m2['changed_key_count']} changed rows after MERGE"
assert m2["added_key_count"]   == 0
assert m2["removed_key_count"] == 0
```
