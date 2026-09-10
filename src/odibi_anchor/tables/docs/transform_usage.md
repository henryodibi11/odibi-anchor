# transform + apply_transform + rollback — Full Usage Reference

---

## transform — Function Signature

```python
transform_plan_context(
    profile: dict,
    *,
    subject: str | None = None,
    layer: str = "silver",
    include_cast: bool = True,
    include_null_clean: bool = True,
    include_dedup: bool = True,
    include_drop_constant: bool = False,
    include_standardize: bool = True,
    custom_overrides: dict | None = None,
    output_format: str = "dict",
) -> dict | str
```

## apply_transform — Function Signature

```python
apply_transform_context(
    df,
    plan_ctx: dict,
    *,
    steps: list[int] | None = None,
    dry_run: bool = False,
    min_confidence: float = 0.0,
    checkpoint: bool = True,
    spark_persist: str = "cache",
    auto_unpersist: bool = False,
    max_checkpoints: int | None = None,
    engine: str = "auto",
    output_format: str = "dict",
) -> dict | str
```

## rollback — Call Pattern

```python
anchor("rollback", result, to=N)  # returns df state before step N ran
```

---

## transform Parameters — Complete Reference

### `profile`
Output from `anchor("profile_table")`. Auto-detected and adapted — you don't need to manually convert the output format.

### `layer`
Target medallion layer. Currently reserved for future use — does not affect step generation. Default `"silver"`.

### `include_*` flags
Control which step types are generated:
- `include_standardize=True` — snake_case column renames (always run first in order)
- `include_null_clean=True` — replace `''`, `'N/A'`, `'null'`, `'none'`, `'na'`, `'n/a'` with `None`
- `include_cast=True` — cast string columns to inferred type (int, float, bool, date)
- `include_dedup=True` — add dedup step when key candidates detected in profile
- `include_drop_constant=False` — off by default; drops columns with one unique value or all-null

### `custom_overrides`
Per-column overrides. Three forms all work:
```python
custom_overrides = {
    "My Column": "skip",           # skip this column entirely
    "Another Col": False,          # equivalent to "skip"
    "Third Col": {"skip": True},   # dict form
}
```

---

## apply_transform Parameters — Complete Reference

### `steps`
Optional list of step order numbers to run. Use to apply a subset of the plan:
```python
result = anchor("apply_transform", df, plan, steps=[1, 3])  # only steps 1 and 3
```

### `dry_run`
When `True`, validates all steps without mutating the DataFrame. `result["df"]` contains the original data unchanged. Use to preview what would change.

### `min_confidence`
Skip steps below this confidence threshold. Steps with confidence `< min_confidence` appear in `steps_skipped` with a warning finding. Useful for applying only high-confidence steps first:
```python
result = anchor("apply_transform", df, plan, min_confidence=0.95)
```

### `checkpoint`
When `True` (default), stores the DataFrame state before each step. Required for rollback to work. Set to `False` for fire-and-forget pipelines where rollback is not needed.

### `spark_persist`
Spark checkpoint persistence mode when `checkpoint=True`:
- `"cache"` (default) — Spark `.cache()`, memory+disk
- `"local_checkpoint"` — Spark `.localCheckpoint()`, truncates lineage
- `"none"` — no persistence, checkpoints are unpersisted DataFrames (lineage preserved)

### `auto_unpersist`
When `True`, releases all Spark checkpoints after all steps complete. Saves memory for pipelines that don't need rollback. `result["checkpoints"]` will be empty.

### `max_checkpoints`
Caps the number of cached checkpoints. When the limit is reached, the oldest checkpoint (lowest step order) is evicted and cannot be rolled back to. Use for very long plans to bound memory:
```python
result = anchor("apply_transform", df, plan, max_checkpoints=3)
```

---

## transform Output — Complete Reference

### Step dict structure

```python
{
    "order":       1,                  # execution order number
    "action":      "null_clean",       # step type
    "columns":     ["status"],         # affected columns
    "confidence":  0.95,               # 0.0–1.0
    "code_spark":  "df = df.withColumn('status', when(...))",
    "code_pandas": "df['status'] = df['status'].replace({...}, None)",
    "description": "Replace null-like strings in 'status' with None",
    "details":     {"null_like_values": ["N/A", "", "null"]},  # step-specific metadata
}
```

### Key output fields

```python
plan["steps"]               # review this before applying
plan["code_pandas"]         # complete pandas script — paste into notebook to validate
plan["code_spark"]          # complete Spark script
plan["metrics"]["min_confidence"]  # lowest confidence step — check risks if < 0.95
plan["risks"]               # read this carefully before applying
```

---

## apply_transform Output — Complete Reference

### Accessing the result

```python
result["df"]               # USE THIS — the transformed DataFrame
result["df_before"]        # original input, always available
result["steps_applied"]    # e.g. [1, 2, 3, 4, 5]
result["steps_skipped"]    # steps that were skipped with reason
result["row_count_before"] # before
result["row_count_after"]  # after
result["rows_dropped"]     # dedup removed this many rows
result["columns_renamed"]  # {"My Column": "my_column"} — use for downstream rename mapping
result["step_audit"]       # [{step_order, action, rows_before, rows_after, delta}]
result["checkpoints"]      # {1: df, 2: df, ...} — for rollback
result["checkpoints_evicted"]  # steps that were evicted by max_checkpoints
```

---

## Common Patterns

### Full quality chain
```python
profile_ctx = anchor("profile_table", df, subject="queue_bronze")
plan = anchor("transform", profile_ctx)

# Review plan before applying
print(plan["code_pandas"])   # inspect generated code
for r in plan["risks"]:      # check any warnings
    print(r)

result = anchor("apply_transform", df, plan)
clean_df = result["df"]

# Verify
verify_ctx = anchor("quality", clean_df)
```

### Selective apply — skip uncertain steps
```python
plan = anchor("transform", profile_ctx)
low_conf = [s for s in plan["steps"] if s["confidence"] < 0.9]
print(f"Skipping {len(low_conf)} low-confidence steps")

result = anchor("apply_transform", df, plan, min_confidence=0.9)
```

### Rollback after a bad step
```python
result = anchor("apply_transform", df, plan)

# Step 4 (cast) turned out wrong — roll back to before step 4
recovered_df = anchor("rollback", result, to=3)  # state after step 3, before step 4
```

### Memory-safe apply for large Spark DataFrames
```python
result = anchor("apply_transform", spark_df, plan,
            max_checkpoints=3,           # keep only last 3 checkpoints
            spark_persist="local_checkpoint")
```

### Audit what changed
```python
result = anchor("apply_transform", df, plan)
diff_ctx = anchor("diff", result["df_before"], result["df"],
              keys=["Application ID"])
print(diff_ctx["summary"])  # "0 added, 0 removed, 12 changed, 32 unchanged"
```

---

## Edge Cases

| Scenario | Behavior |
|---|---|
| `dry_run=True` | `result["df"]` is original unchanged; `steps_applied` shows what would run |
| Step has `confidence=0.0` | Runs unless `min_confidence > 0.0` |
| Plan has 0 steps | `steps_applied=[]`, `result["df"]` is original |
| `rollback(result, to=0)` | Returns `result["df_before"]` (original input) |
| `rollback(result, to=N)` where N was evicted | `KeyError` — evicted checkpoints cannot be recovered |
| `auto_unpersist=True` | `result["checkpoints"]` is empty — rollback not possible |
| Column in `custom_overrides` not in profile | Override silently ignored |
