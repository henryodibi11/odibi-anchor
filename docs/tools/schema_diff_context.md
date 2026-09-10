# schema_diff_context

`schema_diff_context` is a standalone context generator that turns two
DataFrame schemas into a compact, structured change packet for humans
and LLMs.

It answers:

```text
What changed in the schema?
Is this safe to write, merge, score, or publish?
What should happen next?
```

## Public API

```python
from odibi_anchor.tables import schema_diff_context, render_schema_diff_report

def schema_diff_context(
    old_df: Any,
    new_df: Any,
    *,
    old_subject: str = "old_df",
    new_subject: str = "new_df",
    subject: str | None = None,
    engine: Literal["auto", "pandas", "spark"] = "auto",
    include_unchanged: bool = True,
    max_unchanged: int = 50,
    output_format: Literal["dict", "markdown"] = "dict",
    spark: Any = None,
) -> dict[str, Any] | str:
    ...
```

## Scope

- Pandas and Spark implementations.
- Spark path uses `df.schema.fields` (local metadata, **zero Spark actions**).
- Supports table-name strings (Spark only, requires `spark=` kwarg).
- No framework, registry, base class, persistence, or config.
- Single dependency: `odibi_anchor._utils.engine_utils.detect_engine`.

## Output Shape

```python
{
    "kind": "schema_diff_context",
    "subject": "old_df -> new_df",
    "old_subject": "old_df",
    "new_subject": "new_df",
    "summary": "Schema changed: 1 added, 0 removed, 1 type changed, 8 unchanged. Compatibility: breaking.",
    "metrics": {
        "engine": "pandas",
        "old_column_count": 9,
        "new_column_count": 10,
        "added_count": 1,
        "removed_count": 0,
        "type_changed_count": 1,
        "nullable_changed_count": 0,
        "unchanged_count": 8,
        "unchanged_returned_count": 8,
        "unchanged_truncated_count": 0,
        "reordered_count": 0,
        "net_column_delta": 1,
        "has_changes": True,
        "is_breaking_change": True,
        "compatibility": "breaking",  # "unchanged"|"additive"|"breaking"
    },
    "added": [{"column": ..., "dtype": ..., "position": ...}],
    "removed": [{"column": ..., "dtype": ..., "position": ..., "fix_expr": ...}],
    "type_changed": [{
        "column": ..., "old_dtype": ..., "new_dtype": ...,
        "old_type_family": ..., "new_type_family": ...,
        "risk_level": ...,
        "fix_expr": ..., "fix_safe": ...,
    }],
    "unchanged": [{"column": ..., "dtype": ..., "position": ...}],
    "reordered": [{"column": ..., "old_position": ..., "new_position": ...}],
    "nullable_changed": [{
        "column": ..., "old_nullable": ..., "new_nullable": ...,
        "direction": ...,  # "tightened" or "relaxed"
        "risk_level": ...,  # "high" or "info"
    }],
    "likely_renames": [{
        "old_column": ..., "new_column": ...,
        "confidence": ...,  # "high" or "medium"
        "reason": ...,  # "case_change" or "naming_convention_change"
        "dtype_match": ...,
    }],
    "write_safety": {
        "can_write": bool,
        "blockers": [str],
        "warnings": [str],
        "recommendation": str,
    },
    "findings": [{"check_type": ..., "detail": ..., "severity": ...}],
    "risks": [{"risk": ..., "severity": ..., "message": ...}],
    "samples": [],
    "suggested_next_actions": [str],
}
```

## Example Usage

### Basic (Pandas)

```python
import pandas as pd
from odibi_anchor.tables import schema_diff_context

old_df = pd.DataFrame({
    "asset_id": [1, 2],
    "mw": pd.Series([100.0, 110.0], dtype="float64"),
})

new_df = pd.DataFrame({
    "asset_id": ["1", "2"],
    "mw": pd.Series([100.0, 110.0], dtype="float64"),
    "market": ["ERCOT", "PJM"],
})

ctx = schema_diff_context(
    old_df, new_df,
    old_subject="asset_capacity_baseline",
    new_subject="asset_capacity_current",
)
print(ctx["summary"])
print(f"Breaking: {ctx['metrics']['is_breaking_change']}")
```

### Table-Name Input (Spark only)

```python
from odibi_anchor.tables import schema_diff_context

ctx = schema_diff_context(
    "catalog.schema.old_table",
    "catalog.schema.new_table",
    spark=spark,
)

# Mixed input (one table name, one DataFrame)
ctx = schema_diff_context(
    "catalog.schema.target_table",
    my_transformed_df,
    spark=spark,
)
```

### Pipeline Decision Gate

```python
ctx = schema_diff_context(old_df, new_df)

if not ctx["write_safety"]["can_write"]:
    raise SchemaBlockedError(ctx["write_safety"]["blockers"])
```

### Markdown for LLM

```python
report = schema_diff_context(old_df, new_df, output_format="markdown")
# Or two-step:
from odibi_anchor.tables import render_schema_diff_report
ctx = schema_diff_context(old_df, new_df)
report = render_schema_diff_report(ctx, show_unchanged=False)
```

## v2 Features

### Nullable Change Detection (Spark only)

Detects `nullable: True → False` (tightened = breaking) and
`False → True` (relaxed = informational):

```python
ctx["nullable_changed"]
# [{"column": "name", "old_nullable": True, "new_nullable": False,
#   "direction": "tightened", "risk_level": "high"}]
```

### Rename Heuristics

Detects case changes and naming convention changes without
reclassifying the added/removed lists:

```python
ctx["likely_renames"]
# [{"old_column": "assetId", "new_column": "asset_id",
#   "confidence": "high", "reason": "naming_convention_change",
#   "dtype_match": True}]
```

### Fix Code Snippets

Type-changed entries include ready-to-paste cast expressions:

```python
for tc in ctx["type_changed"]:
    print(tc["fix_expr"])
    # .withColumn('amount', col('amount').cast('decimal(10,2)'))
    print(tc["fix_safe"])
    # True (same type family) or False (cross-family)
```

### Write Safety Assessment

Pre-flight check before merging/writing:

```python
ctx["write_safety"]
# {"can_write": False,
#  "blockers": ["Target column 'region' is NOT NULL but missing from source."],
#  "warnings": ["Source has columns not in target: ['extra']."],
#  "recommendation": "BLOCKED. Fix blockers before writing."}
```

## When To Use

Use this before:

- writing to Delta tables;
- merging source data into a target table;
- updating validation contracts;
- model scoring or feature table refreshes;
- handing schema drift evidence to an AI agent.

## Performance

- **Zero Spark actions** — both Spark and Pandas paths extract schema
  metadata only (`df.schema.fields` for Spark).
- Intentionally different from `table_contract_summary` which samples data.

## Gotchas

- Container types (ArrayType, MapType) must be classified by prefix
  (`startswith`), not substring — Spark's `ArrayType(StringType(), True)`
  contains "string" as a substring.
- Nullable detection is Spark-only. Pandas dtypes don't carry reliable
  nullable info — returns `[]` for pandas.
- Table-name input requires `spark=` kwarg explicitly.
