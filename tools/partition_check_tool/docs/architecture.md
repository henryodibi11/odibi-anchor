# Partition Check Tool — Architecture

## System Overview

The partition check tool diagnoses small files, partition skew, and file layout
issues on Delta tables. It reads file-level metadata from Delta's transaction
log and produces actionable OPTIMIZE recommendations with estimated speedup.

```
Delta Table Name ("catalog.schema.table")
       │
       ▼
┌────────────────────────────────────────────────┐
│ 1. DESCRIBE DETAIL table                        │
│    → numFiles, sizeInBytes, partitionColumns     │
└────────┬───────────────────────────────────────┘
         │
         ▼
┌────────────────────────────────────────────────┐
│ 2. List individual file sizes                    │
│    (via Delta log file listing)                   │
└────────┬───────────────────────────────────────┘
         │
         ▼
┌────────────────────────────────────────────────┐
│ 3. File size analysis                            │
│    avg, median, min, max, p10, p90               │
│    small_file count + percentage                  │
└────────┬───────────────────────────────────────┘
         │
         ▼
┌────────────────────────────────────────────────┐
│ 4. Partition skew detection                      │
│    skew_ratio = max_partition_size / min          │
└────────┬───────────────────────────────────────┘
         │
         ▼
┌────────────────────────────────────────────────┐
│ 5. OPTIMIZE recommendation + speedup estimate    │
└────────────────────────────────────────────────┘
         │
         ▼
   Anchor Standard Output Contract
```

## Key Metrics

| Metric | Threshold | Meaning |
|--------|-----------|--------|
| `small_file_threshold_mb` | Default 128 MB | Files below this are "small" |
| `skew_ratio_threshold` | Default 10.0 | Partition is skewed if largest/smallest exceeds this |

## Design Decisions

### Spark-Only

Partition check reads file-level metadata from Delta (`DESCRIBE DETAIL`, file listing).
This information is only accessible via the Delta Lake APIs on Spark. There is no
pandas equivalent.

### Non-Invasive

The tool only reads metadata and computes statistics. It never runs OPTIMIZE or
modifies data. The `suggested_next_actions` output contains ready-to-run SQL
commands that the user must explicitly execute.

### Speedup Estimation

The estimated speedup is a directional heuristic based on:
- Ratio of current files to optimal file count (target: 128MB per file)
- Proportion of small files
- Whether liquid clustering or Z-ordering is already in use

Typical estimates: 2–3x for moderate small file issues, 5–10x for severe cases.

## Internal Structure

```
partition_check_tool/
├── partition_check_impl.py  → Main implementation (674 lines)
│   ├── _get_table_detail()     → DESCRIBE DETAIL → numFiles, size, partitions
│   ├── _list_file_sizes()      → Delta log → per-file size list
│   ├── _compute_file_stats()   → avg/median/min/max/p10/p90
│   ├── _detect_partition_skew()→ Per-partition aggregation + ratio
│   ├── _estimate_speedup()     → Heuristic speedup calculation
│   └── partition_check_context()→ Orchestrator
├── __init__.py
├── README.md
└── tool.json
```

## Anchor Standard Output Contract

```python
{
    "kind": "partition_check",
    "subject": "catalog.schema.orders",
    "summary": "⚠️ 400 small files (80%). OPTIMIZE recommended — estimated 3-6x speedup.",
    "metrics": {
        "total_files": 500,
        "total_size_mb": 2048,
        "partition_columns": ["status"],
        "file_size": {
            "avg_mb": 4.1,
            "median_mb": 2.8,
            "min_mb": 0.01,
            "max_mb": 128.5,
            "p10_mb": 0.5,
            "p90_mb": 12.0,
        },
        "small_files": {"count": 400, "pct": 0.80},
        "partition_skew": {
            "skewed": True,
            "skew_ratio": 22.5,
            "largest_partition": "status=active (450 files)",
            "smallest_partition": "status=cancelled (20 files)",
        },
        "estimated_query_cost": {
            "estimated_speedup_after_optimize": "3-6x",
        },
    },
    "findings": [...],
    "risks": [...],
    "suggested_next_actions": [
        "OPTIMIZE catalog.schema.orders;",
        "VACUUM catalog.schema.orders RETAIN 168 HOURS;",
    ],
}
```
