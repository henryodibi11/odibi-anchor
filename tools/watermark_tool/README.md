# Watermark

## Documentation

* [Architecture](docs/architecture.md) — System overview, staleness classification, dual-engine support
* [Usage](docs/usage.md) — Full parameter reference, staleness interpretation
* [Code Walkthrough](docs/code_walkthrough.md) — Worked examples: stale pipeline, NULL watermark detection

> Diagnose incremental load staleness by comparing the watermark (timestamp or datetime) column between a source and target table — tells you how far behind your pipeline is and how many rows are pending.

---

## When to Use

- A pipeline appears to be running but target data looks stale
- You want to verify that an incremental load picked up all new source rows
- You need to measure lag between a source system and a downstream table
- You want to know the typical load cadence and detect gaps

---

## Quick Start

```python
import sys
sys.path.insert(0, "/Workspace/Users/user@example.com/odibi_anchor/src")
from odibi_anchor.bootstrap import init
anchor, ROOT, MANIFEST = init()

import pandas as pd

# Source has 5 rows through Jan 14; target only loaded through Jan 12 — 2 days behind
source = pd.DataFrame({
    "order_id":   [1, 2, 3, 4, 5],
    "event_time": pd.to_datetime(["2024-01-10", "2024-01-11", "2024-01-12",
                                  "2024-01-13", "2024-01-14"]),
    "amount":     [100., 200., 75., 50., 150.],
})
target = pd.DataFrame({
    "order_id":   [1, 2, 3],
    "event_time": pd.to_datetime(["2024-01-10", "2024-01-11", "2024-01-12"]),
    "amount":     [100., 200., 75.],
})

ctx = anchor("watermark", source, target, watermark_col="event_time", subject="orders_pipeline")
```

Pass table names instead of DataFrames when working with real pipeline tables:
```python
ctx = anchor("watermark",
         "catalog.schema.source_orders",
         "catalog.schema.target_orders",
         watermark_col="event_time")
```

> **Important:** The watermark column must have a datetime dtype (`pd.to_datetime()` or Spark `TimestampType`). Raw string columns will cause a TypeError.

---

## Usage

```python
# Same watermark column name in both tables
ctx = anchor("watermark", source, target, watermark_col="event_time")

# Different column names in source and target
ctx = anchor("watermark", source, target,
         source_watermark_col="created_at",
         target_watermark_col="loaded_at")

# Change lookback window (default 30 days)
ctx = anchor("watermark", source, target, watermark_col="updated_ts", lookback_days=7)
```

---

## Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `source` | str or DataFrame | Yes | Source table name or DataFrame |
| `target` | str or DataFrame | Yes | Target table name or DataFrame |
| `watermark_col` | str | One of wm_col or src/tgt cols | Watermark column (same name in both tables) |
| `source_watermark_col` | str | Alt to watermark_col | Watermark column name in source (when names differ) |
| `target_watermark_col` | str | Alt to watermark_col | Watermark column name in target (when names differ) |
| `lookback_days` | int | No | Window for cadence and gap analysis (default 30) |
| `subject` | str | No | Display name for the pipeline |
| `output_format` | str | No | `"dict"` (default) or `"markdown"` |

---

## Output

```python
ctx["summary"]
# "STALE: target watermark is 2024-01-12 00:00:00, source has 2 rows through 2024-01-14. 2.0-day lag."

ctx["metrics"]["staleness"]           # "stale" | "current" | "unknown"
ctx["metrics"]["lag_hours"]           # 48.0
ctx["metrics"]["lag_days"]            # 2.0
ctx["metrics"]["pending_count"]       # 2 — rows in source newer than target watermark
ctx["metrics"]["source_watermark"]    # "2024-01-14 00:00:00"
ctx["metrics"]["target_watermark"]    # "2024-01-12 00:00:00"
ctx["metrics"]["typical_cadence"]     # "daily" | "hourly" | "weekly" | etc.
ctx["metrics"]["gaps"]                # List of detected time gaps (missing load windows)

ctx["findings"]     # Human-readable lag description and pending row count
ctx["risks"]        # Warnings about unusual lag or null watermarks
ctx["suggested_next_actions"]  # Next anchor() calls
```

---

## Reading the Output

- `staleness = "stale"` means target is behind source — look at `pending_count` for severity
- `gaps` in `metrics["gaps"]` indicates missing load windows (the pipeline skipped certain time ranges)
- If `type_compatible = False`, the watermark columns have incompatible dtypes — fix the column dtype before comparing

---

## Pairs Well With

- `anchor("delta_diff", target_table, keys=[...])` — after a catch-up load, verify the right rows were written
- `anchor("diagnose_empty", result_df, ...)` — if pending rows appear to land but result is still empty
- `anchor("pre_merge", source_df, target, keys=[...])` — validate source before running the incremental merge

---

## Direct Import (no anchor() dispatcher needed)

```python
import sys
import pandas as pd
sys.path.append("/Workspace/Users/user@example.com/odibi_anchor/src")
sys.path.insert(0, "/Workspace/Users/user@example.com/odibi_anchor/src")

from tools.watermark_tool.watermark_impl import watermark_debug_context

source = pd.DataFrame({
    "order_id":   [1, 2, 3, 4, 5],
    "event_time": pd.to_datetime(["2024-01-10", "2024-01-11", "2024-01-12",
                                  "2024-01-13", "2024-01-14"]),
    "amount":     [100., 200., 75., 50., 150.],
})
target = pd.DataFrame({
    "order_id":   [1, 2, 3],
    "event_time": pd.to_datetime(["2024-01-10", "2024-01-11", "2024-01-12"]),
    "amount":     [100., 200., 75.],
})

ctx = watermark_debug_context(source, target, watermark_col="event_time",
                              subject="orders_pipeline")

ctx["summary"]
# "STALE: target watermark is 2024-01-12 00:00:00, source has 2 rows through 2024-01-14. 2.0-day lag."

ctx["metrics"]["pending_count"]
# 2

ctx["metrics"]["staleness"]
# "stale"
```

> **Note:** This tool imports from `odibi_anchor._utils`. The `src/` path is required.
