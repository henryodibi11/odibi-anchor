# Coerce Fix Tool — Architecture

## System Overview

The coerce fix tool applies automatic corrections to DataFrame columns based on
classification output from `anchor("coerce_check")`. It reads the per-column mismatch
category (whitespace, case, unicode, numeric_representation, date_format) and
applies the corresponding fix function.

```
anchor("coerce_check", old_df, new_df, keys=[...], columns=[...])
       │
       ▼
   coerce_ctx dict
   (per-column classification: "whitespace", "case", "genuine", etc.)
       │
       ▼
┌──────────────────────────────────────────────────┐
│          coerce_fix_context()                   │
│                                                  │
│  1. Parse column_results from coerce_ctx         │
│  2. Filter: skip "genuine" + "identical" columns │
│  3. Match category → fix function                │
│  4. Apply fix (or dry_run plan)                  │
│  5. Capture before/after samples                 │
│  6. Count rows affected                          │
│  7. Assemble Anchor standard output                  │
│                                                  │
└──────────────────────────────────────────────────┘
       │
       ▼
   Output: { kind: "coerce_fix", df: fixed_df, fixes_applied: [...], ... }
```

## Fix Types

| Category | Fix Function | Operation |
|----------|-------------|------------|
| `whitespace` | `_fix_whitespace` | Strip leading/trailing whitespace, collapse internal runs |
| `case` | `_fix_case` | Normalize to UPPER or LOWER |
| `whitespace+case` | `_fix_whitespace` + `_fix_case` | Both operations chained |
| `unicode` | `_fix_unicode` | Strip zero-width characters (\u200b, \ufeff, etc.) + NFC normalize |
| `numeric_representation` | `_fix_numeric` | Remove commas/spaces, strip trailing `.0` |
| `date_format` | `_fix_date` | Parse flexibly with `pd.to_datetime()`, reformat to target |

### Non-Fixable Categories (Skipped)

| Category | Reason |
|----------|--------|
| `genuine` | Real data differences, not formatting |
| `identical` | No mismatches found |
| Unknown categories | Not in `_FIXABLE_CATEGORIES` set |

## Design Decisions

### Non-Destructive by Default

- `dry_run=True`: Returns the fix plan without modifying any data
- `df.copy()` is called before applying fixes — the original DataFrame is never mutated
- Unparseable values in date fixing are left unchanged (fallback to original)

### Column-Selective

- By default, processes ALL columns classified as fixable by coerce_check
- `columns=` parameter limits scope to specific columns
- "genuine" differences are always skipped (require manual investigation)

### Wrong-Side Detection

After applying fixes, if ALL columns show 0 rows affected, the tool emits a
risk warning: "You may be fixing the wrong DataFrame." This catches the common
error of passing the reference side instead of the side with formatting issues.

### Pandas-Only (Current)

The tool currently supports only pandas DataFrames, matching the coerce_check
engine. Spark support is planned for future release. The `detect_engine()` call
validates this up-front with a clear error message.

## Internal Structure

```
coerce_fix_tool/
├── coerce_fix_impl.py    → Main implementation (478 lines)
│   ├── _fix_whitespace()   → .str.strip() + regex collapse
│   ├── _fix_case()         → .str.upper() / .str.lower()
│   ├── _fix_unicode()      → regex + unicodedata.normalize
│   ├── _fix_numeric()      → regex + float/int conversion
│   ├── _fix_date()         → pd.to_datetime + strftime
│   ├── _apply_fix_pandas() → Category → function dispatcher
│   ├── _capture_samples()  → Before/after diff sampling
│   └── coerce_fix_context()→ Orchestrator + output assembly
├── __init__.py
├── README.md
├── tool.json             → Anchor tool registry metadata
└── test_coerce_fix.py
```

## Integration with coerce_check

The typical workflow:

1. `anchor("diff", old_df, new_df, keys=[...])` → finds `value_changed` columns
2. `anchor("coerce_check", old_df, new_df, keys=[...], columns=[...])` → classifies mismatches
3. `anchor("coerce_fix", new_df, coerce_ctx)` → applies corrections
4. `anchor("diff", old_df, fixed_df, keys=[...])` → verify fixes resolved the mismatches

## Anchor Standard Output Contract

```python
{
    "kind": "coerce_fix",
    "subject": "<label>",
    "summary": "Fixed 3 column(s): name (case→UPPER), date (date_format→...). 150 values corrected.",
    "metrics": {
        "columns_fixed": 3,
        "columns_skipped_genuine": 1,
        "total_values_corrected": 150,
        "by_category": {"case": 100, "whitespace": 50},
    },
    "findings": ["name: UPPER (100 rows)", "date: parse to ISO date (50 rows)"],
    "risks": [...],
    "samples": {
        "name": {"category": "case", "before": ["alice", "Bob"], "after": ["ALICE", "BOB"]},
    },
    "suggested_next_actions": [...],
    "fixes_applied": [...],
    "skipped": [...],
    "df": <fixed DataFrame>,
}
```

The `df` key in the output contains the corrected DataFrame, ready for downstream use.
