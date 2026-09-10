# schema_diff — Full Usage Reference

---

## Function Signature

```python
schema_diff_context(
    old_df,
    new_df,
    *,
    old_subject: str = "old_df",
    new_subject: str = "new_df",
    subject: str | None = None,
    engine: str = "auto",
    include_unchanged: bool = True,
    max_unchanged: int = 50,
    output_format: str = "dict",
    spark: Any = None,
) -> dict | str
```

---

## Parameters — Complete Reference

### `old_df` / `new_df`
Accept three input types:
- pandas DataFrame
- Spark DataFrame
- Unity Catalog table name string — `"catalog.schema.table"` (requires `spark=` parameter)

### `old_subject` / `new_subject`
Display labels for each side. Appear in report headers and summary strings. Default `"old_df"` / `"new_df"`.

### `subject`
Optional override that replaces both subject labels in the output. Use when you want a single combined label: `subject="gold_new v1 vs v2"`.

### `engine`
- `"auto"` — detected from input type
- `"pandas"` — force pandas schema extraction
- `"spark"` — force Spark schema extraction

When passing table name strings, engine is automatically set to `"spark"`.

### `include_unchanged`
When `True` (default), unchanged columns appear in `ctx["unchanged"]`. Set to `False` to reduce payload size when there are many stable columns.

### `max_unchanged`
Cap on how many unchanged columns to include in the payload. Prevents oversized output when comparing wide tables. Default 50.

### `spark`
Required when passing table name strings. Pass the active `SparkSession`.

---

## Output Contract — Complete Reference

### Top-level keys

| Key | Type | Description |
|---|---|---|
| `kind` | str | `"schema_diff_context"` |
| `subject` | str | Display label |
| `status` | str | `"ok"` or `"error"` |
| `summary` | str | One-line verdict with breaking flag |
| `metrics` | dict | Counts and breaking change flag |
| `added` | list[dict] | Columns in new_df only |
| `removed` | list[dict] | Columns in old_df only |
| `changed_type` | list[dict] | Columns in both but with different dtype |
| `unchanged` | list[dict] | Columns identical in both (up to max_unchanged) |
| `findings` | list[str] | Discovered issues |
| `risks` | list[str] | Breaking changes flagged |
| `suggested_next_actions` | list[str] | Next steps |

### `metrics` sub-keys

| Key | Description |
|---|---|
| `added_count` | Number of columns in new_df only |
| `removed_count` | Number of columns in old_df only |
| `type_changed_count` | Number of columns with dtype change |
| `unchanged_count` | Number of identical columns |
| `is_breaking_change` | `True` if any removed or type-changed columns |

### Field entry structure (added / removed / unchanged)

```python
{"column": "amount", "dtype": "float64", "position": 2}
# For Spark:
{"column": "amount", "dtype": "DoubleType()", "position": 2, "nullable": True}
```

### `changed_type` entry

```python
{
    "column":    "id",
    "old_dtype": "int64",
    "new_dtype": "object",
    "position":  0,
}
```

---

## Common Patterns

### Gate before diff
```python
schema_ctx = anchor("schema_diff", old_df, new_df)
if schema_ctx["metrics"]["is_breaking_change"]:
    removed = [f["column"] for f in schema_ctx["removed"]]
    changed = [f["column"] for f in schema_ctx["changed_type"]]
    raise ValueError(f"Breaking schema change — removed: {removed}, type changed: {changed}")

# Safe to proceed
diff_ctx = anchor("diff", old_df, new_df, keys=["id"])
```

### Compare production table versions
```python
ctx = anchor("schema_diff",
         "analytics_dev.data_engineering_gold.gold_v1",
         "analytics_dev.data_engineering_gold.gold_v2",
         spark=spark,
         old_subject="gold_v1",
         new_subject="gold_v2")

print(ctx["summary"])
for col in ctx["added"]:
    print(f"  + {col['column']} ({col['dtype']})")
for col in ctx["removed"]:
    print(f"  - {col['column']} ({col['dtype']}) BREAKING")
```

### Exclude unchanged from large-table comparison
```python
ctx = anchor("schema_diff", old_df, new_df, include_unchanged=False)
# ctx["unchanged"] will be empty — only changes shown
```

---

## Edge Cases

| Scenario | Behavior |
|---|---|
| Both DataFrames identical schemas | `is_breaking_change=False`, all columns in `unchanged` |
| Column reordered (same name, different position) | `unchanged` — position noted but not breaking |
| Table name string without `spark=` | `ValueError` |
| Mixed pandas/Spark inputs | `TypeError` |
| `max_unchanged=0` | `unchanged` is empty — counts still accurate in `metrics` |
| New column same name, different type | Appears in `changed_type`, not `added` |
