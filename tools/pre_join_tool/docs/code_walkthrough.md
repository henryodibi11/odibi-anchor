# pre_join -- Code Walkthrough

Step-by-step execution traces showing how the tool analyzes join safety.

---

## Worked Example 1: Safe 1:1 Join

**Scenario:** Orders table (1000 rows, unique customer_id) joining to customers
table (500 rows, unique customer_id). Most keys overlap.

### Setup

```python
import pandas as pd
import numpy as np

# 1000 orders with unique customer_ids 1-1000
orders = pd.DataFrame({
    "customer_id": range(1, 1001),
    "amount": np.random.uniform(10, 500, 1000),
})

# 500 customers with ids 1-500 (subset of order ids)
customers = pd.DataFrame({
    "customer_id": range(1, 501),
    "name": [f"Customer_{i}" for i in range(1, 501)],
})
```

### Execution Trace

**Step 1: Build composite keys**

```python
left_key_series = orders["customer_id"].fillna("__NULL__").astype(str)
# Series: ["1", "2", "3", ..., "1000"]

right_key_series = customers["customer_id"].fillna("__NULL__").astype(str)
# Series: ["1", "2", "3", ..., "500"]
```

Single-column key, so no concatenation needed.

**Step 2: Key overlap analysis**

```python
left_key_set = {"1", "2", ..., "1000"}   # 1000 distinct
right_key_set = {"1", "2", ..., "500"}    # 500 distinct

overlap_keys = left_key_set & right_key_set  # {"1".."500"}
overlap_count = 500
overlap_pct = 500 / 1000 = 0.5  # 50% of left keys found in right

left_orphan_keys = {"501".."1000"}  # 500 left keys with no right match
right_orphan_keys = set()           # 0 right keys unused
```

**Step 3: Cardinality detection**

```python
left_key_distinct = 1000
right_key_distinct = 500
left_count = 1000
right_count = 500

# left_count (1000) > left_key_distinct (1000)? No
# right_count (500) > right_key_distinct (500)? No
# Result: "1:1"
```

**Step 4: Null key audit**

```python
# No nulls in either key column
left_null_key_count = 0
right_null_key_count = 0
```

**Step 5: Predict output size**

```python
matching_left_rows = 500  # rows where customer_id is in {1..500}
# cardinality is "1:1" -> predicted = matching_left_rows
predicted_rows = 500
fanout_ratio = 500 / 1000 = 0.5
# 0.5 <= 1.05 -> fanout_risk = "none"
```

**Step 6: Format compatibility**

```python
# Sample 50 key values from each side: ["1", "2", ..., "50"]
# Whitespace check: no values have leading/trailing space
# Case check: numeric strings, no case variation
# Unicode check: no zero-width chars
# Result: format_compatible = True
```

**Step 7: Build contract**

```python
findings = [
    "500 left keys (50.0%) have no match in right -- use LEFT JOIN to preserve",
    "Cardinality is 1:1 -- clean join expected",
]
risks = []  # No nulls, no fanout, format OK
summary = "OK: LEFT JOIN recommended, 1:1 cardinality, 50.0% overlap"

suggested_next_actions = [
    "Use LEFT JOIN to preserve 500 orphan rows: left_df.join(right_df, 'customer_id', 'left')",
    "Investigate orphans: anchor('case_file', left_df, column='customer_id', filter='where:...')",
]
```

### Final Output

```python
ctx["metrics"]["cardinality"]       # "1:1"
ctx["metrics"]["fanout_risk"]       # "none"
ctx["metrics"]["predicted_rows"]    # 500
ctx["metrics"]["overlap_pct"]       # 0.5
ctx["metrics"]["left_orphan_count"] # 500
```

---

## Worked Example 2: Dangerous many:many with Extreme Fanout

**Scenario:** Transaction line items joining to product categories.
Both sides have duplicate keys (product_id appears ~3x on left, ~5x on right).

### Setup

