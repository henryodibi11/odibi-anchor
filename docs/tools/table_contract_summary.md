# table_contract_summary

`table_contract_summary` answers "what is this table?" in a structured,
JSON-serializable format. Feed the output to a human reviewer, paste it
into a Genie/ChatGPT prompt, or use it as input to downstream validation.

| What It Detects | How |
|---|---|
| Shape | Row count, column count |
| Schema | Column names, dtypes |
| Grain | Candidate key uniqueness, duplicate/null key detection |
| Freshness | Latest timestamp, age in days, staleness threshold |
| Column health | Null rates, distinct counts, top values, inferred roles |
| Risks | High-null columns, all-null columns, duplicate keys, stale data |
| Samples | Capped row examples for quick inspection |

## Public API

```python
from odibi_anchor.tables import table_contract_summary, render_contract_report

def table_contract_summary(
    df: Any,
    *,
    subject: str = "dataframe",
    candidate_key_columns: list[str] | None = None,
    freshness_columns: list[str] | None = None,
    profile_columns: list[str] | None = None,
    sample_columns: list[str] | None = None,
    sample_limit: int = 10,
    max_profile_columns: int = 50,
    high_null_rate_threshold: float = 0.50,
    stale_after_days: float | None = None,
    reference_time: str | datetime | None = None,
    include_value_examples: bool = True,
    engine: Literal["auto", "pandas", "spark"] = "auto",
    spark_sample_size: int = 5000,
    output_format: Literal["dict", "markdown"] = "dict",
) -> dict[str, Any] | str:
    ...
```

## Parameters

| Parameter | Type | Default | Purpose |
|---|---|---|---|
| `df` | DataFrame | required | Pandas or Spark DataFrame |
| `subject` | str | `"dataframe"` | Human label |
| `candidate_key_columns` | list[str] | None | Check grain uniqueness |
| `freshness_columns` | list[str] | None | Override auto-detect |
| `profile_columns` | list[str] | None | Explicit columns to profile |
| `sample_columns` | list[str] | None | Columns in sample rows |
| `sample_limit` | int | 10 | Max sample rows |
| `max_profile_columns` | int | 50 | Cap for wide tables |
| `high_null_rate_threshold` | float | 0.50 | Null rate risk threshold |
| `stale_after_days` | float | None | Staleness threshold |
| `reference_time` | str/datetime | None | For deterministic age |
| `include_value_examples` | bool | True | Include top_values |
| `engine` | str | `"auto"` | Force `"pandas"` or `"spark"` |
| `spark_sample_size` | int | 5000 | Rows sampled for Spark |
| `output_format` | str | `"dict"` | `"dict"` or `"markdown"` |

## Output Shape

```python
{
    "kind": "table_contract_summary",
    "subject": "catalog.schema.table",
    "summary": "one-line natural language summary",
    "metrics": {
        "row_count": 31882,
        "column_count": 48,
        "source_engine": "spark",
        # ... additional metrics
    },
    "schema": [
        {"column": "asset_id", "dtype": "string", "position": 0},
        ...
    ],
    "profile": {
        "columns": [...],      # column names profiled
        "omitted_columns": [...],  # columns skipped
    },
    "candidate_keys": {
        "provided_key": {
            "columns": ["asset_id"],
            "is_unique": True,
            "status": "unique",
            "duplicate_count": 0,
            "null_key_count": 0,
        },
        "inferred_unique_columns": ["record_id"],
    },
    "freshness": {
        "selected_column": "updated_at",
        "latest_value": "2026-05-08T12:00:00",
        "latest_age_days": 2.5,
        "is_stale": False,
    },
    "findings": [...],
    "risks": [...],
    "samples": [...],
    "suggested_next_actions": [...],
}
```

## Example Usage

### Basic (Pandas)

```python
import pandas as pd
from odibi_anchor.tables import table_contract_summary

df = pd.DataFrame({
    "asset_id": ["A1", "A2", "A3"],
    "region": ["ERCOT", "MISO", "PJM"],
    "capacity_mw": [100.0, 250.5, 300.0],
})

ctx = table_contract_summary(df, subject="gold.energy.assets")
print(ctx["summary"])
print(f"Rows: {ctx['metrics']['row_count']}")
```

### Candidate Key Check

```python
ctx = table_contract_summary(
    df,
    subject="silver.energy.readings",
    candidate_key_columns=["asset_id", "date"],
)

pk = ctx["candidate_keys"]["provided_key"]
print(f"Key unique: {pk['is_unique']}")
print(f"Duplicates: {pk['duplicate_count']}")
```

### Freshness Detection

```python
ctx = table_contract_summary(
    df,
    stale_after_days=7,
    reference_time="2026-05-08T00:00:00Z",
)

fr = ctx["freshness"]
print(f"Column: {fr['selected_column']}")
print(f"Age: {fr['latest_age_days']} days")
print(f"Stale: {fr['is_stale']}")
```

### Spark (Hybrid Approach)

```python
spark_df = spark.table("catalog.schema.table")

ctx = table_contract_summary(
    spark_df,
    subject="catalog.schema.table",
    candidate_key_columns=["id"],
    spark_sample_size=5000,
)

# Exact metrics from full table
print(f"Exact row count: {ctx['metrics']['row_count']:,}")
```

**Spark hybrid approach:**

| Metric | Source | Exact? |
|---|---|---|
| Row count | Spark `df.count()` | Yes |
| Null counts per column | Spark aggregation | Yes |
| Duplicate key detection | Spark `groupBy().count()` | Yes |
| Top values, distinct counts | Pandas sample | Approximate |
| Freshness detection | Pandas sample | Approximate |
| Numeric stats | Pandas sample | Approximate |

### Markdown Report

```python
from odibi_anchor.tables import render_contract_report

# One-liner
md = table_contract_summary(df, output_format="markdown")

# Two-step (when you need both dict and markdown)
ctx = table_contract_summary(df)
md = render_contract_report(ctx, show_samples=True)
```

### LLM Prompt Integration

```python
import json

ctx = table_contract_summary(df, subject="gold.energy.assets")

# Option 1: Full JSON (most structured)
prompt = f"Table contract:\n```json\n{json.dumps(ctx, indent=2)}\n```"

# Option 2: Markdown (40% fewer tokens)
md = table_contract_summary(df, output_format="markdown")
prompt = f"Table context:\n\n{md}\n\nBased on this, write a validation query."
```

## Design Decisions

1. **Hybrid Spark approach** — Exact aggregations on full table (row count,
   nulls, key uniqueness), approximate profiling from sample (top values,
   distinct counts, stats).

2. **Pandas-first profiling** — Full profiling logic lives in the pandas path.
   Spark path samples to pandas and reuses it.

3. **No framework, no base class** — Single file, standalone function.
   Only dependency: `detect_engine`.

4. **Output format dual-mode** — dict for agents, markdown for humans.

## When To Use

Use this when:

- exploring an unfamiliar table before writing logic;
- documenting grain and contract for a pipeline;
- feeding table context to LLM prompts;
- checking freshness/staleness before consumption.

## Gotchas

- `spark_sample_size` only affects profiling approximation, NOT exact metrics
- Freshness auto-detection looks for timestamp/date columns — override with
  `freshness_columns` if the column name is non-obvious
- Wide tables (>50 columns) are capped by `max_profile_columns`
