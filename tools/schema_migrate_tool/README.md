# Schema Migrate

## Documentation

* [Architecture](docs/architecture.md) — Pipeline overview, _SAFE_CASTS matrix, rename detection
* [Usage](docs/usage.md) — Full parameter reference, migration_plan format, result structure
* [Code Walkthrough](docs/code_walkthrough.md) — Worked examples: ADD COLUMN, rename detection, unsafe cast

> Generate executable DDL (`ALTER TABLE`) and PySpark migration code from a `schema_diff` — tells you exactly how to evolve a target table to match a new schema.

---

## When to Use

- A source schema changed (columns added, removed, or retyped) and you need to update the target table
- You want safe, auditable DDL before running it — `dry_run=True` by default
- You want PySpark alternatives to DDL for columns that can't be added in SQL
- You suspect a column was renamed and want the tool to infer it rather than detecting as add+remove

---

## Quick Start

```python
import sys
sys.path.insert(0, "/Workspace/Users/user@example.com/odibi_anchor/src")
from odibi_anchor.bootstrap import init
anchor, ROOT, MANIFEST = init()

import pandas as pd

# Old schema (existing data)
old_df = pd.DataFrame({"order_id": [1, 2], "amount": [10., 20.], "status": ["a", "b"]})
# New schema (incoming data — "status" removed, "region" added)
new_df = pd.DataFrame({"order_id": [1, 2], "amount": [10., 20.], "region": ["N", "S"]})

# Step 1: detect schema differences
schema_diff = anchor("schema_diff", old_df, new_df)

# Step 2: generate migration code
ctx = anchor("schema_migrate", schema_diff, "catalog.schema.orders")
```

---

## Usage

```python
# Standard — dry run (default, no changes applied)
diff = anchor("schema_diff", old_df, new_df)
ctx = anchor("schema_migrate", diff, "catalog.schema.my_table", dry_run=True)

# Include DROP COLUMN statements (commented-out warnings by default)
ctx = anchor("schema_migrate", diff, "catalog.schema.my_table", drop_columns=True)

# Adjust rename detection sensitivity (0.0–1.0, default 0.8)
ctx = anchor("schema_migrate", diff, "catalog.schema.my_table", rename_threshold=0.6)
```

---

## Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `schema_diff_ctx` | dict | Yes | Output from `anchor("schema_diff")` |
| `target_table` | str | Yes | Fully qualified table name for DDL (`catalog.schema.table`) |
| `drop_columns` | bool | No | If True, generates DROP COLUMN statements (default False — emits warnings only) |
| `dry_run` | bool | No | If True (default), generates code but does NOT execute it |
| `rename_threshold` | float | No | Similarity threshold to detect renames (default 0.8) |
| `output_format` | str | No | `"dict"` (default) or `"markdown"` |

---

## Output

```python
ctx["summary"]
# "Migration plan: 1 ADD COLUMN. Dry run — no changes applied."

ctx["migration_plan"]  # Full plan dict with DDL + PySpark for each operation

# ADD COLUMN
ctx["migration_plan"]["add_columns"][0]
# {"column": "region", "type": "STRING", "default": "NULL",
#  "ddl": "ALTER TABLE catalog.schema.orders ADD COLUMN `region` STRING;",
#  "pyspark": "# Column 'region' (STRING) — add via DDL above or schema evolution"}

# REMOVED COLUMN (warning by default)
ctx["migration_plan"]["drops"][0]
# {"column": "status", "action": "warning_only",
#  "ddl": "-- WARNING: Column 'status' was removed. Uncomment to drop:
#          -- ALTER TABLE catalog.schema.orders DROP COLUMN `status`;"}

ctx["metrics"]["columns_added"]    # 1
ctx["metrics"]["columns_removed"]  # 1
ctx["metrics"]["dry_run"]         # True
```

---

## Reading the Output

- DDL statements are ready to copy into a notebook cell — review them before running
- `drops` entries are commented-out by default — you must explicitly pass `drop_columns=True` to generate executable DROP statements
- `likely_renames` in the schema_diff output (upstream) are resolved here as RENAME instead of add+remove pairs
- Check `risks` in the output before applying — it flags unsafe type casts (e.g., BIGINT → INT)

**Typical apply workflow:**
```python
diff = anchor("schema_diff", old_df, new_df)
ctx = anchor("schema_migrate", diff, "catalog.schema.my_table", dry_run=True)
# Review ctx["migration_plan"] and ctx["risks"]
# Then run the DDL manually in a SQL cell
```

---

## Pairs Well With

- `anchor("schema_diff", old_df, new_df)` — always run this first to detect the differences
- `anchor("delta_diff", "catalog.schema.my_table", keys=[...])` — verify no row-level changes after DDL
- `anchor("pre_merge", source_df, target, keys=[...])` — re-check merge safety after schema evolution

---

## Direct Import (no anchor() dispatcher needed)

```python
import sys
import pandas as pd
sys.path.append("/Workspace/Users/user@example.com/odibi_anchor/src")
sys.path.insert(0, "/Workspace/Users/user@example.com/odibi_anchor/src")

from tools.schema_migrate_tool.schema_migrate_impl import schema_migrate_context

# Step 1 must go via anchor("schema_diff") — schema_diff is a core tool, not a standalone file
old_df = pd.DataFrame({"order_id": [1, 2], "amount": [10., 20.], "status": ["a", "b"]})
new_df = pd.DataFrame({"order_id": [1, 2], "amount": [10., 20.], "region": ["N", "S"]})
schema_diff = anchor("schema_diff", old_df, new_df, output_format="dict")

ctx = schema_migrate_context(schema_diff, "catalog.schema.orders", dry_run=True)

ctx["summary"]
# "Migration plan: 1 ADD COLUMN. Dry run — no changes applied."

ctx["migration_plan"]["add_columns"][0]["ddl"]
# "ALTER TABLE catalog.schema.orders ADD COLUMN `region` STRING;"
```

> **Note:** This tool imports from `odibi_anchor._utils`. The `src/` path is required.
