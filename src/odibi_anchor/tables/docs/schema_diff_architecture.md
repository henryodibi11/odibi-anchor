# schema_diff — Architecture

**Module:** `tables/schema_diff_context.py`
**Entry point:** `schema_diff_context(old_df, new_df)`

---

## System Overview

```
old_df / "catalog.schema.table"     new_df / "catalog.schema.table"
              │                                      │
              └──────────────┬───────────────────────┘
                             ▼
                    [1] Resolve inputs
                        (table name strings → DataFrames via Spark)
                             │
                             ▼
                    [2] Extract _SchemaField list
                        {column, dtype, position, nullable?}
                        (pandas: df.dtypes; spark: df.schema)
                             │
                             ▼
                    [3] Compare field lists
                        added / removed / type_changed / unchanged
                             │
                             ▼
                    [4] Set is_breaking_change flag
                        (True if removed or type_changed non-empty)
                             │
                             ▼
                    [5] Build context dict → render or return
```

---

## Field Comparison Model

Each column is represented as a `_SchemaField` dataclass:

```python
@dataclass(frozen=True)
class _SchemaField:
    column:   str
    dtype:    str        # string representation of type
    position: int        # zero-based ordinal position
    nullable: bool|None  # Spark only; None for pandas
```

Comparison is done by column name, not position. Columns that were reordered but otherwise unchanged are classified as `unchanged` (position change is noted but not breaking).

---

## Breaking Change Detection

`is_breaking_change=True` when:
- Any column was removed (existing downstream queries will fail)
- Any column changed type (may cause silent data loss or cast errors)

`is_breaking_change=False` when:
- Only additions (new columns added — backwards compatible)
- Only unchanged columns
- Only position reordering

---

## Design Decisions

**Name-based matching, not position-based.**
Column reordering is not a breaking change. Two DataFrames with the same columns in different order are treated as structurally compatible.

**String dtype representation.**
Dtypes are compared as strings (`"float64"`, `"object"`, `"StringType()"`) rather than native types. This makes pandas and Spark schemas comparable on the same string dimension.

**Table name string support.**
Passing a Unity Catalog table name string (`"catalog.schema.table"`) resolves the schema via `spark.table(name).schema` without loading any data. Only the schema is read — no rows are scanned.

**`include_unchanged=True` by default.**
Unchanged columns are included in the output so the full picture is visible. Cap with `max_unchanged` if the payload is too large for LLM context.

---

## Internal Structure

```
tables/
└── schema_diff_context.py
    ├── schema_diff_context()          ← public entry point
    ├── _SchemaField                   ← frozen dataclass for field comparison
    ├── _resolve_inputs()              ← table name string → DataFrame
    ├── _extract_schema_pandas()       ← df.dtypes → list[_SchemaField]
    ├── _extract_schema_spark()        ← df.schema → list[_SchemaField]
    ├── _schema_diff_context_pandas()  ← pandas implementation
    ├── _schema_diff_context_spark()   ← spark implementation
    └── render_schema_diff_report()    ← markdown renderer
```

---

## Anchor Standard Output Contract

```python
ctx = {
    "kind":    "schema_diff_context",
    "subject": "old_df vs new_df",
    "status":  "ok",
    "summary": "2 changes: 1 added, 1 removed (BREAKING)",
    "metrics": {
        "added_count":        1,
        "removed_count":      1,
        "type_changed_count": 0,
        "unchanged_count":    1,
        "is_breaking_change": True,
    },
    "added": [
        {"column": "status", "dtype": "object", "position": 2}
    ],
    "removed": [
        {"column": "amount", "dtype": "float64", "position": 1}
    ],
    "changed_type": [],
    "unchanged": [
        {"column": "id", "dtype": "int64", "position": 0}
    ],
    "findings":               [...],
    "risks":                  [...],
    "suggested_next_actions": [...],
}
```

---

## Integration

```
schema_diff (are schemas compatible?)
  ├── is_breaking_change=True → fix schema first
  └── is_breaking_change=False → proceed to diff (row-level comparison)
```

Always run `schema_diff` before `diff` when you're unsure if schemas have changed. A schema mismatch will cause `diff` to fail or silently miss columns.
