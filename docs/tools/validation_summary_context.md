# validation_summary_context

`validation_summary_context` is a standalone context generator that turns
validation rule results into a compact, structured context packet for
humans and LLMs.

It answers:

```text
Is this data valid?
Is it safe to promote/write?
What needs to be fixed?
```

## Public API

```python
from odibi_anchor.validation import validation_summary_context

def validation_summary_context(
    df: Any,
    rules: list[dict[str, Any]],
    *,
    subject: str = "validation",
    engine: Literal["auto", "pandas", "spark"] = "auto",
    severity_map: dict[str, str] | None = None,
    sample_failures: int = 5,
    output_format: Literal["dict", "markdown"] = "dict",
    spark: Any = None,
) -> dict[str, Any] | str:
    ...
```

## Scope

- Pandas and Spark implementations.
- Spark path batches all rules into a single `select()` action for
  efficiency (unique rules use a separate groupBy).
- No framework, registry, base class, persistence, or config.
- Output is a plain dictionary that is easy to log, assert in tests,
  or paste into Genie/LLM prompts.
- Single dependency: `odibi_anchor._utils.engine_utils.detect_engine`.

## Rule Format

Rules use plain dicts, compatible with
`odibi.validation.quarantine.split_valid_invalid`:

```python
rules = [
    {"type": "not_null", "columns": ["id", "name"]},
    {"type": "unique", "columns": ["id"]},
    {"type": "accepted_values", "column": "status",
     "values": ["active", "pending"]},
    {"type": "range", "column": "amount", "min": 0, "max": 100},
    {"type": "regex", "column": "email",
     "pattern": r"^.+@.+\..+$"},
    {"type": "custom_sql", "condition": "amount > 0"},
]
```

## Severity System

Default severities:

| Rule Type | Default Severity |
|-----------|-----------------|
| not_null | blocker |
| unique | blocker |
| accepted_values | warning |
| range | warning |
| regex | warning |
| custom_sql | warning |

Override with `severity_map`:

```python
ctx = validation_summary_context(
    df, rules,
    severity_map={
        "range:amount": "blocker",    # specific
        "accepted_values": "info",    # broad
    },
)
```

Key format: `"{type}:{column}"` (specific) or `"{type}"` (broad).

## Promotion Safety

`is_promotion_safe` = True when **zero blocker-level rules fail**.
Warning-level failures alone do NOT block promotion.

## Output Shape

```python
{
    "kind": "validation_summary_context",
    "subject": "silver.energy.transactions",
    "summary": "2/4 rules failed (150/10,000 rows affected). Promotion: BLOCKED.",
    "metrics": {
        "engine": "spark",
        "total_rows": 10000,
        "rules_evaluated": 4,
        "rules_passed": 2,
        "rules_failed": 2,
        "blocker_count": 1,
        "warning_count": 1,
        "rows_with_any_failure": 150,
        "overall_pass_rate": 0.985,
        "is_promotion_safe": False,
    },
    "rules": [
        {
            "rule_id": "not_null:id,name",
            "rule_type": "not_null",
            "columns": ["id", "name"],
            "severity": "blocker",
            "passed": False,
            "failed_count": 50,
            "failed_rate": 0.005,
            "detail": "Columns must not be null: ['id', 'name']",
            "sample_failures": [
                {"id": None, "name": "Alice", "amount": 10.0},
            ],
        },
    ],
    "blockers": ["not_null:id,name: 50 rows failed (0.5%)"],
    "warnings": ["range:amount: 100 rows failed (1.0%)"],
    "recommendation": "BLOCKED. 1 blocker(s) must be resolved before promotion.",
    "findings": [...],
    "risks": [...],
    "samples": [],
    "suggested_next_actions": [...],
    "quarantine_call": "from odibi.validation import ...\n...",
}
```

### v2 Per-Rule Fields

Each rule in `rules[]` now includes:

```python
{
    "rule_id": "range:amount",
    ...
    "fix_expr": "df = df.filter((col('amount') >= 0) & ...)",
    # Only present when rule can't be evaluated:
    "status": "error",  # omitted for normal rules
}
```

