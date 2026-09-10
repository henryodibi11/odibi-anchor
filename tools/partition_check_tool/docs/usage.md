# Partition Check Tool — Usage

## Signature

```python
partition_check_context(
    table: str,
    small_file_threshold_mb: int = 128,
    skew_ratio_threshold: float = 10.0,
    subject: str | None = None,
    output_format: str = "dict",
) -> dict | str
```

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `table` | str | Yes | — | Fully qualified Delta table name (`"catalog.schema.table"`) |
| `small_file_threshold_mb` | int | No | 128 | Files below this size (MB) are flagged as "small" |
| `skew_ratio_threshold` | float | No | 10.0 | Partition is skewed if largest/smallest exceeds this ratio |
| `subject` | str | No | table name | Display label |
| `output_format` | str | No | `"dict"` | `"dict"` or `"markdown"` |

## Basic Usage

```python
# Default thresholds
ctx = anchor("partition_check", "catalog.schema.orders")

# Custom thresholds for stricter checks
ctx = anchor("partition_check", "catalog.schema.orders",
         small_file_threshold_mb=256, skew_ratio_threshold=5)
```

## Result Structure

| Field | Type | Description |
|-------|------|-------------|
| `kind` | str | Always `"partition_check"` |
| `summary` | str | One-line verdict with OPTIMIZE recommendation |
| `metrics.total_files` | int | Total number of data files |
| `metrics.total_size_mb` | float | Total table size in MB |
| `metrics.partition_columns` | list[str] | Partition column names (empty if unpartitioned) |
| `metrics.file_size.avg_mb` | float | Average file size |
| `metrics.file_size.median_mb` | float | Median file size |
| `metrics.file_size.min_mb` | float | Smallest file |
| `metrics.file_size.max_mb` | float | Largest file |
| `metrics.file_size.p10_mb` | float | 10th percentile |
| `metrics.file_size.p90_mb` | float | 90th percentile |
| `metrics.small_files.count` | int | Files below threshold |
| `metrics.small_files.pct` | float | Fraction of files that are small (0–1) |
| `metrics.partition_skew.skewed` | bool | Whether skew ratio exceeded threshold |
| `metrics.partition_skew.skew_ratio` | float | max partition size / min partition size |
| `metrics.estimated_query_cost.estimated_speedup_after_optimize` | str | Directional estimate (e.g. "3-6x") |
| `findings` | list[str] | File layout issues |
| `risks` | list[str] | Conditions needing action |
| `suggested_next_actions` | list[str] | OPTIMIZE/VACUUM commands |

## Interpreting Results

| Condition | Verdict | Action |
|-----------|---------|--------|
| `small_files.pct > 0.5` | Heavy small file problem | Run OPTIMIZE immediately |
| `small_files.pct > 0.1` | Moderate small files | Schedule OPTIMIZE |
| `small_files.pct == 0` | Healthy file layout | No action needed |
| `partition_skew.skewed == True` | Partition imbalance | Consider re-partitioning |

## Direct Import

```python
import sys
sys.path.append("/Workspace/Users/user@example.com/odibi_anchor/src")
sys.path.append("/Workspace/Users/user@example.com/odibi_anchor")

from tools.partition_check_tool.partition_check_impl import partition_check_context

ctx = partition_check_context("catalog.schema.orders")
```
