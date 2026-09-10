# Data Onboarding Reference

> Preserved technique source for the native owner.

Take a raw, unfamiliar data source and make it pipeline-ready. This skill is the router —
it sequences your existing skills into a repeatable playbook with decision gates between phases.

**Load this skill when:**
- Onboarding a new Excel file, CSV, JSON/JSONL, or external data source
- Ingesting data from an unfamiliar table or API for the first time
- Someone hands you a file and says "get this into our lakehouse"
- You need to go from "what is this?" to "pipeline-ready" systematically

## The Onboarding Flow

```
Phase 1-2: Source Discovery & Ingestion   "What IS this? Read it correctly."
    │
    ▼
Phase 3: Profiling & Investigation        "What's in it? What's wrong?"
    │
    ▼
Phase 4: Quality & Remediation            "Fix what's broken."
    │
    ▼
Phase 5: Schema Design                    "Design the target table."
    │
    ▼
Phase 6: Pipeline Readiness               "Build the repeatable pipeline."
```

## Phase Routing — Load the Right Sub-Skill

### Phase 1-2: Source Discovery & Ingestion

Route by source type:

| Source type | Load skill | First action |
|---|---|---|
| **Excel (.xlsx/.xls/.xlsm)** | [Excel reference](excel.md) | Inspect workbook metadata with an available native reader |
| **CSV / TSV** | [CSV reference](csv.md) | Encoding + delimiter detection |
| **Existing Delta/Spark table** | Skip to Phase 3 | `anchor("profile_table", "catalog.schema.table")` |
| **JSON / JSONL** | [JSON reference](json.md) | Bounded stdlib/streaming structure inspection |
| **API / Parquet** | Skip to Phase 3 | Read with native Spark/Pandas or the project's approved reader -> `anchor("profile_table", df, subject="source")` |

**Phase 1-2 Gate:** You have a DataFrame where row count matches expected, column names are
actual headers (not data values), all columns are STRING type, and `anchor("profile_table")` confirms
structural integrity.

### Phase 3: Profiling & Investigation

Use [profiling](profiling.md) for the full investigation chain.

```
profile_table → microscope (flagged columns) → case_file (specific problems)
```

For data refreshes (new month's file vs prior version), also run:
```
anchor("diff", old_df, new_df, keys=[...]) → anchor("coerce_check") on columns with value_changed > 0
```

**Phase 3 Gate:** You can list every quality issue, the candidate key is verified, and you know
which columns need cleaning vs. which are clean.

### Phase 4: Quality & Remediation

Use [quality](quality.md) for transform validation and acceptance checks.

**Phase 4 Gate:** `anchor("quality")` passes with zero duplicate/null keys, `anchor("validate")` passes
all business rules, and row count delta from raw is explainable.

### Phase 5: Schema Design

**Load `skills/schema-design/SKILL.md`** — already exists, covers grain, keys, types,
partitioning, SCD patterns.

**Phase 5 Gate:** Grain defined, key verified unique, column type map documented, metadata
columns included.

### Phase 6: Pipeline Readiness

Use [planning](planning.md) for the onboarding evidence checklist.
**Use established pipeline patterns** for pipeline templates.

**Phase 6 Gate:** Pipeline notebook reads, transforms, quality-gates, and persists. It is
idempotent on re-run, follows project I/O policy, and gates production writes.

## Decision Matrix — When to Stop Earlier

| Scenario | Stop after | Reason |
|---|---|---|
| "Just explore this file" | Phase 3 | Analysis mode — no pipeline needed |
| "Is this data good enough?" | Phase 4 | Quality assessment only |
| "Design the target table" | Phase 5 | Schema design — pipeline comes later |
| "Build the full pipeline" | Phase 6 | Full onboarding |
| "Compare this month vs last" | Phase 3 | Diff/coerce_check workflow |
| "Load new version of existing source" | Phase 1-2 + Refresh | Use [the local refresh technique](refresh.md) after ingestion |

## Anti-Patterns

| Don't | Do instead |
|---|---|
| Jump to Phase 5 because "the data looks clean" | Profile first (Phase 3) — you don't know it's clean until you prove it |
| Read Excel directly without inspecting the workbook | Inspect sheets, dimensions, headers, formulas, and merged ranges first |
| Type-cast in bronze layer | Read as ALL STRING in bronze, TRY_CAST in silver |
| Write ad-hoc cleaning code | Use `anchor("transform")` → `anchor("apply_transform")` |
| Skip quality gate before persist | `anchor("quality")` is MANDATORY before writing |
| Design schema before profiling | Profile evidence drives schema decisions |
| NEVER skip `anchor("profile_table")` before ingestion | Blind ingestion causes silent data loss | Profile first, ingest second |
| NEVER assume source format without detection | Files lie about their extension | Detect encoding, delimiter, header position |
| NEVER write ad-hoc cleaning code | Anchor transforms are tested and rollbackable | `anchor("transform")` → `anchor("apply_transform")` |

## Common Gotchas by Source Type

| Source | Gotcha | Detection | Fix |
|---|---|---|
| CSV | BOM marker (\ufeff) in first column name | `anchor("microscope", df, first_col)` shows invisible prefix | Strip BOM or re-read with `encoding='utf-8-sig'` |
| CSV | Embedded newlines inside quoted fields | Row count mismatch after read | Use `multiLine=True` option |
| Excel | Serial dates (44927 instead of 2023-01-01) | `anchor("microscope")` shows numeric range in date col | Convert: `pd.to_datetime(col, origin='1899-12-30', unit='D')` |
| Excel | Merged cells create nulls in child rows | Null pattern in `anchor("profile_table")` | Forward-fill before processing |
| JSON | Mixed schemas across records | `anchor("profile_table")` shows sparse columns | Schema-on-read with explicit struct |
| JSON | Deeply nested arrays | High null% in flattened columns | Choose flatten depth, `explode()` arrays |
| API | Pagination silently drops records | Row count less than expected | Verify total count from API metadata |
| API | Rate limiting causes partial loads | Intermittent row count drops | Add retry logic, verify completeness |

## Full Onboarding Example

```python
# Step 1: Profile the raw source
result = anchor("profile_table", raw_df, subject="queue_pjm_raw")
# → Shows row count, column types, null%, distinct counts

# Step 2: Deep-dive suspicious columns
result = anchor("microscope", raw_df, "queue_date", subject="queue_pjm.queue_date")
# → Reveals if dates are strings, serials, or proper timestamps

# Step 3: Check data quality
result = anchor("quality", raw_df, subject="queue_pjm", keys=["project_id"])
# → Key uniqueness, null rates, type consistency
```

```python
# Step 4: Generate transform plan from profile
result = anchor("transform", profile_result, subject="queue_pjm")
# → Suggests: trim whitespace, cast dates, handle nulls

# Step 5: Apply transforms (with rollback support)
result = anchor("apply_transform", raw_df, transform_plan, checkpoint=True)
# → Returns cleaned df + rollback capability

# Step 6: Validate cleaned output
result = anchor("validate", cleaned_df, rules=[
    {"column": "project_id", "rule": "not_null"},
    {"column": "queue_date", "rule": "is_date"},
    {"column": "mw_capacity", "rule": "positive"},
])
```