```python
import pandas as pd
import numpy as np

# 3000 line items, ~3 per product_id (1000 distinct products)
product_ids_left = np.repeat(range(1, 1001), 3)
np.random.shuffle(product_ids_left)
line_items = pd.DataFrame({
    "product_id": product_ids_left,
    "quantity": np.random.randint(1, 10, 3000),
})

# 5000 category assignments, ~5 per product_id (1000 distinct products)
product_ids_right = np.repeat(range(1, 1001), 5)
np.random.shuffle(product_ids_right)
categories = pd.DataFrame({
    "product_id": product_ids_right,
    "category": np.random.choice(["A", "B", "C", "D", "E"], 5000),
})
```

### Execution Trace

**Step 1: Build composite keys**

```python
left_key_series = line_items["product_id"].fillna("__NULL__").astype(str)
right_key_series = categories["product_id"].fillna("__NULL__").astype(str)
```

**Step 2: Key overlap analysis**

```python
left_key_set = {"1", "2", ..., "1000"}   # 1000 distinct
right_key_set = {"1", "2", ..., "1000"}  # 1000 distinct (same ids)

overlap_keys = left_key_set & right_key_set  # all 1000
overlap_count = 1000
overlap_pct = 1000 / 1000 = 1.0  # 100% overlap

left_orphan_count = 0
right_orphan_count = 0
```

**Step 3: Cardinality detection**

```python
left_key_distinct = 1000
right_key_distinct = 1000
left_count = 3000
right_count = 5000

# left_count (3000) > left_key_distinct (1000)? YES
# right_count (5000) > right_key_distinct (1000)? YES
# Result: "many:many"
```

**Step 4: Right-side duplication stats**

```python
right_dups = right_key_series.value_counts()
# Each product_id appears exactly 5 times
right_rows_per_key_avg = 5.0
right_rows_per_key_max = 5
```

**Step 5: Predict output size**

```python
matching_left_rows = 3000  # all left rows have matching keys
# cardinality is "many:many":
# For overlap keys, compute right dups over overlap only
right_rows_per_key_avg_overlap = 5.0

predicted_rows = 3000 * 5.0 = 15000
fanout_ratio = 5.0
# 5.0 is between 3.0 and 10.0 -> fanout_risk = "high"
```

**Step 6: Build contract**

```python
findings = [
    "Cardinality is many:many -- each left row may match multiple right rows (avg 5.0, max 5)",
]
risks = [
    "Fanout risk is HIGH: predicted 15,000 output rows (max 5x on key '500')",
]
summary = "WARNING: INNER JOIN safe -- Fanout risk is HIGH: predicted 15,000 output rows"

suggested_next_actions = [
    "Investigate fanout: anchor('microscope', right_df, 'product_id') to check for duplicates",
]
```

### Final Output

```python
ctx["metrics"]["cardinality"]       # "many:many"
ctx["metrics"]["fanout_risk"]       # "high"
ctx["metrics"]["predicted_rows"]    # 15000
ctx["metrics"]["fanout_ratio"]      # 5.0
ctx["metrics"]["overlap_pct"]       # 1.0
```

---

## Worked Example 3: Format Mismatch (Trailing Whitespace)

**Scenario:** Left has clean keys, right has keys with trailing spaces.
Join will silently produce 0 matches unless keys are trimmed.

### Setup

```python
import pandas as pd

left = pd.DataFrame({
    "code": ["ABC", "DEF", "GHI"],
    "value": [1, 2, 3],
})

right = pd.DataFrame({
    "code": ["ABC ", "DEF ", "GHI "],  # trailing space!
    "label": ["Alpha", "Delta", "Gamma"],
})
```

### Execution Trace

**Step 1: Build composite keys**

```python
left_key_series = ["ABC", "DEF", "GHI"]
right_key_series = ["ABC ", "DEF ", "GHI "]  # note trailing space
```

**Step 2: Key overlap**

