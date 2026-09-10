# pre_join — Architecture

## Purpose

Validate two DataFrames are safe to join BEFORE joining. Detects key overlap gaps,
cardinality issues, null keys that will silently drop rows, format mismatches
(whitespace/case/unicode), and predicts the output size with fanout risk assessment.

---

## Pipeline

```
left_df + right_df + keys (or left_keys/right_keys)
    |
    +-- 1. Build Composite Keys ------- Concatenate multi-column keys into single string
    |                                    (separator="|||", null sentinel="__NULL__")
    |
    +-- 2. Key Overlap Analysis -------- Set intersection/subtraction on distinct keys
    |       - overlap_count/pct
    |       - left_orphan_count/pct (keys with no right match)
    |       - right_orphan_count/pct (keys unused by left)
    |
    +-- 3. Cardinality Detection ------- Compare distinct keys vs row count
    |       - 1:1, 1:many, many:1, many:many
    |
    +-- 4. Null Key Audit -------------- Per-column null counts on key columns
    |       - Identifies rows that will silently drop
    |
    +-- 5. Format Compatibility -------- Sample 50 key values, check for:
    |       - Trailing/leading whitespace
    |       - Case mismatches (same value, different casing)
    |       - Zero-width unicode characters (category Cf, Mn)
    |
    +-- 6. Output Size Prediction ------ Based on matching_left_rows × right_dups_per_key
    |       - predicted_rows, fanout_ratio, fanout_risk
    |
    +-- 7. Build Contract -------------- Findings, risks, suggested actions
```

---

## Cardinality Detection

| left_count > left_distinct | right_count > right_distinct | Result |
|:-:|:-:|:--|
| No | No | `1:1` |
| No | Yes | `1:many` |
| Yes | No | `many:1` |
| Yes | Yes | `many:many` |

Logic: If total rows exceed distinct key count, that side has duplicate keys.

---

## Fanout Risk Scale

| Fanout Ratio | Risk Level | Meaning |
|---|---|---|
| ≤ 1.05 | `none` | No row expansion |
| 1.05 – 1.5 | `low` | Minimal expansion |
| 1.5 – 3.0 | `medium` | Moderate duplication |
| 3.0 – 10.0 | `high` | Significant row explosion |
| > 10.0 | `extreme` | Dangerous expansion |

Fanout formula for 1:many / many:many:
```
predicted_rows = matching_left_rows × avg_right_dups_per_overlap_key
fanout_ratio = avg_right_dups_per_overlap_key
```

For 1:1 / many:1:
```
predicted_rows = matching_left_rows
fanout_ratio = matching_left_rows / left_count
```

---

## Format Compatibility Checks

| Check | Detection | Example |
|-------|-----------|--------|
| Whitespace | `v != v.strip()` on sample values | `"ABC "` vs `"ABC"` |
| Case | Same `.lower()` but different original | `"Smith"` vs `"SMITH"` |
| Unicode | `unicodedata.category(ch)` in `("Cf", "Mn")` | Zero-width joiners, combining marks |

Samples 50 key values from each side. Reports `format_compatible: bool` and `format_issues: list`.

---

## Design Decisions

| Decision | Rationale |
|----------|----------|
| Inlined engine detection (`_detect_engine`) | Avoids importing from `_utils.engine_utils` — tool stays standalone |
| Composite key via string concatenation | Supports multi-column keys without complex hashing; separator `"\|\|\|"` avoids collisions |
| Null sentinel `"__NULL__"` | Prevents null keys from matching each other (nulls should drop, not join) |
| `matching_left_rows` instead of `overlap_pct × left_count` | Accurate when left-side duplication is non-uniform across keys |
| Right dups computed over overlap keys only | Prevents non-matching keys from diluting the average |
| Separate Pandas and Spark engines (not abstracted) | Each uses native idioms: pandas sets vs Spark intersect/subtract |
| Markdown renderer structured around 4 questions | "Will I lose rows?", "Will it explode?", "Why is overlap low?", "Are keys dirty?" |
| sample_limit on orphans and fanout keys | Prevents large output; defaults to 10 |

---

## Internal Functions

| Function | Lines | Responsibility |
|----------|-------|---------------|
| `_detect_engine(df)` | 24-43 | Detect pandas/spark (inlined, not imported) |
| `_build_composite_key_pandas(df, keys)` | 54-60 | Concatenate multi-column keys (pandas) |
| `_build_composite_key_spark(df, keys)` | 63-69 | Concatenate multi-column keys (Spark) |
| `_null_analysis_pandas(df, keys, side)` | 72-83 | Per-column null counts (pandas) |
| `_null_analysis_spark(df, keys, side)` | 86-101 | Per-column null counts (Spark, single-pass) |
| `_determine_cardinality(l_dist, r_dist, l_count, r_count)` | 104-120 | Classify as 1:1/1:many/many:1/many:many |
| `_predict_output_size(l_count, r_count, matching, card, avg_dups)` | 123-167 | Predict rows + fanout risk |
| `_check_format_compatibility(left_sample, right_sample)` | 170-216 | Whitespace/case/unicode checks |
| `_analyze_pandas(left, right, l_keys, r_keys, limit)` | 223-348 | Full analysis pipeline (pandas) |
| `_analyze_spark(left, right, l_keys, r_keys, limit)` | 355-494 | Full analysis pipeline (Spark) |
| `_build_contract(analysis, l_subj, r_subj, l_keys, r_keys)` | 501-664 | Assemble Anchor contract from raw analysis |
| `_render_markdown(contract)` | 671-776 | Decision-structured markdown report |
| `pre_join_context(...)` | 783-911 | Public entry point |

---

## Output Contract

```python
{
    "kind": "pre_join",
    "subject": "orders -> customers",
    "summary": "OK: INNER JOIN safe, 1:1 cardinality, 96.0% overlap",
    "metrics": {
        "left_count": int,
        "right_count": int,
        "left_key_distinct": int,
        "right_key_distinct": int,
        "overlap_count": int,
        "overlap_pct": float,          # overlap / left_distinct
        "matching_left_rows": int,     # actual left rows that will survive INNER JOIN
        "matching_left_rows_pct": float,
        "left_orphan_count": int,
        "left_orphan_pct": float,
        "right_orphan_count": int,
        "right_orphan_pct": float,
        "cardinality": str,            # "1:1" | "1:many" | "many:1" | "many:many"
        "right_rows_per_key_avg": float,
        "right_rows_per_key_max": int,
        "left_null_key_count": int,
        "left_null_key_pct": float,
        "right_null_key_count": int,
        "right_null_key_pct": float,
        "null_key_columns": {"col": {"left_nulls": int, "right_nulls": int}},
        "predicted_rows": int,
        "fanout_ratio": float,
        "fanout_risk": str,            # "none" | "low" | "medium" | "high" | "extreme"
        "format_compatible": bool,
        "format_mismatch_description": str | None,
    },
    "findings": [str, ...],
    "risks": [str, ...],
    "samples": {
        "left_orphan_keys": [str, ...],
        "right_orphan_keys": [str, ...],
        "max_fanout_keys": [{"key": str, "right_matches": int}, ...],
        "format_mismatch_examples": [{"type": str, ...}, ...],
    },
    "suggested_next_actions": [str, ...],
}
```

---

## Engine Strategy

Engine detection is **inlined** (not imported from `_utils.engine_utils`):

- **Pandas**: Uses `set()` operations for overlap/orphans, `value_counts()` for duplication stats
- **Spark**: Uses `.distinct()`, `.intersect()`, `.subtract()`, `.groupBy().count()`, left semi-join for matching rows
- Both engines produce identical output dict structure