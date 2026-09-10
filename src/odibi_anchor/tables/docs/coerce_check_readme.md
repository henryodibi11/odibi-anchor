# coerce_check

> After `diff` finds value mismatches, `coerce_check` answers *why* — classifying each difference as whitespace, case, unicode, numeric representation, date format, or a genuinely different value.

---

## Documentation

* [Architecture](coerce_check_architecture.md) — Classification pipeline, category hierarchy, design decisions
* [Usage](coerce_check_usage.md) — Full parameter reference, output contract, per-column results
* [Code Walkthrough](coercion_classifier_walkthrough.md) — Worked examples with classification traces

---

## When to Use

- `anchor("diff")` found `value_changed > 0` on a column and you need to know if it's a real change or a representation issue (trim/case/unicode)
- You're about to join two tables on a string key and want to know if case or whitespace is causing silent mismatches
- A reconciliation shows mismatches but downstream validation says the values should be equal
- You want a suggested fix (TRIM, UPPER, CAST AS INT, etc.) for each column before writing a cleaning step

**Anti-pattern:** Don't run `coerce_check` on every column — scope it to columns where `diff` showed `value_changed > 0`. Running it on unchanged columns wastes time and produces noise. Don't run it on numeric columns that `diff` already confirmed as equal.

---

## Quick Start

```python
# Step 1: diff finds which columns changed
diff_ctx = anchor("diff", previous_df, current_df, keys=["Application ID"])

# Step 2: coerce_check explains why
changed_cols = [c["column"] for c in diff_ctx["metrics"]["changed_column_counts"]
                if c["value_changed"] > 0]

coerce_ctx = anchor("coerce_check", previous_df, current_df,
                keys=["Application ID"], columns=changed_cols)
```

---

## Usage

### Check all common columns
```python
ctx = anchor("coerce_check", old_df, new_df, keys=["id"])
```

### Scope to specific columns (recommended after diff)
```python
ctx = anchor("coerce_check", old_df, new_df, keys=["id"],
         columns=["Generic Queue Status", "Project Status"])
```

### Markdown report
```python
report = anchor("coerce_check", old_df, new_df, keys=["id"], output_format="markdown")
```

---

## Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `old_df` | pandas DataFrame | Yes | — | Previous/source DataFrame |
| `new_df` | pandas DataFrame | Yes | — | Current/target DataFrame |
| `keys` | list[str] | Yes | — | Join key columns |
| `columns` | list[str] | No | all common non-key cols | Columns to classify. Scope to `value_changed` columns from diff |
| `subject` | str | No | auto | Display label |
| `sample_limit` | int | No | `20` | Max example pairs per column |
| `output_format` | str | No | `"dict"` | `"dict"` or `"markdown"` |

> **Pandas only.** Spark support not yet implemented. Convert Spark DataFrames with `.toPandas()` before calling.

---

## Output

### Top-level summary

```python
ctx["metrics"]["total_mismatches"]       # total differing value pairs across all columns
ctx["metrics"]["total_representation"]   # mismatches explained by coercion (fixable)
ctx["metrics"]["total_genuine"]          # mismatches that are truly different values
ctx["metrics"]["representation_pct"]     # % of mismatches that are representation issues
ctx["metrics"]["category_totals"]        # {"case": 12, "whitespace": 3, "genuine": 2, ...}
```

### Per-column results

```python
col = ctx["column_results"]["Generic Queue Status"]
col["dominant_category"]  # "case"
col["confidence"]         # 0.86
col["suggested_fix"]      # "UPPER()/LOWER() both sides before comparing"
col["total_mismatches"]   # 14
col["categories"]         # {"case": 12, "whitespace+case": 2}
```

### Sample pairs

```python
for sample in ctx["samples"]["Generic Queue Status"]:
    print(sample["old"], "→", sample["new"], f"({sample['category']})")
# "Scoping" → "SCOPING" (case)
# "  Active  " → "ACTIVE" (whitespace+case)
```

---

## Reading the Output

**Classification categories:**

| Category | What it means | Suggested fix |
|---|---|---|
| `whitespace` | Values match after TRIM | `TRIM()` or `.strip()` |
| `case` | Values match after case-fold | `UPPER()`/`LOWER()` |
| `whitespace+case` | Both TRIM and case fix needed | `TRIM()` + `UPPER()`/`LOWER()` |
| `unicode` | Zero-width or invisible chars (U+200B, FEFF) | Strip invisible characters |
| `numeric_representation` | Same number, different string format (`"1.0"` vs `"1"`) | `CAST AS INT/FLOAT` |
| `date_format` | Same date, different format (`"2024-01-15"` vs `"01/15/2024"`) | Parse to DATE type |
| `genuine` | Values are truly different — no coercion resolves it | Business logic fix required |

- `dominant_category` is the most common category for that column
- `confidence` is `dominant_count / total_mismatches` — high confidence means one root cause dominates
- `genuine` mismatches are NOT fixable by coerce_fix — they require business logic decisions

---

## Pairs Well With

- `anchor("diff", old_df, new_df, keys=[...])` — always run first; scope `columns=` to `value_changed > 0` columns
- `anchor("coerce_fix", df, coerce_ctx)` — apply the fixes coerce_check identified (in `tools/coerce_fix_tool/`)
- `anchor("case_file", df, column="...", filter="where:...")` — inspect the actual rows driving mismatches

---

## Direct Import (no anchor() dispatcher)

```python
import sys
import pandas as pd
sys.path.append("/Workspace/Users/user@example.com/odibi_anchor/src")

from odibi_anchor.tables.coercion_classifier import coercion_check_context

old_df = pd.DataFrame({"id": [1, 2], "status": ["Active", "  SCOPING  "]})
new_df = pd.DataFrame({"id": [1, 2], "status": ["ACTIVE", "Scoping"]})

ctx = coercion_check_context(old_df, new_df, keys=["id"])
print(ctx["summary"])
print(ctx["column_results"]["status"]["dominant_category"])  # "whitespace+case"
print(ctx["column_results"]["status"]["suggested_fix"])
```
