# Data-operations planning reference

> Preserved operations planning technique; onboarding-specific use is summarized by the onboarding owner.

Evidence-gathering checklist for data engineering tasks. Execute this checklist BEFORE calling
`anchor("task")`. Every item you complete becomes a `known_fact` or `constraint` in your plan.

Use this reference only after `data-operations` owns a material transformation or mutation.

## Architecture Decisions — Settle BEFORE Gathering Evidence

Before profiling a single table, answer these questions. They govern every decision downstream.

### Where Does Business Logic Live?

| Logic type | Belongs in | NOT in |
|---|---|---|
| Column transforms (rename, cast, trim) | PySpark Column expressions in a Python module | SQL VIEW, notebook cell |
| Market/source-specific mappings | Declarative Python dicts + `_apply_column_map()` | SQL CASE/WHEN blocks |
| Date/string parsing | PySpark Column expression functions (`F.when`, `F.regexp_extract`, etc.) | Unity Catalog SQL UDFs |
| Enrichment joins & derived columns | `build_enriched()` style PySpark function | SQL VIEW joining multiple CTEs |
| Merge/diff/status logic | Python function in `lib/transforms.py` or equivalent | Inline SQL in a notebook |
| Ad-hoc exploration, one-off queries | SQL notebook or `spark.sql()` | — (SQL is fine here) |

**Rule:** If logic will run in production, it lives in a Python module as PySpark. SQL is for ad-hoc exploration only.

### Why PySpark Over SQL for Business Logic?

1. **Testable** — Python functions can be unit tested with pandas engine; SQL views cannot
2. **Reviewable** — diffs on Python dicts and functions are readable; 150-line SQL views are not
3. **Composable** — PySpark functions chain; SQL views nest and become opaque
4. **Maintainable** — adding a market means adding a dict entry, not editing a CASE/WHEN cascade

### When SQL is Acceptable

- Interactive exploration in a notebook
- Simple `spark.sql("SELECT count(*) FROM ...")` sanity checks
- One-off data fixes that won't be repeated
- NEVER for logic that will be scheduled, reused, or reviewed by teammates

**Record as constraints:**
- "All business logic in PySpark Python modules — no SQL views, no UC UDFs for transform logic"
- "Declarative column maps for market/source-specific mappings"
- "Column expression functions over UDFs for parse/transform operations"

## Evidence Checklist — Execute In Order

### 1. Understand the Source

```python
source_df = spark.table("catalog.schema.source_table")
anchor("profile_table", source_df, subject="catalog.schema.source_table")
```

