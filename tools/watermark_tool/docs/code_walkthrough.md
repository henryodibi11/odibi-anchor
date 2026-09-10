# Watermark Tool — Code Walkthrough

## Worked Example 1: Stale Pipeline Detection

Scenario: Source has data through 2026-06-08 14:00, target only through 2026-06-07 02:00.
Pipeline runs daily. Target is 1.5 cadence periods behind.

### Input

```python
import pandas as pd

source = pd.DataFrame({
    "order_id": range(1, 1501),
    "updated_at": pd.date_range("2026-05-09", periods=1500, freq="h"),
    "amount": [100.0] * 1500,
})
# source max(updated_at) = 2026-06-08 14:00:00

target = pd.DataFrame({
    "order_id": range(1, 1201),
    "updated_at": pd.date_range("2026-05-09", periods=1200, freq="h"),
    "amount": [100.0] * 1200,
})
# target max(updated_at) = 2026-06-07 02:00:00

watermark_col = "updated_at"
```

### Execution Trace

**1. Resolve inputs**
```
detect_engine(source) = "pandas"
detect_engine(target) = "pandas"
# Both are DataFrames, no spark.table() needed
```

**2. Compute max watermarks**
```python
source_max = source["updated_at"].max()  # 2026-06-08 14:00:00
target_max = target["updated_at"].max()  # 2026-06-07 02:00:00
```

**3. Calculate lag**
```python
from datetime import timedelta

lag = source_max - target_max  # timedelta(hours=36)
lag_hours = lag.total_seconds() / 3600  # 36.0
lag_days = lag_hours / 24               # 1.5
```

**4. Count pending rows**
```python
pending = source[source["updated_at"] > target_max]
pending_count = len(pending)  # 1500 - 1200 = 300 rows
# (actually depends on exact data, but ~300 rows after the target watermark)
```

**5. Detect cadence** (within lookback_days=30)
```python
# Get distinct dates in the last 30 days of source data
recent = source[source["updated_at"] >= source_max - timedelta(days=30)]
distinct_dates = recent["updated_at"].dt.date.unique()

# Compute intervals between consecutive dates
intervals = [distinct_dates[i+1] - distinct_dates[i] for i in range(len(distinct_dates)-1)]
median_interval = statistics.median(intervals)  # ~1 day

typical_cadence = "daily"
```

**6. Classify staleness**
```python
cadence_hours = 24  # daily = 24h
cadence_multiples = lag_hours / cadence_hours  # 36 / 24 = 1.5

# Classification:
# < 1 cadence → "up_to_date"
# 1-3 cadences → "stale"
# > 3 cadences → "very_stale"

staleness = "stale"  # (1.5 is between 1 and 3)
```

**7. Scan for gaps**
```python
# Generate expected daily timestamps over lookback window
expected = pd.date_range(end=source_max.date(), periods=30, freq="D")

# Check which expected dates have data in target
target_dates = set(target["updated_at"].dt.date.unique())
missing = [d for d in expected if d.date() not in target_dates]

gaps = []  # No gaps in this case (just a lag)
```

**8. Assemble output**
```python
{
    "kind": "watermark",
    "subject": "watermark_check",
    "summary": "STALE: target watermark is 2026-06-07 02:00:00, "
               "source has ~300 rows through 2026-06-08 14:00. 36.0h lag.",
    "metrics": {
        "staleness": "stale",
        "lag_hours": 36.0,
        "lag_days": 1.5,
        "pending_count": 300,
        "source_watermark": "2026-06-08 14:00:00",
        "target_watermark": "2026-06-07 02:00:00",
        "typical_cadence": "daily",
        "gaps": [],
    },
    "findings": [
        "Target is 36 hours (1.5 daily cadence periods) behind source",
        "300 source rows are newer than target watermark",
    ],
    "risks": [
        "Pipeline is 1.5x behind expected daily cadence — verify scheduler",
    ],
    "suggested_next_actions": [
        "Check pipeline run history for failures",
        "After catch-up: anchor('delta_diff', target, keys=[...]) to verify",
    ],
}
```

---

## Worked Example 2: NULL Watermark (Initial Load Failure)

Scenario: Target table exists but has all NULL values in the watermark column.
This indicates the initial load may not have completed.

### Input

```python
source = pd.DataFrame({
    "id": [1, 2, 3],
    "event_time": pd.to_datetime(["2026-06-01", "2026-06-02", "2026-06-03"]),
})

target = pd.DataFrame({
    "id": [1, 2, 3],
    "event_time": [None, None, None],  # All NULL
})
target["event_time"] = pd.to_datetime(target["event_time"])
```

### Execution Trace

**1. Compute max watermarks**
```python
source_max = pd.Timestamp("2026-06-03")
target_max = pd.NaT  # All values are NULL
```

**2. NULL detection triggers**
```python
if pd.isna(target_max):
    staleness = "watermark_null"
    # Skip lag calculation, pending count, cadence
```

**3. Output**
```python
{
    "kind": "watermark",
    "summary": "WATERMARK_NULL: target watermark column is entirely NULL — "
               "initial load may not have completed.",
    "metrics": {
        "staleness": "watermark_null",
        "lag_hours": None,
        "lag_days": None,
        "pending_count": 3,  # All source rows are "pending"
        "source_watermark": "2026-06-03 00:00:00",
        "target_watermark": None,
        "typical_cadence": None,
        "gaps": [],
    },
    "findings": [
        "Target watermark column has no non-null values",
        "All 3 source rows are newer than target (effectively all pending)",
    ],
    "risks": [
        "Target table has no watermark values — initial load may not have completed",
    ],
    "suggested_next_actions": [
        "Verify target table was successfully loaded (check row count)",
        "Check if watermark column is being populated during ingestion",
    ],
}
```

---

## Key Python Patterns

### Datetime Arithmetic for Lag Calculation

```python
from datetime import timedelta

source_max = pd.Timestamp("2026-06-08 14:00:00")
target_max = pd.Timestamp("2026-06-07 02:00:00")

lag = source_max - target_max  # Timedelta('1 days 12:00:00')
lag_hours = lag.total_seconds() / 3600  # 36.0
```

Works identically for pandas Timestamps and Python datetime objects.

### pandas date_range for Gap Detection

```python
import pandas as pd

# Generate expected daily timestamps
expected = pd.date_range(start="2026-05-01", end="2026-06-08", freq="D")

# Compare against actual data
actual_dates = set(target["event_time"].dt.normalize().unique())
missing = expected[~expected.isin(actual_dates)]

# Each missing date is a "gap" where no load occurred
gaps = [{"date": str(d.date()), "expected": True, "found": False} for d in missing]
```

### Dual-Engine Dispatch

```python
def _compute_max_watermark(df, col, engine):
    if engine == "pandas":
        return df[col].max()
    else:  # spark
        from pyspark.sql import functions as F
        row = df.agg(F.max(col)).collect()[0]
        return row[0]

def _compute_pending(df, col, threshold, engine):
    if engine == "pandas":
        return len(df[df[col] > threshold])
    else:
        from pyspark.sql import functions as F
        return df.filter(F.col(col) > threshold).count()
```

Both engines produce identical results. The branching is purely operational,
not algorithmic.
