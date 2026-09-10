# schema_diff

> Compare the schemas of two DataFrames or Unity Catalog tables — surfaces added columns, removed columns, type changes, and whether the change is breaking.

---

## Documentation

* [Architecture](schema_diff_architecture.md) — Field comparison model, breaking change detection, design decisions
* [Usage](schema_diff_usage.md) — Full parameter reference, output contract, table name strings
* [Code Walkthrough](schema_diff_walkthrough.md) — Worked examples with field-level traces

---

## When to Use

- Before running `anchor("diff")` to confirm schemas are compatible
- A pipeline failed with a column-not-found or type-mismatch error and you need to see what changed
- You want to know if a new table version introduced breaking changes before updating downstream logic
- Comparing a DataFrame in memory against a Unity Catalog table schema

**Anti-pattern:** Don't use `schema_diff` when you want row-level changes — use `anchor("diff")` for that. Don't write manual `df.dtypes` comparisons — `schema_diff` gives you added/removed/type-changed columns and a `is_breaking_change` flag in one call.

---

## Quick Start

```python
import pandas as pd

old_df = pd.DataFrame({"id": [1], "amount": [10.0]})
new_df = pd.DataFrame({"id": [1], "status": ["new"]})  # "amount" removed, "status" added

ctx = anchor("schema_diff", old_df, new_df)
ctx["metrics"]["is_breaking_change"]  # True — "amount" was removed
```

### Compare Unity Catalog tables directly
```python
ctx = anchor("schema_diff",
         "catalog.schema.table_v1",
         "catalog.schema.table_v2",
         spark=spark)
```

---

## Usage

### Basic DataFrame comparison
```python
ctx = anchor("schema_diff", old_df, new_df)
```

### With labels
```python
ctx = anchor("schema_diff", old_df, new_df,
         old_subject="queue_2026_06_04",
         new_subject="queue_2026_06_11")
```

### Compare Unity Catalog tables
```python
ctx = anchor("schema_diff",
         "analytics_dev.data_engineering.gold_v1",
         "analytics_dev.data_engineering.gold_v2",
         spark=spark)
```

### Markdown report
```python
report = anchor("schema_diff", old_df, new_df, output_format="markdown")
```

---

## Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `old_df` | DataFrame or str | Yes | — | Reference schema. Pandas/Spark DataFrame or `"catalog.schema.table"` |
| `new_df` | DataFrame or str | Yes | — | Current schema. Same types as `old_df` |
| `old_subject` | str | No | `"old_df"` | Label for the reference in output |
| `new_subject` | str | No | `"new_df"` | Label for the current in output |
| `subject` | str | No | auto | Overrides both subject labels |
| `engine` | str | No | `"auto"` | `"auto"`, `"pandas"`, or `"spark"` |
| `include_unchanged` | bool | No | `True` | Include unchanged columns in output |
| `max_unchanged` | int | No | `50` | Cap on unchanged columns in payload |
| `output_format` | str | No | `"dict"` | `"dict"` or `"markdown"` |
| `spark` | SparkSession | No | `None` | Required when passing table name strings |

---

## Output

### Key metrics fields

```python
ctx["metrics"]["is_breaking_change"]  # True if any column removed or type changed
ctx["metrics"]["added_count"]         # columns in new_df only
ctx["metrics"]["removed_count"]       # columns in old_df only
ctx["metrics"]["type_changed_count"]  # columns present in both but with different dtype
ctx["metrics"]["unchanged_count"]     # columns identical in both
```

### Change lists

```python
ctx["added"]        # [{"column": "status", "dtype": "object", "position": 3}]
ctx["removed"]      # [{"column": "amount", "dtype": "float64", "position": 2}]
ctx["changed_type"] # [{"column": "id", "old_dtype": "int64", "new_dtype": "object"}]
ctx["unchanged"]    # [...] — up to max_unchanged entries
```

---

## Reading the Output

- `is_breaking_change=True` means `diff` will fail or produce wrong results — resolve schema issues before running a row-level diff
- Removed columns are always breaking (downstream queries will fail)
- Type changes may or may not be breaking depending on whether the cast is widening (int→float, safe) or narrowing (float→int, lossy)
- Added columns are non-breaking for existing downstream logic but should be documented

---

## Pairs Well With

- `anchor("diff", old_df, new_df, keys=[...])` — run after `schema_diff` confirms schemas are compatible
- `anchor("transform", profile_ctx)` — if `schema_diff` reveals type mismatches, `transform` can generate the cast steps

---

## Direct Import (no anchor() dispatcher)

```python
import sys
import pandas as pd
sys.path.append("/Workspace/Users/user@example.com/odibi_anchor/src")

from odibi_anchor.tables.schema_diff_context import schema_diff_context

old_df = pd.DataFrame({"id": [1], "amount": [10.0]})
new_df = pd.DataFrame({"id": [1], "status": ["new"]})

ctx = schema_diff_context(old_df, new_df, old_subject="v1", new_subject="v2")
print(ctx["metrics"]["is_breaking_change"])  # True
print(ctx["removed"])  # [{"column": "amount", "dtype": "float64", "position": 1}]
```
