# Diagnose Empty

> Find out why a DataFrame has zero rows (or far fewer than expected).

---

## When to Use

- Your pipeline produced an empty result and you don't know why
- A join, filter, or aggregation returned far fewer rows than expected
- You have the result and the upstream DataFrames and want the tool to tell you where the rows disappeared
- You want to know whether the problem is a bad join key, a too-tight filter, a type mismatch, or an all-null upstream column

**Anti-pattern:** Don't use `diagnose_empty` if the DataFrame is populated and you just want to understand its shape — use `profile_table` instead. `diagnose_empty` is specifically for the "where did my rows go?" scenario.

---

## Quick Start

```python
import pandas as pd

# Inline sample data — replace with your own DataFrames
# IDs are intentionally non-overlapping so the join produces 0 rows
orders = pd.DataFrame({
    "customer_id": [101, 102, 103],
    "order_date":  ["2024-01-10", "2024-02-15", "2024-03-01"],
})
customers = pd.DataFrame({
    "customer_id": [201, 202, 203],  # different IDs — zero overlap with orders
    "name":        ["Alice", "Dave", "Eve"],
})
result_df = orders.merge(customers, on="customer_id", how="inner")  # genuinely empty

ctx = anchor("diagnose_empty", result_df,
         upstreams={"orders": orders, "customers": customers},
         keys=["customer_id"])
```

---

## Usage

### Basic — empty result with one upstream
```python
ctx = anchor("diagnose_empty", result_df,
         upstreams={"source": source_df})
```

### With multiple upstreams and join keys
```python
ctx = anchor("diagnose_empty", result_df,
         upstreams={"orders": orders_df, "customers": customers_df},
         keys=["customer_id"])
```

### With the filter expression that was applied
```python
ctx = anchor("diagnose_empty", result_df,
         upstreams={"events": events_df},
         keys=["event_id"],
         filter_expr="event_date >= '2024-01-01'")
```

### Label the result
```python
ctx = anchor("diagnose_empty", result_df,
         upstreams={"orders": orders_df, "customers": customers_df},
         keys=["customer_id"],
         subject="monthly_order_summary")
```

### Markdown output
```python
report = anchor("diagnose_empty", result_df,
            upstreams={"orders": orders_df},
            output_format="markdown")
print(report)
```

### Direct Import (no anchor() dispatcher needed)

```python
import sys
import pandas as pd
sys.path.insert(0, "/Workspace/Users/user@example.com/odibi_anchor/src")

from tools.diagnose_empty_tool.diagnose_empty_impl import diagnose_empty_context

orders = pd.DataFrame({
    "customer_id": [101, 102, 103],
    "order_date":  ["2024-01-10", "2024-02-15", "2024-03-01"],
})
customers = pd.DataFrame({
    "customer_id": [201, 202, 203],  # different IDs — zero overlap
    "name":        ["Alice", "Dave", "Eve"],
})
result_df = orders.merge(customers, on="customer_id", how="inner")  # genuinely empty

ctx = diagnose_empty_context(
    result_df,
    upstreams={"orders": orders, "customers": customers},
    keys=["customer_id"],
)
print(ctx["summary"])
print(ctx["metrics"]["dropout_cause"])
print(ctx["suggested_next_actions"])
```

---

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `result_df` | DataFrame | ✓ | — | The empty or sparse output DataFrame to diagnose |
| `upstreams` | dict[str, DataFrame] | ✓ | — | Upstream DataFrames that produced the result. Keys are display names |
| `keys` | list[str] | | `[]` | Join/grain key columns (enables key overlap analysis) |
| `filter_expr` | str | | None | The filter expression that was applied, if any (enables boundary analysis) |
| `subject` | str | | `"output"` | Display name for the result DataFrame |
| `sample_limit` | int | | 10 | Max samples per diagnostic check |
| `output_format` | str | | `"dict"` | `"dict"` or `"markdown"` |

---

## Output

### Key Fields

