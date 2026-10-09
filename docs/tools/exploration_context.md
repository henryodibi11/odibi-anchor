# exploration_context

`exploration_context` is a composite first-look tool that combines dataset
profiling, automatic grain detection, freshness analysis, and key suggestions
into a single call. It eliminates the 3-5 call exploration sequence agents
typically perform when encountering an unfamiliar table.

It answers:

```text
How big is this table and what columns does it have?
What is the grain (primary key)?
How fresh is the data?
What quality risks should I check?
What keys should I use for merges/diffs?
```

Designed to be the **first tool called** on any unfamiliar dataset.

## Public API

```python
from odibi_anchor.profiling import exploration_context

def exploration_context(
    df: Any,
    *,
    subject: str = "dataframe",
    candidate_keys: list[str] | None = None,
    sample_limit: int = 5,
    top_values_limit: int = 5,
    high_null_threshold: float = 0.5,
    engine: str = "auto",
    output_format: str = "dict",
) -> dict[str, Any] | str:
    ...
```

## Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `df` | DataFrame | required | Pandas or Spark DataFrame |
| `subject` | str | `"dataframe"` | Table name or human label |
| `candidate_keys` | list[str] | None | Columns to test for grain. If None, auto-detects |
| `sample_limit` | int | 5 | Maximum sample rows |
| `top_values_limit` | int | 5 | Top values per low-cardinality column |
| `high_null_threshold` | float | 0.5 | Null % threshold for flagging |
| `engine` | str | `"auto"` | `"auto"`, `"pandas"`, or `"spark"` |
| `output_format` | str | `"dict"` | `"dict"` or `"markdown"` |

## Output Shape

```python
{
    "kind": "exploration_context",
    "subject": "silver.order_snapshots",
    "summary": "silver.order_snapshots: 186,420 rows, 18 cols, grain=[order_id, snapshot_year, snapshot_month], fresh (18.9d ago). 2 high-null col(s).",
    "metrics": {
        "row_count": 186420,
        "column_count": 18,
        "high_null_column_count": 2,
        "unique_columns": [],
        "grain_is_unique": True,
        "has_freshness_signal": True,
    },
    "columns": ["order_id", "region", ...],
    "column_profiles": {
        "order_id": {
            "dtype": "StringType()",
            "null_count": 0,
            "null_pct": 0.0,
            "distinct_count": 15104,
            "is_unique": False,
        },
        ...
    },
    "grain_analysis": {
        "best_grain": ["order_id", "snapshot_year", "snapshot_month"],
        "is_unique": True,
        "duplicate_rate": 0.0,
        "candidates_tested": [
            {"columns": ["order_id"], "is_unique": False, "duplicate_rate": 0.919},
            {"columns": ["order_id", "snapshot_year", "snapshot_month"], "is_unique": True, "duplicate_rate": 0.0},
        ],
    },
    "freshness": {
        "column": "_extracted_at",
        "min": "2026-04-21 16:21:03",
        "max": "2026-04-22 14:59:10",
        "staleness": "18.9d ago",
        "staleness_hours": 454.49,
    },
    "suggested_keys": ["order_id", "snapshot_year", "snapshot_month"],
    "findings": [...],
    "risks": [...],
    "samples": [...],
    "suggested_next_actions": [...],
}
```

## Grain Detection Logic

The tool auto-detects grain by testing candidate key combinations in priority order:

1. **Single columns that are unique** (tested first — guaranteed grain if found)
2. **Columns with ID-like names** (`id`, `key`, `code`, `number`, `num`, `pk`, `sk`)
3. **ID column + grain partition columns** (the snapshot pattern)
   - Tests `[id_col + all partition cols]` as a combo first
   - Then tests `[id_col + each partition col]` individually
   - Partition hints: `snapshot_year`, `snapshot_month`, `snapshot_date`, `snapshot_day`,
     `partition`, `period`, `batch`, `version`, `revision`, `year`, `month`, `week`, `day`, `quarter`
