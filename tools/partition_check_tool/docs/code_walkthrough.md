# Partition Check Tool — Code Walkthrough

## Worked Example 1: Small File Detection

Scenario: A table has 500 files, most created by small appends. 80% are under 128MB.

### Input

```python
table = "catalog.schema.orders"
small_file_threshold_mb = 128
skew_ratio_threshold = 10.0
```

### Execution Trace

**1. Get table detail**
```python
spark.sql("DESCRIBE DETAIL catalog.schema.orders")
```
Returns:
```
numFiles = 500
sizeInBytes = 2,147,483,648  (2 GB total)
partitionColumns = ["status"]
```

**2. List individual file sizes**

Reads from Delta log (AddFile entries):
```
file_sizes_mb = [0.01, 0.02, 0.5, 0.8, 1.2, 2.0, ..., 128.5, 130.0, ...]
# 500 values
```

**3. Compute file size statistics**
```python
import statistics

avg_mb = sum(file_sizes_mb) / len(file_sizes_mb)  # = 4.1
median_mb = statistics.median(file_sizes_mb)        # = 2.8
min_mb = min(file_sizes_mb)                         # = 0.01
max_mb = max(file_sizes_mb)                         # = 128.5
p10_mb = sorted(file_sizes_mb)[50]                  # = 0.5  (index 500*0.1)
p90_mb = sorted(file_sizes_mb)[450]                 # = 12.0 (index 500*0.9)
```

**4. Count small files**
```python
small_files = [f for f in file_sizes_mb if f < 128]
small_count = len(small_files)  # = 400
small_pct = 400 / 500           # = 0.80
```

**5. Estimate speedup**

Heuristic:
```python
# Optimal file count: total_size / target_file_size
optimal_files = total_size_mb / 128  # = 2048 / 128 = 16 files
file_ratio = actual_files / optimal_files  # = 500 / 16 = 31.25

# Map ratio to speedup range:
# ratio > 20 → "5-10x"
# ratio 5-20 → "3-6x"
# ratio 2-5 → "2-3x"
# ratio < 2 → "minimal"

estimated_speedup = "5-10x"  # (31.25 > 20)
```

**6. Output**
```python
{
    "kind": "partition_check",
    "summary": "⚠️ 400 small files (80%). OPTIMIZE recommended — estimated 5-10x speedup.",
    "metrics": {
        "total_files": 500,
        "total_size_mb": 2048,
        "partition_columns": ["status"],
        "file_size": {
            "avg_mb": 4.1, "median_mb": 2.8,
            "min_mb": 0.01, "max_mb": 128.5,
            "p10_mb": 0.5, "p90_mb": 12.0,
        },
        "small_files": {"count": 400, "pct": 0.80},
        "estimated_query_cost": {
            "estimated_speedup_after_optimize": "5-10x",
        },
    },
    "findings": [
        "400 of 500 files (80%) are below 128MB threshold",
        "Median file size 2.8MB is far below optimal 128MB target",
    ],
    "risks": [
        "Small file problem: 80% of files under threshold — queries scan excessive metadata",
    ],
    "suggested_next_actions": [
        "OPTIMIZE catalog.schema.orders;",
        "VACUUM catalog.schema.orders RETAIN 168 HOURS;",
    ],
}
```

---

## Worked Example 2: Partition Skew Detection

Scenario: Table partitioned by `status`. Three partitions with highly uneven distribution.

### Input

```
Partition distribution:
  status=active:    450 files, 1800 MB
  status=pending:    30 files, 120 MB
  status=cancelled:  20 files, 80 MB
```

### Execution Trace

**1. Detect partition skew**

Per-partition file count aggregation:
```python
partition_sizes = {
    "status=active": 450,
    "status=pending": 30,
    "status=cancelled": 20,
}

max_partition = 450  ("active")
min_partition = 20   ("cancelled")
skew_ratio = 450 / 20 = 22.5

skew_ratio (22.5) > threshold (10.0) → skewed = True
```

**2. Output additions**
```python
"partition_skew": {
    "skewed": True,
    "skew_ratio": 22.5,
    "largest_partition": "status=active (450 files, 1800 MB)",
    "smallest_partition": "status=cancelled (20 files, 80 MB)",
},

"risks": [
    "Partition skew ratio 22.5x exceeds threshold 10.0x — "
    "queries filtering on 'active' will scan disproportionately more files",
],

"findings": [
    "Partition 'status=active' has 90% of all files (450/500)",
    "Consider re-partitioning by a higher-cardinality column",
],
```

---

## Key Python Patterns

### statistics.median for File Size Analysis

```python
import statistics

file_sizes = [0.01, 0.5, 2.0, 4.0, 128.0]  # MB
median = statistics.median(file_sizes)        # 2.0

# Percentiles via sorted list indexing:
sorted_sizes = sorted(file_sizes)
p10 = sorted_sizes[int(len(sorted_sizes) * 0.1)]
p90 = sorted_sizes[int(len(sorted_sizes) * 0.9)]
```

### PySpark Delta API for File Listing

```python
# Get table metadata:
detail = spark.sql("DESCRIBE DETAIL catalog.schema.orders").collect()[0]
num_files = detail["numFiles"]
size_bytes = detail["sizeInBytes"]
partition_cols = detail["partitionColumns"]

# Get individual file sizes from Delta log:
# (Implementation reads AddFile actions from the DeltaTable object)
from delta.tables import DeltaTable
dt = DeltaTable.forName(spark, "catalog.schema.orders")
# Access internal file listing through Delta APIs
```

### Heuristic Speedup Estimation

```python
def _estimate_speedup(total_size_mb, total_files, target_file_mb=128):
    optimal_files = max(1, total_size_mb / target_file_mb)
    ratio = total_files / optimal_files

    if ratio > 20:
        return "5-10x"
    elif ratio > 5:
        return "3-6x"
    elif ratio > 2:
        return "2-3x"
    else:
        return "minimal"
```

This is directional, not precise. The actual speedup depends on query patterns,
cluster size, and caching behavior. It correctly identifies tables that will
benefit most from compaction.
