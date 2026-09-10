# Watermark Tool — Architecture

## System Overview

The watermark tool diagnoses incremental load staleness by comparing the watermark
(timestamp) column between a source and target table. It computes lag, detects
cadence, scans for gaps, and classifies staleness.

```
Source Table/DataFrame          Target Table/DataFrame
       │                                 │
       ▼                                 ▼
┌────────────────────────────────────────────────────────┐
│              watermark_debug_context()                  │
│                                                          │
│  1. Detect watermark column (auto or explicit)           │
│  2. Compute max watermark each side                      │
│  3. Calculate lag (hours, days)                           │
│  4. Count pending rows (source > target watermark)       │
│  5. Detect cadence (hourly, daily, weekly)                │
│  6. Scan for gaps in expected cadence                    │
│  7. Classify staleness                                   │
│                                                          │
└────────────────────────────────────────────────────────┘
       │
       ▼
   Anchor Standard Output Contract
```

## Staleness Classification

| Staleness | Condition | Meaning |
|-----------|-----------|--------|
| `up_to_date` | lag < 1 cadence period | Target is current |
| `stale` | lag = 1–3 cadence periods | Target is behind, likely a missed load |
| `very_stale` | lag > 3 cadence periods | Target is significantly behind |
| `watermark_null` | Target watermark column is all NULL | Initial load may not have completed |
| `filter_mismatch` | Source/target dtypes incompatible | Column type issue prevents comparison |

## Dual-Engine Support

The tool works with both pandas DataFrames and Spark DataFrames/table names:

| Input Type | Handling |
|------------|----------|
| pandas DataFrame | Direct series max/min/count |
| Spark DataFrame | Spark aggregation operations |
| String (table name) | `spark.table(name)` then Spark operations |

## Key Computations

### Lag Calculation
```
lag = source_max_watermark - target_max_watermark
lag_hours = lag.total_seconds() / 3600
lag_days = lag_hours / 24
```

### Pending Count
```
pending_count = count(source rows WHERE watermark > target_max_watermark)
```

### Cadence Detection
Analyzes the distribution of timestamps in the source within `lookback_days`:
- Compute intervals between consecutive distinct watermark values
- Use median interval to classify: hourly, daily, weekly, monthly

### Gap Detection
Uses `pandas.date_range` to generate expected timestamps based on detected cadence,
then identifies missing periods in the target table.

## Design Decisions

### Dual-Engine Dispatch

The same function handles both pandas and Spark inputs. It uses `detect_engine()`
to branch into the appropriate code path, enabling consistent API regardless of
how data is loaded.

### Auto vs Explicit Watermark Column

- If `watermark_col` is provided, uses it directly
- If `source_watermark_col`/`target_watermark_col` provided, uses different columns per side
- If neither provided, raises an error with helpful message

### Lookback Window

`lookback_days=30` limits cadence and gap analysis to recent data. This avoids
skewing cadence detection with historical periods that may have had different
load frequencies.

## Internal Structure

```
watermark_tool/
├── watermark_impl.py       → Main implementation (628 lines)
│   ├── _resolve_input()       → str/DF → DataFrame
│   ├── _compute_watermarks()  → max/min per side
│   ├── _compute_pending()     → count source rows after target max
│   ├── _detect_cadence()      → interval analysis → cadence label
│   ├── _detect_gaps()         → expected vs actual timestamps
│   ├── _classify_staleness()  → lag/cadence → staleness category
│   └── watermark_debug_context() → Orchestrator
├── __init__.py
├── README.md
└── tool.json
```

## Anchor Standard Output Contract

```python
{
    "kind": "watermark",
    "subject": "orders_pipeline",
    "summary": "STALE: target watermark is 2026-06-07 02:00, source has 1500 rows through 2026-06-08 14:00. 36h lag.",
    "metrics": {
        "staleness": "stale",
        "lag_hours": 36.0,
        "lag_days": 1.5,
        "pending_count": 1500,
        "source_watermark": "2026-06-08 14:00:00",
        "target_watermark": "2026-06-07 02:00:00",
        "typical_cadence": "daily",
        "gaps": [],
    },
    "findings": [
        "Target is 36 hours behind source (1.5 cadence periods)",
        "1500 source rows are newer than target watermark",
    ],
    "risks": [
        "Pipeline is 1.5x behind expected cadence — verify scheduler is running",
    ],
    "suggested_next_actions": [
        "Check pipeline run history for failures",
        "After catch-up: anchor('delta_diff', target_table, keys=[...]) to verify",
    ],
}
```