| Field | Type | What It Tells You |
|-------|------|-------------------|
| `summary` | str | One-line diagnosis: result row count, upstream counts, and identified cause |
| `metrics.result_count` | int | Rows in the result DataFrame |
| `metrics.upstream_counts` | dict | Row counts for each upstream |
| `metrics.dropout_stage` | str | Name of the upstream where most rows disappear |
| `metrics.dropout_cause` | str | Root cause: `"key_overlap"`, `"filter_boundary"`, `"type_mismatch"`, `"null_key"`, `"all_null_column"`, `"unknown"` |
| `funnel` | dict | Row count at each stage (each upstream + the result) |
| `findings` | list[str] | What the tool discovered at each diagnostic step |
| `risks` | list[str] | What will cause the empty result to persist if not fixed |
| `samples.key_overlaps` | list | Per-upstream-pair key overlap stats |
| `samples.type_mismatches` | list | Key columns with different types across upstreams |
| `samples.all_null_columns` | dict | Columns that are entirely null per upstream |
| `samples.filter_analysis` | dict | Per-upstream filter boundary analysis |
| `suggested_next_actions` | list[str] | Concrete fix steps |

### Example Output

```python
ctx = anchor("diagnose_empty", result_df,
         upstreams={"orders": orders_df, "customers": customers_df},
         keys=["customer_id"])

ctx["summary"]
# "EMPTY OUTPUT: result has 0 rows. Upstreams: orders=150,000, customers=42,000.
#  Cause: type_mismatch (stage: orders)."

ctx["metrics"]["dropout_cause"]
# "zero_key_overlap"

ctx["samples"]["type_mismatches"]
# [{"key_column": "customer_id",
#   "types_by_source": {"orders": "LongType", "customers": "StringType"}}]

ctx["suggested_next_actions"]
# ["Cast customer_id to a common type before joining:
#   orders_df.withColumn('customer_id', F.col('customer_id').cast('string'))"]
```

---

## Reading the Output

### What to look for first
1. `summary` — the one-line diagnosis
2. `metrics.dropout_cause` — the root cause category
3. `metrics.dropout_stage` — which upstream is the culprit
4. `suggested_next_actions` — the fix

### Dropout cause categories

| Cause | What It Means | What To Do |
|-------|--------------|------------|
| `key_overlap` | Join keys barely overlap between upstream tables | Check if keys use the same values, are loaded from the same source |
| `filter_boundary` | The filter expression excludes all rows from one or more upstreams | Widen the date range or filter condition; verify upstream freshness |
| `type_mismatch` | Join key columns have different types across upstreams | Cast to a common type before joining |
| `null_key` | Key column is entirely null in one upstream | Fix upstream ingestion that writes null keys |
| `all_null_column` | A non-key column is entirely null, causing a filter or join to eliminate all rows | Fix the upstream write or downstream filter that depends on this column |
| `unknown` | No single cause identified | Check the `funnel` manually and inspect the step with the largest row drop |

### Reading the funnel

The `funnel` shows row counts for each stage:

```python
ctx["funnel"]
# {
#   "orders":    {"count": 150000, "columns": 12},
#   "customers": {"count": 42000,  "columns": 8},
#   "__result__":{"count": 0,      "columns": 18}
# }
```

If `orders` and `customers` both have rows but `__result__` has zero, the problem is in the join or filter between them — not in the source data itself.

---

## Pairs Well With

| If you found... | Then run... | Why |
|-----------------|-------------|-----|
| Low key overlap | `anchor("pre_join", left_df, right_df, keys=["id"])` | Get full join diagnosis including orphan samples and format check |
| Type mismatch on key | `anchor("microscope", upstream_df, "customer_id")` | See actual values to understand what the type difference looks like |
| All-null column | `anchor("case_file", upstream_df, column="col", filter="nulls")` | Confirm the column is fully null and see surrounding context |
| Filter boundary issue | `anchor("microscope", upstream_df, "event_date")` | See the actual date distribution to find the right filter boundary |
| Root cause fixed | Run the pipeline again, then `anchor("profile_table", result_df)` | Confirm the result is now populated and healthy |

---

## Documentation

Detailed documentation is available in the `docs/` folder:

- [Architecture](docs/architecture.md) — Pipeline design, data flow, internal functions, and design decisions
- [Usage Guide](docs/usage.md) — Full parameter reference, calling patterns, result structure, and error handling
- [Code Walkthrough](docs/code_walkthrough.md) — Step-by-step execution traces with 3 worked examples (zero overlap, type mismatch, filter boundary)
