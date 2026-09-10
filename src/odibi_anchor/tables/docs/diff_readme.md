# diff

> Compare two DataFrames by business key — surfaces added rows, removed rows, changed values, and generates a ready-to-paste MERGE INTO statement.

---

## Documentation

* [Architecture](diff_architecture.md) — Pipeline overview, change categories, design decisions
* [Usage](diff_usage.md) — Full parameter reference, output contract, edge cases
* [Code Walkthrough](diff_ops_walkthrough.md) — Worked examples with step-by-step execution traces

---

## When to Use

- You have two snapshots of the same table (today vs yesterday, source vs CRM, staging vs prod) and need to know what changed
- A pipeline loaded new data and you want to verify what was added, updated, or removed before writing downstream
- You need a MERGE INTO statement scoped to only the rows and columns that actually changed
- You ran `anchor("schema_diff")` and schemas match — now you want the row-level story

**Anti-pattern:** Don't use `diff` when you only want to compare schemas — use `anchor("schema_diff")` for that. Don't write manual `df.merge()` loops — `diff` gives you structured output, per-column change counts, and MERGE SQL in one call.

---

## Quick Start

```python
import pandas as pd

old_df = pd.DataFrame({"id": [1, 2, 3], "status": ["new", "active", "pending"]})
new_df = pd.DataFrame({"id": [1, 2, 4], "status": ["new", "retired", "new"]})

ctx = anchor("diff", old_df, new_df, keys=["id"])
# id=3 removed, id=4 added, id=2 status changed
```

---

## Usage

### Basic diff
```python
ctx = anchor("diff", old_df, new_df, keys=["id"])
```

### Markdown report
```python
report = anchor("diff", old_df, new_df, keys=["id"], output_format="markdown")
```

### Compare specific columns only
```python
ctx = anchor("diff", old_df, new_df, keys=["id"], compare_columns=["status", "amount"])
```

### Label the diff subject
```python
ctx = anchor("diff", yesterday_df, today_df, keys=["Application ID"],
         subject="gold_new: 2026-06-04 vs 2026-06-11")
```

### Weekly queue pull comparison (real-world pattern)
```python
cols = ["Application ID", "Interconnection Size (MW)", "Generic Queue Status"]
previous_df = spark.table("analytics_dev.data_engineering.queue_2026_06_04").select(*cols).toPandas()
current_df  = spark.table("analytics_dev.data_engineering.queue_2026_06_11").select(*cols).toPandas()

ctx = anchor("diff", previous_df, current_df, keys=["Application ID"])
```

---

## Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `old_df` | DataFrame | Yes | — | Previous/reference snapshot |
| `new_df` | DataFrame | Yes | — | Current/target snapshot |
| `keys` | list[str] | Yes | — | Business key columns — define the grain |
| `compare_columns` | list[str] | No | all common non-key cols | Columns to compare. Omit to compare everything |
| `subject` | str | No | `"old_df vs new_df"` | Display label in output |
| `engine` | str | No | `"auto"` | `"auto"`, `"pandas"`, or `"spark"` |
| `sample_limit` | int | No | `20` | Max rows per sample section (added/removed/changed) |
| `output_format` | str | No | `"dict"` | `"dict"` or `"markdown"` |

> Both `old_df` and `new_df` must use the same engine. Mixed pandas/Spark raises `ValueError`.

---

## Output

### Key metrics fields

| Field | What it tells you |
|---|---|
| `ctx["metrics"]["added_key_count"]` | Rows in new_df but not old_df |
| `ctx["metrics"]["removed_key_count"]` | Rows in old_df but not new_df |
| `ctx["metrics"]["changed_key_count"]` | Rows present in both but with value differences |
| `ctx["metrics"]["unchanged_key_count"]` | Rows identical in both |
| `ctx["metrics"]["added_key_pct"]` | Added as % of new_df row count |
| `ctx["metrics"]["changed_column_counts"]` | Per-column: keys changed, null→value, value→null, value≠value |
| `ctx["status"]` | `"ok"` (changes within normal range) or `"error"` (anomalous) |
| `ctx["merge_expr"]` | Ready-to-paste MERGE INTO SQL (only when status is `"ok"`) |

### Sample rows

```python
ctx["samples"]["added_rows"]    # rows that appear only in new_df
ctx["samples"]["removed_rows"]  # rows that appear only in old_df
ctx["samples"]["changed_rows"]  # rows present in both with diffs (includes before/after)
```

### Action items

```python
ctx["findings"]               # what the diff discovered
ctx["risks"]                  # anomalies worth investigating
ctx["suggested_next_actions"] # e.g. "run coerce_check on Generic Queue Status"
```

---

## Reading the Output

- `changed_column_counts` breaks each changed column into three sub-types: `null_to_value` (was null, now has data), `value_to_null` (had data, now null), and `value_changed` (both had values but they differ). Only `value_changed > 0` warrants a `coerce_check` — the other two are genuine data changes.
- `status="error"` fires when added or removed row % exceeds a safety threshold — this protects against bad source files silently wiping your table.
- `merge_expr` is scoped to only the columns that actually changed — not a full INSERT/UPDATE for every column.

**Change sub-type guide:**

| Sub-type | Meaning | What to do |
|---|---|---|
| `null_to_value` | Field was empty, now populated | Normal backfill — verify intentional |
| `value_to_null` | Field had data, now empty | Potential data loss — investigate |
| `value_changed` | Both had values, they differ | Run `coerce_check` to classify root cause |

---

## Pairs Well With

- `anchor("schema_diff", old_df, new_df)` — run first when you suspect column additions or type changes
- `anchor("coerce_check", old_df, new_df, keys=[...], columns=[...])` — run after diff on columns with `value_changed > 0`
- `anchor("case_file", df, column="...", filter="where:...")` — drill into specific changed rows

---

## Direct Import (no anchor() dispatcher)

```python
import sys
import pandas as pd
sys.path.append("/Workspace/Users/user@example.com/odibi_anchor/src")

from odibi_anchor.tables.diff_ops import diff_tables_by_key, render_diff_report

old_df = pd.DataFrame({"id": [1, 2], "status": ["new", "active"]})
new_df = pd.DataFrame({"id": [1, 2, 3], "status": ["new", "retired", "new"]})

ctx = diff_tables_by_key(old_df, new_df, keys=["id"])
print(ctx["summary"])
# "1 added, 0 removed, 1 changed, 1 unchanged"

print(ctx["merge_expr"])
# MERGE INTO target USING source ON ...
```