```python
left_key_set = {"ABC", "DEF", "GHI"}
right_key_set = {"ABC ", "DEF ", "GHI "}  # different strings!

overlap_keys = set()  # EMPTY -- no exact matches
overlap_count = 0
overlap_pct = 0.0  # 0% overlap!

left_orphan_count = 3  # all left keys are "orphans"
right_orphan_count = 3  # all right keys are "orphans"
```

**Step 3: Format compatibility check**

```python
left_sample_vals = ["ABC", "DEF", "GHI"]
right_sample_vals = ["ABC ", "DEF ", "GHI "]

# Whitespace check:
# right_has_ws = any(v != v.strip() for v in right_sample_vals if v)
# "ABC " != "ABC".strip()? -> "ABC " != "ABC" -> True
right_has_ws = True

result = {
    "compatible": False,
    "issues": ["whitespace"],
    "examples": [{"type": "whitespace", "value": "'ABC '"}],
}
```

**Step 4: Build contract**

```python
findings = [
    "3 left keys (100.0%) have no match in right -- use LEFT JOIN to preserve",
    "3 right keys (100.0%) have no match in left -- will be excluded in LEFT JOIN",
    "Cardinality is 1:1 -- clean join expected",
    "Format mismatch detected: whitespace -- keys may fail to match despite being semantically equal",
]
risks = [
    "Format incompatibility (whitespace) will cause false non-matches -- clean keys before joining",
]
summary = "WARNING: LEFT JOIN recommended -- Format incompatibility (whitespace) will cause false non-matches"
```

### Final Output

```python
ctx["metrics"]["format_compatible"]  # False
ctx["metrics"]["overlap_pct"]        # 0.0
ctx["samples"]["format_mismatch_examples"]
# [{"type": "whitespace", "value": "'ABC '"}]
ctx["suggested_next_actions"]
# ["Fix format mismatch: TRIM and normalize keys before joining"]
```

---

## Key Python Patterns

### 1. Composite Key Construction

```python
_SEPARATOR = "|||"
_NULL_SENTINEL = "__NULL__"

def _build_composite_key_pandas(df, key_columns):
    parts = [df[c].fillna(_NULL_SENTINEL).astype(str) for c in key_columns]
    if len(parts) == 1:
        return parts[0]
    return parts[0].str.cat(parts[1:], sep=_SEPARATOR)
```

Single column: just fillna + astype(str). Multi-column: `str.cat` with separator.
Null sentinel prevents null keys from matching (nulls should drop, not join).

### 2. Set-Based Overlap (Pandas)

```python
left_key_set = set(left_key_series.unique())
right_key_set = set(right_key_series.unique())
overlap_keys = left_key_set & right_key_set    # intersection
left_orphans = left_key_set - right_key_set    # subtract
```

Fast O(n) set operations. Spark uses `.intersect()` and `.subtract()` instead.

### 3. Accurate Row Prediction via matching_left_rows

```python
# Not overlap_pct * left_count (inaccurate with non-uniform duplication)
matching_left_rows = int(left_key_series.isin(overlap_keys).sum())

# For 1:many/many:many, also compute right dups over overlap keys only
right_overlap_dups = right_key_series[right_key_series.isin(overlap_keys)].value_counts()
right_rows_per_key_avg_overlap = float(right_overlap_dups.mean())

predicted = matching_left_rows * right_rows_per_key_avg_overlap
```

### 4. Format Compatibility via unicodedata

```python
import unicodedata

def _has_zwc(val: str) -> bool:
    return any(unicodedata.category(ch) in ("Cf", "Mn") for ch in val)
```

Detects zero-width characters (Cf = format chars, Mn = non-spacing marks)
that are invisible but prevent string equality.

### 5. Decision-Structured Markdown

The renderer organizes output around 4 user questions:
1. "Will I lose rows?" → Row Survival table
2. "Will it explode?" → Fanout Risk + top keys
3. "Why is overlap low?" → Orphan samples + format hints (only if <90%)
4. "Are keys dirty?" → Null counts + format issues (only if present)

Conditional sections prevent noise when everything is clean.