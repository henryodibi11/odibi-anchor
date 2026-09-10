# explain_row -- Code Walkthrough

Step-by-step execution traces showing how the tool traces column lineage.

---

## Worked Example 1: Exact Match + Coerced Value

**Scenario:** Output row has `order_id=123`, `customer_name="ALICE"`, `total=99.99`.
Two upstreams: `orders` has the amount, `customers` has the name (with different case).

### Setup

```python
import pandas as pd

orders = pd.DataFrame({
    "order_id": [123, 124, 125],
    "total": [99.99, 149.50, 200.00],
    "status": ["shipped", "pending", "cancelled"],
})

customers = pd.DataFrame({
    "order_id": [123, 124, 125],
    "customer_name": ["Alice", "Bob", "Carol"],  # lowercase
})

# Output after join + UPPER transform on name
output_df = pd.DataFrame({
    "order_id": [123, 124, 125],
    "customer_name": ["ALICE", "BOB", "CAROL"],  # uppercased
    "total": [99.99, 149.50, 200.00],
    "status": ["shipped", "pending", "cancelled"],
})
```

### Execution Trace

**Step 1: Extract target row** via `_extract_row(output_df, ["order_id"], {"order_id": 123}, "pandas")`

```python
# Boolean mask: output_df["order_id"] == 123
# Returns first matching row as dict:
output_row = {"order_id": 123, "customer_name": "ALICE", "total": 99.99, "status": "shipped"}
```

**Step 2: Trace column origins** via `_trace_column_origins`

For each column in output_row:

- **`order_id`** (join key):
  - Found in `orders`: value=123, `_classify_value_match(123, 123)` -> `"exact"`
  - Found in `customers`: value=123 -> `"exact"`
  - First match wins -> origin="orders"

- **`customer_name`**:
  - NOT in `orders` columns -> skip
  - Found in `customers`: extract row where order_id=123 -> upstream_value="Alice"
  - `_classify_value_match("ALICE", "Alice")`:
    - Direct equality: `"ALICE" == "Alice"` -> False
    - String equality: `str("ALICE") == str("Alice")` -> False
    - Normalized: `"alice" == "alice"` -> True -> `"coerced"`
  - origin="customers", match_type="coerced"

- **`total`**:
  - Found in `orders`: extract row -> upstream_value=99.99
  - `_classify_value_match(99.99, 99.99)` -> `"exact"`
  - origin="orders"

- **`status`**:
  - Found in `orders`: upstream_value="shipped"
  - `"shipped" == "shipped"` -> `"exact"`
  - origin="orders"

**Step 3: Detect join paths**

```python
join_paths = [
    {"upstream": "orders", "join_keys": ["order_id"], "match_count": 1},
    {"upstream": "customers", "join_keys": ["order_id"], "match_count": 1},
]
```

**Step 4: Build metrics**

```python
metrics = {
    "output_columns": 4,
    "columns_traced": 4,
    "columns_untraced": 0,
    "upstream_sources_matched": 2,
    "exact_matches": 3,   # order_id, total, status
    "transformed_values": 0,
    "coerced_values": 1,  # customer_name
    "null_filled_values": 0,
}
```

**Step 5: Build findings**

```python
findings = [
    "Column origins: 3 from orders, 1 from customers",
]
# Note: coerced is not flagged as a risk (it's expected normalization)
```

### Final Output

```python
ctx["column_lineage"][1]  # customer_name
# {"column": "customer_name", "output_value": "ALICE",
#  "origin": "customers", "upstream_value": "Alice",
#  "match_type": "coerced", "join_key": False, ...}
```

---

## Worked Example 2: Null-Filled + Transformed Values

**Scenario:** Output has `status=None` but upstream has `status="Active"` for this key.
Also, `amount` was transformed (different value in output vs upstream).

### Setup

```python
import pandas as pd
import numpy as np

source = pd.DataFrame({
    "id": [1, 2, 3],
    "status": ["Active", "Inactive", "Pending"],
    "amount": [100.0, 200.0, 300.0],
    "notes": [None, None, None],  # all null in source
})

output_df = pd.DataFrame({
    "id": [1, 2, 3],
    "status": [None, "Inactive", "Pending"],  # id=1 status was nulled out
    "amount": [110.0, 200.0, 300.0],          # id=1 amount changed
    "notes": ["System generated", None, None], # id=1 null was filled
})
```

### Execution Trace

**Step 1: Extract row** where id=1:

