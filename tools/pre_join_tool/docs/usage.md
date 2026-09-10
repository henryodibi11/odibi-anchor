# pre_join — Usage Guide

## Function Signature

```python
pre_join_context(
    left_df,                              # Left DataFrame (positional)
    right_df,                             # Right DataFrame (positional)
    keys: list[str] | None = None,       # Join keys (same name both sides)
    left_keys: list[str] | None = None,  # Left key columns (when names differ)
    right_keys: list[str] | None = None, # Right key columns (when names differ)
    left_subject: str = "left",          # Display name for left side
    right_subject: str = "right",        # Display name for right side
    output_format: str = "dict",         # "dict" or "markdown"
    sample_limit: int = 10,              # Max orphan/fanout samples
) -> dict | str
```

---

## Parameter Reference

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `left_df` | DataFrame | Yes | -- | Left side of the join (fact table, primary) |
| `right_df` | DataFrame | Yes | -- | Right side of the join (dimension, lookup) |
| `keys` | `list[str]` | Yes* | -- | Key columns when names match in both DataFrames |
| `left_keys` | `list[str]` | Yes* | -- | Left key columns when names differ from right |
| `right_keys` | `list[str]` | Yes* | -- | Right key columns when names differ from left |
| `left_subject` | `str` | No | `"left"` | Display name for left DataFrame in report |
| `right_subject` | `str` | No | `"right"` | Display name for right DataFrame in report |
| `output_format` | `str` | No | `"dict"` | `"dict"` for programmatic, `"markdown"` for report |
| `sample_limit` | `int` | No | `10` | Max orphan keys and fanout keys to show |

\* Either `keys` OR both `left_keys` + `right_keys` required. Not all three.

---

## Calling via anchor() Dispatcher

```python
# Same key name both sides
ctx = anchor("pre_join", orders_df, customers_df, keys=["customer_id"])

# Different key names (asymmetric)
ctx = anchor("pre_join", fact_df, dim_df,
         left_keys=["cust_id"], right_keys=["customer_id"],
         left_subject="fact_queue", right_subject="dim_customer")

# Multi-column composite key
ctx = anchor("pre_join", left_df, right_df,
         keys=["project_id", "period"])

# Markdown report
report = anchor("pre_join", df1, df2, keys=["id"], output_format="markdown")
print(report)
```

---

## Key Resolution Logic

```python
# Option 1: keys= (same name both sides)
anchor("pre_join", df1, df2, keys=["customer_id"])
# internally: left_keys=["customer_id"], right_keys=["customer_id"]

# Option 2: left_keys + right_keys (different names)
anchor("pre_join", df1, df2,
   left_keys=["cust_id"], right_keys=["customer_id"])

# String shorthand (single key)
anchor("pre_join", df1, df2, keys="customer_id")  # auto-wrapped to list
```

Rules:
- `left_keys` and `right_keys` must have equal length
- Each key column must exist in its respective DataFrame
- Providing both `keys` and `left_keys`/`right_keys` uses `keys` (it takes precedence)

---

## Result Structure

| Field | Type | Description |
|-------|------|-------------|
| `kind` | `str` | Always `"pre_join"` |
| `subject` | `str` | `"left_subject -> right_subject"` |
| `summary` | `str` | One-line verdict: "OK: INNER JOIN safe..." or "WARNING: ..." |
| `metrics.left_count` | `int` | Total left rows |
| `metrics.right_count` | `int` | Total right rows |
| `metrics.overlap_count` | `int` | Distinct keys present in both sides |
| `metrics.overlap_pct` | `float` | overlap / left_distinct |
| `metrics.matching_left_rows` | `int` | Actual left rows that will survive INNER JOIN |
| `metrics.matching_left_rows_pct` | `float` | Survival rate |
| `metrics.left_orphan_count` | `int` | Left keys with no right match |
| `metrics.right_orphan_count` | `int` | Right keys unused |
| `metrics.cardinality` | `str` | `"1:1"` \| `"1:many"` \| `"many:1"` \| `"many:many"` |
| `metrics.right_rows_per_key_avg` | `float` | Average duplicates per right key |
| `metrics.right_rows_per_key_max` | `int` | Maximum duplicates for any single right key |
| `metrics.predicted_rows` | `int` | Expected output row count after join |
| `metrics.fanout_ratio` | `float` | Expansion multiplier |
| `metrics.fanout_risk` | `str` | `"none"` \| `"low"` \| `"medium"` \| `"high"` \| `"extreme"` |
| `metrics.left_null_key_count` | `int` | Null values in left key columns |
| `metrics.format_compatible` | `bool` | Whether keys have format issues |
| `samples.left_orphan_keys` | `list` | Sample keys missing from right |
| `samples.right_orphan_keys` | `list` | Sample keys missing from left |
| `samples.max_fanout_keys` | `list` | Keys with highest right-side duplicates |
| `findings` | `list[str]` | Observations about orphans, cardinality, nulls, format |
| `risks` | `list[str]` | Actionable warnings (null drops, fanout, format) |
| `suggested_next_actions` | `list[str]` | anchor() commands for follow-up investigation |

