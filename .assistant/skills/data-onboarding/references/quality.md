# Data Onboarding — Quality & Remediation

> Preserved onboarding quality technique.

Clean the data systematically using the transform pipeline. Never with ad-hoc code.

**You were routed here from `skills/data-onboarding/SKILL.md`.** Return there after completing
Phase 4 to continue with Phase 5 (schema design).

## Applicable assurance overlay

When structured selection activates `data.quality`, use
`.assistant/references/assurance/standards-overlays.md` for the applicable outcomes and
result-evidence contract. This workflow remains the procedure owner; the pointer creates no
obligation by itself.

## Hard Rule: NEVER Write Raw Cleaning Code

| Instead of... | Use... |
|---|---|
| `df.fillna(...)` | `anchor("transform")` → `anchor("apply_transform")` |
| `df.dropDuplicates(...)` | Transform plan generates dedup step automatically |
| `df.withColumn("col", F.trim(...))` | Transform plan includes `null_cleanup` step |
| Manual `REGEXP_REPLACE` + `TRY_CAST` | Transform plan includes `cast` step |
| `df.filter(...)` for bad rows | `anchor("validate")` identifies, transform removes |
| `df.drop("col")` for constants | Transform plan `drop_constant` step (enable explicitly) |

## When to Use coerce_fix Instead of Transform

If `anchor("coerce_check")` shows the issues are purely **formatting** (whitespace, case, unicode,
numeric_representation, date_format) — use `coerce_fix` for targeted column fixes:

```python
# After diff reveals formatting mismatches between old and new data
coerce_ctx = anchor("coerce_check", old_df, new_df, keys=["id"], columns=["name", "status"])

# Generate fix plan from coerce_check findings, then apply
fix_plan = anchor("transform", coerce_ctx)  # Generates transform plan from findings
cleaned_df = anchor("apply_transform", new_df, fix_plan)
# → Whitespace: TRIM + collapse internal whitespace
# → Case: UPPER or LOWER (configurable via plan)
# → Unicode: strip zero-width characters + NFC normalize
# → Numeric: remove commas/spaces, strip trailing .0
# → Date: parse flexibly to ISO format (YYYY-MM-DD)
```

**Use transform → apply_transform when:** Mismatches are formatting (`representation_pct` > 50% in coerce_check).
**Use transform from profile_table when:** Issues are structural (nulls, types, dedup, outliers, constants).

## Step 1: Generate Transform Plan

Feed the profiler output directly — transform auto-detects profiler context:

```python
profile_ctx = anchor("profile_table", df, subject="source_name", output_format="dict")
plan_ctx = anchor("transform", profile_ctx, subject="source_name")
```

**Review the plan before applying.** The plan generates steps for:

| Step | What it does | When it applies |
|---|---|---|
| `standardize_columns` | Normalize column names (lowercase, underscores) | Always |
| `null_cleanup` | TRIM whitespace, collapse null sentinels ('', 'N/A', '--') to NULL | Always |
| `cast` | `TRY_CAST(NULLIF(TRIM(col), ''))` for type conversions | Columns with inferable types |
| `boolean_cast` | Y/N/Yes/No → true/false | Boolean-pattern columns |
| `date_parse` | Format-aware date parsing | Date-pattern columns |
| `drop_constant` | Remove all-null or single-value columns | Only if `include_drop_constant=True` |
| `dedup` | Deduplicate on candidate key with explicit ordering | If duplicates detected |

### Customizing the Plan

```python
# Skip type casting (keep everything as string — bronze layer)
plan_ctx = anchor("transform", profile_ctx, include_cast=False)

# Also drop constant/all-null columns
plan_ctx = anchor("transform", profile_ctx, include_drop_constant=True)

# Exclude specific columns from all transforms
plan_ctx = anchor("transform", profile_ctx, custom_overrides={"legacy_col": "skip"})
```

## Step 2: Apply with Checkpoints

