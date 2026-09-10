# Pre-Merge

> Validate a source DataFrame is safe to `MERGE INTO` a Delta target — catches duplicate keys, null merge keys, schema incompatibilities, and unexpected row volumes before you write a single row.

---

## When to Use

- You're about to run a `MERGE INTO` and want to verify the source is clean
- A previous merge produced duplicates or missed rows and you want to diagnose why
- You want to know ahead of time how many rows will be inserted vs. updated
- You need to confirm schema compatibility between source and target before writing

---

## Quick Start

```python
import sys
sys.path.insert(0, "/Workspace/Users/user@example.com/odibi_anchor/src")
from odibi_anchor.bootstrap import init
anchor, ROOT, MANIFEST = init()

import pandas as pd

# Source data — order_id=2 appears twice (will block the merge)
source = pd.DataFrame({
    "order_id": [1, 2, 3, 2],
    "status":   ["shipped", "pending", "pending", "shipped"],
    "amount":   [100., 200., 75., 200.],
})
target = pd.DataFrame({
    "order_id": [1, 2, 4],
    "status":   ["processing", "pending", "delivered"],
    "amount":   [100., 180., 50.],
})

ctx = anchor("pre_merge", source, target, keys=["order_id"])
# BLOCKED: MERGE unsafe — 1 duplicate key(s)
```

Pass a Delta table name instead of a DataFrame when checking against a real target:
```python
ctx = anchor("pre_merge", source_df, "catalog.schema.orders", keys=["order_id"])
```

---

## Usage

```python
# Against a DataFrame target
ctx = anchor("pre_merge", source_df, target_df, keys=["order_id"])

# Against a Delta table (loads via spark.table())
ctx = anchor("pre_merge", source_df, "catalog.schema.my_table", keys=["order_id"])

# Composite merge keys
ctx = anchor("pre_merge", source_df, target_df, keys=["order_id", "line_item"])
```

---

## Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `source_df` | DataFrame | Yes | The source data to merge |
| `target` | str or DataFrame | Yes | Delta table name or a target DataFrame |
| `keys` | list[str] | Yes | Merge key column(s) |
| `source_subject` | str | No | Display label for source (default `"source"`) |
| `target_subject` | str | No | Display label for target — overridden by table name if target is a string |
| `output_format` | str | No | `"dict"` (default) or `"markdown"` |
| `sample_limit` | int | No | Max duplicate rows to show in samples (default 10) |

> Works with both pandas and Spark DataFrames. Pass a string to `target` to load a Delta table via Spark.

---

## Output

```python
ctx["summary"]
# "BLOCKED: MERGE unsafe — 1 duplicate key(s) — schema compatible — MIXED merge (1 inserts, 2 updates)"

ctx["metrics"]["source_duplicate_key_count"]  # 1 — BLOCKS the merge
ctx["metrics"]["source_null_key_count"]       # 0
ctx["metrics"]["insert_count"]                # 1 (keys only in source)
ctx["metrics"]["update_count"]                # 2 (keys in both source and target)
ctx["metrics"]["merge_behavior"]              # "MIXED" | "INSERT_ONLY" | "UPDATE_ONLY"
ctx["metrics"]["schema_compatible"]           # True

ctx["findings"]   # What was found (informational)
ctx["risks"]      # What will block or corrupt the merge
ctx["samples"]    # Sample duplicate key rows
ctx["suggested_next_actions"]  # Next steps
```

---

## Reading the Output

The summary tells you the merge result in one line — look for `BLOCKED` vs `SAFE`:

| Summary prefix | Meaning |
|---|---|
| `BLOCKED` | Merge will produce incorrect results — fix the source first |
| `SAFE` | Merge is ready to run |

The most common `BLOCKED` reasons:

| Cause | Metric key | Fix |
|---|---|---|
| Duplicate merge keys | `source_duplicate_key_count > 0` | Deduplicate source before merging |
| Null merge keys | `source_null_key_count > 0` | Drop or fill null key rows |
| Schema mismatch | `schema_compatible = False` | Run `anchor("schema_migrate")` first |

---

## Pairs Well With

- `anchor("coerce_fix", source_df, coerce_ctx)` — fix case/whitespace issues before the merge
- `anchor("schema_migrate", ...)` — evolve target schema if `schema_compatible = False`
- `anchor("delta_diff", "catalog.schema.target", keys=[...])` — verify the merge produced expected changes afterward

---

## Direct Import (no anchor() dispatcher needed)

```python
import sys
import pandas as pd
sys.path.append("/Workspace/Users/user@example.com/odibi_anchor/src")
sys.path.insert(0, "/Workspace/Users/user@example.com/odibi_anchor/src")

from tools.pre_merge_tool.pre_merge_impl import pre_merge_context

source = pd.DataFrame({
    "order_id": [1, 2, 3, 2],
    "status":   ["shipped", "pending", "pending", "shipped"],
    "amount":   [100., 200., 75., 200.],
})
target = pd.DataFrame({
    "order_id": [1, 2, 4],
    "status":   ["processing", "pending", "delivered"],
    "amount":   [100., 180., 50.],
})

ctx = pre_merge_context(source, target, keys=["order_id"])

ctx["summary"]
# "BLOCKED: MERGE unsafe — 1 duplicate key(s) — schema compatible — MIXED merge (1 inserts, 2 updates)"

ctx["metrics"]["source_duplicate_key_count"]
# 1
```

> **Note:** This tool imports from `odibi_anchor._utils`. The `src/` path is required.

---

## Documentation

Detailed documentation is available in the `docs/` folder:

- [Architecture](docs/architecture.md) — Pipeline design, type compatibility matrix, merge behavior classification, and output contract
- [Usage Guide](docs/usage.md) — Full parameter reference, target resolution, result structure, and common patterns
- [Code Walkthrough](docs/code_walkthrough.md) — 3 worked examples: clean INSERT-only, BLOCKED (dupes + type mismatch), and WARNING (null keys + large batch)
