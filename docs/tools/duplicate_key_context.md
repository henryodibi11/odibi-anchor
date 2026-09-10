# duplicate_key_context

`duplicate_key_context` analyzes duplicate key combinations in a DataFrame
and produces a structured report for grain validation. It answers:

```text
Does this data have duplicate keys?
How many rows are affected?
Which key combinations are duplicated?
```

## Public API

```python
from odibi_anchor.validation import duplicate_key_context

def duplicate_key_context(
    df: Any,
    keys: str | Sequence[str],
    *,
    subject: str | None = None,
    engine: str = "auto",
    sample_limit: int = 20,
    include_duplicate_rows: bool = False,
    row_sample_limit: int = 20,
    treat_nulls_as_duplicates: bool = True,
) -> dict[str, Any]:
    ...
```

## Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `df` | DataFrame | required | Pandas or Spark DataFrame |
| `keys` | str or list[str] | required | Key column(s) to check for uniqueness |
| `subject` | str | None | Human label for the dataset |
| `engine` | str | `"auto"` | Force `"pandas"` or `"spark"` |
| `sample_limit` | int | 20 | Max duplicate key groups to return |
| `include_duplicate_rows` | bool | False | Include full row data for duplicates |
| `row_sample_limit` | int | 20 | Max rows per duplicate group |
| `treat_nulls_as_duplicates` | bool | True | Count NULL keys as duplicates |

## Output Shape

```python
{
    "kind": "duplicate_key_context",
    "subject": "my_table",
    "summary": "my_table: found 2 duplicate key group(s) affecting 5 of 7 row(s) for key(s) [id].",
    "passed": False,
    "has_duplicates": True,
    "has_null_keys": False,
    "metrics": {
        "engine": "pandas",
        "row_count": 7,
        "key_columns": ["id"],
        "key_column_count": 1,
        "unique_key_count": 4,
        "duplicate_key_count": 2,
        "duplicate_row_count": 5,
        "excess_duplicate_row_count": 3,
        "duplicate_key_rate": 0.5,
        "duplicate_row_rate": 0.714,
        "excess_duplicate_row_rate": 0.429,
        "max_rows_per_key": 3,
        "null_key_row_count": 0,
        "all_null_key_row_count": 0,
        "null_key_rate": 0.0,
        "null_counts_by_key": {"id": 0},
        "sample_limit": 20,
        "sampled_duplicate_key_count": 2,
        "treat_nulls_as_duplicates": True,
    },
    "samples": [
        {"key": {"id": 3}, "row_count": 3, "excess_row_count": 2},
        {"key": {"id": 2}, "row_count": 2, "excess_row_count": 1},
    ],
    "sample_duplicate_rows": [],  # populated when include_duplicate_rows=True
    "findings": [...],
    "risks": [...],
    "suggested_next_actions": [...],
}
```

### Key Output Fields

- **`passed`** — Boolean. True = no duplicates found (key is unique).
- **`has_duplicates`** — Boolean. Inverse of passed.
- **`has_null_keys`** — Boolean. True if any key column has nulls.
- **`metrics["duplicate_key_count"]`** — Number of distinct key values that repeat.
- **`metrics["duplicate_row_count"]`** — Total rows participating in duplicates.
- **`metrics["excess_duplicate_row_count"]`** — Rows that would be dropped by dedup.
- **`metrics["duplicate_key_rate"]`** — Fraction of unique keys that are duplicated.
- **`metrics["max_rows_per_key"]`** — Worst-case duplication factor.
- **`samples`** — List of duplicate key groups with counts (capped by sample_limit).

## Example Usage

### Basic Check

```python
import pandas as pd
from odibi_anchor.validation import duplicate_key_context

df = pd.DataFrame({
    "id": [1, 2, 2, 3, 3, 3, 4],
    "val": [10, 20, 21, 30, 31, 32, 40],
})

result = duplicate_key_context(df, keys=["id"])
print(result["summary"])
# "dataframe: found 2 duplicate key group(s) affecting 5 of 7 row(s)..."

if not result["passed"]:
    print(f"Duplicates: {result['metrics']['duplicate_key_count']} groups")
    print(f"Affected rows: {result['metrics']['duplicate_row_count']}")
    print(f"Max per key: {result['metrics']['max_rows_per_key']}")
```

### Composite Keys

```python
result = duplicate_key_context(
    df,
    keys=["asset_id", "reading_date"],
    subject="silver.energy.readings",
)
```

### Include Full Duplicate Rows

```python
result = duplicate_key_context(
    df,
    keys=["id"],
    include_duplicate_rows=True,
    row_sample_limit=5,
)

# sample_duplicate_rows populated with actual row data
for row in result["sample_duplicate_rows"]:
    print(row)
```

### Pipeline Gate

```python
result = duplicate_key_context(df, keys=["id"])

if not result["passed"]:
    raise RuntimeError(
        f"Key violation: {result['metrics']['duplicate_key_count']} "
        f"duplicate groups ({result['metrics']['excess_duplicate_row_count']} excess rows)"
    )
```

### Null Key Analysis

```python
result = duplicate_key_context(
    df,
    keys=["customer_id", "order_id"],
    treat_nulls_as_duplicates=True,
)

if result["has_null_keys"]:
    print(f"Null key rows: {result['metrics']['null_key_row_count']}")
    print(f"Null counts: {result['metrics']['null_counts_by_key']}")
```

## Metric Definitions

| Metric | Formula |
|--------|---------|
| `duplicate_key_count` | Count of distinct key values appearing > 1 time |
| `duplicate_row_count` | Sum of all rows in groups where count > 1 |
| `excess_duplicate_row_count` | `duplicate_row_count - duplicate_key_count` (rows removed by dedup) |
| `duplicate_key_rate` | `duplicate_key_count / unique_key_count` |
| `duplicate_row_rate` | `duplicate_row_count / row_count` |
| `null_key_rate` | `null_key_row_count / row_count` |

## When To Use

Use this:

- before writing to a keyed table (verify grain uniqueness);
- after a join to check for row explosion;
- as evidence for `task_execution_context` (confirm key assumptions);
- before `diff_tables_by_key` (which requires unique keys).

## Gotchas

- There is no `status` field — use `passed` (boolean) instead
- `samples` contains key groups with counts, NOT full row data
  (use `include_duplicate_rows=True` for row data)
- `treat_nulls_as_duplicates=True` means all-NULL keys count as one group
- Single string key is accepted: `keys="id"` (no list needed)
