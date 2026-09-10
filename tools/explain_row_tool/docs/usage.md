# explain_row — Usage Guide

## Function Signature

```python
explain_row_context(
    output_df,                         # DataFrame containing the row to trace
    keys: list[str] | None = None,    # Key column(s) to identify the row
    values: dict | None = None,       # Key values to locate the row
    upstream: dict | None = None,     # {"name": df, ...} upstream sources
    output_format: str = "dict",      # "dict" or "markdown"
    sample_limit: int = 5,            # Max upstream matches per column
) -> dict | str
```

---

## Parameter Reference

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `output_df` | DataFrame (Pandas or Spark) | Yes | -- | The result DataFrame containing the row you want to trace |
| `keys` | `list[str]` | Yes | -- | Key column(s) used to identify the specific row (must exist in output_df) |
| `values` | `dict` | Yes | -- | Key values that uniquely identify the row: `{"col": value}`. Must contain all keys. |
| `upstream` | `dict[str, DataFrame]` | Yes | -- | Dict mapping display names to upstream DataFrames to search through |
| `output_format` | `str` | No | `"dict"` | `"dict"` for programmatic access, `"markdown"` for rendered report |
| `sample_limit` | `int` | No | `5` | Maximum upstream matches to show per column in `all_sources` |

---

## Calling via anchor() Dispatcher

```python
# Basic: trace a specific row
ctx = anchor("explain_row", output_df,
         keys=["order_id"],
         values={"order_id": 12345},
         upstream={"orders": orders_df, "customers": customers_df})

# Multi-key lookup
ctx = anchor("explain_row", output_df,
         keys=["project_id", "date"],
         values={"project_id": "PROJ-100", "date": "2024-03-15"},
         upstream={"raw": raw_df, "dim": dim_df})

# Markdown output
report = anchor("explain_row", output_df,
            keys=["id"],
            values={"id": 42},
            upstream={"source": src_df},
            output_format="markdown")
```

---

## Values Format

The `values` dict identifies which row to trace:

```python
# Single key
values = {"order_id": 12345}

# Composite key
values = {"project_id": "PROJ-100", "period": "2024-Q1"}
```

Rules:
- Must contain ALL keys listed in `keys` parameter (ValueError if missing)
- Values must match exactly (no fuzzy matching for row lookup)
- If multiple rows match, the first is used

---

## Result Structure

| Field | Type | Description |
|-------|------|-------------|
| `kind` | `str` | Always `"explain_row"` |
| `subject` | `str` | Key=value description of the traced row |
| `summary` | `str` | One-line: how many sources, columns traced, transforms found |
| `metrics.output_columns` | `int` | Total columns in the output row |
| `metrics.columns_traced` | `int` | Columns successfully matched to an upstream |
| `metrics.columns_untraced` | `int` | Columns with no upstream match |
| `metrics.upstream_sources_matched` | `int` | How many upstreams contributed columns |
| `metrics.exact_matches` | `int` | Columns with identical values in upstream |
| `metrics.transformed_values` | `int` | Columns where upstream value differs |
| `metrics.coerced_values` | `int` | Columns matching after normalization |
| `metrics.null_filled_values` | `int` | Columns where upstream was null but output has value |
| `column_lineage` | `list[dict]` | Per-column origin trace (see below) |
| `join_paths` | `list[dict]` | How each upstream connects via keys |
| `samples.output_row` | `dict` | The actual output row values |
| `samples.upstream_rows` | `dict` | Matched rows from each upstream |
| `findings` | `list[str]` | Observations about transforms, nulls, alternatives |
| `risks` | `list[str]` | Warnings about value mismatches |
| `suggested_next_actions` | `list[str]` | anchor() commands to investigate further |

### Column Lineage Entry

```python
{
    "column": "customer_name",
    "output_value": "ALICE SMITH",
    "origin": "customers",           # which upstream
    "upstream_value": "Alice Smith",  # value in that upstream
    "match_type": "coerced",         # how they relate
    "join_key": False,
    "all_sources": [...]             # all upstreams with this column
}
```

---

## Common Patterns

### Pattern 1: Investigating unexpected value in output

```python
# "Why does this row have status='CANCELLED' when I expected 'ACTIVE'?"
ctx = anchor("explain_row", result_df,
         keys=["order_id"],
         values={"order_id": 5678},
         upstream={"orders": orders_df, "status_updates": updates_df})

# Check where status came from
for c in ctx["column_lineage"]:
    if c["column"] == "status":
        print(f"Origin: {c['origin']}, upstream had: {c['upstream_value']}, match: {c['match_type']}")
```

### Pattern 2: Tracing after a join pipeline

```python
# Output is result of joining 3 tables -- trace where each column came from
ctx = anchor("explain_row", final_output,
         keys=["project_id"],
         values={"project_id": "PROJ-2847"},
         upstream={
             "raw_projects": raw_df,
             "dim_customers": cust_df,
             "fact_metrics": metrics_df,
         })

print(ctx["summary"])  # "Row traced through 3 upstream source(s)..."
```

### Pattern 3: Follow-up investigation

```python
ctx = anchor("explain_row", output_df, keys=["id"], values={"id": 42},
         upstream={"source": src_df})

# If columns are transformed, investigate with diff
if ctx["metrics"]["transformed_values"] > 0:
    diff = anchor("diff", src_df, output_df, keys=["id"])
```

---

## Error Handling

| Error | Cause | Fix |
|-------|-------|-----|
| `ValueError: output_df is required` | First arg is None | Pass DataFrame as first arg or kwarg |
| `ValueError: keys is required` | keys not provided | Provide `keys=["id"]` |
| `ValueError: values is required` | values not provided | Provide `values={"id": 123}` |
| `ValueError: upstream is required` | upstream is None or not a dict | Provide `upstream={"name": df}` |
| `ValueError: values dict is missing keys` | values doesn't have all keys | Ensure all keys appear in values dict |
| `ValueError: Row not found` | No row matches key=value in output_df | Verify values match actual data |
| `TypeError: Cannot detect engine` | output_df is not Pandas/Spark | Pass a real DataFrame |

---

## Tips

- **Check `column_lineage` with `match_type="transformed"`** -- these are the interesting columns where values changed
- **Use `all_sources`** to see if an exact match exists in a different upstream (the tool reports this in findings)
- **The tool traces backward** from output to upstreams -- it doesn't show what transformations were applied, just that the value changed
- **For forward tracing** ("what will happen to this source row?"), use `pre_join` instead
- **Pairs well with `anchor("diff")` and `anchor("microscope")`** for deeper investigation of transformed columns