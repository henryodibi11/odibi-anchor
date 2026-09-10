# suggest_rules — Code Walkthrough

This document traces three worked examples through the rule inference pipeline, showing exactly how data characteristics map to generated rules.

---

## Example 1: not_null Rule Inference

### Input Data

```python
df = pd.DataFrame({
    "customer_id": [101, 102, 103, 104, 105, 106, 107, 108, 109, 110],
    "email":       ["a@x.com", "b@x.com", "c@x.com", "d@x.com", "e@x.com",
                    "f@x.com", "g@x.com", "h@x.com", "i@x.com", "j@x.com"],
    "phone":       ["555-0001", None, "555-0003", None, "555-0005",
                    "555-0006", None, "555-0008", "555-0009", "555-0010"],
})
```

### Profile Output (relevant fields)

```python
column_profiles = {
    "customer_id": {"null_pct": 0.0, ...},   # 0 nulls out of 10
    "email":       {"null_pct": 0.0, ...},   # 0 nulls out of 10
    "phone":       {"null_pct": 0.3, ...},   # 3 nulls out of 10
}
```

### Code Path: `_infer_not_null(profiles, config)`

```python
# With strictness="standard" -> config = {"null_tolerance": 0.0, ...}
tolerance = config["null_tolerance"]  # 0.0

for col, p in profiles.items():
    null_pct = p.get("null_pct", 0.0)
    if null_pct <= tolerance:  # 0.0 <= 0.0 -> True for customer_id, email
        not_null_cols.append(col)
    # phone: 0.3 <= 0.0 -> False, skipped
```

### Generated Rule

```python
{
    "type": "not_null",
    "columns": ["customer_id", "email"],
    "_confidence": 0.9,
    "_reason": "0% null in profile (tolerance: 0%)"
}
```

### Key Insight

The `not_null` engine groups all qualifying columns into a single rule (multi-column). Confidence is fixed at 0.9 because 0% nulls is strong evidence but doesn’t guarantee a business constraint. The `phone` column is excluded because its 30% null rate exceeds the tolerance.

With `strictness="lenient"` (null_tolerance=0.05), only columns with >5% nulls would be excluded — phone would still fail at 30%, but a column with 3% nulls would pass.

---

## Example 2: accepted_values Rule Inference

### Input Data

```python
df = pd.DataFrame({
    "status": ["Active", "Inactive", "Pending", "Closed", "Archived",
               "Active", "Active", "Pending", "Active", "Inactive"],
    "priority": [1, 2, 3, 1, 2, 3, 1, 2, 1, 3],  # numeric
    "region":  ["US", "EU", "US", "APAC", "EU", "US", "US", "APAC", "EU", "US"],
})
```

### Profile Output (relevant fields)

```python
column_profiles = {
    "status": {
        "distinct_count": 5,
        "inferred_type": "string",
        "top_values": [
            {"value": "Active", "count": 4},
            {"value": "Inactive", "count": 2},
            {"value": "Pending", "count": 2},
            {"value": "Closed", "count": 1},
            {"value": "Archived", "count": 1},
        ],
    },
    "priority": {
        "distinct_count": 3,
        "inferred_type": "integer",  # numeric -> skipped
        "top_values": [{"value": 1}, {"value": 2}, {"value": 3}],
    },
    "region": {
        "distinct_count": 3,
        "inferred_type": "string",
        "top_values": [
            {"value": "US", "count": 5},
            {"value": "EU", "count": 3},
            {"value": "APAC", "count": 2},
        ],
    },
}
```

### Code Path: `_infer_accepted_values(profiles, config)`

```python
for col, p in profiles.items():
    distinct_count = p.get("distinct_count", 999)

    # Gate 1: cardinality check
    if distinct_count > ACCEPTED_VALUES_MAX_DISTINCT:  # > 20? No
        continue
    if distinct_count < 2:  # < 2? No
        continue

    # Gate 2: numeric type check
    inferred_type = p.get("inferred_type", "")
    if any(kw in inferred_type for kw in ("numeric", "integer", "float", "decimal")):
        continue  # priority is "integer" -> SKIPPED

    # Gate 3: top_values coverage
    top_values = p.get("top_values", [])
    values = [tv["value"] for tv in top_values if tv.get("value") is not None]
    # status: 5 values captured, distinct_count=5 -> 5/5 = 100% coverage -> PASS
    # region: 3 values captured, distinct_count=3 -> 3/3 = 100% coverage -> PASS

    if len(values) < distinct_count * 0.8:  # Need >= 80% coverage
        continue
```

### Generated Rules

```python
# Rule for "status"
{
    "type": "accepted_values",
    "column": "status",
    "values": ["Active", "Archived", "Closed", "Inactive", "Pending"],  # sorted
    "_confidence": 0.7,
    "_reason": "5 distinct values, categorical"
}

# Rule for "region"
{
    "type": "accepted_values",
    "column": "region",
    "values": ["APAC", "EU", "US"],  # sorted
    "_confidence": 0.7,
    "_reason": "3 distinct values, categorical"
}
```

### Key Insight

The `accepted_values` engine applies three gates:
1. **Cardinality** — must be between 2 and 20 distinct values
2. **Type** — numeric columns are excluded (they get `range` rules instead)
3. **Coverage** — top_values must capture ≥80% of distinct values