```python
result = anchor("apply_transform", df, plan_ctx, checkpoint=True)
cleaned_df = result["df"]
```

**Checkpoint = rollback safety.** If a step produces unexpected results:

```python
# Roll back to before the problematic step
before_cast = anchor("rollback", result, to="cast")

# Investigate what went wrong
anchor("microscope", before_cast, "problem_column", output_format="dict")

# Re-apply with adjustments
```

### Spark Checkpoint Options

| Option | When to use |
|---|---|
| `spark_persist="cache"` (default) | Normal pipelines — materializes each checkpoint |
| `spark_persist="local_checkpoint"` | Long pipelines with many steps — truncates lineage |
| `spark_persist="none"` | Quick exploration — lazy, recomputes on rollback |
| `auto_unpersist=True` | Fire-and-forget — no rollback needed, free memory immediately |
| `max_checkpoints=3` | Memory-constrained — keep only last N checkpoints |

## Step 2b: Pre-Join Validation (if joining to reference data)

Before joining cleaned data to dimension or lookup tables, validate the join keys:

```python
# Profile both sides to check key overlap and quality
left_ctx = anchor("profile_table", cleaned_df, subject="cleaned_source")
right_ctx = anchor("profile_table", dim_df, subject="dim_table")

# Validate join keys: no nulls, no duplicates, compatible types
anchor("validate", cleaned_df, rules=[
    {"column": "join_key", "rule": "not_null"},
    {"column": "join_key", "rule": "unique"},
])
```

This catches:
- NULL keys that will silently drop
- Duplicate keys causing fanout (many:many producing more rows than expected)
- Format mismatches via profiling (whitespace, case, unicode causing false non-matches)

**If validation shows issues** → fix keys before joining (TRIM, UPPER, filter NULLs).
**If validation passes** → proceed with the join confidently.

## Step 3: Validate Business Rules

For rules the profiler can't infer — domain-specific constraints:

```python
anchor("validate", cleaned_df, rules=[
    {"type": "not_null", "columns": ["primary_key", "required_field"]},
    {"type": "unique", "columns": ["primary_key"]},
    {"type": "accepted_values", "column": "status",
     "values": ["Active", "Inactive", "Pending"]},
    {"type": "range", "column": "capacity_mw", "min": 0, "max": 10000},
    {"type": "expression", "expr": "start_date <= end_date",
     "description": "start before end"},
])
```

### Step 3b: Derive Validation Rules from Profile

Instead of writing rules manually, use profile findings to inform them:

```python
# Profile tells you which columns are not_null, unique, have ranges, etc.
profile_ctx = anchor("profile_table", cleaned_df, subject="source_name", output_format="dict")

# Build rules from profile findings:
# - null_pct=0 → not_null rule
# - distinct_pct=1.0 → unique rule
# - numeric columns → range rules from min/max
rules = [
    {"column": "id", "rule": "not_null"},
    {"column": "id", "rule": "unique"},
    {"column": "status", "rule": "accepted_values", "values": ["Active", "Withdrawn", "Pending"]},
    {"column": "mw_capacity", "rule": "range", "min": 0, "max": 5000},
]

# Validate with rules
anchor("validate", cleaned_df, rules=rules)
```

**Review rules before applying** — profile findings inform data patterns, not business intent.
Use `profile_ctx["findings"]` to identify which columns need which rule types.

## Step 4: Quality Gate

```python
anchor("quality", cleaned_df, subject="source_name", keys=["primary_key"])
```

This is the final quality check. It catches:
- Duplicate keys (MUST be zero)
- Null keys (MUST be zero)
- Anomalies in value distributions

## Step 4b: Pre-Merge Validation (if writing to Delta via MERGE)

Before running MERGE INTO on the target table, validate the cleaned data is merge-safe:

