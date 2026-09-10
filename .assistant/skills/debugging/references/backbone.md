# Debugging techniques reference

> Preserved technique source for the native debugging owner.

Silent data failures — pipeline ran successfully but output is wrong. Load when there's no stack trace, only wrong results.

Use this reference for silent wrong-data failures. For code exceptions, stay in the
`debugging` skill and begin with `anchor("trace", "<exact traceback>")`.

## `anchor("debug")` — The One-Shot Investigation Workflow

When you don't know WHERE the problem is, run `anchor("debug")` first. It chains multiple diagnostic tools automatically:

```python
# Step 1: Run the debug workflow on your broken result
result = anchor("debug", result_df,
    upstreams={"source": raw_df, "dim": dim_df},
    keys=["order_id"],
    filter_expr="status = 'Active'"
)
```

**What it does (adaptive sequence):**
1. `diagnose_empty` — identifies which upstream/stage caused dropout
2. `pre_join` — runs ONLY for problematic upstream pairs (low key overlap)
3. `explain_row` — traces specific rows ONLY if result has data
4. `pre_merge` — runs ONLY if `target=` is provided for merge debugging

**Expected output:**
```
# Workflow: debug
## Steps Executed
  1. diagnose_empty: PASSED (result has 0 rows, dropout_cause: zero_key_overlap)
  2. pre_join (source↔dim): FAILED (overlap_pct: 0.02, type mismatch: INT vs STRING)
  3. explain_row: SKIPPED (result empty)
  4. pre_merge: SKIPPED (no target provided)
## Summary
  Root cause: join key type mismatch between source.order_id (INT) and dim.order_id (STRING)
## Suggested Next Actions
  - MUST: Cast join keys to same type before join
  - Run: anchor("coerce_check", source_df, dim_df, keys=["order_id"])
```

**When to use `anchor("debug")` vs individual tools:**

| Situation | Use | Why |
|---|---|---|
| Don't know where the problem is | `anchor("debug", ...)` | Auto-routes through diagnostic chain |
| Know it's a null issue | `anchor("microscope", df, "col")` | Targeted, faster |
| Know it's a join issue | `anchor("validate", df, ...)` + `anchor("microscope", ...)` | Direct validation |
| Need to trace one row | `anchor("trace_row", df, key_values={...})` | Row-level forensics |
| Comparing before/after | `anchor("diff", old_df, new_df, keys=[...])` | Change detection |

**NEVER skip `anchor("debug")` when the root cause is unclear** — manual investigation takes 10x longer and misses interaction effects between stages.

## Triage Decision Tree

Start here. Identify the symptom, follow the branch.