- `fix_expr` — Ready-to-paste filter/deduplicate code (engine-aware)
- `status` — Set to `"error"` when schema mismatch prevents evaluation

## Example Usage

### Basic (Pandas)

```python
import pandas as pd
from odibi_anchor.validation import validation_summary_context

df = pd.DataFrame({
    "id": [1, None, 3, 4],
    "amount": [10, -5, 200, 50],
    "status": ["active", "invalid", "pending", "active"],
})

rules = [
    {"type": "not_null", "columns": ["id"]},
    {"type": "range", "column": "amount", "min": 0, "max": 100},
    {"type": "accepted_values", "column": "status",
     "values": ["active", "pending"]},
]

ctx = validation_summary_context(df, rules)
print(ctx["summary"])
# "3/3 rules failed (3/4 rows affected). Promotion: BLOCKED."
```

### Spark with Table Name

```python
from odibi_anchor.validation import validation_summary_context

ctx = validation_summary_context(
    "catalog.schema.my_table",
    rules=[
        {"type": "not_null", "columns": ["id"]},
        {"type": "unique", "columns": ["id"]},
    ],
    subject="silver.energy.transactions",
    spark=spark,
)
```

### Pipeline Decision Gate

```python
ctx = validation_summary_context(df, rules, sample_failures=0)

if not ctx["metrics"]["is_promotion_safe"]:
    raise RuntimeError(
        f"Validation blocked: {ctx['blockers']}"
    )
```

### Markdown for LLM

```python
report = validation_summary_context(
    df, rules, output_format="markdown"
)
# Feed directly to LLM prompt
```

## v2 Features

### Fix Code Snippets (`fix_expr`)

Every failed rule includes a ready-to-paste fix expression:

| Rule Type | Pandas | Spark |
|-----------|--------|-------|
| not_null | `df = df[df['col'].notna()]` | `df = df.filter(col('col').isNotNull())` |
| unique | `df = df.drop_duplicates(subset=['col'])` | `df = df.dropDuplicates(['col'])` |
| range | `df = df[(df['col'] >= 0) & ...]` | `df = df.filter((col('col') >= 0) & ...)` |
| accepted_values | `df = df[df['col'].isin([...])]` | `df = df.filter(col('col').isin([...]))` |

### Schema Mismatch Detection

Detects incompatible rule-column pairs **before** evaluation:

- Range rule on string column → error (not silently passing)
- Regex rule on numeric column → error

Mismatched rules produce `"status": "error"` instead of being
evaluated, with `failed_count = total_rows`.

### Quarantine Call Generation

When blockers fail, `quarantine_call` contains a complete
`split_valid_invalid()` invocation:

```python
from odibi.validation import split_valid_invalid

result = split_valid_invalid(df, [
    {'type': 'not_null', 'columns': ['id']},
    {'type': 'unique', 'columns': ['id']},
])
valid_df = result.valid_df
invalid_df = result.invalid_df
# 2 blocker rule(s) applied
```

## When To Use

Use this before:

- writing to Delta tables (pre-write gate);
- promoting data from bronze to silver;
- merging source data into a target table;
- handing data quality evidence to an AI agent;
- running a pipeline step that assumes clean input.

## Spark Performance

The Spark path minimizes actions:

| Action | Purpose |
|--------|---------|
| 1 `select(sum(...))` | Batch failure counts for all non-unique rules |
| 1 `groupBy` per unique rule | Uniqueness check (can't batch) |
| 1 `filter(~combined).count()` | Rows with any failure |
| N `filter().limit().toPandas()` | Sample collection (failed rules only) |

With `sample_failures=0`: exactly 2 + unique_rule_count actions.

## Gotchas

- `custom_sql` on Pandas uses `df.query()` — some Spark SQL expressions
  (COALESCE, CASE WHEN) won't work. Use simple expressions.
- NULL handling in Spark range checks is correct but asymmetric —
  `rows_with_any_failure` may undercount when failures are NULL-induced.
- Unique rules cannot be batched (require `groupBy`) — handled separately.
