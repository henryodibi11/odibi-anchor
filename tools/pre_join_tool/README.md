# Pre-Join Check

> Validate that two DataFrames are safe to join before joining — before you write the join.

---

## When to Use

- You are about to join two DataFrames and want to know if rows will be lost
- You suspect a fan-out (one key matches many rows on the right side) and want to confirm before it blows up row counts
- Keys look like they should match but the join produces fewer rows than expected
- You want to know whether INNER JOIN or LEFT JOIN is appropriate for your data

**Anti-pattern:** Don't run `pre_join` as a formality after the join already ran. Use it *before* writing the join so you can choose the right join type and fix bad keys before they cause silent data loss.

---

## Quick Start

```python
import pandas as pd

# Inline sample data — replace with your own DataFrames
orders = pd.DataFrame({
    "order_id":    [1, 2, 3, 4],
    "customer_id": [101, 102, 103, None],  # one null key
    "amount":      [150.0, 200.0, 75.0, 50.0],
})
customers = pd.DataFrame({
    "customer_id": [101, 102, 104],  # 103 is missing (left orphan)
    "name":        ["Alice", "Bob", "Carol"],
})

result = anchor("pre_join", orders, customers, keys=["customer_id"])
```

---

## Usage

### Basic
```python
result = anchor("pre_join", left_df, right_df, keys=["id"])
```

### Composite key
```python
result = anchor("pre_join", left_df, right_df, keys=["order_id", "line_id"])
```

### Asymmetric keys (left key name differs from right key name)
```python
result = anchor("pre_join", orders_df, customers_df,
            keys=["customer_id"],
            right_keys=["id"])
```

### Label the DataFrames in output
```python
result = anchor("pre_join", orders_df, customers_df,
            keys=["customer_id"],
            left_subject="orders",
            right_subject="customers")
```

### Markdown output
```python
report = anchor("pre_join", left_df, right_df, keys=["id"], output_format="markdown")
print(report)
```

### Direct Import (no anchor() dispatcher needed)

```python
import sys
import pandas as pd
sys.path.insert(0, "/Workspace/Users/user@example.com/odibi_anchor/src")

from tools.pre_join_tool.pre_join_impl import pre_join_context

orders = pd.DataFrame({
    "order_id":    [1, 2, 3, 4],
    "customer_id": [101, 102, 103, None],
    "amount":      [150.0, 200.0, 75.0, 50.0],
})
customers = pd.DataFrame({
    "customer_id": [101, 102, 104],
    "name":        ["Alice", "Bob", "Carol"],
})

result = pre_join_context(orders, customers, keys=["customer_id"])
print(result["summary"])
print(result["metrics"]["cardinality"])
print(result["suggested_next_actions"])
```

---

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `left_df` | DataFrame | ✓ | — | Left side of the join |
| `right_df` | DataFrame | ✓ | — | Right side of the join |
| `keys` | list[str] | ✓ | — | Join key column names. Applied to both sides unless `right_keys` is given |
| `right_keys` | list[str] | | same as `keys` | Right-side key names when they differ from left |
| `left_subject` | str | | `"left"` | Display label for the left DataFrame |
| `right_subject` | str | | `"right"` | Display label for the right DataFrame |
| `sample_limit` | int | | 5 | Number of orphan key examples to show |
| `output_format` | str | | `"dict"` | `"dict"` or `"markdown"` |

---

## Output

### Key Fields

