# Coerce Fix

> Apply TRIM, case normalization, and date format fixes based on what `coerce_check` classified — returns the corrected DataFrame.

---

## Documentation

* [Architecture](docs/architecture.md) — Pipeline overview, fix types, design decisions
* [Usage](docs/usage.md) — Full parameter reference, result structure, verification workflow
* [Code Walkthrough](docs/code_walkthrough.md) — Worked examples: case fix, date normalization, wrong-side detection

---

## When to Use

- `anchor("coerce_check")` found value mismatches and you want to apply the fixes automatically
- Source data has case inconsistencies (e.g., `"active"` vs `"ACTIVE"`) that are causing join failures
- Whitespace-padded strings are silently breaking downstream logic
- You want to normalize a column before a merge without writing TRIM/UPPER manually

---

## Quick Start

```python
import sys
sys.path.insert(0, "/Workspace/Users/user@example.com/odibi_anchor/src")
from odibi_anchor.bootstrap import init
anchor, ROOT, MANIFEST = init()

import pandas as pd

# Source with case + whitespace issues
raw = pd.DataFrame({
    "order_id":    [1, 2, 3],
    "customer_id": ["CUST-001", "cust-002", "  CUST-003  "],  # case + whitespace
    "status":      ["Active", "active", "ACTIVE"],             # case inconsistency
})
canonical = pd.DataFrame({
    "order_id":    [1, 2, 3],
    "customer_id": ["CUST-001", "CUST-002", "CUST-003"],
    "status":      ["ACTIVE", "ACTIVE", "ACTIVE"],
})

# Step 1: classify mismatches
coerce_ctx = anchor("coerce_check", raw, canonical, keys=["order_id"])

# Step 2: apply fixes
fix_ctx = anchor("coerce_fix", raw, coerce_ctx)

# The corrected DataFrame
corrected_df = fix_ctx["df"]
```

---

## Usage

```python
# Standard: fix based on coerce_check output
coerce_ctx = anchor("coerce_check", source_df, reference_df, keys=["id"])
fix_ctx = anchor("coerce_fix", source_df, coerce_ctx)
clean_df = fix_ctx["df"]

# Dry run: see what would change without modifying
fix_ctx = anchor("coerce_fix", source_df, coerce_ctx, dry_run=True)

# Fix only specific columns
fix_ctx = anchor("coerce_fix", source_df, coerce_ctx, columns=["customer_id"])

# Use lowercase instead of UPPER
fix_ctx = anchor("coerce_fix", source_df, coerce_ctx, case_target="lower")
```

---

## Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `df` | DataFrame | Yes | The source DataFrame to fix |
| `coerce_ctx` | dict | Yes | Output from `anchor("coerce_check")` |
| `columns` | list[str] | No | Limit fixes to specific columns (applies all fixable columns by default) |
| `case_target` | str | No | `"upper"` (default) or `"lower"` |
| `date_target` | str | No | Target date format string (default `"%Y-%m-%d"`) |
| `dry_run` | bool | No | If True, compute fixes but do NOT modify — report only (default False) |
| `output_format` | str | No | `"dict"` (default) or `"markdown"` |

> Works with both pandas and Spark DataFrames.

---

## Output

```python
fix_ctx["summary"]
# "Fixed 2 column(s): customer_id (case→UPPER), status (case→UPPER). 3 values corrected."

fix_ctx["df"]          # The corrected DataFrame — use this for downstream work

fix_ctx["fixes_applied"]  # List of fixes applied
# [{"column": "customer_id", "category": "case", "operation": "UPPER", "rows_affected": 1},
#  {"column": "status",      "category": "case", "operation": "UPPER", "rows_affected": 2}]

fix_ctx["metrics"]["columns_fixed"]          # 2
fix_ctx["metrics"]["total_values_corrected"] # 3
fix_ctx["metrics"]["by_category"]           # {"case": 3}

fix_ctx["skipped"]    # Columns skipped (e.g., "genuine" value differences, not representation)
```

---

## Reading the Output

- `fixes_applied` tells you exactly which columns were touched and how many rows changed
- `skipped` columns had mismatches that were NOT representation issues — those need business logic, not TRIM/UPPER
- If `dry_run=True`, `fix_ctx["df"]` contains the ORIGINAL data unchanged — check `fixes_applied` to preview changes

**Column fix categories:**

| Category | What was applied |
|---|---|
| `case` | UPPER or LOWER normalization |
| `whitespace` | TRIM (leading/trailing) |
| `date` | Date format normalization to `date_target` |

---

## Pairs Well With

- `anchor("coerce_check", source_df, reference_df, keys=[...])` — always run this first to identify what needs fixing
- `anchor("diff", clean_df, reference_df, keys=[...])` — verify 0 `value_changed` after applying fixes
- `anchor("pre_merge", clean_df, target_table, keys=[...])` — check merge safety after cleaning

---

## Direct Import (no anchor() dispatcher needed)

```python
import sys
import pandas as pd
sys.path.append("/Workspace/Users/user@example.com/odibi_anchor/src")
sys.path.insert(0, "/Workspace/Users/user@example.com/odibi_anchor/src")

from tools.coerce_fix_tool.coerce_fix_impl import coerce_fix_context
from tools.pre_join_tool.pre_join_impl import pre_join_context  # for coerce_check step

# Source with case + whitespace issues
raw = pd.DataFrame({
    "order_id":    [1, 2, 3],
    "customer_id": ["CUST-001", "cust-002", "  CUST-003  "],
    "status":      ["Active", "active", "ACTIVE"],
})
canonical = pd.DataFrame({
    "order_id":    [1, 2, 3],
    "customer_id": ["CUST-001", "CUST-002", "CUST-003"],
    "status":      ["ACTIVE", "ACTIVE", "ACTIVE"],
})

coerce_ctx = anchor("coerce_check", raw, canonical, keys=["order_id"], output_format="dict")
fix_ctx = coerce_fix_context(raw, coerce_ctx)

fix_ctx["summary"]
# "Fixed 2 column(s): customer_id (case→UPPER), status (case→UPPER). 3 values corrected."

fix_ctx["df"]
# corrected DataFrame with CUST-002, CUST-003, ACTIVE values
```

> **Note:** This tool imports from `odibi_anchor._utils`. The `src/` path is required.