---

## Common Patterns

### Pattern 1: Quick safety check before join

```python
ctx = anchor("pre_join", orders, customers, keys=["customer_id"])

if ctx["metrics"]["fanout_risk"] in ("high", "extreme"):
    print("STOP: join will explode rows")
    print(f"Predicted: {ctx['metrics']['predicted_rows']:,} rows")
else:
    result = orders.merge(customers, on="customer_id")
```

### Pattern 2: Diagnose why joined result has wrong row count

```python
# "My join produced 5M rows but I expected 1M"
ctx = anchor("pre_join", fact_df, dim_df, keys=["project_id"],
         left_subject="fact", right_subject="dim")
print(ctx["metrics"]["cardinality"])      # "many:many" -- that's the problem
print(ctx["samples"]["max_fanout_keys"])  # Shows which keys have most dups
```

### Pattern 3: Asymmetric keys with meaningful names

```python
ctx = anchor("pre_join", queue_df, project_dim_df,
         left_keys=["proj_id"], right_keys=["project_id"],
         left_subject="interconnection_queue",
         right_subject="project_dimension")

# Check if key format differences explain low overlap
if not ctx["metrics"]["format_compatible"]:
    print("Format issues:", ctx["samples"]["format_mismatch_examples"])
```

### Pattern 4: Use findings to decide join type

```python
ctx = anchor("pre_join", left, right, keys=["id"])

if ctx["metrics"]["left_orphan_count"] > 0:
    # LEFT JOIN to preserve orphans
    result = left.merge(right, on="id", how="left")
else:
    # INNER JOIN safe
    result = left.merge(right, on="id", how="inner")
```

---

## Error Handling

| Error | Cause | Fix |
|-------|-------|-----|
| `ValueError: pre_join requires both left_df and right_df` | Missing DataFrame arg | Pass both as positional or keyword |
| `ValueError: pre_join requires either 'keys' or both 'left_keys' and 'right_keys'` | No key specification | Add `keys=["col"]` |
| `ValueError: left_keys and right_keys must have same length` | Mismatched key lists | Ensure same number of key columns each side |
| `ValueError: Left DataFrame missing key columns: [...]` | Key not in left columns | Check spelling; available columns shown in error |
| `ValueError: Right DataFrame missing key columns: [...]` | Key not in right columns | Check spelling |
| `ValueError: Cannot detect engine` | Not a Pandas/Spark DataFrame | Pass a real DataFrame |

---

## Tips

- **Check `fanout_risk` before any join** — "extreme" means the join will produce orders of magnitude more rows than expected
- **`matching_left_rows` is more accurate than `overlap_pct`** — overlap is by distinct keys, but matching_left_rows counts actual rows (important when left has non-uniform duplication)
- **Look at `samples.left_orphan_keys`** to understand WHY overlap is low (format issues? wrong keys? data subset?)
- **Use `left_subject`/`right_subject`** for readable reports when checking multiple joins
- **Pairs well with `anchor("microscope", right_df, "key_col")`** to investigate duplicate keys on the right side
- **Format issues cause silent drops** — if `format_compatible` is False, clean keys with TRIM/LOWER before joining