```python
output_row = {"id": 1, "status": None, "amount": 110.0, "notes": "System generated"}
```

**Step 2: Trace column origins**

- **`id`** (key): source has id=1 -> exact match

- **`status`**: output_value=None, upstream_value="Active"
  - `_is_null(None)` -> True (output is null)
  - `_is_null("Active")` -> False (upstream has value)
  - Classification: output null + upstream non-null -> `"transformed"`
  - (Value was nulled out -- opposite of null_filled)

- **`amount`**: output_value=110.0, upstream_value=100.0
  - `110.0 == 100.0` -> False
  - `str(110.0) == str(100.0)` -> False ("110.0" != "100.0")
  - Normalized also different
  - Classification: `"transformed"`

- **`notes`**: output_value="System generated", upstream_value=None
  - `_is_null(None)` -> True (upstream is null)
  - `_is_null("System generated")` -> False (output has value)
  - Classification: upstream null + output non-null -> `"null_filled"`

**Step 3: Build findings**

```python
findings = [
    "status: output=None but source has 'Active' -- value was transformed or overwritten",
    "amount: output=110.0 but source has 100.0 -- value was transformed or overwritten",
    "notes: output='System generated' but source has NULL -- value was filled (COALESCE or default)",
    "Column origins: 4 from source",
]
```

**Step 4: Build risks**

```python
risks = [
    "status value mismatch ('Active'->None): investigate transformation logic or check if wrong upstream row was joined",
    "amount value mismatch (100.0->110.0): investigate transformation logic...",
    "notes null-fill: verify the default 'System generated' is correct business logic",
]
```

### Final Output

```python
ctx["metrics"]
# {"output_columns": 4, "columns_traced": 4, "columns_untraced": 0,
#  "exact_matches": 1, "transformed_values": 2, "null_filled_values": 1, ...}

ctx["suggested_next_actions"]
# ["Investigate status transformation: anchor('diff', source_df, output_df, keys=['id'], columns=['status'])",
#  "Check for duplicate upstream keys: anchor('pre_join', output_df, source_df, keys=['id'])",
#  "Profile the suspicious column: anchor('microscope', output_df, 'status')", ...]
```

---

## Key Python Patterns

### 1. Null Detection with math.isnan

```python
def _is_null(value):
    if value is None:
        return True
    try:
        import math
        if isinstance(value, float) and math.isnan(value):
            return True
    except (TypeError, ValueError):
        pass
    return False
```

Handles Python None, numpy NaN, and pandas NaT edge cases.

### 2. Multi-Level Value Classification

```python
def _classify_value_match(output_value, upstream_value):
    # 1. Both null -> exact
    # 2. One null -> null_filled or transformed
    # 3. Direct equality -> exact
    # 4. String equality -> coerced
    # 5. Normalized (strip+lower) equality -> coerced
    # 6. Otherwise -> transformed
```

The cascade ensures the most specific match type is returned.

### 3. First-Match-Wins with Full History

```python
for upstream_name, upstream_df in upstream.items():
    # ... extract and classify ...
    col_result["all_sources"].append(source_info)  # always track all
    if col_result["origin"] is None:               # but first wins primary
        col_result["origin"] = upstream_name
```

This means upstream dict ordering matters -- put the "expected" source first.

### 4. Row Extraction (Pandas Path)

```python
mask = pd.Series([True] * len(df), index=df.index)
for key in keys:
    mask = mask & (df[key] == values[key])
matched = df[mask]
return matched.iloc[0] if not matched.empty else None
```

Starts with all-True mask, progressively narrows by each key.

### 5. Join Path Detection

Separate from lineage tracing -- tells you HOW upstreams connect:

```python
for upstream_name, upstream_df in upstream.items():
    matching_keys = _find_matching_keys(upstream_df, keys, {}, engine)
    # Test if those keys actually match a row
    matched_row = _extract_row(upstream_df, matching_keys, key_values, engine)
    paths.append({"upstream": upstream_name, "join_keys": matching_keys, "match_count": 1 if matched_row else 0})
```

### 6. Contract Assembly

```python
ctx = build_base_context(
    kind="explain_row",
    subject=subject,
    summary=summary,
    metrics=metrics,
    findings=findings, risks=risks, samples=samples,
    suggested_next_actions=suggested_next_actions,
    column_lineage=column_lineage,  # extra field
    join_paths=join_paths,          # extra field
)
return finalize_context(ctx, output_format, render_explain_row_report)
```