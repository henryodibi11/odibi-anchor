# coerce_check — Full Usage Reference

---

## Function Signature

```python
coercion_check_context(
    old_df,
    new_df,
    keys: list[str],
    columns: list[str] | None = None,
    *,
    subject: str | None = None,
    sample_limit: int = 20,
    output_format: str = "dict",
) -> dict | str
```

---

## Parameters — Complete Reference

### `old_df` / `new_df`
Pandas DataFrames. **Spark not supported** — convert with `.toPandas()` first.

### `keys`
Join keys for the inner join. All key columns must exist in both DataFrames.

### `columns`
Optional. Restricts classification to the listed columns. Best practice: pass only columns where `diff` showed `value_changed > 0`. Omitting this checks all common non-key columns, which includes unchanged columns and wastes classification cycles.

### `sample_limit`
Controls how many example value pairs are collected per column. Default 20. Increase for detailed debugging of a specific column.

### `output_format`
- `"dict"` (default) — returns structured dict for programmatic access
- `"markdown"` — formatted report for display

---

## Output Contract — Complete Reference

### Top-level keys

| Key | Type | Description |
|---|---|---|
| `kind` | str | `"coercion_check_context"` |
| `subject` | str | Display label |
| `status` | str | `"ok"` or `"error"` |
| `summary` | str | One-line verdict |
| `metrics` | dict | Aggregate counts and percentages |
| `column_results` | dict | Per-column classification (keyed by column name) |
| `samples` | dict | Example value pairs per column (keyed by column name) |
| `findings` | list[str] | Discovered issues |
| `risks` | list[str] | Columns with mixed genuine/representation issues |
| `suggested_next_actions` | list[str] | Next steps |

### `metrics` sub-keys

| Key | Description |
|---|---|
| `total_mismatches` | Total differing value pairs across all columns |
| `total_representation` | Mismatches explained by coercion (whitespace, case, unicode, etc.) |
| `total_genuine` | Mismatches that are truly different values |
| `representation_pct` | `total_representation / total_mismatches` |
| `category_totals` | `{"case": N, "whitespace": N, "genuine": N, ...}` across all columns |

### `column_results` entry

```python
ctx["column_results"]["col_name"] = {
    "total_mismatches":  14,        # total mismatching pairs for this column
    "categories": {                  # count per category
        "case":           10,
        "whitespace+case": 2,
        "genuine":         2,
    },
    "dominant_category": "case",    # most common category
    "confidence":         0.714,    # dominant_count / total_mismatches
    "suggested_fix": "UPPER()/LOWER() both sides before comparing",
}
```

### `samples` entry

```python
ctx["samples"]["col_name"] = [
    {"old": "Scoping",   "new": "SCOPING",  "category": "case"},
    {"old": "  Active  ", "new": "ACTIVE",  "category": "whitespace+case"},
    {"old": "01/15/2024", "new": "2024-01-15", "category": "date_format"},
]
```

---

## Common Patterns

### Full comparison chain after diff
```python
diff_ctx = anchor("diff", old_df, new_df, keys=["id"])
changed_cols = [
    c["column"]
    for c in diff_ctx["metrics"]["changed_column_counts"]
    if c["value_changed"] > 0
]

if changed_cols:
    coerce_ctx = anchor("coerce_check", old_df, new_df,
                    keys=["id"], columns=changed_cols)

    # Which columns are fixable vs genuine?
    for col, result in coerce_ctx["column_results"].items():
        if result["dominant_category"] == "genuine":
            print(f"{col}: genuine difference — needs business review")
        else:
            print(f"{col}: {result['dominant_category']} — apply {result['suggested_fix']}")
```

### Check only one column in detail
```python
ctx = anchor("coerce_check", old_df, new_df, keys=["id"],
         columns=["Generic Queue Status"], sample_limit=50)

result = ctx["column_results"]["Generic Queue Status"]
print(f"Dominant: {result['dominant_category']} ({result['confidence']:.0%} confidence)")
for s in ctx["samples"]["Generic Queue Status"]:
    print(f"  '{s['old']}' → '{s['new']}' ({s['category']})")
```

### Decide whether to apply coerce_fix
```python
ctx = anchor("coerce_check", old_df, new_df, keys=["id"])
fixable = {col for col, r in ctx["column_results"].items()
           if r["dominant_category"] != "genuine" and r["confidence"] >= 0.8}

if fixable:
    fix_ctx = anchor("coerce_fix", old_df, ctx, columns=list(fixable))
    clean_df = fix_ctx["df"]
```

---

## Edge Cases

| Scenario | Behavior |
|---|---|
| Column has no mismatches | `dominant_category="identical"`, `total_mismatches=0` |
| All mismatches are `genuine` | `representation_pct=0.0` — coerce_fix will have nothing to do |
| Column in `columns=` not in one DataFrame | Silently skipped |
| Row has null on either side | Excluded from classification (null transitions belong to diff) |
| Mixed categories with no dominant | Category with most counts wins; confidence reflects ambiguity |
| Spark DataFrame passed | `NotImplementedError` — convert to pandas first |
