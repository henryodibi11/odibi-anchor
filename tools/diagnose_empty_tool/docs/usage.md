# diagnose_empty — Usage Guide

## Function Signature

```python
diagnose_empty_context(
    result_df,                          # The empty/sparse output DataFrame
    upstreams: dict[str, Any],          # {"name": df, ...} upstream sources
    keys: list[str] | None = None,     # Join/grain key columns
    filter_expr: str | None = None,    # Filter expression applied (if any)
    subject: str = "output",           # Display name for the result
    output_format: str = "dict",       # "dict" or "markdown"
    sample_limit: int = 10,            # Max samples per diagnostic check
) -> dict | str
```

---

## Parameter Reference

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `result_df` | DataFrame (Pandas or Spark) | Yes | — | The output DataFrame to diagnose (typically empty or with fewer rows than expected) |
| `upstreams` | `dict[str, DataFrame]` | Yes | — | Dict mapping display names to upstream DataFrames. At minimum one upstream is required. Keys are used for labeling in output. |
| `keys` | `list[str]` | No | `[]` | Join/grain key columns. Enables key overlap and type mismatch analysis when provided. Must exist in at least 2 upstreams for overlap analysis. |
| `filter_expr` | `str` | No | `None` | The filter expression that was applied to produce the result. Used for boundary analysis. For Spark: passed to `.where()`. For Pandas: passed to `.query()` with `.eval()` fallback. |
| `subject` | `str` | No | `"output"` | Human-readable name for the result DataFrame. Appears in summary and markdown heading. |
| `output_format` | `str` | No | `"dict"` | `"dict"` returns the full context dict. `"markdown"` returns a rendered markdown string. |
| `sample_limit` | `int` | No | `10` | Maximum number of sample values to return per diagnostic (orphan keys, etc.). Controls memory usage on large datasets. |

---

## Calling via anchor() Dispatcher

```python
# Simplest form
ctx = anchor("diagnose_empty", result_df, upstreams={"source": source_df})

# With keys for join analysis
ctx = anchor("diagnose_empty", result_df,
         upstreams={"orders": orders_df, "customers": customers_df},
         keys=["customer_id"])

# With filter boundary check
ctx = anchor("diagnose_empty", result_df,
         upstreams={"events": events_df},
         keys=["event_id"],
         filter_expr="event_date >= '2024-01-01'")

# Named subject + markdown output
report = anchor("diagnose_empty", result_df,
            upstreams={"orders": orders_df, "customers": customers_df},
            keys=["customer_id"],
            subject="monthly_summary",
            output_format="markdown")
```

---

## Upstreams Format

The `upstreams` dict maps **display names** to DataFrames:

```python
upstreams = {
    "orders": orders_df,        # First upstream
    "customers": customers_df,  # Second upstream
}
```

Rules:
- At least one upstream is required (ValueError if empty)
- Names appear in the funnel, findings, and suggestions
- For key overlap analysis, at least 2 upstreams are needed
- Mixed engines (one Pandas, one Spark) are supported — the Spark side is converted to Pandas on key columns only

---

## Result Structure

| Field | Type | Description |
|-------|------|-------------|
| `kind` | `str` | Always `"diagnose_empty"` |
| `subject` | `str` | The display name provided |
| `summary` | `str` | One-line diagnosis with row counts, upstream counts, and identified cause |
| `metrics.result_count` | `int` | Row count of the result DataFrame |
| `metrics.result_column_count` | `int` | Number of columns in the result |
| `metrics.upstream_counts` | `dict[str, int]` | Row count per upstream |
| `metrics.dropout_stage` | `str` | Stage identifier where rows disappeared (e.g., `"join:orders↔customers"`) |
| `metrics.dropout_cause` | `str` | Root cause category (see table below) |
| `funnel` | `dict` | Row count at each stage: `{"upstream_name": {"count": N, "columns": M}, "__result__": {...}}` |
| `findings` | `list[str]` | Diagnostic findings in priority order |
| `risks` | `list[str]` | Risk statements about the identified cause |
| `samples.key_overlaps` | `list[dict]` | Per-pair overlap stats (distinct counts, overlap %, orphan samples) |
| `samples.type_mismatches` | `list[dict]` | Key columns with different types across upstreams |
| `samples.all_null_columns` | `dict[str, list]` | Upstream name → list of all-null column names |
| `samples.filter_analysis` | `dict` | Per-upstream filter pass/fail with counts |
| `suggested_next_actions` | `list[str]` | Actionable fix suggestions |