| Symptom | Likely Cause | Jump To |
|---|---|---|
| Row count too low | Overly aggressive filter or INNER JOIN dropping rows | [Row Count Mismatch](#row-count-mismatch) |
| Row count too high | JOIN fanout or missing dedup | [Row Count Mismatch](#row-count-mismatch) |
| Unexpected NULLs in output | TRY_CAST silent failure, schema drift, upstream blanks | [Null Investigation](#null-investigation) |
| Duplicates in "deduped" table | Key assumption wrong or non-deterministic ordering | [Duplicate Investigation](#duplicate-investigation) |
| Values don't match source | Type coercion, timezone, encoding, or trailing whitespace | [Value Mismatch](#value-mismatch) |
| Table not updating | Watermark stuck, merge predicate wrong, or stale cache | [Stale Data](#stale-data) |
| MERGE INTO fails | Duplicate source keys, null keys, schema mismatch, type incompatibility | [Merge Failures](#merge-failures) |
| Aggregates are off | Fanout before agg, or counting NULLs differently | [Aggregation Errors](#aggregation-errors) |
| Pipeline output is empty | Filter too aggressive, join key mismatch, upstream empty, type mismatch | [Empty Output](#empty-output) |
| Single row has wrong value | Upstream coercion, join brought wrong match, transform logic error | [Row-Level Tracing](#row-level-tracing) |

---

## Row Count Mismatch

**Diagnostic sequence (run in order):**

```python
# 1. Count at source
source_count = spark.table("catalog.schema.source").count()

# 2. Count at each pipeline stage
after_filter_count = df_filtered.count()
after_join_count = df_joined.count()
after_dedup_count = df_deduped.count()
target_count = spark.table("catalog.schema.target").count()

# 3. Print the funnel
print(f"Source: {source_count} → Filter: {after_filter_count} → "
      f"Join: {after_join_count} → Dedup: {after_dedup_count} → Target: {target_count}")
```

**Common culprits:**

| Drop point | Cause | Fix |
|---|---|---|
| Filter → too few | WHERE clause too strict | Check actual values: `df.select("status").distinct().show()` |
| Join → too few | INNER JOIN + key mismatch | Switch to LEFT, then check: `df.join(dim, ..., "left_anti").count()` |
| Join → too many | One-to-many fanout | Profile join key: `dim.groupBy("key").count().where("count > 1")` |
| Dedup → too few | Correct (dedup removes rows) | Verify expected ratio |

**Automated pre-join validation — run BEFORE joining:**

```python
# Validate join keys: no nulls, no duplicates, compatible types
anchor("validate", source_df, rules=[{"column": "join_key", "rule": "not_null"}])
anchor("validate", dim_df, rules=[{"column": "join_key", "rule": "not_null"}])

# Profile both sides to check key overlap and cardinality
anchor("microscope", source_df, "join_key")  # distinct count, nulls, top values
anchor("microscope", dim_df, "join_key")     # compare distinct counts for overlap
# → If source has keys not in dim → orphan rows will drop in INNER JOIN
# → If dim has duplicates on key → fanout risk (row count will explode)
```

**Key diagnostic query — join key overlap:**

```sql
-- Find orphan keys (rows that won't survive INNER JOIN)
SELECT COUNT(*) AS orphan_count
FROM source s
LEFT ANTI JOIN dim d ON s.key = d.key
```

---

## Null Investigation

**Diagnostic sequence:**

```python
from pyspark.sql import functions as F

# 1. Null concentration per column
null_report = df.select([
    F.round(F.sum(F.col(c).isNull().cast("int")) / F.count("*") * 100, 1).alias(c)
    for c in df.columns
])
null_report.show(truncate=False)

# 2. Find the stage where NULLs appear
# Compare null counts before and after each transform
```

**Common culprits:**

| Pattern | Cause | Fix |
| --- | --- | --- |
| NULLs only after TRY_CAST | Source data doesn't match expected format | Sample raw values: `df.where(F.col("cast_col").isNull()).select("raw_col").show(20, False)` |
| NULLs after JOIN | LEFT JOIN + missing dim entry | Check: `df.where(F.col("dim_field").isNull()).select("join_key").distinct()` |
| NULLs in source | Upstream schema change or empty cells | Compare against previous load: count NULLs in current vs prior partition |
| All values NULL for a column | Column renamed upstream, schema drift | Check: `spark.table("source").columns` vs expected |

**TRY_CAST debugging (most common for Excel/CSV sources):**

```sql
-- Find values that TRY_CAST silently nullifies
SELECT DISTINCT raw_column
FROM source_table
WHERE raw_column IS NOT NULL
  AND TRY_CAST(NULLIF(TRIM(raw_column), '') AS DATE) IS NULL
```

---

## Duplicate Investigation

**Diagnostic sequence:**

```python
# 1. Are there actually duplicates on the expected key?
from pyspark.sql import functions as F

key_cols = ["project_id", "queue_date"]
dupes = df.groupBy(key_cols).agg(F.count("*").alias("cnt")).where("cnt > 1")
dupe_count = dupes.count()
print(f"Duplicate key combinations: {dupe_count}")

# 2. Sample a duplicate to understand WHY
if dupe_count > 0:
    sample_key = dupes.limit(1).collect()[0]
    filter_expr = " AND ".join([f"{k} = '{sample_key[k]}'" for k in key_cols])
    df.where(filter_expr).show(truncate=False)
```

**Common culprits:**

| Pattern | Cause | Fix |
| --- | --- | --- |
| Dupes on assumed key | Key isn't actually unique in source | Add more columns to key or use composite |
| Dupes after dedup | Non-deterministic ordering (ties in ORDER BY) | Add tiebreaker: `ORDER BY updated_at DESC, _row_hash` |
| Dupes from multiple loads | Pipeline not idempotent (INSERT without MERGE) | Switch to MERGE or add dedup after load |
| NULL in key column | NULLs collapse in PARTITION BY | Filter NULLs before dedup or COALESCE to sentinel |

**Key check — ordering determinism:**

```sql
-- Find ties that make ROW_NUMBER non-deterministic
SELECT project_id, queue_date, COUNT(*) AS ties
FROM source
GROUP BY project_id, queue_date, file_modified_at  -- include ORDER BY col
HAVING COUNT(*) > 1
```

---

## Value Mismatch

**First step — use anchor tools to classify the mismatches automatically:**

```python
# 1. Diff to see WHAT changed — now includes null_to_value, value_to_null, value_changed per column
ctx = anchor("diff", old_df, new_df, keys=["id"])
# Check changed_column_counts: columns with value_changed > 0 have real value differences
# Columns with only null_to_value or value_to_null are null transitions, not value mismatches

# 2. For columns with value_changed > 0, classify WHY they differ
ctx = anchor("coerce_check", old_df, new_df, keys=["id"],
         columns=["suspect_col1", "suspect_col2"])
# → dominant_category tells you the root cause:
#   whitespace, case, unicode, numeric_representation, date_format, or genuine
# → suggested_fix tells you exactly what to do (TRIM, UPPER, strip zero-width, CAST, etc.)
```

**If coerce_check shows mostly representation issues** → apply the suggested fix, re-diff.
**If coerce_check shows mostly genuine** → fall through to manual investigation below.

**Manual diagnostic sequence (when coerce_check shows genuine differences):**

```python
# 1. Side-by-side comparison (sample 5 rows)
source_sample = spark.table("source").limit(5).select("id", "suspect_col")
target_sample = spark.table("target").limit(5).select("id", "suspect_col")

source_sample.join(target_sample, "id", "inner").show(truncate=False)

# 2. Check for invisible characters
from pyspark.sql import functions as F
df.select(
    F.col("col"),
    F.length(F.col("col")).alias("len"),
    F.hex(F.encode(F.col("col"), "UTF-8")).alias("hex_bytes")
).where("col IS NOT NULL").show(5, False)
```

**Common culprits:**

| Symptom | Cause | Fix |
| --- | --- | --- |
| Dates off by one day | Timezone conversion (UTC vs local) | Check source TZ, cast with `AT TIME ZONE` |
| Numbers slightly off | Float precision or string→double | Use DECIMAL(precision, scale) instead of DOUBLE |
| Strings don't match | Trailing whitespace, non-breaking spaces, BOM | `anchor("coerce_check")` classifies automatically; or TRIM + REGEXP_REPLACE |
| Booleans wrong | "True"/"False" vs "Y"/"N" vs "1"/"0" | Map explicitly, don't rely on CAST |
| Amounts doubled | Same data loaded from two files/partitions | Check `_source_file` or `_extracted_at` for overlap |

---

## Stale Data

**Diagnostic sequence:**

```sql
-- 1. When was target last updated?
SELECT MAX(_extracted_at) AS last_load, MAX(updated_at) AS latest_record
FROM catalog.schema.target

-- 2. Is source ahead of target?
SELECT MAX(updated_at) AS source_latest
FROM catalog.schema.source

-- 3. Check watermark (if incremental)
-- The watermark is usually MAX(target.updated_at)
-- If source has records AFTER this but they're not flowing, the predicate is wrong
```

**Delta version comparison — compare what changed between versions:**

```python
# Read two specific Delta versions as DataFrames
old_df = spark.read.format("delta").option("versionAsOf", 10).table("catalog.schema.target")
new_df = spark.read.format("delta").option("versionAsOf", 12).table("catalog.schema.target")

# Use anchor("diff") for row-level comparison
anchor("diff", old_df, new_df, keys=["project_id"])
# → Row-level diff: inserts, updates, deletes between versions
# → Column-level change classification per column

# Check DESCRIBE HISTORY for operation metadata and version numbers
# DESCRIBE HISTORY catalog.schema.target LIMIT 10;
```

**Common culprits:**

| Pattern | Cause | Fix |
| --- | --- | --- |
| Target stuck at old date | Watermark column has NULLs → MAX returns NULL → loads everything/nothing | COALESCE watermark: `COALESCE(MAX(updated_at), '1900-01-01')` |
| MERGE not updating | ON clause too broad (matches wrong row) | Verify MERGE predicate matches business key |
| Data in source, not in target | Filter excludes new records (status change) | Review WHERE clause against new data |
| Spark cache stale | DataFrame cached from prior run | `spark.catalog.clearCache()` or re-read |

---

## Aggregation Errors

**Diagnostic sequence:**

```python
# 1. Check for fanout BEFORE aggregation
pre_agg_count = df_before_agg.count()
expected_groups = df_before_agg.select("group_key").distinct().count()
print(f"Rows: {pre_agg_count}, Groups: {expected_groups}, Ratio: {pre_agg_count/expected_groups:.1f}")
# If ratio >> expected, there's fanout

# 2. Verify one group manually
df_before_agg.where("group_key = 'sample_value'").show(truncate=False)
```

**Common culprits:**

| Symptom | Cause | Fix |
| --- | --- | --- |
| SUM too high | JOIN fanout before aggregation (1:N) | Dedup or aggregate BEFORE joining |
| COUNT includes NULLs | Using COUNT(*) instead of COUNT(col) | Use COUNT(col) to exclude NULLs |
| AVG seems wrong | NULLs excluded from AVG denominator | Decide: COALESCE to 0 or accept NULL exclusion |
| Totals don't add up across groups | Double-counting shared entities | Check grain — entity appears in multiple groups |

---

## Merge Failures

**First step — validate merge keys and schema to catch blockers:**

```python
# Check for duplicate merge keys (#1 MERGE failure: "matched multiple source rows")
anchor("duplicate", source_df, ["project_id", "queue_date"])

# Validate merge keys are not null (null keys silently won't match)
anchor("validate", source_df, rules=[
    {"column": "project_id", "rule": "not_null"},
    {"column": "queue_date", "rule": "not_null"},
])

# Check schema compatibility with target
anchor("schema_diff", source_df, spark.table("catalog.schema.target"))
# → Type mismatches will cause write failure
```

**If validation fails** → fix the issue before attempting the merge.

**Common culprits:**

| Error / Symptom | Cause | Fix |
| --- | --- | --- |
| "MERGE ON condition matched multiple source rows" | Duplicate keys in source DataFrame | `source_df.dropDuplicates(["key1", "key2"])` or add tiebreaker |
| MERGE runs but updates 0 rows | NULL merge keys (NULL != NULL in ON clause) | Filter: `source_df.where(F.col("key").isNotNull())` |
| MERGE fails with type error | Source string column → target int column | Cast before merge: `.withColumn("col", F.col("col").cast("int"))` |
| MERGE succeeds but inserts everything | Key column has trailing whitespace or case mismatch | TRIM/UPPER keys on both sides |
| Unexpectedly large merge (full reload) | Source batch is historical, not incremental | Check `source_target_ratio` in pre_merge output — flag if >2x |
| Schema evolution error | Source has new columns not in target | Enable schema evolution: `spark.conf.set("spark.databricks.delta.schema.autoMerge.enabled", "true")` |

**Manual diagnostic (when pre_merge isn't enough):**

```python
# 1. Check what the MERGE actually did (Delta history)
spark.sql("DESCRIBE HISTORY catalog.schema.target LIMIT 5").show(truncate=False)
# Look at: operationMetrics.numTargetRowsInserted, numTargetRowsUpdated, numTargetRowsDeleted

# 2. Verify merge predicate matches business key
# If predicate is too broad (matches wrong rows) → updates overwrite good data
# If predicate is too narrow (never matches) → inserts everything as new

# 3. Check for silent type coercion in merge keys
from pyspark.sql import functions as F
source_df.select(F.col("key"), F.typeof(F.col("key")).alias("key_type")).distinct().show()
target_types = spark.table("catalog.schema.target").select(F.col("key"), F.typeof(F.col("key")).alias("key_type")).limit(5).show()
```

---

## Empty Output

**First step — systematically check WHY the result is zero rows:**

```python
# 1. Are upstreams empty?
anchor("profile_table", source_df, subject="source")  # Check row_count > 0
anchor("profile_table", dim_df, subject="dim")        # Check row_count > 0

# 2. Do join keys overlap? Check key values on both sides
anchor("microscope", source_df, "project_id")  # See distinct values
anchor("microscope", dim_df, "project_id")     # Compare — do they match?

# 3. Does filter eliminate everything?
anchor("case_file", source_df, column="status", filter="where:status = 'Active'")
# → If 0 rows match, the filter is too restrictive
```

**Common culprits:**

| Ranked Cause | What Happened | Fix |
| --- | --- | --- |
| `filter_eliminates_all` | WHERE clause matches 0 rows | Check actual values: `df.select("status").distinct().show()` |
| `key_mismatch` | JOIN keys don't overlap (0% match) | Check key formats: `anchor("coerce_check", left, right, keys=[...])` |
| `upstream_empty` | Source table itself has 0 rows | Investigate upstream pipeline |
| `type_mismatch` | Key is string in one side, int in other | Cast before join: `.withColumn("key", F.col("key").cast("string"))` |
| `all_null_columns` | Key column is entirely NULL | Check upstream ETL — column may have been dropped or renamed |

---

## Row-Level Tracing

**When you need to understand WHERE a specific output value came from:**

```python
# Trace one row back through its upstream sources
anchor("trace_row", output_df, keys=["project_id"], values={"project_id": "PJM-12345"},
   upstream={"source": source_df, "dim": dim_df})
# → For each output column: which upstream it came from, whether value matches
# → Flags: coerced (type changed), orphan (no upstream match), conflict (multiple sources disagree)
# → Shows full lineage: output_value ← upstream_value (source: dim.project_name)
```

**When to use trace_row vs other tools:**

| Scenario | Use |
| --- | --- |
| One row has a wrong value — need to trace origin | `anchor("trace_row", ...)` |
| Many rows have wrong values — pattern investigation | `anchor("coerce_check", ...)` or `anchor("diff", ...)` |
| Row is missing entirely | Profile upstreams + `anchor("case_file", ...)` |
| Column has systematic issue (all nulls, all wrong) | `anchor("microscope", ...)` |

---

## Domain-Specific Failure Modes

### Excel/CSV Sources (Bronze)

| Issue | How it manifests | Fix |
| --- | --- | --- |
| Header row in data | First row has column names as values | Skip header: `option("header", "true")` or filter where value = column name |
| Mixed types in column | "123", "N/A", "TBD" in numeric column | TRY_CAST catches this, but check null rate after cast |
| Merged cells | Same value with blanks below (filled range in Excel) | Forward-fill: window function with LAST_VALUE(IGNORE NULLS) |
| Date serial numbers | "45292" instead of "2024-01-15" | Detect: if numeric and 5 digits, convert from Excel serial |
| Trailing whitespace | "Active " ≠ "Active" in joins | TRIM all string columns at bronze→silver boundary |
| BOM character | "\uFEFF" prefix on first column name | Strip in column rename or REGEXP_REPLACE |

### ISO/Energy Domain

| Issue | How it manifests | Fix |
| --- | --- | --- |
| MW capacity as string | "1,234.5" fails numeric cast (comma) | REGEXP_REPLACE(',', '') before TRY_CAST |
| Queue position reuse | Same queue_id assigned to new project after withdrawal | Composite key must include effective_date or version |
| Status inconsistency | "Active"/"ACTIVE"/"active" across ISOs | Normalize: UPPER(TRIM(status)) at silver |
| Date format variance | "01/15/2024" (MISO) vs "2024-01-15" (PJM) vs "Jan 15, 2024" (ERCOT) | ISO-specific date parsing in bronze→silver |
| Withdrawn projects reappear | Project withdrawn then re-queued with same ID | SCD2 or add re-queue sequence number to key |
| Interconnection ID format change | ISO changes ID format mid-history | COALESCE(new_format_id, legacy_id) as unified key |

### PySpark-Specific

| Issue | How it manifests | Fix |
| --- | --- | --- |
| Partition skew | One task takes 10x longer, OOM | Salting or repartition before join |
| Broadcast threshold | Join runs shuffle when dim is small | `F.broadcast(dim_df)` for <10MB tables |
| Lazy eval hides error | Error only surfaces at action (count/write) | Add .count() checkpoints between transforms for debugging |
| Column ambiguity after join | "Reference 'id' is ambiguous" | Alias DataFrames or drop duplicate columns immediately after join |
| withColumn chain perf | 50+ withColumn calls → slow plan | Use select with multiple expressions instead |
| `catalog.tableExists()` stale on Spark Connect | Returns `False` for tables that exist between .py file runs (USER_ISOLATION) | Use `spark.sql(f"SELECT 1 FROM {table} LIMIT 1")` in try/except — SQL engine always sees committed tables |

---

## Integration with odibi-anchor

```python
# 1. Check if this failure pattern is known
anchor("known_bad", error_text="row count dropped after join", task_type="debugging")

# 2. Log the investigation
anchor("known_error", "target has 50% fewer rows than source", subject="silver_queue_positions pipeline")

# 3. If genuinely reusable, capture the evidence-backed resolution, then assess its ID
observation = anchor("learning", "capture", observation_type="reusable_practice", ...)
anchor("learning", "assess", outcome="observations_recorded",
   observation_ids=[observation["item"]["item_id"]])

# 4. Capture bounded table evidence; `touched` is only for changed file paths
source_observation = anchor("observe_table", source_profile, subject="catalog.schema.source_table")
target_observation = anchor("observe_table", target_profile, subject="catalog.schema.target_table")
```

---

## Investigation Workflow

1. **Identify symptom** → pick branch from triage tree
2. **Check known patterns** → `anchor("known_bad", error_text="...", task_type="debugging")`
3. **Run diagnostic sequence** for that branch (in order)
4. **Isolate the stage** where data diverges
5. **Identify root cause** from common culprits table
6. **Fix and verify** → re-run pipeline, compare counts
7. **Gate** → `anchor("gate")` before delivery — no args needed
8. **Assess reusable learning** → capture a genuine Observation or close with `nothing_reusable_learned`

Content quality and the non-authority firewall are owned by
the global operating contract.

## Anti-Drift Rules

- NEVER write manual `df.merge()` + column comparison — use `anchor("diff", old, new, keys=[...])`
- NEVER write manual null counts or `value_counts()` — use `anchor("profile_table", df)` or `anchor("microscope", df, col)`
- NEVER guess at root cause without running `anchor("debug")` first when cause is unclear
- NEVER skip `anchor("known_bad")` at investigation start — the pattern may already be documented
- NEVER deliver with open post-gate debt — assess real Observations or explicitly record
  `nothing_reusable_learned`
- NEVER run `anchor("gate")` without fixing the actual issue — gate checks session state, not just timing
