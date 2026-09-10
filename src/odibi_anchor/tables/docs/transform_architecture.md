# transform + apply_transform — Architecture

**Modules:**
- `tables/transform_plan_context.py` — plan generator
- `tables/apply_transform_context.py` — plan executor

---

## System Overview

```
profile_ctx (from anchor("profile_table"))
       │
       ▼
  transform_plan_context()
  ┌────────────────────────────────────────────────────┐
  │  [1] Auto-detect profiler output kind              │
  │      adapt_profile_to_transform_input() if needed  │
  │                                                    │
  │  [2] Generate steps in order:                      │
  │      standardize (snake_case column names)         │
  │      null_clean  (replace '', 'N/A', 'null', etc.) │
  │      cast        (string → int/float/date/bool)    │
  │      date_parse  (string date → DATE type)         │
  │      drop_constant (all-null / constant columns)   │
  │      dedup       (remove duplicate rows)           │
  │                                                    │
  │  [3] Assign order numbers                          │
  │  [4] Generate code_spark + code_pandas blocks      │
  │  [5] Build metrics, findings, risks                │
  └────────────────────────────────────────────────────┘
       │
       │  plan_ctx (review steps + code before proceeding)
       ▼
  apply_transform_context()
  ┌────────────────────────────────────────────────────┐
  │  [1] Validate plan_ctx kind                        │
  │  [2] Optionally filter steps (steps=, min_conf)    │
  │  [3] For each step:                                │
  │      checkpoint df state (before step)             │
  │      dispatch to handler function                  │
  │      record row counts before/after                │
  │      evict oldest checkpoint if max_checkpoints    │
  │  [4] Collect renamed columns, step audit           │
  │  [5] Build result dict                             │
  └────────────────────────────────────────────────────┘
       │
       ▼
  result["df"]   (transformed DataFrame)
  result["checkpoints"]  (rollback state per step)
```

---

## Step Types

| Action | What it does | Generated code pattern |
|---|---|---|
| `standardize` | Rename columns to snake_case | `df.rename(columns={...})` |
| `null_clean` | Replace null-like strings with `None` | `df["col"].replace({...}, None)` |
| `cast` | Cast string column to inferred type | `df["col"].astype(int/float/bool)` |
| `date_parse` | Parse string dates to datetime | `pd.to_datetime(df["col"], format=...)` |
| `drop_constant` | Drop columns that are 100% identical | `df.drop(columns=[...])` |
| `dedup` | Remove duplicate rows on detected key | `df.drop_duplicates(subset=[...])` |

Each step dict:
```python
{
    "order":      1,
    "action":     "standardize",
    "columns":    ["My Column", "Another Col"],
    "confidence": 0.95,
    "code_spark": "df = df.withColumnRenamed(...)",
    "code_pandas": "df = df.rename(columns={...})",
    "description": "Rename 2 columns to snake_case",
}
```

---

## Checkpoint Model

`apply_transform` stores the DataFrame state before each step executes:

```
step 1 runs → checkpoint[1] = df_before_step_1
step 2 runs → checkpoint[2] = df_before_step_2
step 3 runs → checkpoint[3] = df_before_step_3
result["df"] = df_after_all_steps
result["df_before"] = original input (always)
```

`rollback(result, to=2)` returns `checkpoint[2]` — the state before step 2 ran (i.e. after step 1).

**Spark persistence:** Spark DataFrames are cached (`.cache()`) at each checkpoint to prevent DAG re-execution. Use `auto_unpersist=True` for fire-and-forget pipelines where rollback is not needed.

---

## Design Decisions

**No exec() or eval().**
All step handlers are concrete functions dispatched by action name. Generated `code_spark`/`code_pandas` blocks are for human review only — they are not executed. The actual execution goes through typed handlers.

**Confidence scores.**
Each step has a confidence float (0.0–1.0) reflecting how certain the generator is that the step is correct. Steps with low confidence (`< 0.99` for casts) are flagged in `risks`. Use `min_confidence=` in `apply_transform` to skip uncertain steps.

**Review-then-execute workflow.**
`transform` returns a plan. The user reviews `plan["steps"]` and `plan["code_pandas"]` before calling `apply_transform`. This is intentional — automated cleaning has consequences, and the generated code is the audit trail.

**Profiler auto-adaptation.**
`transform_plan_context` accepts raw output from `anchor("profile_table")` and auto-adapts the format. The user doesn't need to manually convert.

---

## Internal Structure

```
tables/
├── transform_plan_context.py
│   ├── transform_plan_context()       ← public entry point
│   ├── adapt_profile_to_transform_input() ← profiler output adapter
│   ├── _gen_standardize_step()        ← snake_case rename steps
│   ├── _gen_null_cleanup_steps()      ← null-like string cleanup
│   ├── _gen_cast_steps()              ← type cast steps
│   ├── _gen_date_parse_steps()        ← date format detection + parse
│   ├── _gen_drop_constant_steps()     ← constant column detection
│   ├── _gen_dedup_step()              ← dedup key detection
│   ├── _assemble_code_spark()         ← full Spark code block
│   └── _assemble_code_pandas()        ← full pandas code block
│
└── apply_transform_context.py
    ├── apply_transform_context()      ← public entry point
    ├── _dispatch_step_pandas()        ← routes action → pandas handler
    ├── _dispatch_step_spark()         ← routes action → spark handler
    ├── _handle_standardize_pandas()   ← rename handler
    ├── _handle_null_clean_pandas()    ← null replacement handler
    ├── _handle_cast_pandas()          ← cast handler
    ├── _handle_dedup_pandas()         ← dedup handler
    ├── _evict_oldest_checkpoint()     ← memory management
    └── render_apply_transform_report() ← markdown renderer
```

---

## Anchor Standard Output Contract

### transform output

```python
plan = {
    "kind":    "transform_plan_context",
    "subject": "queue_bronze",
    "summary": "6 transforms: 1 standardize, 3 null_clean, 2 cast",
    "metrics": {
        "total_steps":       6,
        "columns_affected":  8,
        "step_counts":       {"standardize": 1, "null_clean": 3, "cast": 2},
        "has_dedup":         False,
        "min_confidence":    0.90,
    },
    "steps":       [...],        # list of step dicts
    "code_spark":  "...",        # full Spark code block
    "code_pandas": "...",        # full pandas code block
    "findings":    [...],
    "risks":       [...],
    "suggested_next_actions": ["MUST: Review generated code before executing"],
}
```

### apply_transform output

```python
result = {
    "kind":             "apply_transform_context",
    "subject":          "queue_bronze",
    "summary":          "Applied 6 steps. 0 rows dropped.",
    "df":               <transformed DataFrame>,
    "df_before":        <original DataFrame>,
    "checkpoints":      {1: df0, 2: df1, 3: df2, ...},
    "steps_applied":    [1, 2, 3, 4, 5, 6],
    "steps_skipped":    [],
    "row_count_before": 44,
    "row_count_after":  44,
    "rows_dropped":     0,
    "columns_renamed":  {"Interconnection Size (MW)": "interconnection_size_mw"},
    "step_audit":       [{...}],
    "checkpoints_evicted": [],
    "metrics":          {...},
    "findings":         [...],
    "risks":            [...],
}
```

---

## Integration

```
quality chain:
  profile_table → quality → validate → transform → apply_transform → rollback?
                                            │
                                            └──→ diff (verify what changed)
                                            └──→ quality (verify issues resolved)
```
