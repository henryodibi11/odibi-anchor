# diagnose_empty Code Walkthrough

This document traces through the tool execution with concrete worked examples showing how each internal function produces its output.

---

## Worked Example 1: Zero Key Overlap

**Scenario:** An inner join between orders and products produces 0 rows because the key columns have completely different value ranges.

### Setup

```python
import pandas as pd

orders = pd.DataFrame({"product_id": [1, 2, 3, 4, 5], "quantity": [10, 20, 30, 40, 50]})
products = pd.DataFrame({"product_id": [101, 102, 103, 104, 105], "name": ["Widget", "Gadget", "Doohickey", "Thingamajig", "Whatchamacallit"]})
result_df = orders.merge(products, on="product_id", how="inner")  # 0 rows
```

### Execution Trace

**Step 1: _detect_engine** -- Both are Pandas DataFrames -> returns "pandas".

**Step 2: _build_row_count_funnel** -- Counts rows in each upstream + result.

```python
funnel = {
    "orders":     {"count": 5, "columns": 2, "engine": "pandas"},
    "products":   {"count": 5, "columns": 2, "engine": "pandas"},
    "__result__": {"count": 0, "columns": 3, "engine": "pandas"},
}
```

**Step 3: _key_overlap_analysis** -- keys=["product_id"] with 2 upstreams triggers pairwise overlap via _key_overlap_pandas:

```python
set_a = set(orders["product_id"].dropna().unique())    # {1, 2, 3, 4, 5}
set_b = set(products["product_id"].dropna().unique())  # {101, 102, 103, 104, 105}
overlap = set_a & set_b        # {} empty
only_a = set_a - set_b         # {1, 2, 3, 4, 5}
only_b = set_b - set_a         # {101, 102, 103, 104, 105}
overlap_pct = 0 / 10           # 0.0
```

Result: overlap_count=0, overlap_pct=0.0, sample_only_a=[1,2,3,4,5], sample_only_b=[101,102,103,104,105]

**Step 4: _filter_boundary_analysis** -- Skipped (no filter_expr).

**Step 5: _all_null_columns** -- No all-null columns in either upstream.

**Step 6: _type_mismatch_detection** -- Both have product_id as int64. No mismatch.

**Step 7: _rank_causes** -- Priority evaluation:
- Priority 1 (type mismatch): No
- Priority 2 (zero overlap): YES -> overlap_count==0

```python
dropout_stage = "join:orders<->products"
dropout_cause = "zero_key_overlap"
findings = ["Zero key overlap between orders<->products (A=5, B=5)"]
```

**Step 8: _build_suggestions** -- Generates actionable next steps including anchor('pre_join') reference.

### Final Output

```python
ctx["metrics"]["dropout_cause"]   # "zero_key_overlap"
ctx["metrics"]["dropout_stage"]   # "join:orders<->products"
ctx["samples"]["key_overlaps"][0]["overlap_pct"]  # 0.0
```

---

## Worked Example 2: Type Mismatch on Join Key

**Scenario:** orders.customer_id is int, customers.customer_id is string. Join produces 0 rows because 123 != "123" in set operations.

### Setup

```python
import pandas as pd

orders = pd.DataFrame({"customer_id": [101, 102, 103, 104, 105], "amount": [99.99, 149.50, 200.00, 75.25, 310.00]})
customers = pd.DataFrame({"customer_id": ["101", "102", "103", "104", "105"], "name": ["Alice", "Bob", "Carol", "Dave", "Eve"]})
result_df = orders.merge(customers, on="customer_id", how="inner")  # 0 rows due to type mismatch
```

### Execution Trace

**Step 1: Funnel** -- orders=5, customers=5, result=0.

**Step 2: _type_mismatch_detection**:

```python
types_found = {"orders": "int64", "customers": "object"}
unique_types = {"int64", "object"}  # len > 1 -> MISMATCH
```

Result: [{"key_column": "customer_id", "types_by_source": {"orders": "int64", "customers": "object"}, "unique_types": ["int64", "object"]}]

**Step 3: _rank_causes** -- Priority 1 (type mismatch) fires FIRST (before key overlap):

```python
dropout_stage = "type_mismatch:customer_id"
dropout_cause = "key_type_mismatch"
findings = ["Type mismatch on key 'customer_id': orders=int64, customers=object"]
```