```python
# Validate merge keys: no nulls, no duplicates (the #1 MERGE failure)
anchor("validate", cleaned_df, rules=[
    {"column": "project_id", "rule": "not_null"},
    {"column": "queue_date", "rule": "not_null"},
])

# Check for duplicate merge keys
anchor("duplicate", cleaned_df, ["project_id", "queue_date"])

# Compare schema against target
anchor("schema_diff", cleaned_df, spark.table("catalog.schema.target_table"))
```

This catches:
- Duplicate source keys (the #1 MERGE failure: "matched multiple source rows")
- NULL merge keys (silently won't match — rows lost without error)
- Schema/type mismatches (string→int will fail at write time)

**If validation shows issues** → fix before merging:
- Duplicates: `cleaned_df.dropDuplicates(["project_id", "queue_date"])`
- NULL keys: filter or investigate why they're null
- Type mismatch: `.withColumn("col", F.col("col").cast("target_type"))`

**If validation passes** → proceed with MERGE confidently.

## Step 5: Verify Remediation

Compare cleaned vs raw to confirm transforms didn't corrupt data:

```python
# Schema changes — columns added/removed/retyped
anchor("schema_diff", df, cleaned_df)

# Row count delta — explainable?
raw_count = df.count()
clean_count = cleaned_df.count()
print(f"Raw: {raw_count:,} → Cleaned: {clean_count:,} (delta: {raw_count - clean_count:,})")
# Delta should equal: deduped rows + filtered null-key rows
```

### Step 5b: Schema Evolution (if target table schema changed)

If `anchor("schema_diff")` shows structural changes needed on the target Delta table:

```python
# Detect schema differences
diff_ctx = anchor("schema_diff", cleaned_df, spark.table("catalog.schema.target"))
# → Shows: added columns, dropped columns, type changes
# → Review diff_ctx["findings"] to classify safety:
#    safe (add column), caution (widen type), unsafe (narrow type)

# Apply schema changes manually with ALTER TABLE:
# ALTER TABLE catalog.schema.target ADD COLUMN new_col STRING;
# ALTER TABLE catalog.schema.target ALTER COLUMN col TYPE BIGINT;
```

**Or use the composed workflow** which chains schema_diff → transform → validate automatically:

```python
anchor("evolve", cleaned_df, target_schema="catalog.schema.target", subject="source_name")
```

**Safety rules:**
- Adding columns → always safe
- Widening types (int→bigint, float→double) → safe
- Narrowing types (bigint→int, double→float) → **unsafe** — data loss possible
- Dropping columns → only with explicit `allow_drops=True`

### Free Checkpoints When Done

```python
anchor("unpersist", result)  # Release all cached checkpoints
```

## Phase 4 Gate

✅ You have a `cleaned_df` where:
- `anchor("quality")` passes with zero duplicate keys and zero null keys
- `anchor("validate")` passes all business rules
- You can explain every transform step that was applied
- Row count delta from raw is explainable (dedup count + null-key filter count)
- `anchor("schema_diff")` shows only expected changes (no surprise column drops)

❌ If quality or validate fails → iterate. Do NOT proceed to schema design with dirty data.

## Post-Write: Storage Health Check

After writing to Delta, verify the table's physical layout is healthy:

```python
# Profile the target table to check row counts, freshness, and basic health
anchor("profile_table", "catalog.schema.target")
# → Confirms: row count, column count, freshness, quality_score
# → If quality issues detected: follow suggested_next_actions

# For small files / partition issues, use Delta OPTIMIZE directly:
# OPTIMIZE catalog.schema.target ZORDER BY (project_id);
# VACUUM catalog.schema.target RETAIN 168 HOURS;
```

**Common post-write issues:**

| Issue | Symptom | Fix |
|---|---|---|
| Small files | Thousands of <1MB files after streaming/frequent merges | `OPTIMIZE catalog.schema.target` |
| Partition skew | One partition has 10x more data | Repartition by a more balanced key |
| Empty partitions | Partitions with 0 rows after DELETE | `VACUUM catalog.schema.target` |

**Return to `skills/data-onboarding/SKILL.md` Phase 5.**