---

## Dropout Cause Reference

| Cause | Meaning | Typical Fix |
|-------|---------|-------------|
| `key_type_mismatch` | Join key has different types across upstreams | Cast key to common type before joining |
| `zero_key_overlap` | Key values in upstream A and B have 0 intersection | Verify key source, check date/partition filtering |
| `filter_kills_all` | The filter expression matches 0 rows in an upstream | Widen filter or fix data freshness |
| `key_columns_all_null` | A key column is 100% NULL in at least one upstream | Fix upstream ingestion or schema mapping |
| `empty_upstream` | An upstream has 0 rows (source is empty) | Check source system or preceding pipeline stage |
| `undetermined` | No single cause detected | Add intermediate stages as upstreams to narrow down |

---

## Common Patterns

### Pattern 1: Empty join result

The most common case — pass the empty join output plus both source DataFrames:

```python
result = orders.merge(customers, on="customer_id", how="inner")
# result is empty!

ctx = anchor("diagnose_empty", result,
         upstreams={"orders": orders, "customers": customers},
         keys=["customer_id"])

print(ctx["metrics"]["dropout_cause"])  # e.g. "zero_key_overlap"
```

### Pattern 2: Empty after filter

```python
filtered = events_df.where("event_date >= '2024-06-01'")
# filtered is empty!

ctx = anchor("diagnose_empty", filtered,
         upstreams={"events": events_df},
         filter_expr="event_date >= '2024-06-01'")

print(ctx["metrics"]["dropout_cause"])  # e.g. "filter_kills_all"
```

### Pattern 3: Multi-upstream pipeline

```python
ctx = anchor("diagnose_empty", final_output,
         upstreams={
             "raw_orders": raw_orders_df,
             "dim_customers": dim_customers_df,
             "dim_products": dim_products_df,
         },
         keys=["order_id", "customer_id"])
```

### Pattern 4: Follow-up after diagnosis

```python
# Diagnosed: zero_key_overlap
ctx = anchor("diagnose_empty", result, upstreams={...}, keys=["id"])

# Follow up with pre_join for detailed key analysis
pj = anchor("pre_join", orders_df, customers_df, keys=["customer_id"])

# Or microscope for column-level detail
ms = anchor("microscope", orders_df, "customer_id")
```

---

## Error Handling

| Error | Cause | Fix |
|-------|-------|-----|
| `ValueError: result_df is required` | First arg is None | Pass the DataFrame as first positional arg or `result_df=` kwarg |
| `ValueError: upstreams dict is required` | upstreams is None or empty | Provide at least one upstream: `upstreams={"source": df}` |
| `ValueError: Invalid output_format` | output_format not in `{"dict", "markdown"}` | Use `"dict"` or `"markdown"` |
| `TypeError` from `guard_dataframe_type` | result_df is not a Pandas or Spark DataFrame | Ensure you pass an actual DataFrame, not a list or dict |

---

## Tips

- **Always provide keys** when the empty result comes from a join — without keys, overlap and type mismatch checks are skipped
- **Provide filter_expr as a string** exactly as it was applied — the tool re-applies it to test boundaries
- **Use meaningful upstream names** — they appear in findings and suggestions, making diagnosis readable
- **Check `suggested_next_actions` first** — it gives concrete anchor() commands to run next
- **For Spark**: filter_expr is passed directly to `.where()`, so use Spark SQL syntax
- **For Pandas**: filter_expr is passed to `.query()` with `.eval()` fallback, so use pandas query syntax