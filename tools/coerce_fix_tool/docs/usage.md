# Coerce Fix Tool — Usage

## Signature

```python
coerce_fix_context(
    df,
    coerce_ctx: dict,
    columns: list[str] | None = None,
    case_target: str = "upper",
    date_target: str = "%Y-%m-%d",
    dry_run: bool = False,
    subject: str | None = None,
    sample_limit: int = 5,
    output_format: str = "dict",
) -> dict | str
```

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `df` | pandas DataFrame | Yes | — | The DataFrame to fix (typically the "new" side from coerce_check) |
| `coerce_ctx` | dict | Yes | — | Output from `anchor("coerce_check")`. Provides per-column mismatch classification |
| `columns` | list[str] | No | None (all) | Limit fixes to specific columns. Default: all non-genuine columns |
| `case_target` | str | No | `"upper"` | Target case for case fixes: `"upper"` or `"lower"` |
| `date_target` | str | No | `"%Y-%m-%d"` | strftime format string for date normalization |
| `dry_run` | bool | No | `False` | If True, return fix plan without applying changes |
| `subject` | str | No | auto | Display label for output |
| `sample_limit` | int | No | 5 | Max before/after sample pairs per column |
| `output_format` | str | No | `"dict"` | `"dict"` or `"markdown"` |

## Basic Usage

```python
# Step 1: Find differences
diff_ctx = anchor("diff", old_df, new_df, keys=["id"])

# Step 2: Classify the differences
coerce_ctx = anchor("coerce_check", old_df, new_df, keys=["id"],
                columns=["name", "email", "updated_date"])

# Step 3: Apply fixes
fix_result = anchor("coerce_fix", new_df, coerce_ctx)

# Step 4: Get the corrected DataFrame
fixed_df = fix_result["df"]
```

## Dry Run Mode

```python
# Preview what will be fixed without changing anything
plan = anchor("coerce_fix", new_df, coerce_ctx, dry_run=True)

# plan["fixes_planned"] shows:
# [{"column": "name", "category": "case", "operation": "UPPER"}, ...]
#
# plan["skipped"] shows:
# [{"column": "notes", "category": "genuine", "reason": "genuine differences"}]
```

## Limiting to Specific Columns

```python
# Only fix the "name" column
result = anchor("coerce_fix", new_df, coerce_ctx, columns=["name"])
```

## Customizing Fix Targets

```python
# Use lowercase instead of uppercase for case normalization
result = anchor("coerce_fix", new_df, coerce_ctx, case_target="lower")

# Use MM/DD/YYYY format for dates
result = anchor("coerce_fix", new_df, coerce_ctx, date_target="%m/%d/%Y")
```

## Result Structure

| Field | Type | Description |
|-------|------|-------------|
| `kind` | str | Always `"coerce_fix"` |
| `subject` | str | Display label |
| `summary` | str | One-line description of what was fixed |
| `metrics.columns_fixed` | int | Number of columns that were corrected |
| `metrics.columns_skipped_genuine` | int | Columns skipped as genuine differences |
| `metrics.total_values_corrected` | int | Total cell values changed |
| `metrics.by_category` | dict | Breakdown: `{"case": 100, "whitespace": 50}` |
| `findings` | list[str] | Per-column fix description |
| `risks` | list[str] | Warnings (e.g. wrong-side detection) |
| `samples` | dict | Per-column before/after samples |
| `fixes_applied` | list[dict] | Details of each fix: column, category, operation, rows_affected |
| `skipped` | list[dict] | Columns that were skipped with reasons |
| `df` | DataFrame | The corrected DataFrame |
| `suggested_next_actions` | list[str] | Recommended verification steps |

## Verification Workflow

After applying fixes, always verify:

```python
# Re-run diff to confirm mismatches resolved
verify = anchor("diff", old_df, fixed_df, keys=["id"])

# Or re-run coerce_check for detailed comparison
verify = anchor("coerce_check", old_df, fixed_df, keys=["id"],
            columns=["name", "email"])
```

## Direct Import

```python
import sys
sys.path.append("/Workspace/Users/user@example.com/odibi_anchor")

from tools.coerce_fix_tool.coerce_fix_impl import coerce_fix_context

result = coerce_fix_context(
    df=new_df,
    coerce_ctx=coerce_ctx,
    case_target="upper",
    dry_run=False,
)
fixed_df = result["df"]
```