**Record as known_facts:**
- Source table/file path (full 3-level name)
- Row count
- Column names and types (especially: which are strings that should be typed)
- Null rates per column (which columns have NULLs, what percentage)
- Cardinality of key columns (are they unique? what's the grain?)
- Freshness (when was data last updated)
- Delta metadata if applicable (partitioning, clustering, size)

**If source is a file (Excel/CSV):**
- File path and format
- Header row position
- Known data quality issues (mixed types, merged cells, trailing whitespace)
- Date format(s) used
- Numeric formatting (commas in numbers, currency symbols)

### 2. Understand the Target

If the target table already exists:

```python
target_df = spark.table("catalog.schema.target_table")
anchor("profile_table", target_df, subject="catalog.schema.target_table")
anchor("contract", target_df, subject="catalog.schema.target_table", candidate_key_columns=["id"])
```

**Record as known_facts:**
- Target table name (full 3-level name)
- Current schema (column names, types)
- Current row count
- Primary/business key
- Partitioning and clustering
- Write mode (append, overwrite, merge)
- Downstream consumers (who reads this table?)

If the target doesn't exist yet:

**Record as known_facts:**
- Target table name (planned)
- Planned schema (columns, types)
- Primary/business key
- Planned partitioning
- Write mode
- "Target does not exist yet — will be created"

### 3. Define the Grain

The grain is the level of detail in each row. This is the most important design decision.

| Question | How to answer |
|---|---|
| What does one row represent? | e.g., "one project per queue date" or "one invoice line item" |
| What columns define uniqueness? | The composite key (e.g., `project_id + queue_date`) |
| Is the grain verified? | `df.groupBy(key_cols).count().where("count > 1").count()` = 0 |

**Record as known_facts:**
- Grain definition in plain English
- Composite key columns
- Verified unique? (yes/no, with count of duplicates if any)

**Rule:** If you cannot define the grain, STOP. Ask the user.
Wrong grain = wrong everything downstream.

### 4. Map Source-to-Target

Build the explicit mapping:

| Source Column | Target Column | Transform | Notes |
|---|---|---|---|
| `raw_project_id` | `project_id` | TRIM, UPPER | Business key |
| `capacity_mw` | `capacity_mw` | REGEXP_REPLACE(',','') → TRY_CAST(DOUBLE) | Has commas |
| `queue_date` | `queue_date` | TRY_CAST(DATE) | Format: MM/DD/YYYY |
| (none) | `_extracted_at` | current_timestamp() | Audit column |
| `legacy_status` | (drop) | — | Replaced by `status` |

**Record as known_facts:**
- Full column mapping table
- Columns that need type casting (and what format)
- Columns to add (audit columns, derived columns)
- Columns to drop
- Columns requiring special handling (nulls, mixed types)

### 5. Profile for Data Quality Issues

```python
anchor("profile_table", source_df, subject="catalog.schema.source_table", level="deep")
```

**Record as known_facts:**
- Columns with >5% null rate (need COALESCE or filter decision)
- Columns with suspicious cardinality (constant columns, near-unique columns)
- Columns with mixed types (strings that look like numbers/dates)
- Outlier values that suggest data quality problems

**Record as constraints:**
- Null handling strategy per column (COALESCE to default, filter out, leave as NULL)
- Type casting strategy (TRY_CAST with NULLIF(TRIM(...), ''))
- Dedup strategy (ROW_NUMBER with explicit ordering + tiebreaker)

### 6. Check Framework Functions

```python
anchor("lookup", "project data access conventions")
anchor("lookup", "project persistence conventions")
anchor("lookup", "relevant_transformer_name")
```

**Record as known_facts:**
- Correct import paths and signatures
- Available options (merge predicates, write modes, etc.)
- Any existing transformers that do part of what you need

**Record as constraints:**
- Use native Spark/Pandas I/O unless the current project defines an approved abstraction
- Separate transforms from persistence; validate and gate production writes
- NEVER `CAST()` — use `TRY_CAST()` with `NULLIF(TRIM(col), '')`
- Reusable transforms return DataFrames; the caller owns persistence

### 7. Define Pipeline Steps

Design the pipeline as numbered, verifiable steps:

```
Step 1: Read source → verify row count matches expectation
Step 2: Standardize columns → verify no duplicate column names
Step 3: Type cast → verify null rate didn't spike (TRY_CAST failures)
Step 4: Dedup → verify key uniqueness
Step 5: Quality gate → verify no duplicate keys, null key rate = 0
Step 6: Write to target → verify target row count
```

**Record as acceptance_criteria:**
- One criterion per step with a specific verification
- Final row count expectation (or acceptable range)
- Key uniqueness verified
- Null rate thresholds for critical columns
- Idempotent on re-run (same input → same output)

### 8. Bound Historical Context

**Record as known_facts:**
- Current authoritative records describing prior source/target issues
- Current data contracts and observed quality constraints

After task acceptance, review only its bounded `memory_context` for advisory prior patterns.
Do not issue a pre-task full-store/tag scan or force selected advice into the plan.

## Output — What You Feed to `anchor("task")`

After completing this checklist, you should have:

```python
known_facts = [
    "Source: analytics_dev.bronze.queue_pjm (45,000 rows, 22 columns, all STRING)",
    "Target: analytics_dev.silver.queue_positions (will create, does not exist)",
    "Grain: one row per (project_id, queue_date) — verified unique in source",
    "Key columns: project_id (0% null), queue_date (0.2% null → filter)",
    "Capacity: has commas ('1,234.5') — needs REGEXP_REPLACE before TRY_CAST",
    "Dates: MM/DD/YYYY format — needs TRY_CAST with format",
    "15 columns to keep, 7 to drop (legacy/duplicate)",
    "Null handling: COALESCE status to 'Unknown', filter null queue_date",
    "Past learning: PJM renamed project_id to proj_id in ERAS-2025 batch",
    "Data access: no project-specific abstraction is configured; use native Spark",
    "Persistence: initial load uses an approved overwrite after quality validation",
]

constraints = [
    "Use TRY_CAST with NULLIF(TRIM(...), '') for all type conversions",
    "Use the current project's documented I/O conventions and gate writes",
    "Dedup with ROW_NUMBER + tiebreaker (_row_hash) — no non-deterministic ordering",
    "Must be idempotent on re-run",
    "3-level Unity Catalog names always",
]

acceptance_criteria = [
    "Target has ~44,900 rows (source minus null queue_date minus duplicates)",
    "Zero duplicate (project_id, queue_date) combinations in target",
    "capacity_mw column is DOUBLE type with <1% null rate",
    "All date columns are DATE type",
    "Pipeline re-run produces identical output",
]

in_scope = ["Bronze → Silver transform for PJM queue data"]
out_of_scope = ["Silver → Gold aggregation", "Other ISO sources"]

risks = [
    "PJM may rename columns again — add column validation at bronze read",
    "Commas in capacity may have other formats — sample first 1000 rows",
]
```

Continue the accepted task using the gathered evidence.
## Formal Spec compliance
If accepted task policy requires a formal Spec, use `writing-specs`, include its
compliance requirements, and review the exact linked Spec to a rating ≥ good
before consequential effects. Do not infer a Spec requirement from mode alone.

---

## Layer Design Conventions (Bronze / Silver / Gold)

Settle these BEFORE designing any medallion pipeline. These are team-proven defaults—deviate only with explicit justification recorded in `known_facts`.

### Bronze Layer
- **Write mode**: Full overwrite — bronze is a faithful raw copy, not an accumulator
- **Column types**: ALL columns as STRING — no casting at bronze; that is silver's job
- **Incremental window**: 14-day watermark on `_fivetran_synced` (buffer for late-arriving source changes)
- **Audit column**: `_ingested_at = current_timestamp()` stamped at write time
- **Parallelism**: `ThreadPoolExecutor` is appropriate — each table is isolated I/O, no shared state, connection pool capped by `MAX_BRONZE_WORKERS`
- **Orchestration contract**: `main()` MUST `return results` dict so the orchestrator can gate on per-table failures before running the next layer

### Silver Layer
- **Write mode**: MERGE (not overwrite) — incremental upsert keyed on business/surrogate keys
- **Dedup ordering**: `_fivetran_synced DESC`, NOT `_ingested_at` — two versions of the same source row in the same bronze run share an identical `_ingested_at`; ordering becomes non-deterministic. `_fivetran_synced` is set by Fivetran each time the source row changes, so it is always distinct between versions
- **Read window**: Full bronze snapshot with 14-day overlap — ensures late-arriving source changes are caught
- **Type casting**: Happens here, not at bronze — `TRY_CAST` with `NULLIF(TRIM(col), '')` for all conversions
- **Parallelism**: `ThreadPoolExecutor` still appropriate but lower `max_workers` than bronze (more memory per table)
- **Orchestration contract**: `main()` MUST `return results` dict

### Gold Layer
- **Write mode**: Full overwrite with `overwriteSchema=True` — consumers always get a clean schema; upstream column additions do not require a manual `ALTER TABLE`
- **Parallelism**: Sequential only — small number of tables (3–5), simpler to reason about failures
- **Dedup**: `row_number()` window function — Spark Connect SCPAP004 compliant; NEVER `dropDuplicates()` (non-deterministic without explicit ordering)
- **Source filters**: `_fivetran_deleted` applied at read time with an inline comment explaining soft-delete exclusion — do not assume upstream cleaned it
- **Schema contract**: Final `.select()` is the schema contract; no intermediate `.drop()` calls

### Orchestrator (`run_all.py`) Pattern
- **Early-stop on layer failure**: Check layer results before proceeding — running gold on broken silver produces silently wrong output
- **Module reload**: `importlib.reload(layer_module)` per layer so notebook edits are picked up without a kernel restart
- **Per-layer timing**: Log layer duration so failures are easy to locate in output
- **Single entry point**: One file runs the full pipeline; `main(spark)` called at bottom

### Fivetran Source Conventions
When the source layer is Fivetran-synced:
- `_fivetran_deleted = TRUE`: soft-delete flag — filter at read time; never assume upstream already excluded it
- `_fivetran_synced`: Fivetran-set timestamp of last source-system change — use for dedup ordering, not `_ingested_at`
- `_fivetran_id`: Fivetran-generated surrogate key — valid as primary key when no natural key exists in the source system

### Spark Connect Gotchas (USER_ISOLATION Clusters)

When pipeline code runs as workspace .py files on USER_ISOLATION clusters (Spark Connect):

| API | Problem | Fix |
| --- | --- | --- |
| `spark.catalog.tableExists()` | Returns stale `False` between .py file executions — catalog cache doesn't refresh | `spark.sql(f"SELECT 1 FROM {table} LIMIT 1")` in try/except |
| `spark.catalog.listTables()` | Same staleness issue | Use `SHOW TABLES IN schema` via spark.sql |

**Root cause**: Spark Connect sessions use a catalog API cache that doesn't invalidate between
separate .py file executions on the same cluster. The SQL engine bypasses this cache entirely.

**Pattern**: Any existence check should use `spark.sql()` with try/except, never `spark.catalog.*`:
```python
def _table_exists(table_name: str) -> bool:
    spark = _get_spark()
    try:
        spark.sql(f"SELECT 1 FROM {table_name} LIMIT 1")
        return True
    except Exception:
        return False
```
