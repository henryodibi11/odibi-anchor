# pre_merge — Usage Guide

## Function Signature

```python
pre_merge_context(
    source_df,                            # Source DataFrame (positional)
    target,                               # Delta table name (str) or DataFrame (positional)
    keys: list[str] | None = None,       # Merge key column(s) (positional or kwarg)
    source_subject: str = "source",      # Display name for source
    target_subject: str = "target",      # Display name for target (overridden if target is str)
    output_format: str = "dict",         # "dict" or "markdown"
    sample_limit: int = 10,              # Max duplicate/sample entries
) -> dict | str
```

---

## Parameter Reference

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `source_df` | DataFrame | Yes | -- | Source data to merge into target |
| `target` | `str` or DataFrame | Yes | -- | Delta table name (`"catalog.schema.table"`) or target DataFrame |
| `keys` | `list[str]` | Yes | -- | Merge key column(s) — must exist in both source and target |
| `source_subject` | `str` | No | `"source"` | Label for source in output |
| `target_subject` | `str` | No | `"target"` | Label for target (auto-set to table name if target is str) |
| `output_format` | `str` | No | `"dict"` | `"dict"` for programmatic, `"markdown"` for report |
| `sample_limit` | `int` | No | `10` | Max duplicate key samples to show |

---

## Calling via anchor() Dispatcher

```python
# Production: target is a Delta table name
ctx = anchor("pre_merge", source_df, "catalog.schema.my_table", keys=["id"])

# Testing: target is a DataFrame
ctx = anchor("pre_merge", source_df, target_df, keys=["id"])

# Multi-key merge
ctx = anchor("pre_merge", batch_df, "gold.analytics.dim_project",
         keys=["project_id", "effective_date"])

# Named subjects + markdown
report = anchor("pre_merge", incremental_df, "silver.ops.events",
            keys=["event_id"],
            source_subject="daily_batch",
            output_format="markdown")
print(report)
```

---

## Target Resolution

| Target type | Behavior |
|-------------|----------|
| `str` (e.g. `"catalog.schema.table"`) | Loads via `spark.table(target)`; uses table name as `target_subject` |
| DataFrame | Uses directly; `target_subject` kwarg used for labeling |

When target is a string, requires an active SparkSession. For unit testing,
pass a pandas DataFrame as target instead.

---

## Result Structure

| Field | Type | Description |
|-------|------|-------------|
| `kind` | `str` | Always `"pre_merge"` |
| `subject` | `str` | `"source_subject → target_subject"` |
| `summary` | `str` | Status line: `OK` / `WARNING` / `BLOCKED` + merge behavior |
| `metrics.source_count` | `int` | Total source rows |
| `metrics.target_count` | `int` | Total target rows |
| `metrics.source_key_distinct` | `int` | Distinct key combinations in source |
| `metrics.source_duplicate_key_count` | `int` | Key groups with duplicates (0 = safe) |
| `metrics.source_null_key_count` | `int` | Rows with any NULL in merge keys |
| `metrics.overlap_count` | `int` | Source keys that exist in target (= UPDATEs) |
| `metrics.insert_count` | `int` | Source keys NOT in target (= INSERTs) |
| `metrics.update_count` | `int` | Source keys IN target (= UPDATEs) |
| `metrics.merge_behavior` | `str` | `UPDATE-only` \| `INSERT-only` \| `MIXED` \| `EMPTY` |
| `metrics.source_target_ratio` | `float` | source_count / target_count |
| `metrics.schema_compatible` | `bool` | All common columns have compatible types |
| `metrics.columns_added` | `list` | Columns in source not in target |
| `metrics.columns_removed` | `list` | Columns in target not in source |
| `metrics.columns_type_mismatch` | `list` | Columns with incompatible types |
| `findings` | `list[str]` | Observations (dupe-free, schema OK, behavior prediction) |
| `risks` | `list[str]` | Blockers and warnings |
| `samples.duplicate_keys` | `list` | Top duplicate key groups with counts |
| `samples.type_mismatch_columns` | `list` | Columns with incompatible types |
| `suggested_next_actions` | `list[str]` | Fix commands (deduplicate, filter nulls, cast types) |

---

## Summary Status Meanings

| Status | Meaning | Will MERGE succeed? |
|--------|---------|--------------------|
| `OK` | No risks detected | Yes |
| `WARNING` | Issues but merge won't fail | Yes, but with silent data issues |
| `BLOCKED` | Duplicate keys or type mismatches | No — merge WILL fail |

---

## Common Patterns

### Pattern 1: Pre-flight check before incremental merge

```python
ctx = anchor("pre_merge", daily_batch_df, "gold.analytics.fact_events",
         keys=["event_id"])

if "BLOCKED" in ctx["summary"]:
    print("Cannot merge:", ctx["risks"])
    # Fix issues first
else:
    # Proceed with merge
    daily_batch_df.write.format("delta").mode("overwrite").option(
        "mergeSchema", "true"
    ).saveAsTable(...)
```

### Pattern 2: Diagnose why MERGE INTO failed

```python
# Error: "Cannot perform Merge as multiple source rows matched..."
ctx = anchor("pre_merge", source_df, "silver.ops.projects", keys=["project_id"])
print(ctx["metrics"]["source_duplicate_key_count"])  # > 0 = the cause
print(ctx["samples"]["duplicate_keys"])  # Shows which keys are duplicated
```

### Pattern 3: Validate schema before merge with schema evolution

```python
ctx = anchor("pre_merge", new_source_df, existing_target_df, keys=["id"])

# Check what columns would be added/removed
print(ctx["metrics"]["columns_added"])    # New columns source would add
print(ctx["metrics"]["columns_removed"])  # Target columns source doesn't have

# Check type safety
for tm in ctx["metrics"]["columns_type_mismatch"]:
    print(f"{tm['column']}: {tm['source_type']} → {tm['target_type']} ({tm['note']})")
```

### Pattern 4: Check merge behavior prediction

```python
ctx = anchor("pre_merge", batch_df, "gold.dim.customers", keys=["customer_id"])

print(ctx["metrics"]["merge_behavior"])  # "MIXED"
print(f"Will INSERT: {ctx['metrics']['insert_count']:,}")
print(f"Will UPDATE: {ctx['metrics']['update_count']:,}")

# Flag full reloads masquerading as incremental
if ctx["metrics"]["source_target_ratio"] > 2.0:
    print("WARNING: Source much larger than target — is this really incremental?")
```

---

## Error Handling

| Error | Cause | Fix |
|-------|-------|-----|
| `ValueError: source_df is required` | Missing source arg | Pass source DataFrame |
| `ValueError: target is required` | Missing target arg | Pass table name or DataFrame |
| `ValueError: keys is required` | No merge keys specified | Add `keys=["id"]` |
| `ValueError: Merge key(s) not found in source` | Key column doesn't exist in source | Check column names |
| `ValueError: Merge key(s) not found in target` | Key column doesn't exist in target | Check target schema |
| `RuntimeError: No active SparkSession` | Target is string but no Spark available | Use DataFrame target for testing |

---

## Tips

- **Duplicate keys = MERGE WILL FAIL** — this is the #1 merge failure cause. Always check before merge.
- **NULL keys silently don't match** — `NULL != NULL` in MERGE ON, so these rows become phantom INSERTs
- **`source_target_ratio > 2`** — usually means you're accidentally merging a full reload rather than an increment
- **Schema check catches string→int** — common when source comes from CSV/Excel with string types
- **Use `output_format="markdown"`** for quick visual validation in notebooks
- **Pairs with `anchor("pre_join")`** — pre_join checks JOIN safety; pre_merge checks MERGE INTO safety