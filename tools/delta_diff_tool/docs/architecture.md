# Delta Diff Tool — Architecture

## System Overview

The delta diff tool compares two versions of a Delta table at business-key level.
It uses Delta time travel to read historical snapshots and performs a full
key-based diff with NULL-aware change classification per column.

```
Delta Table Name ("catalog.schema.table")
      │
      ▼
┌────────────────────────────────────────────────────────┐
│ 1. DESCRIBE HISTORY                               │
│    → List versions, timestamps, operations         │
└────────┬───────────────────────────────────────────────┘
         │
         ▼
┌────────────────────────────────────────────────────────┐
│ 2. Resolve versions                               │
│    old_version: explicit, timestamp, or N versions ago │
│    new_version: explicit or current (latest)       │
└────────┬───────────────────────────────────────────────┘
         │
         ▼
┌────────────────────────────────────────────────────────┐
│ 3. Read both snapshots via Delta time travel      │
│    spark.read.format("delta")                      │
│         .option("versionAsOf", N).table(name)      │
└────────┬───────────────────────────────────────────────┘
         │
         ▼
┌────────────────────────────────────────────────────────┐
│ 4. Business-key diff                              │
│    - Key-based set ops: added, removed, common    │
│    - NULL-safe column comparison (eqNullSafe)     │
│    - Per-column change classification             │
└────────┬───────────────────────────────────────────────┘
         │
         ▼
┌────────────────────────────────────────────────────────┐
│ 5. (Optional) coerce_check on changed columns     │
└────────┬───────────────────────────────────────────────┘
         │
         ▼
   Anchor Standard Output Contract
```

## Key Concepts

### Delta Time Travel

Delta Lake stores versioned snapshots. Each mutation (INSERT, UPDATE, DELETE, MERGE)
creates a new version. The tool reads any two versions using:

```python
spark.read.format("delta").option("versionAsOf", N).table(table_name)
```

### NULL-Aware Comparison

Standard equality (`col_a == col_b`) treats NULL=NULL as unknown (three-value logic).
Delta diff uses Spark's `eqNullSafe` operator:

```python
~F.col("old.col").eqNullSafe(F.col("new.col"))
```

This correctly identifies:
- NULL → value (null_to_value)
- value → NULL (value_to_null)
- value → different value (value_changed)

### Version Resolution

Versions can be specified three ways:

| Input | Resolution |
|-------|------------|
| Integer (e.g. `5`) | Direct version number |
| Timestamp string (e.g. `"2026-06-01"`) | Latest version at or before that timestamp |
| `None` + `versions_ago=N` | `current_version - N` |

## Internal Structure

```
delta_diff_tool/
├── delta_diff_impl.py  → Main implementation (549 lines)
│   ├── _get_spark_session()      → Get active SparkSession
│   ├── _get_delta_history()      → DESCRIBE HISTORY → list[dict]
│   ├── _resolve_version()        → spec → int version number
│   ├── _read_delta_version()     → Read snapshot at version N
│   ├── _classify_column_changes()→ Per-column null_to_value/value_to_null/value_changed
│   ├── _compute_diff_metrics()   → Full diff: added/removed/changed/unchanged
│   └── delta_diff_context()      → Orchestrator + output assembly
├── __init__.py
├── README.md
├── tool.json
└── test_delta_diff.py
```

## Design Decisions

### Spark-Only

Delta versioning is a Spark/Databricks concept. There is no pandas equivalent.
The tool validates SparkSession availability up-front.

### Key-Based Diff (Not Row-Position)

Rows are matched by business keys, not position. This correctly handles:
- Reordered rows (same data, different order)
- Partial updates (only some rows changed)
- Appends mixed with updates

### Schema Evolution Handling

If columns were added or removed between versions:
- `schema_changed = True` is reported
- `added_cols` / `removed_cols` listed
- Comparison uses only the intersection of columns

### Optional coerce_check Integration

With `include_coerce_check=True`, after identifying changed columns, the tool
automatically runs `coerce_check` to classify whether changes are genuine data
differences or formatting artifacts (whitespace, case, dates).

## Anchor Standard Output Contract

```python
{
    "kind": "delta_diff",
    "subject": "catalog.schema.orders",
    "summary": "Version 4→5 (MERGE by user@...) : 10 added, 2 removed, 50 changed, 938 unchanged",
    "metrics": {
        "table": "catalog.schema.orders",
        "old_version": 4,
        "new_version": 5,
        "old_timestamp": "2026-06-07 10:00:00",
        "new_timestamp": "2026-06-08 14:00:00",
        "new_operation": "MERGE",
        "old_row_count": 1000,
        "new_row_count": 1008,
        "added_count": 10,
        "removed_count": 2,
        "changed_count": 50,
        "unchanged_count": 938,
        "changed_column_counts": {
            "status": {"null_to_value": 5, "value_to_null": 0, "value_changed": 45},
            "amount": {"null_to_value": 0, "value_to_null": 2, "value_changed": 3},
        },
        "schema_changed": false,
    },
    "findings": [...],
    "risks": [...],
    "samples": {
        "added_rows": [...],
        "removed_rows": [...],
        "changed_rows": [...],
    },
    "suggested_next_actions": [...],
}
```
