# quality_gate_context

`quality_gate_context` answers "is this DataFrame structurally safe to
write downstream?" for both humans and LLMs. It produces a structured
dict (or markdown report) containing per-check pass/fail results, a
binary write-safety decision, fix code, and suggested next actions.

**Distinction from validation_summary_context:**
- `validation_summary_context` = "does data pass business rules"
- `quality_gate_context` = "is data structurally safe to write"

## Public API

```python
from odibi_anchor.validation import quality_gate_context, render_quality_gate_report

def quality_gate_context(
    df: Any,
    *,
    keys: list[str] | None = None,
    target_schema: Any = None,
    checks: list[str] | None = None,
    subject: str = "dataframe",
    engine: Literal["auto", "pandas", "spark"] = "auto",
    sample_limit: int = 10,
    freshness_column: str | None = None,
    df_name: str = "df",
    thresholds: dict | None = None,
    output_format: Literal["dict", "markdown"] = "dict",
) -> dict[str, Any] | str:
    ...
```

## Checks Implemented

| Check ID | Requires | Severity | What It Catches |
|----------|----------|----------|-----------------|
| row_count | nothing | blocker (0 rows) / warning (<10) | Empty or tiny DataFrames |
| duplicate_keys | keys | blocker | Would violate grain |
| null_keys | keys | blocker | Would break joins |
| schema_compat | target_schema | blocker (missing) / warning (extra) | Column mismatches |
| null_columns | nothing | warning | 100% null dead columns |
| completeness | nothing | info | Per-column null summary |
| freshness | freshness_column | warning | Stale data |

Checks auto-skip when their required input is not provided (e.g., no `keys`
means duplicate_keys and null_keys are skipped, not errored).

## Output Shape

```python
{
    "kind": "quality_gate_context",
    "subject": "catalog.schema.table",
    "engine": "pandas",
    "status": "pass" | "warn" | "fail",
    "summary": "5/5 checks passed. Write safe.",
    "metrics": {
        "total_rows": 1000,
        "checks_run": 5,
        "checks_passed": 4,
        "checks_failed": 0,
        "checks_warned": 1,
        "is_write_safe": True,
    },
    "checks": [{
        "check_id": "duplicate_keys",
        "status": "pass" | "fail" | "warn",
        "severity": "blocker",
        "detail": "No duplicate keys found.",
        "fix_expr": "df = df.drop_duplicates(subset=['id'])",
        "samples": [{"id": 1, "count": 2}],
        # Per-check extras:
        "duplicate_key_pct": 0.05,
        "null_key_pct": 0.0,
        "missing_columns": [],
    }],
    "findings": [{...}],
    "risks": [{...}],
    "fix_all_expr": "# Combined fix:\ndf = df.drop_duplicates(subset=['id'])\ndf = df.dropna(subset=['id'])",
    "fix_impact": {
        "rows_dropped_by_fix": 50,
        "is_upper_bound": True,
    },
    "recommendation": "FAIL. 1 blocker check(s) must be resolved.",
    "suggested_next_actions": [str],
}
```

### Key Decision Fields

- **`status`** — Overall gate: `"pass"`, `"warn"`, or `"fail"`
- **`metrics["is_write_safe"]`** — Boolean. True = safe to write. False = fix first.
- **`fix_all_expr`** — Combined fix code. Paste, run, re-check.

## Example Usage

### Basic Pre-Write Gate

```python
import pandas as pd
from odibi_anchor.validation import quality_gate_context

df = pd.DataFrame({
    "id": [1, 2, 2, None],
    "name": ["a", "b", "c", "d"],
})

result = quality_gate_context(df, keys=["id"])

if not result["metrics"]["is_write_safe"]:
    print(f"BLOCKED: {result['summary']}")
    print(f"Fix:\n{result['fix_all_expr']}")
else:
    # Safe to write
    saver.save(df, ...)
```

### With Target Schema Validation

```python
from pyspark.sql.types import StructType, StructField, StringType, IntegerType

target_schema = StructType([
    StructField("id", IntegerType(), False),
    StructField("name", StringType(), True),
    StructField("region", StringType(), False),
])

result = quality_gate_context(
    df,
    keys=["id"],
    target_schema=target_schema,
    subject="silver.energy.assets",
)
```

### Freshness Check

```python
result = quality_gate_context(
    df,
    keys=["id"],
    freshness_column="updated_at",
    thresholds={"max_staleness_hours": 24},
)
```

### Custom Variable Name in Fix Expressions

```python
result = quality_gate_context(
    silver_df,
    keys=["asset_id", "date"],
    df_name="silver_df",
)
# fix_all_expr uses "silver_df" not "df":
# silver_df = silver_df.drop_duplicates(subset=['asset_id', 'date'])
```

### Markdown Output

```python
report = quality_gate_context(df, keys=["id"], output_format="markdown")
print(report)

# Or two-step:
from odibi_anchor.validation import render_quality_gate_report
result = quality_gate_context(df, keys=["id"])
report = render_quality_gate_report(result)
```

## Design Decisions

1. **`is_write_safe` is THE binary decision** — Agent reads one field. No
   reasoning needed. True = write. False = fix first.

2. **Fix expressions are engine-aware** — Pandas gets `df.drop_duplicates(...)`,
   Spark gets `df.dropDuplicates([...])`.

3. **`fix_all_expr` combines all fixes in order** — Agent pastes one block,
   runs it, re-checks. No per-check iteration needed.

4. **fix_impact is upper-bound** — Null rows and duplicate rows may overlap,
   so actual drop count ≤ sum of per-check drops.

5. **Schema compat: Missing = blocker, Extra = warning** — Missing columns
   would cause write failure. Extra columns are just noise.

6. **Freshness is a warning, not a blocker** — Stale data should trigger
   review, not automatic rejection.

## When To Use

Use this before:

- ANY write to a Delta table;
- merging into a target table;
- promoting from bronze to silver/gold;
- running an ML pipeline that assumes clean input.

## Gotchas

- Status values are `"pass"`, `"warn"`, `"fail"` (NOT "blocked")
- `is_write_safe` can be True even when status is "warn" (warnings don't block)
- Empty DataFrame (0 rows) is a blocker; 1-9 rows is a warning
- When `keys=None`, duplicate_keys and null_keys checks are silently skipped
