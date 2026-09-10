# suggest_rules — Usage Guide

## Entry Point

```python
suggest_rules_context(
    profile_ctx: dict | None = None,
    df: Any = None,
    subject: str | None = None,
    strictness: str = "standard",
    include_types: list[str] | None = None,
    exclude_columns: list[str] | None = None,
    output_format: str = "dict",
) -> dict | str
```

Via dispatcher: `anchor("suggest_rules", ...)`

---

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `profile_ctx` | `dict \| None` | `None` | Output from `anchor("profile_table", df, output_format="dict")`. If provided, skips re-profiling. |
| `df` | `DataFrame \| None` | `None` | Raw pandas/Spark DataFrame. Auto-profiled if `profile_ctx` not given. |
| `subject` | `str \| None` | `None` | Display name for the table. Inferred from profile_ctx if omitted. |
| `strictness` | `str` | `"standard"` | One of `"strict"`, `"standard"`, `"lenient"`. Controls tolerance thresholds. |
| `include_types` | `list[str] \| None` | `None` | Limit output to specific rule types. Options: `not_null`, `unique`, `accepted_values`, `range`, `expression`, `type_check`. |
| `exclude_columns` | `list[str] \| None` | `None` | Skip these columns during rule generation. Audit columns are auto-excluded. |
| `output_format` | `str` | `"dict"` | `"dict"` returns standard Anchor contract. `"markdown"` returns rendered string. |

**Input requirement:** You must provide at least one of `profile_ctx` or `df`. If neither is given, a `ValueError` is raised.

---

## Two Entry Paths

### Path 1: From raw DataFrame (auto-profiles internally)

```python
ctx = anchor("suggest_rules", df=df, subject="orders")
```

Internally calls `dataset_profile_context(df, subject="orders", output_format="dict")` then passes the result through the rule inference pipeline.

### Path 2: From existing profile (skips re-profiling)

```python
profile = anchor("profile_table", df, subject="orders", output_format="dict")
ctx = anchor("suggest_rules", profile_ctx=profile)
```

Useful when you’ve already profiled the data and want to iterate on rule generation with different `strictness` or `include_types` without re-profiling.

---

## Filtering Rules

### By rule type (include_types)

```python
# Only not_null and unique rules
ctx = anchor("suggest_rules", df=df, include_types=["not_null", "unique"])
```

Valid values: `not_null`, `unique`, `accepted_values`, `range`, `expression`, `type_check`

Passing an invalid type raises `ValueError` with the valid options listed.

### By column (exclude_columns)

```python
# Skip timestamps that change every load
ctx = anchor("suggest_rules", df=df, exclude_columns=["_extracted_at", "updated_at"])
```

Audit columns are auto-excluded regardless: `_extracted_at`, `_source_file`, `_row_hash`, `_loaded_at`, `_ingested_at`.

---

## Output Contract

Returns a standard Anchor contract dict with these keys:

| Key | Type | Description |
|---|---|---|
| `kind` | `str` | Always `"suggest_rules"` |
| `subject` | `str` | Table name |
| `summary` | `str` | e.g. `"Generated 7 validation rules from profile (1 accepted_values, 1 not_null, 3 range, 2 unique)"` |
| `metrics` | `dict` | Aggregated stats (see below) |
| `rules` | `list[dict]` | The generated rules — pass directly to `anchor("validate")` |
| `findings` | `list[str]` | Human-readable notes about what was inferred |
| `risks` | `list[str]` | Warnings (e.g. high-cardinality accepted_values) |
| `suggested_next_actions` | `list[str]` | Recommended follow-up steps |

### Metrics

| Metric | Description |
|---|---|
| `total_rules` | Total number of rules generated |
| `by_type` | Dict mapping rule type → count |
| `avg_confidence` | Mean confidence across all rules |
| `columns_covered` | Number of distinct columns with at least one rule |
| `columns_skipped` | Number of columns excluded |
| `strictness` | The strictness level used |

### Rule Format

Each rule in `ctx["rules"]` is a dict compatible with `anchor("validate")`:

```python
# not_null rule (multi-column)
{"type": "not_null", "columns": ["order_id", "amount", "status"],
 "_confidence": 0.9, "_reason": "0% null in profile (tolerance: 0%)"}

# unique rule (single-column)
{"type": "unique", "columns": ["order_id"],
 "_confidence": 0.85, "_reason": "100% distinct, 0% null — candidate key"}

# accepted_values rule
{"type": "accepted_values", "column": "status",
 "values": ["active", "inactive", "pending"],
 "_confidence": 0.7, "_reason": "3 distinct values, categorical"}

# range rule
{"type": "range", "column": "amount",
 "min": 0, "max": 330.0,
 "_confidence": 0.6, "_reason": "observed range [0.01, 300.0], 10% margin applied"}

# expression rule
{"type": "expression",
 "expr": "`created_at` <= `updated_at`",
 "description": "created_at before updated_at",
 "_confidence": 0.75, "_reason": "date column pair with temporal ordering"}
```

Fields prefixed with `_` are metadata (confidence, reason) — they are stripped automatically by the renderer in markdown mode.

---

## Constants

| Constant | Value | Purpose |
|---|---|---|
| `ACCEPTED_VALUES_MAX_DISTINCT` | 20 | Max distinct values for accepted_values rule generation |
| `DATE_COLUMN_KEYWORDS` | `{"date", "time", "timestamp", "dt", "datetime", "created", "updated", "modified"}` | Keywords for date column detection |
| `AUDIT_COLUMN_PATTERNS` | `{"_extracted_at", "_source_file", "_row_hash", "_loaded_at", "_ingested_at"}` | Auto-excluded audit columns |

---

## Typical Workflow

```python
# 1. Profile the data
profile = anchor("profile_table", df, subject="sales", output_format="dict")

# 2. Generate rules
ctx = anchor("suggest_rules", profile_ctx=profile)

# 3. Review and filter by confidence
rules = [r for r in ctx["rules"] if r.get("_confidence", 0) >= 0.7]

# 4. Validate
result = anchor("validate", df, rules=rules)
```

---

## Error Cases

| Error | Cause | Fix |
|---|---|---|
| `ValueError: Must provide profile_ctx or df` | Neither input provided | Pass `df=` or `profile_ctx=` |
| `ValueError: strictness must be one of...` | Invalid strictness value | Use `"strict"`, `"standard"`, or `"lenient"` |
| `ValueError: Invalid include_types` | Unrecognized rule type | Check valid types: `not_null`, `unique`, `accepted_values`, `range`, `expression`, `type_check` |
| `TypeError: profile_ctx must be a dict` | Passed markdown string instead of dict | Re-run with `output_format="dict"` |
| `ValueError: profile_ctx has no column_profiles` | Incomplete profile | Re-run `anchor("profile_table", df, output_format="dict")` |
