# Schema Migrate Tool — Architecture

## System Overview

The schema migrate tool generates executable DDL (`ALTER TABLE`) and PySpark migration
code from a `schema_diff` context. It classifies schema changes (add, type_cast,
rename, drop) and produces ready-to-run SQL statements with safety assessment.

```
anchor("schema_diff", old_df, new_df)
       │
       ▼
   schema_diff_ctx dict
   (added_columns, removed_columns, type_changes, likely_renames)
       │
       ▼
┌───────────────────────────────────────────────────┐
│        schema_migrate_context()                  │
│                                                   │
│  1. Parse schema_diff: added, removed, retyped   │
│  2. Detect renames (SequenceMatcher, threshold)   │
│  3. Check type cast safety (_SAFE_CASTS matrix)   │
│  4. Generate DDL per change                       │
│  5. Generate PySpark alternative per change       │
│  6. Safety assessment (risks)                     │
│  7. Assemble migration_plan                       │
│                                                   │
└───────────────────────────────────────────────────┘
       │
       ▼
   Output: migration_plan with DDL + PySpark + safety flags
   (dry_run=True by default — never auto-executes)
```

## Change Classification

| Change Type | Detection | DDL Generated |
|-------------|-----------|---------------|
| Add column | Column in new, not in old | `ALTER TABLE t ADD COLUMN col TYPE` |
| Type cast | Same column name, different type | `ALTER TABLE t ALTER COLUMN col SET DATA TYPE new_type` |
| Rename | Removed + added with name similarity ≥ threshold | `ALTER TABLE t RENAME COLUMN old TO new` |
| Drop | Column in old, not in new | `ALTER TABLE t DROP COLUMN col` (commented by default) |

## Key Internal Components

### _SAFE_CASTS Compatibility Matrix

A `dict[tuple[str, str], bool]` mapping `(source_type, target_type) → safe`:

```python
_SAFE_CASTS = {
    ("int", "bigint"): True,      # widening: always safe
    ("bigint", "int"): False,     # narrowing: possible data loss
    ("float", "double"): True,    # precision widening
    ("string", "int"): False,     # parse may fail
    ("int", "string"): True,      # always representable
    # ... comprehensive mapping
}
```

Unsafe casts are flagged in `risks` with the specific concern.

### SequenceMatcher for Rename Detection

Uses `difflib.SequenceMatcher` with configurable threshold (default 0.8):

```python
from difflib import SequenceMatcher

ratio = SequenceMatcher(None, "customer_name", "cust_name").ratio()
# ratio = 0.85 > threshold 0.8 → classified as rename
```

Pairs the most-similar removed+added columns. Only suggests a rename when the
similarity exceeds the threshold AND both columns have compatible types.

## Design Decisions

### Dry Run by Default

`dry_run=True` is the default. The tool generates DDL strings and PySpark code
but NEVER executes them. The user must explicitly copy and run the statements.
This prevents accidental schema mutations.

### Drop Columns Disabled by Default

Even when `dry_run=False`, DROP COLUMN is commented-out unless
`drop_columns=True` is explicitly passed. This double safety prevents
accidental data loss.

### DDL + PySpark Dual Output

Every change produces both:
- A DDL statement (for `spark.sql(...)` execution)
- A PySpark code snippet (for programmatic use)

This covers both interactive and pipeline use cases.

## Internal Structure

```
schema_migrate_tool/
├── schema_migrate_impl.py  → Main implementation (765 lines)
│   ├── _SAFE_CASTS             → Type compatibility matrix
│   ├── _detect_renames()       → SequenceMatcher fuzzy matching
│   ├── _generate_add_ddl()     → ADD COLUMN DDL + PySpark
│   ├── _generate_cast_ddl()    → ALTER COLUMN DDL + PySpark
│   ├── _generate_rename_ddl()  → RENAME COLUMN DDL
│   ├── _generate_drop_ddl()    → DROP COLUMN DDL (guarded)
│   └── schema_migrate_context()→ Orchestrator
├── __init__.py
├── README.md
└── tool.json
```

## Anchor Standard Output Contract

```python
{
    "kind": "schema_migrate",
    "subject": "catalog.schema.orders",
    "summary": "Migration plan: 1 ADD, 1 RENAME, 1 unsafe CAST. Dry run.",
    "metrics": {
        "columns_added": 1,
        "columns_removed": 0,
        "type_casts": 1,
        "renames": 1,
        "drops": 0,
        "unsafe_casts": 1,
        "dry_run": True,
    },
    "migration_plan": {
        "add_columns": [{"column": ..., "type": ..., "ddl": ..., "pyspark": ...}],
        "type_casts": [{"column": ..., "from": ..., "to": ..., "safe": ..., "ddl": ...}],
        "renames": [{"old_name": ..., "new_name": ..., "similarity": ..., "ddl": ...}],
        "drops": [{"column": ..., "action": "warning_only", "ddl": ...}],
    },
    "findings": [...],
    "risks": [...],
    "suggested_next_actions": [...],
}
```