| Field | Type | What It Tells You |
|-------|------|-------------------|
| `summary` | str | One-line verdict: join type recommendation and top issue |
| `metrics.cardinality` | str | `"1:1"`, `"1:many"`, or `"many:many"` |
| `metrics.overlap_count` | int | Distinct keys that exist on both sides |
| `metrics.overlap_pct` | float | Share of left distinct keys that have a right match |
| `metrics.matching_left_rows` | int | Left rows that will survive an INNER JOIN |
| `metrics.matching_left_rows_pct` | float | Share of left rows that will survive |
| `metrics.predicted_rows` | int | Estimated INNER JOIN output size (accounts for fan-out) |
| `metrics.fanout_risk` | str | `"none"`, `"low"`, `"medium"`, `"high"`, or `"extreme"` |
| `metrics.left_null_key_count` | int | Left rows with NULL in a key column (will silently drop) |
| `metrics.right_null_key_count` | int | Right rows with NULL in a key column |
| `metrics.format_compatible` | bool | Whether key values appear format-compatible across sides |
| `findings` | list[str] | What was found: orphans, cardinality, null keys, format issues |
| `risks` | list[str] | What will go wrong: NULL drops, fan-out explosion, format mismatch |
| `samples.left_orphan_keys` | list[str] | Example left keys with no right match |
| `samples.max_fanout_keys` | list[dict] | Keys with the highest right-side match count |
| `samples.format_mismatch_examples` | list | Side-by-side examples of mismatched key formats |
| `suggested_next_actions` | list[str] | Concrete steps: join type, NULL handling, format fix |

### Example Output

```python
result = anchor("pre_join", orders_df, customers_df, keys=["customer_id"])

result["summary"]
# "WARNING: LEFT JOIN recommended -- NULL keys: 312 left rows (2.1%) will drop silently"

result["metrics"]["cardinality"]
# "1:1"

result["metrics"]["matching_left_rows_pct"]
# 0.9412  -- 94.1% of left rows will survive an INNER JOIN

result["risks"]
# ["NULL keys: 312 left rows (2.1%) will drop silently -- filter or COALESCE before joining"]

result["suggested_next_actions"]
# ["Filter NULL keys before join: left_df.where(F.col('customer_id').isNotNull())"]
```

---

## Reading the Output

### What to look for first
1. `summary` — is the join safe or does it need attention?
2. `metrics.fanout_risk` — will the join multiply rows unexpectedly?
3. `metrics.left_null_key_count` — will rows silently disappear?
4. `metrics.format_compatible` — will keys fail to match despite being semantically equal?

### Choosing a join type

| Situation | Join type to use |
|-----------|------------------|
| `overlap_pct = 1.0`, `fanout_risk = "none"` | INNER JOIN is safe |
| `left_orphan_count > 0` | LEFT JOIN to preserve orphan left rows |
| `fanout_risk = "high"` or `"extreme"` | Investigate before joining — may need dedup on right side |
| `format_compatible = False` | Fix key formatting before any join |

### Common findings and what they mean

| Finding | What It Means | What To Do |
|---------|--------------|------------|
| "Cardinality is 1:many" | One left key matches multiple right rows | Expected for fact-dimension joins; unexpected for dedup targets |
| "N left keys have no match in right" | Left orphans will drop in INNER JOIN | Use LEFT JOIN or investigate why keys are missing |
| "NULL keys on left.customer_id" | Rows with null key will silently drop from any join | Filter nulls or COALESCE before joining |
| "Format mismatch detected" | Keys like `"001"` vs `"1"` won't match despite being equal | TRIM and normalize key columns |
| `fanout_risk = "extreme"` | Predicted output is many times larger than left | De-duplicate the right side or add a filter |

---

## Pairs Well With

| If you found... | Then run... | Why |
|-----------------|-------------|-----|
| Orphan left keys | `anchor("case_file", left_df, column="customer_id", filter="where:customer_id NOT IN (...)")` | See the actual orphan rows and patterns |
| Fan-out risk on right side | `anchor("microscope", right_df, "customer_id")` | Understand why the right key has duplicates |
| NULL keys | `anchor("case_file", left_df, column="customer_id", filter="nulls")` | See which rows have null keys |
| Format mismatch | `anchor("coerce_fix", left_df, coerce_ctx)` | Fix whitespace/case/format issues in keys |
| Join is safe — proceed | `anchor("quality", result_df, subject="joined", keys=["id"])` | Validate the join output before writing |

---

## Documentation

Detailed documentation is available in the `docs/` folder:

- [Architecture](docs/architecture.md) — Pipeline design, cardinality detection, fanout risk scale, format checks, and output contract
- [Usage Guide](docs/usage.md) — Full parameter reference, key resolution logic, result structure, and common patterns
- [Code Walkthrough](docs/code_walkthrough.md) — 3 worked examples: safe 1:1, dangerous many:many fanout, and format mismatch detection
