# dataset_profile_context

`dataset_profile_context` creates a compact first-look profile for a
Pandas or Spark DataFrame. It replaces repeated manual checks such as
`.show()`, `.describe()`, null counts, distinct counts, and top-value
inspection.

Core pattern:

```text
DataFrame → structured profile context → compressed signal → decision/action
```

## Public API

```python
from odibi_anchor.profiling import dataset_profile_context

def dataset_profile_context(
    df: Any,
    columns: list[str] | None = None,
    *,
    subject: str = "dataframe",
    engine: str = "auto",
    sample_limit: int = 5,
    top_values_limit: int = 5,
    high_null_threshold: float = 0.5,
    high_cardinality_threshold: float = 0.9,
    include_samples: bool = True,
) -> dict[str, Any]:
    ...
```

## Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `df` | DataFrame | required | Pandas or Spark DataFrame |
| `columns` | list[str] | None | Subset of columns to profile (default: all) |
| `subject` | str | `"dataframe"` | Human label for the dataset |
| `engine` | str | `"auto"` | Force `"pandas"` or `"spark"` |
| `sample_limit` | int | 5 | Max sample rows returned |
| `top_values_limit` | int | 5 | Top frequent values per column |
| `high_null_threshold` | float | 0.5 | Flag columns above this null rate |
| `high_cardinality_threshold` | float | 0.9 | Flag columns above this distinct ratio |
| `include_samples` | bool | True | Include sample row values |

## Output Shape

```python
{
    "kind": "dataset_profile_context",
    "subject": "demo.invoice_events",
    "summary": "dataframe has 10 rows and 3 profiled columns; 10.0% cells are null...",
    "metrics": {
        "row_count": 10,
        "column_count": 3,
        "profiled_column_count": 3,
        "total_cells": 30,
        "missing_cells": 3,
        "missing_cell_pct": 0.1,
        "duplicate_row_count": 0,
        "duplicate_row_pct": 0.0,
        "type_counts": {"numeric_integer": 2, "text": 1},
        "potential_key_columns": ["id"],
        "all_null_columns": [],
        "constant_columns": [],
        "columns_with_null_like_values": [],
        "high_null_columns": [],
        "high_cardinality_columns": [],
        "columns_recommended_for_type_casting": [],
    },
    "columns": ["id", "name", "amount"],  # List of column name strings
    "column_profiles": {
        "amount": {
            "name": "amount",
            "dtype": "float64",
            "inferred_type": "numeric_float",
            "total_count": 10,
            "non_null_count": 9,
            "null_count": 1,
            "null_pct": 0.1,
            "distinct_count": 9,
            "distinct_pct": 1.0,
            "is_unique": False,
            "is_constant": False,
            "stats": {"min": 10.0, "max": 100.0, "mean": 55.0, ...},
            "top_values": [{"value": 10.0, "count": 1}, ...],
            "quality_flags": [],
            "null_like_values": [],
        },
    },
    "findings": [...],
    "risks": [...],
    "samples": [...],
    "suggested_next_actions": [...],
    "parameters": {...},
}
```

### Key Output Notes

- **`columns`** — A list of column name **strings** (NOT a list of dicts)
- **`column_profiles`** — A dict keyed by column name, each containing stats
- **`null_pct`** — The null rate field (NOT `null_rate` or `high_null_rate`)
- **`metrics["high_null_columns"]`** — List of column names above threshold
- **`metrics["potential_key_columns"]`** — Auto-detected unique columns

## Example Usage

### Basic (Pandas)

```python
import pandas as pd
from odibi_anchor.profiling import dataset_profile_context

df = pd.DataFrame({
    "id": [1, 2, 3],
    "amount": [10.0, None, 30.0],
    "status": ["new", "active", "unknown"],
})

profile = dataset_profile_context(df, subject="demo.invoice_events")
print(profile["summary"])
print(profile["column_profiles"]["amount"]["null_pct"])  # 0.333
```

### Checking for Issues

```python
profile = dataset_profile_context(df)

# High-null columns (via metrics)
for col_name in profile["metrics"]["high_null_columns"]:
    pct = profile["column_profiles"][col_name]["null_pct"]
    print(f"  WARNING {col_name}: {pct:.0%} nulls")

# Potential keys
print(f"Potential keys: {profile['metrics']['potential_key_columns']}")

# All-null dead columns
print(f"Dead columns: {profile['metrics']['all_null_columns']}")
```

### Agent Pattern

```python
profile = dataset_profile_context(df, subject="bronze.raw_events")

# Feed summary to LLM for quick understanding
prompt = f"Table context:\n{profile['summary']}"

# Or inspect specific column for join preparation
col = profile["column_profiles"]["customer_id"]
if not col["is_unique"]:
    print(f"customer_id has {col['distinct_count']} distinct values "
          f"({col['distinct_pct']:.0%} cardinality)")
```

## What It Detects

| Category | Details |
|----------|---------|
| Shape | Row count, column count, total/missing cells |
| Types | Declared dtype, inferred semantic type |
| Nulls | Null count/pct per column, null-like string values ("N/A", "unknown", etc.) |
| Cardinality | Distinct count/pct, potential keys, constants |
| Values | Top frequent values, numeric stats (min/max/mean/median/std/p25/p75) |
| Temporal | Min/max dates for datetime columns |
| Risks | High-null, all-null, constant, high-cardinality, duplicate-row, null-like, type-cast |

## When To Use

Use this as the first step when:

- exploring an unfamiliar table;
- preparing evidence for `task_execution_context`;
- checking data quality before transformation;
- feeding context to an LLM prompt;
- validating input assumptions before joins.

## Gotchas

- `columns` in output is a list of strings, NOT a list of dicts
- Use `column_profiles[col_name]` to access per-column stats
- The field is `null_pct` (not `null_rate`)
- Spark implementation samples to pandas for profiling (exact aggregations
  on full table, approximate profiling from sample)
