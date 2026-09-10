# transform + apply_transform + rollback

> Generate a reviewable, ordered transform plan from a profile — then apply it step-by-step with per-step checkpoints and full rollback support.

This covers three actions that form a single workflow:

| Action | Question | When to call |
|---|---|---|
| `transform` | What should I fix in this DataFrame? | After `profile_table` or `quality` flags issues |
| `apply_transform` | Apply the plan, with checkpoints | After reviewing the generated steps and code |
| `rollback` | Undo to a specific step | When a step produces unexpected results |

---

## Documentation

* [Architecture](transform_architecture.md) — Step generation pipeline, handler dispatch, checkpoint model
* [Usage](transform_usage.md) — Full parameter reference, step types, output contract, rollback patterns
* [Code Walkthrough](transform_plan_walkthrough.md) — Worked examples with generated code traces
* [apply_transform Walkthrough](apply_transform_walkthrough.md) — Step execution, checkpoints, rollback

---

## When to Use

- `profile_table` or `quality` flagged null-like strings, type mismatches, non-snake_case column names, or duplicates that need fixing before loading to a Delta table
- You want a generated, reviewable cleaning plan — not ad-hoc TRIM/CAST code written by hand
- You need a safe, reversible transform with per-step checkpoints for a DataFrame going to silver/gold

**Anti-pattern:** Don't write manual TRIM/CAST/UPPER code when `transform` can generate it. Don't apply transforms without reviewing the generated code — the plan is intentionally a review step, not auto-execute. Don't skip `rollback` support for large DataFrames.

---

## Quick Start

```python
# Step 1: get a profile
profile_ctx = anchor("profile_table", df, subject="queue_bronze")

# Step 2: generate the transform plan
plan = anchor("transform", profile_ctx, subject="queue_bronze")
# Review plan["steps"] and plan["code_pandas"] before proceeding

# Step 3: apply it
result = anchor("apply_transform", df, plan)
clean_df = result["df"]
```

---

## Usage

### Generate a plan
```python
plan = anchor("transform", profile_ctx)
```

### Generate with options
```python
plan = anchor("transform", profile_ctx,
          include_dedup=False,          # skip dedup step
          include_drop_constant=True,   # drop columns that are 100% null or constant
          custom_overrides={"col": "skip"})  # skip a specific column
```

### Apply the full plan
```python
result = anchor("apply_transform", df, plan)
```

### Apply with dry run (preview only)
```python
result = anchor("apply_transform", df, plan, dry_run=True)
# result["df"] is unchanged — review result["steps_applied"] to see what would run
```

### Apply specific steps only
```python
result = anchor("apply_transform", df, plan, steps=[1, 2])  # run only steps 1 and 2
```

### Apply with confidence floor
```python
result = anchor("apply_transform", df, plan, min_confidence=0.9)
# Steps with confidence < 0.9 are skipped with a warning
```

### Rollback to before a specific step
```python
result = anchor("apply_transform", df, plan)
# Something went wrong in step 3
rolled_back_df = anchor("rollback", result, to=2)  # state before step 3 ran
```

---

## Parameters

### transform

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `profile` | dict | Yes | — | Output from `anchor("profile_table")` |
| `subject` | str | No | auto | Display label |
| `layer` | str | No | `"silver"` | Target medallion layer (reserved for future use) |
| `include_cast` | bool | No | `True` | Generate type cast steps |
| `include_null_clean` | bool | No | `True` | Generate null-like string cleanup steps |
| `include_dedup` | bool | No | `True` | Generate dedup step if key candidates detected |
| `include_drop_constant` | bool | No | `False` | Drop all-null or constant columns |
| `include_standardize` | bool | No | `True` | Standardize column names to snake_case |
| `custom_overrides` | dict | No | `None` | Per-column overrides. `{"col": "skip"}` or `{"col": False}` to skip |
| `output_format` | str | No | `"dict"` | `"dict"` or `"markdown"` |

### apply_transform

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `df` | DataFrame | Yes | — | Input DataFrame (pandas or Spark) |
| `plan_ctx` | dict | Yes | — | Output from `anchor("transform")` |
| `steps` | list[int] | No | `None` (all) | Specific step order numbers to run |
| `dry_run` | bool | No | `False` | Validate without mutating |
| `min_confidence` | float | No | `0.0` | Skip steps below this confidence threshold |
| `checkpoint` | bool | No | `True` | Store DataFrame state before each step |
| `spark_persist` | str | No | `"cache"` | Spark checkpoint mode: `"none"`, `"cache"`, `"local_checkpoint"` |
| `auto_unpersist` | bool | No | `False` | Release checkpoints after all steps complete |
| `max_checkpoints` | int | No | `None` | Cap on cached checkpoints (evicts oldest) |
| `engine` | str | No | `"auto"` | `"auto"`, `"pandas"`, or `"spark"` |
| `output_format` | str | No | `"dict"` | `"dict"` or `"markdown"` |

### rollback

```python
anchor("rollback", result, to=N)   # returns the DataFrame state before step N ran
```

---

## Output

### transform

```python
plan["steps"]         # ordered list of step dicts — review before applying
plan["code_spark"]    # full Spark code block for the entire plan
plan["code_pandas"]   # full pandas code block for the entire plan
plan["metrics"]["total_steps"]
plan["metrics"]["step_counts"]   # {"cast": 2, "null_clean": 3, "standardize": 1}
plan["metrics"]["min_confidence"]
plan["risks"]         # e.g. "3 casts below 99% confidence — review before applying"
```

### apply_transform

```python
result["df"]               # transformed DataFrame — use this downstream
result["df_before"]        # original input — always available for full rollback
result["steps_applied"]    # [1, 2, 3, 4] — steps that ran
result["steps_skipped"]    # steps skipped (confidence/selection)
result["row_count_before"] # rows before
result["row_count_after"]  # rows after
result["rows_dropped"]     # dedup rows removed
result["columns_renamed"]  # {"My Column": "my_column", ...}
result["step_audit"]       # per-step row count deltas
result["checkpoints"]      # {step_order: df_state} — for rollback
```

---

## Pairs Well With

- `anchor("profile_table", df, ...)` — always profile before transforming to know what needs fixing
- `anchor("quality", df)` — run after applying to verify issues are resolved
- `anchor("validate", df, rules=[...])` — apply explicit validation rules after cleaning
- `anchor("diff", original_df, clean_df, keys=[...])` — audit exactly what the transform changed

---

## Direct Import (no anchor() dispatcher)

```python
import sys
sys.path.append("/Workspace/Users/user@example.com/odibi_anchor/src")

from odibi_anchor.tables.transform_plan_context import transform_plan_context
from odibi_anchor.tables.apply_transform_context import apply_transform_context

plan = transform_plan_context(profile, subject="queue_bronze")
result = apply_transform_context(df, plan)
clean_df = result["df"]
```
