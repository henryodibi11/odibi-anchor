# Partition Check

## Documentation

* [Architecture](docs/architecture.md) — System overview, metrics, speedup estimation
* [Usage](docs/usage.md) — Full parameter reference, result interpretation
* [Code Walkthrough](docs/code_walkthrough.md) — Worked examples: small file detection, partition skew

> Diagnose small files, partition skew, and file layout issues on a Delta table — tells you whether OPTIMIZE is needed and estimates the query speedup you'd get.

---

## When to Use

- Queries on a Delta table are unexpectedly slow and you suspect small files
- A table was built with many small appends and has never been OPTIMIZE'd
- You want to confirm partition layout is balanced before adding more data
- You're tuning a table for production performance

> **Requires Spark.** `partition_check` reads file-level metadata from Delta (`DESCRIBE DETAIL`) — it cannot work on inline DataFrames. Call it with a fully qualified table name.

---

## Quick Start

```python
import sys
sys.path.insert(0, "/Workspace/Users/user@example.com/odibi_anchor/src")
from odibi_anchor.bootstrap import init
anchor, ROOT, MANIFEST = init()

ctx = anchor("partition_check", "analytics_dev.data_engineering_reference.guide_utility")
```

Replace `analytics_dev.data_engineering_reference.guide_utility` with your table.

---

## Usage

```python
# Default: 128MB small-file threshold, 10x skew ratio
ctx = anchor("partition_check", "catalog.schema.my_table")

# Custom thresholds
ctx = anchor("partition_check", "catalog.schema.my_table",
         small_file_threshold_mb=256,
         skew_ratio_threshold=5)
```

---

## Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `table` | str | Yes | Fully qualified Delta table name (`catalog.schema.table`) |
| `small_file_threshold_mb` | int | No | Files below this size are flagged (default 128 MB) |
| `skew_ratio_threshold` | float | No | Partition is skewed if largest/smallest > this ratio (default 10) |
| `subject` | str | No | Optional display name override |
| `output_format` | str | No | `"dict"` (default) or `"markdown"` |

---

## Output

```python
ctx["summary"]
# "⚠️ 5 small files (100%). OPTIMIZE recommended — estimated 3-6x query speedup."

ctx["metrics"]["total_files"]                        # Total file count
ctx["metrics"]["file_size"]["avg_mb"]                # Average file size
ctx["metrics"]["file_size"]["median_mb"]             # Median file size
ctx["metrics"]["small_files"]["count"]               # Files below threshold
ctx["metrics"]["small_files"]["pct"]                 # Fraction that are small (0–1)
ctx["metrics"]["partition_columns"]                  # List of partition columns (empty if none)
ctx["metrics"]["partition_skew"]["skewed"]           # True if skew ratio exceeded
ctx["metrics"]["partition_skew"]["skew_ratio"]       # max_partition / min_partition
ctx["metrics"]["estimated_query_cost"]["estimated_speedup_after_optimize"]  # e.g., "3-6x"

ctx["findings"]   # Specific file layout issues
ctx["risks"]      # Conditions that need action
ctx["suggested_next_actions"]  # OPTIMIZE and VACUUM commands to run
```

---

## Reading the Output

The summary gives you the single-line verdict. Look for two types of issues:

| Issue | Key to check | Action |
|---|---|---|
| Small files | `small_files.count > 0` | Run `OPTIMIZE catalog.schema.table` |
| Partition skew | `partition_skew.skewed = True` | Review partition key — consider re-partitioning |

If `total_files` is high (thousands+) and `avg_mb` is < 128MB, the table will benefit significantly from OPTIMIZE. The `estimated_speedup_after_optimize` metric gives a directional estimate.

**After OPTIMIZE, re-run `partition_check`** to verify file consolidation worked as expected.

---

## Pairs Well With

- `anchor("delta_diff", "catalog.schema.table", keys=[...])` — verify OPTIMIZE didn't accidentally change row values
- `anchor("watermark", source, target, watermark_col="...")` — if slow queries are masking staleness issues
- `anchor("schema_migrate", ...)` — combine schema evolution with a table OPTIMIZE in the same maintenance window

---

## Direct Import (no anchor() dispatcher needed)

```python
import sys
sys.path.append("/Workspace/Users/user@example.com/odibi_anchor/src")
sys.path.insert(0, "/Workspace/Users/user@example.com/odibi_anchor/src")

from tools.partition_check_tool.partition_check_impl import partition_check_context

# Requires an active SparkSession — runs on Databricks only
ctx = partition_check_context("analytics_dev.data_engineering_reference.guide_utility")

ctx["summary"]
# "⚠️ 5 small files (100%). OPTIMIZE recommended — estimated 3-6x query speedup."

ctx["metrics"]["small_files"]["count"]
# 5

ctx["metrics"]["estimated_query_cost"]["estimated_speedup_after_optimize"]
# "3-6x"
```

> **Note:** This tool imports from `odibi_anchor._utils` and requires an active Spark session. Both `src/` and root paths are required.