The `priority` column is excluded despite having only 3 values because its `inferred_type` is `"integer"`. Values in the output are always sorted alphabetically. Confidence is fixed at 0.7 because accepted_values can become stale when new categories are introduced.

If `distinct_count > 10`, a risk warning is emitted: the values list may need updating as new data arrives.

---

## Example 3: range Rule Inference

### Input Data

```python
df = pd.DataFrame({
    "amount":      [10.50, 250.00, 75.25, 500.00, 1200.00, 50.00, 899.99, 15.00, 3000.00, 42.50],
    "quantity":    [1, 5, 2, 10, 3, 1, 7, 1, 20, 2],
    "description": ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"],  # non-numeric
})
```

### Profile Output (relevant fields)

```python
column_profiles = {
    "amount": {
        "inferred_type": "float",
        "stats": {"min": 10.50, "max": 3000.00},
    },
    "quantity": {
        "inferred_type": "integer",
        "stats": {"min": 1, "max": 20},
    },
    "description": {
        "inferred_type": "string",  # non-numeric -> skipped
        "stats": {},
    },
}
```

### Code Path: `_infer_range(profiles, config)`

```python
# With strictness="standard" -> config = {"range_margin": 0.10, ...}
margin = config["range_margin"]  # 0.10

for col, p in profiles.items():
    inferred_type = p.get("inferred_type", "")
    # Gate: must be numeric
    if not any(kw in inferred_type for kw in ("numeric", "integer", "float", "decimal")):
        continue  # description skipped

    stats = p.get("stats", {})
    min_val = stats.get("min")  # amount: 10.50, quantity: 1
    max_val = stats.get("max")  # amount: 3000.00, quantity: 20

    # Margin calculation for "amount":
    span = 3000.00 - 10.50  # = 2989.50
    range_min = 10.50 - abs(10.50) * 0.10  # = 10.50 - 1.05 = 9.45
    range_max = 3000.00 + abs(3000.00) * 0.10  # = 3000.00 + 300.00 = 3300.00

    # Since min_val >= 0, clamp: range_min = max(0, 9.45) = 9.45
    # Round to 2 decimals: min=9.45, max=3300.0

    # Margin calculation for "quantity":
    # min_val=1, max_val=20, both int, margin != 0 -> float path
    range_min = 1 - abs(1) * 0.10  # = 1 - 0.1 = 0.9
    range_max = 20 + abs(20) * 0.10  # = 20 + 2.0 = 22.0
    # min_val >= 0, clamp: range_min = max(0, 0.9) = 0.9
    # Round: min=0.9, max=22.0
```

### Generated Rules

```python
# Rule for "amount"
{
    "type": "range",
    "column": "amount",
    "min": 9.45,
    "max": 3300.0,
    "_confidence": 0.6,
    "_reason": "observed range [10.5, 3000.0], 10% margin applied"
}

# Rule for "quantity"
{
    "type": "range",
    "column": "quantity",
    "min": 0.9,
    "max": 22.0,
    "_confidence": 0.6,
    "_reason": "observed range [1, 20], 10% margin applied"
}
```

### Key Insight

The `range` engine:
1. Filters to numeric types only
2. Applies a percentage margin to both ends of the observed range
3. Clamps `min` to 0 when observed minimum is non-negative (prevents allowing negatives)
4. Uses integer bounds only when both min/max are int AND margin is 0 (strict mode)
5. Confidence is 0.6 — lowest of all rule types because observed ranges in sample data may not reflect the full population

With `strictness="strict"` (margin=0), the bounds would be exact: amount [10.50, 3000.00], quantity [1, 20] (as integers since both are int and margin=0).

With `strictness="lenient"` (margin=0.50), the bounds expand significantly: amount would get min=5.25, max=4500.0.

---

## Python Patterns

### STRICTNESS_CONFIG

```python
STRICTNESS_CONFIG = {
    "strict":   {"null_tolerance": 0.0, "range_margin": 0.0,  "min_null_pct_for_skip": 0.0},
    "standard": {"null_tolerance": 0.0, "range_margin": 0.10, "min_null_pct_for_skip": 0.0},
    "lenient":  {"null_tolerance": 0.05, "range_margin": 0.50, "min_null_pct_for_skip": 0.05},
}
```

This dict is the single source of truth for threshold values. Each inference engine receives the `config` dict and reads the thresholds it needs.

### Confidence Scoring

Confidence is assigned per rule type based on how strongly data evidence supports the rule:

| Rule Type | Confidence | Rationale |
|---|---|---|
| `not_null` | 0.9 | 0% nulls is strong evidence of a NOT NULL constraint |
| `unique` | 0.85 | 100% distinct + 0% null strongly suggests a primary key |
| `expression` | 0.75 | Date ordering is highly likely when column names match patterns |
| `accepted_values` | 0.7 | Enumeration is probable but new values may appear over time |
| `range` | 0.6 | Sample min/max may not represent population bounds |

### Auto-Profiling

When `df` is passed without `profile_ctx`, the tool imports `dataset_profile_context` from `odibi_anchor.profiling` and runs it internally:

```python
from odibi_anchor.profiling.dataset_profile_context import dataset_profile_context

generated_profile_ctx = dataset_profile_context(
    df,
    subject=subject or "dataframe",
    output_format="dict",
)
```

This produces the same output as calling `anchor("profile_table", df, output_format="dict")` through the dispatcher.