4. **ID column + temporal column** (entity + date pattern)
   - Temporal hints: `date`, `time`, `timestamp`, `datetime`, `created`, `updated`,
     `modified`, `snapshot`, `effective`, `loaded`, `ingested`, `asof`
   - Temporal columns that already matched as partition columns are excluded (no double-testing)
5. **Fallback**: first few columns

Deduplication ensures no combination is tested twice. Capped at 20 candidates total.

When `candidate_keys` is provided, only that combination is tested.

### Why partition hints exist

Many warehouse tables use `entity_id + snapshot_year + snapshot_month` as their grain
(each row is one entity per monthly snapshot, e.g. one order per month in an order snapshot table). Before v0.2.0, these columns were
categorized only as temporal and competed with date columns for a 3-slot cap. Now they
are tested explicitly as grain partition dimensions — a higher-priority category.

## Freshness Detection Logic

1. Finds columns with `TimestampType`/`DateType` schema (Spark) or datetime dtype (pandas)
2. Falls back to name-hinted columns (`updated`, `modified`, `loaded`, etc.)
3. Computes staleness as time since the max value
4. Prioritizes columns named `updated`, `modified`, `loaded`, `ingested`, `extracted`

## Usage Examples

### First look at an unfamiliar table (Spark)

```python
df = spark.table("catalog.schema.my_table")
ctx = exploration_context(df, subject="silver.my_table")

print(ctx["summary"])
# "silver.my_table: 186,420 rows, 18 cols, grain=[order_id, snapshot_year, snapshot_month], fresh (18.9d ago)."

print(ctx["suggested_keys"])
# ["order_id", "snapshot_year", "snapshot_month"]
```

### With domain knowledge (provide candidate keys)

```python
ctx = exploration_context(
    df,
    subject="silver.order_snapshots",
    candidate_keys=["order_id", "snapshot_year", "snapshot_month"],
)
# grain_analysis["is_unique"] → True (confirmed unique)
```

### Pandas DataFrame

```python
import pandas as pd
df = pd.read_csv("data.csv")
ctx = exploration_context(df, subject="raw.invoices")
```

### Markdown report

```python
report = exploration_context(df, subject="silver.events", output_format="markdown")
```

## Illustrative Output (retail order snapshots)

The following is an illustrative example on a hypothetical `silver.order_snapshots`
table (one row per order per monthly snapshot). The numbers are invented for
documentation and do not come from any real dataset:

```
Summary: silver.order_snapshots: 186,420 rows, 18 cols,
  grain=[order_id, snapshot_year, snapshot_month], fresh (18.9d ago). 2 high-null col(s).

Grain Analysis (auto-detected, no candidate_keys provided):
  order_id alone → 91.90% dup rate (not unique — snapshot table)
  order_id + snapshot_year + snapshot_month → 0.00% dup rate ✓ UNIQUE
  order_id + snapshot_year → 84.37% dup rate
  order_id + snapshot_month → 19.82% dup rate
  order_id + order_date → 91.90% dup rate
  order_id + delivered_date → 89.41% dup rate
  order_id + ship_date → 88.75% dup rate

Freshness: _extracted_at, latest 2026-04-22 14:59:10 (18.9d ago)

Risks:
  • 2 columns exceed 50% null rate: gift_message, return_reason
```

The grain is found on the second candidate tested — `_GRAIN_PARTITION_HINTS` correctly
identifies `snapshot_year` and `snapshot_month` as grain dimensions and pairs them with
the ID column before falling through to generic temporal columns.

## When to Use

| Situation | Use exploration_context |
|-----------|----------------------|
| First time seeing a table | Always — replaces 3-5 manual calls |
| Before writing a merge/join | Get suggested_keys |
| Before quality_gate_context | Understand grain + freshness first |
| Diagnosing data quality issues | Column profiles + risks |

## Relationship to Other Tools

| Tool | Purpose | When to use instead |
|------|---------|-------------------|
| `dataset_profile_context` | Deep column-level stats | Need full distribution details |
| `table_contract_summary` | Document for handoff | Need formal table card |
| `duplicate_key_context` | Deep dupe analysis | Need samples of violating rows |
| `exploration_context` | Quick first-look composite | **Start here, then drill down** |
