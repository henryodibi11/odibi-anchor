# Suggest Rules

> Auto-generate a starter set of validation rules from data shape — without writing them by hand.

---

## When to Use

- You just ingested a new table and want baseline validation rules in minutes
- You want to pass real rules to `anchor("validate")` without crafting them from scratch
- You need to enforce data contracts on columns you haven't manually profiled yet
- You want strictness control: zero-tolerance vs. production-realistic vs. exploratory

---

## Quick Start

```python
import sys
sys.path.insert(0, "/Workspace/Users/user@example.com/odibi_anchor/src")
from odibi_anchor.bootstrap import init
anchor, ROOT, MANIFEST = init()

import pandas as pd

df = pd.DataFrame({
    "order_id":    [1, 2, 3, 4, 5],
    "customer_id": [101, 102, 103, None, 101],  # one null, one duplicate
    "amount":      [150.0, 200.0, -50.0, 75.0, 300.0],
    "status":      ["active", "active", "inactive", "active", "active"],
})

ctx = anchor("suggest_rules", df=df, subject="orders")

# Pipe directly to validate
anchor("validate", df, rules=ctx["rules"])
```

---

## Usage

```python
# From raw DataFrame (auto-profiles internally)
ctx = anchor("suggest_rules", df=df, subject="orders_table")

# From an existing profile (skips re-profiling)
profile = anchor("profile_table", df, subject="orders_table", output_format="dict")
ctx = anchor("suggest_rules", profile_ctx=profile)

# Stricter rules — no tolerance for nulls or range violations
ctx = anchor("suggest_rules", df=df, strictness="strict")

# Lenient rules — generous margins, useful for exploration
ctx = anchor("suggest_rules", df=df, strictness="lenient")

# Specific rule types only
ctx = anchor("suggest_rules", df=df, include_types=["not_null", "unique"])
```

---

## Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `df` | DataFrame | One of `df`/`profile_ctx` | Raw DataFrame to profile and generate rules from |
| `profile_ctx` | dict | One of `df`/`profile_ctx` | Output from `anchor("profile_table")` — skips re-profiling |
| `subject` | str | No | Display name for the table |
| `strictness` | str | No | `"strict"`, `"standard"` (default), or `"lenient"` |
| `include_types` | list[str] | No | Limit to specific rule types (see table below) |
| `exclude_columns` | list[str] | No | Skip rule generation for these columns |
| `output_format` | str | No | `"dict"` (default) or `"markdown"` |

**Rule types** that can appear in output or be filtered via `include_types`:

| Type | What it checks |
|---|---|
| `not_null` | Column has no null values |
| `unique` | Column has no duplicate values |
| `accepted_values` | Column only contains a known set of values |
| `range` | Numeric column stays within observed min/max |
| `type_check` | Column values match expected type |
| `expression` | Custom SQL expression rule |

---

## Output

```python
ctx["summary"]   # "Generated 7 validation rules from profile (1 accepted_values, 1 not_null, 3 range, 2 unique)"
ctx["rules"]     # List of rule dicts — pass directly to anchor("validate")
```

Each rule in `ctx["rules"]`:
```python
# not_null rule
{"type": "not_null", "columns": ["order_id", "amount", "status"],
 "_confidence": 0.9, "_reason": "0% null in profile (tolerance: 0%)"}

# accepted_values rule
{"type": "accepted_values", "column": "status",
 "values": ["active", "inactive"],
 "_confidence": 0.7, "_reason": "2 distinct values, categorical"}

# range rule
{"type": "range", "column": "amount",
 "min": -50.0, "max": 300.0,
 "_confidence": 0.8, "_reason": "FLOAT64, no nulls"}
```

---

## Reading the Output

- `_confidence` between 0 and 1 — higher means the tool is more certain this rule is appropriate
- Discard rules with `_confidence < 0.6` or edit the `values`/`min`/`max` before using in production
- `accepted_values` rules are generated only when distinct count ≤ 20 — add manually for high-cardinality lookups

**Typical workflow:**
```python
ctx = anchor("suggest_rules", df=df, subject="my_table")
rules = [r for r in ctx["rules"] if r["_confidence"] >= 0.7]  # filter low-confidence
anchor("validate", df, rules=rules)
```

---

## Pairs Well With

- `anchor("profile_table", df)` — run first to understand data before generating rules
- `anchor("validate", df, rules=ctx["rules"])` — run the generated rules immediately
- `anchor("microscope", df, "col")` — inspect a column before accepting its range rule
- `anchor("case_file", df, column="col", filter="outliers")` — review edge cases before locking in range bounds

---

## Direct Import (no anchor() dispatcher needed)

```python
import sys
import pandas as pd
sys.path.append("/Workspace/Users/user@example.com/odibi_anchor/src")
sys.path.insert(0, "/Workspace/Users/user@example.com/odibi_anchor/src")

from tools.suggest_rules_tool.suggest_rules_impl import suggest_rules_context

df = pd.DataFrame({
    "order_id":    [1, 2, 3, 4, 5],
    "customer_id": [101, 102, 103, None, 101],
    "amount":      [150.0, 200.0, -50.0, 75.0, 300.0],
    "status":      ["active", "active", "inactive", "active", "active"],
})

ctx = suggest_rules_context(df=df, subject="orders")

ctx["summary"]
# "Generated 7 validation rules from profile (1 accepted_values, 1 not_null, 3 range, 2 unique)"

ctx["rules"][0]["type"]
# "not_null"
```

> **Note:** This tool imports from `odibi_anchor._utils`. The `src/` path is required.

---

## Documentation

Detailed documentation is available in the `docs/` directory:

- [Architecture](docs/architecture.md) — Pipeline flow, rule types, strictness levels, design decisions
- [Usage Guide](docs/usage.md) — Full parameter reference, output contract, error cases
- [Code Walkthrough](docs/code_walkthrough.md) — 3 worked examples tracing data through the inference pipeline
