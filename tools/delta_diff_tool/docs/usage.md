# Delta Diff Tool — Usage

## Signature

```python
delta_diff_context(
    table: str,
    keys: list[str],
    old_version: int | str | None = None,
    new_version: int | str | None = None,
    versions_ago: int = 1,
    compare_columns: list[str] | None = None,
    include_coerce_check: bool = False,
    subject: str | None = None,
    output_format: str = "dict",
    sample_limit: int = 20,
) -> dict | str
```

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `table` | str | Yes | — | Fully qualified Delta table name (`"catalog.schema.table"`) |
| `keys` | list[str] | Yes | — | Business key columns for row-level matching |
| `old_version` | int \| str \| None | No | None | Old version: number, timestamp, or None (uses `versions_ago`) |
| `new_version` | int \| str \| None | No | None | New version: number, timestamp, or None (uses current/latest) |
| `versions_ago` | int | No | 1 | If `old_version` is None, compare current vs N versions ago |
| `compare_columns` | list[str] | No | None (all) | Limit diff to specific non-key columns |
| `include_coerce_check` | bool | No | False | Automatically classify changed columns for formatting issues |
| `subject` | str | No | table name | Display label |
| `output_format` | str | No | `"dict"` | `"dict"` or `"markdown"` |
| `sample_limit` | int | No | 20 | Max sample rows per category |

## Version Selection

### Compare current vs previous (default)

```python
# Compares version N (current) vs version N-1
result = anchor("delta_diff", "catalog.schema.orders", keys=["order_id"])
```

### Compare current vs N versions ago

```python
# Compare current vs 3 versions ago
result = anchor("delta_diff", "catalog.schema.orders", keys=["order_id"],
            versions_ago=3)
```

### Explicit version numbers

```python
# Compare specific versions
result = anchor("delta_diff", "catalog.schema.orders", keys=["order_id"],
            old_version=5, new_version=8)
```

### Timestamp-based version

```python
# Compare against a point-in-time snapshot
result = anchor("delta_diff", "catalog.schema.orders", keys=["order_id"],
            old_version="2026-06-01")
```

## Limiting Column Comparison

```python
# Only check status and amount columns for changes
result = anchor("delta_diff", "catalog.schema.orders", keys=["order_id"],
            compare_columns=["status", "amount", "updated_at"])
```

## With Coerce Check

```python
# Automatically classify whether changes are formatting or genuine
result = anchor("delta_diff", "catalog.schema.orders", keys=["order_id"],
            include_coerce_check=True)
```

## Result Structure

| Field | Type | Description |
|-------|------|-------------|
| `kind` | str | Always `"delta_diff"` |
| `summary` | str | One-line: "Version 4→5 (MERGE): 10 added, 2 removed, 50 changed" |
| `metrics.table` | str | Table name |
| `metrics.old_version` | int | Resolved old version number |
| `metrics.new_version` | int | Resolved new version number |
| `metrics.old_timestamp` | str | Timestamp of old version |
| `metrics.new_timestamp` | str | Timestamp of new version |
| `metrics.new_operation` | str | Delta operation (MERGE, WRITE, DELETE, etc.) |
| `metrics.new_user` | str | Who made the change |
| `metrics.old_row_count` | int | Rows in old version |
| `metrics.new_row_count` | int | Rows in new version |
| `metrics.added_count` | int | Rows with keys only in new version |
| `metrics.removed_count` | int | Rows with keys only in old version |
| `metrics.changed_count` | int | Rows with same keys but different values |
| `metrics.unchanged_count` | int | Rows identical in both versions |
| `metrics.changed_column_counts` | dict | Per-column breakdown (see below) |
| `metrics.schema_changed` | bool | Whether columns were added/removed |
| `samples.added_rows` | list[dict] | Sample of new rows |
| `samples.removed_rows` | list[dict] | Sample of deleted rows |
| `samples.changed_rows` | list[dict] | Sample of modified rows (old+new values) |
| `findings` | list[str] | Descriptions of changes |
| `risks` | list[str] | Warnings (e.g. >5% rows removed) |
| `suggested_next_actions` | list[str] | Next steps |

### changed_column_counts Detail

For each changed column:
```python
"status": {
    "null_to_value": 5,    # NULL filled with a value
    "value_to_null": 0,    # Value cleared to NULL
    "value_changed": 45,   # Non-null changed to different non-null
}
```

## Direct Import

```python
import sys
sys.path.append("/Workspace/Users/user@example.com/odibi_anchor")

from tools.delta_diff_tool.delta_diff_impl import delta_diff_context

result = delta_diff_context(
    table="catalog.schema.orders",
    keys=["order_id"],
    versions_ago=1,
)
```
