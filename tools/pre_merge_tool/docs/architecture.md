# pre_merge — Architecture

## Purpose

Validate a source DataFrame is safe to MERGE INTO a Delta target BEFORE executing
the merge. Detects duplicate keys (which cause Delta MERGE to fail), null keys,
schema/type incompatibilities, and predicts merge behavior (INSERT/UPDATE split).

---

## Pipeline

```
source_df + target (table name or DataFrame) + keys
    |
    +-- 1. Resolve Target ------------- If string, spark.table(name); if DF, use directly
    |
    +-- 2. Duplicate Key Check -------- GroupBy keys, count > 1 = duplicates
    |       (Delta MERGE FAILS on duplicate source keys)
    |
    +-- 3. Null Key Check ------------- Per-column null counts on merge keys
    |       (NULL != NULL in MERGE ON, so these rows never match)
    |
    +-- 4. Schema Comparison ---------- Source vs target column types
    |       - Type compatibility matrix lookup
    |       - columns_added (in source, not target)
    |       - columns_removed (in target, not source)
    |       - type_mismatches (incompatible casts)
    |
    +-- 5. Key Overlap Analysis ------- Predict merge behavior
    |       - overlap_count (keys in both = UPDATEs)
    |       - insert_count (keys only in source = INSERTs)
    |       - merge_behavior: UPDATE-only | INSERT-only | MIXED | EMPTY
    |
    +-- 6. Batch Ratio Check ---------- source_count / target_count
    |       (Flags when source >2x target = likely full reload, not increment)
    |
    +-- 7. Build Contract ------------- Summary, findings, risks, actions
```

---

## Type Compatibility Matrix

The tool has a hardcoded `_TYPE_COMPAT` dict mapping `(source_type, target_type)` tuples
to `(compatible, risk, note)`. Types are normalized before lookup.

### Safe Upcasts (compatible=True, risk="none")

| Source | Target | Note |
|--------|--------|------|
| int | long/bigint | implicit upcast |
| short | int/long | implicit upcast |
| byte | short/int/long | implicit upcast |
| float | double | implicit upcast |
| date | timestamp | safe with midnight time |

### Risky Casts (compatible=True, risk="low"/"medium")

| Source | Target | Risk | Note |
|--------|--------|------|------|
| int | float | low | precision loss for large ints |
| long | double | low | precision loss for large longs |
| timestamp | date | low | time component truncated |
| double/float | decimal | medium | precision loss possible |

### Incompatible (compatible=False, risk="high")

| Source | Target | Note |
|--------|--------|------|
| string | int/long/short/byte/float/double/decimal | MERGE will fail on non-numeric values |
| string | boolean | MERGE will fail on non-boolean string values |
| string | date/timestamp | depends on format consistency — likely to fail |

### Type Normalization

```
decimal(10,2) → decimal
varchar(100) → string
integer → int
bigint → long
float64 → double
object → string
datetime64[ns] → timestamp
```

---

## Merge Behavior Classification

| Condition | Behavior |
|-----------|----------|
| All source keys exist in target, none new | `UPDATE-only` |
| No source keys exist in target | `INSERT-only` |
| Some overlap, some new | `MIXED` |
| Source has no distinct keys | `EMPTY` |

---

## Design Decisions

| Decision | Rationale |
|----------|----------|
| Inlined `_detect_engine` | Tool stays standalone; no imports from `_utils.engine_utils` |
| Deferred import of contract utils (inside function) | Tool module can be imported without `odibi_anchor` on sys.path; adds src path at runtime |
| Duplicate key check is a BLOCKER | Delta MERGE raises `DeltaConcurrentAppendException` on duplicate source keys — this is always a hard failure |
| Null keys are a WARNING not BLOCKER | Nulls don't crash MERGE, they just won't match (NULL != NULL) |
| Target accepts string or DataFrame | String triggers `spark.table()` for production use; DataFrame for testing |
| Schema comparison shows added/removed columns | MERGE with `mergeSchema=True` may auto-add, but removed columns indicate drift |
| Batch ratio >2x flagged | Catches common mistake: full reload submitted as incremental batch |
| Summary has 3 states: OK / WARNING / BLOCKED | BLOCKED = merge WILL fail (dupes or type mismatch); WARNING = won't fail but has issues |

---

## Internal Functions

| Function | Lines | Responsibility |
|----------|-------|---------------|
| `_detect_engine(df)` | 16-35 | Detect pandas/spark (inlined) |
| `_normalize_type(type_str)` | 94-123 | Strip params, map aliases to canonical names |
| `_check_type_compatibility(src, tgt)` | 126-150 | Lookup type pair in compat matrix |
| `_get_schema_pandas(df)` | 157-159 | Column→type dict (pandas dtypes) |
| `_get_schema_spark(df)` | 249-251 | Column→type dict (Spark simpleString) |
| `_count_pandas(df)` / `_count_spark(df)` | 162-163/254-255 | Row count |
| `_duplicate_keys_pandas(df, keys, limit)` | 166-184 | GroupBy + filter count>1 + top samples |
| `_duplicate_keys_spark(df, keys, limit)` | 258-278 | Same via Spark groupBy + agg |
| `_null_keys_pandas(df, keys)` | 187-205 | Per-column null counts + any-null row count |
| `_null_keys_spark(df, keys)` | 281-309 | Same via Spark (single-pass null_exprs) |
| `_key_overlap_pandas(source, target, keys)` | 208-242 | Inner merge on keys → overlap/insert/update counts |
| `_key_overlap_spark(source, target, keys)` | 312-341 | intersect() on distinct keys |
| `_compare_schemas(source_schema, target_schema)` | 348-393 | Full column comparison + type compat per column |
| `_render_pre_merge_report(ctx)` | 400-474 | Markdown renderer with metrics table, findings, risks |
| `pre_merge_context(...)` | 481-790 | Public entry point |

---

## Output Contract

```python
{
    "kind": "pre_merge",
    "subject": "source → catalog.schema.table",
    "summary": "OK: MERGE safe — 0 duplicate key(s) — schema compatible — MIXED merge (5 inserts, 10 updates)",
    "metrics": {
        "source_count": int,
        "target_count": int,
        "source_key_distinct": int,
        "source_duplicate_key_count": int,  # 0 = safe, >0 = BLOCKED
        "source_null_key_count": int,
        "source_null_key_pct": float,
        "overlap_count": int,               # keys in both source and target
        "insert_count": int,                # keys only in source
        "update_count": int,                # keys in both
        "merge_behavior": str,              # UPDATE-only | INSERT-only | MIXED | EMPTY
        "source_target_ratio": float,
        "schema_compatible": bool,
        "columns_added": [str, ...],        # in source, not target
        "columns_removed": [str, ...],      # in target, not source
        "columns_type_mismatch": [dict, ...],
        "type_compatibility": {col: {...}}, # per-column compat details
    },
    "findings": [str, ...],
    "risks": [str, ...],
    "samples": {
        "duplicate_keys": [{key: val, "_duplicate_count": int}, ...],
        "null_key_rows": [],
        "type_mismatch_columns": [dict, ...],
    },
    "suggested_next_actions": [str, ...],
}
```

---

## Engine Strategy

Engine detection is **inlined** (not imported). Contract utilities are imported
**inside the entry-point function** with dynamic sys.path manipulation.

- **Pandas**: `groupby().size()` for dupe detection, `.merge(on=keys, how='inner')` for overlap
- **Spark**: `groupBy().count()` + `where("_count > 1")` for dupes, `.intersect()` for overlap
- **Target resolution**: `isinstance(target, str)` → `spark.table(target)` to load Delta table