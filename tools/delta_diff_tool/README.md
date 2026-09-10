# Delta Diff

## Documentation

* [Architecture](docs/architecture.md) — System overview, Delta time travel, NULL-aware comparison
* [Usage](docs/usage.md) — Full parameter reference, version selection, result structure
* [Code Walkthrough](docs/code_walkthrough.md) — Worked examples: standard diff, coerce_check integration

> Compare two versions of a Delta table at business-key level — shows added, removed, and changed rows with NULL-aware column breakdown and Delta history metadata.

---

## When to Use

- A pipeline ran and you suspect it changed the wrong rows
- You want to know exactly which rows and columns changed between two versions
- You need to audit who changed what and when (operations + user logged in output)
- You want to verify that a "fix" changed only the intended rows and nothing else

> **Requires Spark.** `delta_diff` reads Delta version history — it cannot work on inline DataFrames or non-Delta tables. Call it with a fully qualified table name (`catalog.schema.table`).

---

## Quick Start

```python
import sys
sys.path.insert(0, "/Workspace/Users/user@example.com/odibi_anchor/src")
from odibi_anchor.bootstrap import init
anchor, ROOT, MANIFEST = init()

# Compare current version vs. 1 version ago (default)
ctx = anchor("delta_diff",
         "analytics_dev.data_engineering_reference.guide_utility",
         keys=["utility_raw"])
```

Replace `analytics_dev.data_engineering_reference.guide_utility` with your table.

---

## Usage

```python
# Compare current vs. N versions ago
ctx = anchor("delta_diff", "catalog.schema.my_table", keys=["order_id"], versions_ago=3)

# Compare specific versions
ctx = anchor("delta_diff", "catalog.schema.my_table", keys=["order_id"],
         old_version=4, new_version=5)

# Compare only specific columns
ctx = anchor("delta_diff", "catalog.schema.my_table", keys=["order_id"],
         compare_columns=["status", "amount", "last_updated"])

# Include coerce_check on changed columns (catches whitespace/case diffs)
ctx = anchor("delta_diff", "catalog.schema.my_table", keys=["order_id"],
         include_coerce_check=True)
```

---

## Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `table` | str | Yes | Fully qualified Delta table (`catalog.schema.table`) |
| `keys` | list[str] | Yes | Business key columns for row-level matching |
| `old_version` | int or str | No | Specific version number or timestamp string for old snapshot |
| `new_version` | int or str | No | Specific version (default: current/latest) |
| `versions_ago` | int | No | If `old_version` not set, compare current vs N versions ago (default 1) |
| `compare_columns` | list[str] | No | Limit diff to specific columns (compares all by default) |
| `include_coerce_check` | bool | No | Run coerce_check on changed columns automatically (default False) |
| `output_format` | str | No | `"dict"` (default) or `"markdown"` |
| `sample_limit` | int | No | Max sample rows per change category (default 20) |

---

## Output

```python
ctx["summary"]
# "Version 4→5 (UPDATE by user@example.com at 2026-06-03 14:55:39): 0 added, 0 removed, 1 changed, 377 unchanged"

ctx["metrics"]["added_count"]     # Rows in new version that weren't in old
ctx["metrics"]["removed_count"]   # Rows in old version not in new
ctx["metrics"]["changed_count"]   # Rows present in both with at least one column change
ctx["metrics"]["unchanged_count"] # Rows that are identical in both versions
ctx["metrics"]["changed_column_counts"]  # Dict of column → number of changed rows
ctx["metrics"]["old_operation"]   # Delta operation that wrote the old version
ctx["metrics"]["new_operation"]   # Delta operation that wrote the new version
ctx["metrics"]["new_user"]        # User who wrote the new version

ctx["findings"]    # Human-readable list of key changes
ctx["risks"]       # Unexpected patterns worth investigating
ctx["samples"]     # Sample rows for each change category
ctx["suggested_next_actions"]  # Next anchor() calls
```

---

## Reading the Output

The four change categories are **mutually exclusive** and **exhaustive**:

| Category | Meaning |
|---|---|
| `added_count` | Keys exist in new but not in old — new rows |
| `removed_count` | Keys exist in old but not in new — deleted rows |
| `changed_count` | Keys exist in both but ≥1 column value changed |
| `unchanged_count` | Keys exist in both and all compared columns are identical |

`added + removed + changed + unchanged = total unique keys across both versions`

Check `changed_column_counts` to see which columns are driving the most changes — a single pipeline bug often shows up as one column with unexpectedly many changes.

---

## Pairs Well With

- `anchor("coerce_check", old_df, new_df, keys=[...])` — dig into `changed` rows to find whitespace/case/type differences (or pass `include_coerce_check=True` to delta_diff directly)
- `anchor("schema_migrate", ...)` — when `schema_changed=True` in the output
- `anchor("watermark", ...)` — verify no late-arriving records were missed between versions
- `anchor("explain_row", ...)` — trace the exact origin of a specific changed row

---

## Direct Import (no anchor() dispatcher needed)

```python
import sys
sys.path.append("/Workspace/Users/user@example.com/odibi_anchor/src")
sys.path.insert(0, "/Workspace/Users/user@example.com/odibi_anchor/src")

from tools.delta_diff_tool.delta_diff_impl import delta_diff_context

# Requires an active SparkSession — runs on Databricks only
ctx = delta_diff_context(
    "analytics_dev.data_engineering_reference.guide_utility",
    keys=["utility_raw"],
    old_version=4,
    new_version=5,
)

ctx["summary"]
# "Version 4→5 (UPDATE by user@example.com at 2026-06-03 14:55:39): 0 added, 0 removed, 1 changed, 377 unchanged"

ctx["metrics"]["changed_count"]
# 1

ctx["findings"][0]
# "1 rows changed across 1 column(s) (utility_translated)"
```

> **Note:** This tool imports from `odibi_anchor._utils` and requires an active Spark session. Both `src/` and root paths are required.
