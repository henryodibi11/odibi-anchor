# diagnose_empty — Architecture

## Purpose

Diagnose why a DataFrame has zero or unexpectedly few rows by systematically analyzing
upstream DataFrames, key overlap, filter boundaries, null columns, and type mismatches.
The tool identifies the **dropout stage** (where rows disappeared) and the **dropout cause**
(why they disappeared), then suggests concrete fix actions.

---

## Pipeline

```
result_df (empty/sparse) + upstreams dict + optional keys/filter_expr
    │
    ├─ 1. Row Count Funnel ───────── Count rows in each upstream + result
    │
    ├─ 2. Key Overlap Analysis ──── For each upstream pair, compute set intersection
    │                                on key columns (only if keys provided + 2+ upstreams)
    │
    ├─ 3. Filter Boundary ───────── Apply filter_expr to each upstream; check if it
    │                                eliminates all rows (only if filter_expr provided)
    │
    ├─ 4. All-NULL Column Scan ──── Detect columns entirely NULL in each upstream
    │
    ├─ 5. Type Mismatch Detection ─ Compare key column dtypes across upstreams
    │                                (only if keys provided)
    │
    └─ 6. Cause Ranking ─────────── Priority-ranked diagnosis:
                                     type_mismatch > zero_key_overlap > filter_kills_all
                                     > key_columns_all_null > empty_upstream > undetermined
```

---

## Dropout Causes (priority order)

| Priority | Cause | Trigger Condition |
|----------|-------|-------------------|
| 1 | `key_type_mismatch` | Key column has different dtypes across upstreams (e.g., int vs string) |
| 2 | `zero_key_overlap` | Set intersection of key values between two upstreams = 0 |
| 3 | `filter_kills_all` | filter_expr matches 0 rows in at least one upstream |
| 4 | `key_columns_all_null` | A key column is entirely NULL in at least one upstream |
| 5 | `empty_upstream` | An upstream has 0 rows |
| 6 | `undetermined` | None of the above detected — multi-step transform or logic issue |

The cause ranking exits on the **first match** (highest priority wins). Low overlap
(<5%) is logged as a finding but does not override a higher-priority cause.

---

## Design Decisions

| Decision | Rationale |
|----------|----------|
| Funnel visualization | Shows row count at each stage — makes the "where" obvious at a glance |
| Priority-ranked causes | Single root cause is more actionable than a list of warnings |
| Dual-engine (Pandas + Spark) | Tool must work in both local dev (Pandas) and cluster (Spark) |
| Inlined `_detect_engine` | Tools are standalone — no imports from `src/` to avoid coupling |
| Set intersection for key overlap | Simple, correct, works for both engines via Python sets |
| `sample_limit` on orphan keys | Prevents memory blowup when overlap gap is large |
| Separate filter boundary check | Filters and joins are different failure modes; separating them aids diagnosis |
| Contract-based output | Standard Anchor contract (kind, subject, summary, metrics, findings, risks, samples, suggested_next_actions) |

---

## Internal Functions

| Function | Lines | Responsibility |
|----------|-------|---------------|
| `_detect_engine(df)` | 25-46 | Returns `'pandas'` / `'spark'` / `'unknown'` |
| `_get_count(df, engine)` | 54-58 | Row count for either engine |
| `_get_columns(df, engine)` | 61-65 | Column list for either engine |
| `_get_dtype(df, column, engine)` | 68-75 | Dtype string for a column |
| `_build_row_count_funnel(...)` | 83-102 | Stage-by-stage row count across all upstreams + result |
| `_key_overlap_analysis(...)` | 105-144 | Pairwise key overlap for all upstream combinations |
| `_compute_key_overlap(...)` | 147-163 | Dispatch to pandas/spark/mixed overlap logic |
| `_key_overlap_pandas(...)` | 166-197 | Set-based overlap for Pandas DFs |
| `_key_overlap_spark(...)` | 200-238 | intersect/subtract for Spark DFs |
| `_key_overlap_mixed(...)` | 241-256 | Converts Spark side to Pandas, then uses pandas path |
| `_filter_boundary_analysis(...)` | 259-311 | Apply filter to each upstream, check if 0 rows pass |
| `_all_null_columns(...)` | 314-352 | Batch null detection per upstream |
| `_type_mismatch_detection(...)` | 355-385 | Compare key column dtypes across all upstreams |
| `_rank_causes(...)` | 393-499 | Priority-ranked cause determination |
| `_build_suggestions(...)` | 507-582 | Actionable fix suggestions per cause |
| `_render_markdown(ctx)` | 590-678 | Render contract dict as markdown report |
| `diagnose_empty_context(...)` | 686-854 | Main entry point — orchestrates all checks |

---

## Data Flow Diagram

```
┌──────────────────────────┐
│     diagnose_empty_context │
│  (main entry point)       │
└──────────┬───────────────┘
           │
    ┌──────┴──────────────────────────────────────┐
    │                                              │
    ▼                                              ▼
┌────────────────┐  ┌────────────────────┐  ┌─────────────────┐
│ Row Count      │  │ Key Overlap         │  │ Filter Boundary │
│ Funnel         │  │ (pairwise)          │  │ Analysis        │
└───────┬────────┘  └─────────┬──────────┘  └───────┬─────────┘
        │                     │                      │
        │           ┌─────────┴──────────┐           │
        │           │                    │           │
        │           ▼                    ▼           │
        │  ┌──────────────┐  ┌──────────────────┐   │
        │  │ All-NULL      │  │ Type Mismatch   │   │
        │  │ Column Scan   │  │ Detection       │   │
        │  └───────┬──────┘  └────────┬─────────┘   │
        │          │                  │              │
        └──────────┴──────────────────┴──────────────┘
                            │
                            ▼
                 ┌────────────────────┐
                 │   _rank_causes()   │
                 │ Priority: 1→6     │
                 └─────────┬─────────┘
                           │
                           ▼
                 ┌────────────────────┐
                 │ Build contract:    │
                 │ summary, metrics,  │
                 │ findings, risks,   │
                 │ samples, actions   │
                 └────────────────────┘
```

---

## Engine Strategy

The tool uses inlined dual-engine detection (`_detect_engine`) that avoids importing
from the shared `_utils` module. Each check handles both Pandas and Spark:

- **Pandas path**: `set()` operations on column values, `.query()` / `.eval()` for filters
- **Spark path**: `.intersect()` / `.subtract()` for key overlap, `.where()` for filters,
  `F.count(F.col(c))` batch for null detection
- **Mixed path**: Converts Spark side to Pandas via `.toPandas()` on key columns only
  (bounded by `sample_limit`)

---

## Output Contract

Returns a standard Anchor context dict (via `build_base_context` + `finalize_context`):

```python
{
    "kind": "diagnose_empty",
    "subject": "<user-provided name>",
    "summary": "EMPTY OUTPUT: result has N rows. Upstreams: ... Cause: ...",
    "metrics": {
        "result_count": int,
        "result_column_count": int,
        "upstream_counts": {"name": int, ...},
        "dropout_stage": str,   # e.g. "join:orders↔customers"
        "dropout_cause": str,   # e.g. "zero_key_overlap"
    },
    "findings": [str, ...],
    "risks": [str, ...],
    "samples": {
        "key_overlaps": [...],      # per-pair overlap stats
        "type_mismatches": [...],   # key columns with different types
        "all_null_columns": {...},  # upstream → null column list
        "filter_analysis": {...},   # per-upstream filter pass/fail
    },
    "funnel": {"upstream_name": {"count": N, "columns": M}, ...},
    "suggested_next_actions": [str, ...],
}
```