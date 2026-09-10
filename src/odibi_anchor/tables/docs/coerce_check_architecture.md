# coerce_check — Architecture

**Module:** `tables/coercion_classifier.py` (431 lines)
**Entry point:** `coercion_check_context(old_df, new_df, keys=[...])`

---

## System Overview

```
old_df + new_df
       │
       ▼
  [1] Inner join on keys
      (only matched rows — added/removed handled by diff)
       │
       ▼
  [2] For each column:
      filter to rows where both values are non-null
      filter to rows where string representations differ
       │
       ▼
  [3] Classify each value pair
      _classify_pair(old_val, new_val)
       │
       ├── whitespace → strip both → equal?
       ├── case → strip + case-fold → equal?
       ├── unicode → strip invisible chars → equal?
       ├── numeric → parse as float → equal?
       ├── date_format → parse both date formats → equal?
       └── genuine → none of the above resolved it
       │
       ▼
  [4] Aggregate per column
      dominant_category, confidence, suggested_fix
       │
       ▼
  [5] Build context dict → render or return
```

---

## Classification Category Hierarchy

Categories are checked in priority order. The first check that resolves the mismatch wins:

| Priority | Category | Check |
|---|---|---|
| 1 | `whitespace` | `old.strip() == new.strip()` |
| 2 | `case` | `old.strip().casefold() == new.strip().casefold()` |
| 3 | `whitespace+case` | Whitespace resolved it but case still differed (combined) |
| 4 | `unicode` | Strip zero-width chars (U+200B, U+200C, U+200D, U+FEFF, U+00AD) → equal? |
| 5 | `numeric_representation` | Both parseable as float AND `float(old) == float(new)` |
| 6 | `date_format` | Both parseable with any of 10 date formats AND resolved dates equal |
| 7 | `genuine` | No coercion resolved the mismatch — values are truly different |

---

## Design Decisions

**Inner join only — null transitions excluded.**
`null→value` and `value→null` are handled by `diff` as genuine data changes, not coercion issues. `coerce_check` only classifies rows where both sides have a value. Null comparisons are explicitly excluded.

**String coercion only.**
All values are cast to string before comparison. This means numeric columns are checked for string representation differences (`"1.0"` vs `"1"`), not floating-point equality. Use `diff` for numeric precision issues.

**Majority threshold for representation classification.**
A column is classified as a representation issue (not genuine) only when the dominant category accounts for > 50% of mismatches (`_REPR_MAJORITY_THRESHOLD = 0.5`). Columns with mixed genuine/coercion mismatches are not blindly marked as fixable.

**Pandas only.**
The inner join and per-row classification loop uses pandas. Spark support is not implemented. For large datasets, sample before calling.

---

## Internal Structure

```
tables/
└── coercion_classifier.py
    ├── coercion_check_context()     ← public entry point
    ├── _classify_pair()             ← single value pair → category string
    ├── _strip_invisible()           ← unicode zero-width char remover
    ├── _same_date_different_format() ← date parser for 10 format strings
    ├── _SUGGESTED_FIXES             ← category → fix string map
    ├── _REPRESENTATION_CATEGORIES   ← set of fixable category names
    └── render_coercion_report()     ← markdown renderer
```

---

## Anchor Standard Output Contract

```python
ctx = {
    "kind":    "coercion_check_context",
    "subject": "old_df vs new_df",
    "status":  "ok",
    "summary": "14 mismatches across 2 columns — 12 representation (86%), 2 genuine",
    "metrics": {
        "total_mismatches":     14,
        "total_representation": 12,
        "total_genuine":         2,
        "representation_pct":   0.857,
        "category_totals": {"case": 10, "whitespace+case": 2, "genuine": 2},
    },
    "column_results": {
        "Generic Queue Status": {
            "total_mismatches": 12,
            "categories":       {"case": 10, "whitespace+case": 2},
            "dominant_category": "case",
            "confidence":        0.833,
            "suggested_fix":    "UPPER()/LOWER() both sides before comparing",
        },
        "Project Status": {
            "total_mismatches": 2,
            "categories":       {"genuine": 2},
            "dominant_category": "genuine",
            "confidence":        1.0,
            "suggested_fix":    "Values are genuinely different — no coercion fix available",
        },
    },
    "samples": {
        "Generic Queue Status": [
            {"old": "Scoping", "new": "SCOPING", "category": "case"},
            {"old": "  Active  ", "new": "ACTIVE",  "category": "whitespace+case"},
        ],
    },
    "findings":               [...],
    "risks":                  [...],
    "suggested_next_actions": [...],
}
```

---

## Integration

```
diff (value_changed > 0?)
  └──→ coerce_check (why did it change?)
           ├── representation → coerce_fix (apply TRIM/UPPER)
           └── genuine        → business logic decision required
```

Only run `coerce_check` on columns where `diff` showed `value_changed > 0`. Columns with only `null_to_value` or `value_to_null` changes are genuine data transitions — not coercion issues.
