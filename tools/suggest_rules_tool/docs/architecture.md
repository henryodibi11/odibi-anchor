# suggest_rules — Architecture

## Overview

The `suggest_rules` tool auto-generates a starter set of validation rules from a data profile. Its output is directly consumable by `anchor("validate")` — no format conversion needed.

---

## Pipeline

```
Input (profile_ctx OR df)
    │
    ├─ If df provided → auto-profile via dataset_profile_context()
    │
    ▼
Resolve profile_ctx
    │
    ▼
Filter columns (exclude_columns + auto-exclude audit patterns)
    │
    ▼
Per-type inference engines (run in sequence):
    ├─ _infer_not_null()         → columns with 0% nulls
    ├─ _infer_unique()           → candidate key columns (100% distinct, 0% null)
    ├─ _infer_accepted_values()  → low-cardinality categoricals (≤20 distinct)
    ├─ _infer_range()            → numeric columns with observed min/max
    └─ _infer_expressions()      → date ordering pairs (start ≤ end)
    │
    ▼
Aggregate rules + confidence scores
    │
    ▼
Build Anchor contract (kind="suggest_rules")
    │
    ▼
finalize_context() → dict or markdown
```

---

## Rule Types

| Type | Inference Logic | Confidence |
|---|---|---|
| `not_null` | Column null_pct ≤ strictness tolerance | 0.9 |
| `unique` | 100% distinct + 0% null + listed as potential key | 0.85 |
| `accepted_values` | Distinct count ≤ 20, non-numeric, ≥80% values captured in top_values | 0.7 |
| `range` | Numeric column with valid min/max, margin applied per strictness | 0.6 |
| `expression` | Date column pairs matching ordering patterns (created ≤ updated) | 0.75 |

---

## Strictness Levels

Strictness controls how aggressive the rule inference is:

| Level | null_tolerance | range_margin | Behavior |
|---|---|---|---|
| `strict` | 0% | 0% | Exact observed bounds, zero nulls allowed |
| `standard` | 0% | 10% | Small margin on ranges, still zero-null |
| `lenient` | 5% | 50% | Wide margins, tolerates some nulls |

---

## Design Decisions

1. **Output is validate-ready**: The `rules` list in the output dict can be passed directly to `anchor("validate", df, rules=ctx["rules"])`. No transformation layer needed.

2. **Confidence scoring**: Each rule carries `_confidence` (0–1) indicating how strongly the data supports the rule. Downstream consumers can filter by confidence threshold.

3. **Auto-exclude audit columns**: Columns matching patterns like `_extracted_at`, `_source_file`, `_row_hash`, `_loaded_at`, `_ingested_at` are automatically skipped — these change every load and produce useless rules.

4. **Date column detection**: Uses both type inference (`datetime`, `date`, `timestamp`) and keyword matching (`date`, `time`, `created`, `updated`, etc.) to find date columns for expression rules.

5. **ACCEPTED_VALUES_MAX_DISTINCT = 20**: Above this cardinality, accepted_values rules are not generated — they would be brittle and likely incomplete.

6. **Standard Anchor contract**: Output follows `build_base_context` + `finalize_context` pattern, supporting both dict and markdown output formats.

---

## Dependencies

| Module | Purpose |
|---|---|
| `odibi_anchor.profiling.dataset_profile_context` | Auto-profile a DataFrame when no profile_ctx provided |
| `odibi_anchor._utils.contract` | Build/finalize standard Anchor output contract |
| `odibi_anchor._utils.render_utils` | Markdown rendering for output_format="markdown" |

---

## Error Handling

If rule inference raises `KeyError`, `TypeError`, or `AttributeError`, the tool catches the exception and returns a degraded contract with:
- `summary` prefixed with "ERROR:"
- `risks` containing the exception message
- `rules` as empty list
- `suggested_next_actions` guiding the user to inspect their profile_ctx
