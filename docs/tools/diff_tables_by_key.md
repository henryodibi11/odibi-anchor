# diff_tables_by_key

`diff_tables_by_key` compares two DataFrames row-by-row using key columns
and produces a structured change report showing added, removed, changed,
and unchanged rows with column-level detail.

It answers:

```text
What rows were added, removed, or changed?
How many keys were affected and by what percentage?
What MERGE statement would reconcile the difference?
```

## Public API

```python
from odibi_anchor.tables import diff_tables_by_key, render_diff_report

def diff_tables_by_key(
    old_df: Any,
    new_df: Any,
    keys: list[str],
    *,
    subject: str | None = None,
    engine: Literal["auto", "pandas", "spark"] = "auto",
    sample_limit: int = 5,
    output_format: Literal["dict", "markdown"] = "dict",
) -> dict[str, Any] | str:
    ...
```

## Output Shape

```python
{
    "kind": "diff_tables_by_key",
    "subject": "old_df vs new_df",
    "engine": "pandas",
    "status": "ok" | "blocked",
    "summary": "3 keys: 1 added, 1 removed, 1 changed, 0 unchanged.",
    "metrics": {
        "old_row_count": 3,
        "new_row_count": 3,
        "old_key_count": 3,
        "new_key_count": 3,
        "common_key_count": 2,
        "added_key_count": 1,
        "removed_key_count": 1,
        "changed_key_count": 1,
        "unchanged_key_count": 1,
        "added_key_pct": 0.333,
        "removed_key_pct": 0.333,
        "changed_key_pct": 0.333,
        "row_count_delta": 0,
        "has_changes": True,
        "changed_cell_count": 1,
        "changed_column_counts": {"val": 1},
        "compared_column_count": 1,
        "common_column_count": 1,
        "old_only_column_count": 0,
        "new_only_column_count": 0,
        "old_duplicate_key_count": 0,
        "new_duplicate_key_count": 0,
        "old_null_key_row_count": 0,
        "new_null_key_row_count": 0,
        # ... additional metrics
    },
    "columns": [{
        "column": "val",
        "changed_count": 1,
        "changed_pct": 0.5,
    }],
    "samples": {
        "added": [{"id": 4, "val": 40}],
        "removed": [{"id": 3, "val": 30}],
        "changed": [{"key": {"id": 2}, "old": {"val": 20}, "new": {"val": 25}}],
    },
    "merge_expr": "MERGE INTO {target_table} t USING {source_table} s ...",
    "findings": [{"type": ..., "severity": ..., "count": ..., "message": ...}],
    "risks": [{"severity": ..., "type": ..., "message": ..., "count": ...}],
    "suggested_next_actions": [str],
}
```

### Key Output Fields

- **`metrics["added_key_count"]`** — Rows only in new (NOT `diff["inserts"]`)
- **`metrics["removed_key_count"]`** — Rows only in old (NOT `diff["deletes"]`)
- **`metrics["changed_key_count"]`** — Rows in both with value changes
- **`metrics["added_key_pct"]`** — Fraction of old_key_count
- **`merge_expr`** — Ready-to-paste MERGE INTO SQL (only when `status="ok"`)
- **`status`** — `"ok"` when comparison is clean; `"blocked"` when duplicate/null keys

## Example Usage

### Basic Comparison

```python
import pandas as pd
from odibi_anchor.tables import diff_tables_by_key

old_df = pd.DataFrame({"id": [1, 2, 3], "val": [10, 20, 30]})
new_df = pd.DataFrame({"id": [1, 2, 4], "val": [10, 25, 40]})

diff = diff_tables_by_key(old_df, new_df, keys=["id"])
m = diff["metrics"]
print(f"Added: {m['added_key_count']}, Changed: {m['changed_key_count']}, Removed: {m['removed_key_count']}")
```

### Percentage-Based Threshold

```python
diff = diff_tables_by_key(old_df, new_df, keys=["id"])

if diff["metrics"]["removed_key_pct"] > 0.1:
    raise RuntimeError(
        f"Too many removals: {diff['metrics']['removed_key_pct']:.1%}"
    )
```

### Using the MERGE Expression

```python
diff = diff_tables_by_key(old_df, new_df, keys=["id"])

if diff.get("merge_expr"):
    sql = diff["merge_expr"].format(
        target_table="catalog.schema.target",
        source_table="catalog.schema.staging",
    )
    spark.sql(sql)
```

### Spark Comparison

```python
from odibi_anchor.tables import diff_tables_by_key

diff = diff_tables_by_key(
    spark.table("catalog.schema.yesterday"),
    spark.table("catalog.schema.today"),
    keys=["asset_id", "date"],
)
```

### Markdown Output

```python
report = diff_tables_by_key(old_df, new_df, keys=["id"], output_format="markdown")

# Or two-step:
from odibi_anchor.tables import render_diff_report
diff = diff_tables_by_key(old_df, new_df, keys=["id"])
report = render_diff_report(diff)
```

## Design Decisions

1. **Percentage metrics use `old_key_count` as denominator** — Or 1 if zero
   to avoid ZeroDivisionError.

2. **`merge_expr` only generated when `status="ok"`** — Duplicate/null keys
   make a MERGE unsafe.

3. **Spark uses eqNullSafe for comparison** — Both-null must be "equal".

4. **Batched aggregation** — All counts (added/removed/changed/unchanged +
   per-column) computed in ONE Spark `select()` action.

5. **Samples are capped** — Only `sample_limit` rows collected per category.

## Spark Performance

| Action | Purpose |
|--------|---------|
| 1 full outer join | Match keys between old and new |
| 1 `select(sum(...))` | All counts in one pass |
| 3 `limit().collect()` | Samples for added/removed/changed |

## When To Use

Use this:

- after writing to verify expected changes (regression gate);
- before a MERGE to preview what will happen;
- in CI/CD to validate data pipeline outputs;
- to build automated change reports.

## Gotchas

- There is NO `diff["inserts"]`, `diff["updates"]`, `diff["deletes"]` —
  use `diff["metrics"]["added_key_count"]` etc.
- `merge_expr` uses `{target_table}` and `{source_table}` placeholders —
  call `.format()` before executing.
- When keys have duplicates, status becomes "blocked" and merge_expr is omitted.