**Step 4: _build_suggestions**:

```python
suggested_next_actions = [
    "Fix type mismatch: cast 'customer_id' to a common type before joining (e.g., .withColumn(...))",
    "Verify with: anchor('pre_join', orders_df, customers_df, keys=['customer_id'])",
]
```

### Final Output

```python
ctx["metrics"]["dropout_cause"]  # "key_type_mismatch"
ctx["samples"]["type_mismatches"][0]["types_by_source"]
# {"orders": "int64", "customers": "object"}
```

Key insight: type mismatch is Priority 1 because it causes BOTH a type issue AND zero overlap. Reporting type mismatch is more actionable than reporting zero overlap.

---

## Worked Example 3: Filter Kills All Rows

**Scenario:** A date filter eliminates all rows because data only contains 2023 dates but filter asks for 2024+.

### Setup

```python
import pandas as pd

events = pd.DataFrame({
    "event_id": range(1, 101),
    "event_date": pd.date_range("2023-01-01", periods=100, freq="D"),
    "type": ["click"] * 50 + ["view"] * 50,
})
result_df = events.query("event_date >= '2024-01-01'")  # 0 rows
```

### Execution Trace

**Step 1: Funnel** -- events=100, result=0.

**Step 2: _filter_boundary_analysis** -- Applies filter_expr to each upstream:

```python
# events.query("event_date >= '2024-01-01'") returns 0 rows
results["per_upstream"]["events"] = {
    "total": 100,
    "passing": 0,
    "pass_pct": 0.0,
    "all_fail": True,
    "error": None,
}
```

**Step 3: _rank_causes** -- Priority 3 (filter_kills_all) triggers:

```python
dropout_stage = "filter:events"
dropout_cause = "filter_kills_all"
findings = ["Filter 'event_date >= '2024-01-01'' matches 0 of 100 rows in 'events'"]
```

### Final Output

```python
ctx["metrics"]["dropout_cause"]  # "filter_kills_all"
ctx["samples"]["filter_analysis"]["per_upstream"]["events"]["all_fail"]  # True
```

---

## Key Python Patterns

### 1. Dual-Engine Detection (Inlined)

Tools are standalone -- no imports from src/_utils. Detection checks isinstance first, falls back to module path:

```python
def _detect_engine(df):
    try:
        import pandas as pd
        if isinstance(df, pd.DataFrame): return "pandas"
    except Exception: pass
    try:
        from pyspark.sql import DataFrame as SparkDF
        if isinstance(df, SparkDF): return "spark"
    except Exception: pass
    mod = getattr(type(df), "__module__", "") or ""
    if "pyspark.sql" in mod: return "spark"
    if "pandas" in mod: return "pandas"
    return "unknown"
```

### 2. Set Intersection for Key Overlap (Pandas)

```python
set_a = set(df_a[key_col].dropna().unique())
set_b = set(df_b[key_col].dropna().unique())
overlap = set_a & set_b
# Multi-column keys use tuples:
set_a = set(df_a[keys].dropna().apply(tuple, axis=1).unique())
```

### 3. Spark Key Overlap via Native Set Operations

```python
distinct_a = df_a.select(keys).dropna().distinct()
distinct_b = df_b.select(keys).dropna().distinct()
overlap_df = distinct_a.intersect(distinct_b)
only_a_df = distinct_a.subtract(distinct_b)
```

### 4. Batch Null Detection (Spark)

Single-pass count of non-null values per column:

```python
exprs = [F.count(F.col(c)).alias(c) for c in cols]
counts = df.select(exprs).collect()[0]
for col in cols:
    if counts[col] == 0: null_cols.append(col)
```

### 5. Priority-Ranked Early Return

```python
if type_mismatches:
    return stage, "key_type_mismatch", findings  # Priority 1
for overlap in key_overlaps:
    if overlap.get("overlap_count") == 0:
        return stage, "zero_key_overlap", findings  # Priority 2
# ... through Priority 6
```

### 6. Contract Assembly via Anchor Helpers

```python
ctx = build_base_context(kind="diagnose_empty", subject=subject, summary=summary,
    metrics=metrics, findings=findings, risks=risks, samples=samples,
    suggested_next_actions=suggestions, funnel=funnel)
return finalize_context(ctx, output_format, _render_markdown)
```