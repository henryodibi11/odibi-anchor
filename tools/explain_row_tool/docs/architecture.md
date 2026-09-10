# explain_row — Architecture

## Purpose

Trace exactly where each column value in a specific output row came from — across
multiple upstream DataFrames. Reports column-level lineage with match classification,
join path detection, and actionable findings.

---

## Pipeline

```
output_df + keys + values (identify the row) + upstream dict
    |
    +-- 1. Extract Target Row -------- Filter output_df by key=value to get one row
    |
    +-- 2. Trace Column Origins ------ For each column in the output row:
    |       |                            - Search each upstream for same column name
    |       |                            - Filter upstream by join keys
    |       |                            - Compare output value vs upstream value
    |       +-- Classify: exact | transformed | coerced | null_filled | not_found
    |
    +-- 3. Detect Join Paths --------- Which key columns connect output to each upstream
    |
    +-- 4. Build Summary + Findings -- Origin distribution, transformations, risks
    |
    +-- 5. Assemble Contract --------- Standard Anchor output with column_lineage + join_paths
```

---

## Match Types

| Match Type | Meaning | Detection Logic |
|------------|---------|----------------|
| `exact` | Values are identical (including both null) | `output_value == upstream_value` |
| `coerced` | Values match after normalization (whitespace, case, type coercion) | `str(a) == str(b)` or `strip().lower()` match |
| `transformed` | Column exists in upstream but value differs | Column present, neither null, values not equal |
| `null_filled` | Upstream is NULL but output has a value | Upstream null + output non-null (COALESCE/default) |
| `not_found` | No upstream has this column, or no join path connects | Column not in any upstream or key match fails |

Priority for classification: exact > coerced > null_filled > transformed.

---

## Design Decisions

| Decision | Rationale |
|----------|----------|
| Trace-based (backward from output) | More intuitive than forward-tracing: user has a row, wants to know where it came from |
| First-match-wins for primary origin | When multiple upstreams have the same column, first match is reported as origin; all matches listed in `all_sources` |
| Uses shared `engine_utils.detect_engine` | Unlike diagnose_empty (standalone), explain_row imports from `_utils` since it's a higher-level tool |
| `sample_limit` on `all_sources` | Prevents large output when many upstreams match |
| Separate join path detection | Tells user HOW the upstream connects, not just WHAT matched |
| Null handling via `math.isnan` | Catches both Python None and numpy/pandas NaN |
| Key validation upfront | Fails fast if keys not in values dict |

---

## Internal Functions

| Function | Lines | Responsibility |
|----------|-------|---------------|
| `_normalize_for_coercion(value)` | 36-50 | Strip + lowercase for coercion comparison; None for null/NaN |
| `_is_null(value)` | 53-63 | Check None or NaN (float math.isnan) |
| `_classify_value_match(output, upstream)` | 66-109 | Classify relationship between two values |
| `_extract_row(df, keys, values, engine)` | 117-153 | Filter DataFrame to single row by key values |
| `_find_matching_keys(upstream_df, keys, values, engine)` | 161-173 | Which key columns exist in this upstream |
| `_trace_column_origins(output_row, keys, ..., upstream, engine)` | 176-265 | Core lineage: for each column, find upstream origin |
| `_detect_join_paths(output_row, keys, upstream, engine)` | 273-301 | Which keys connect output to each upstream |
| `_build_summary(subject, column_lineage, upstream)` | 309-342 | Human-readable one-line summary |
| `_build_findings(column_lineage)` | 345-396 | Structured findings (transforms, null-fills, alternatives) |
| `_build_risks(column_lineage)` | 399-415 | Risk statements for transformed/null-filled columns |
| `_build_suggested_actions(column_lineage, keys, upstream)` | 418-460 | Actionable anchor() commands to run next |
| `render_explain_row_report(ctx)` | 468-509 | Markdown table rendering of lineage |
| `explain_row_context(...)` | 517-674 | Main entry point — orchestrates all steps |

---

## Output Contract

```python
{
    "kind": "explain_row",
    "subject": "project_id='PROJ-123'",
    "summary": "Row traced through 2 upstream source(s). 5 columns from orders...",
    "metrics": {
        "output_columns": int,
        "columns_traced": int,
        "columns_untraced": int,
        "upstream_sources_matched": int,
        "exact_matches": int,
        "transformed_values": int,
        "coerced_values": int,
        "null_filled_values": int,
    },
    "column_lineage": [
        {
            "column": str,
            "output_value": Any,
            "origin": str | None,       # upstream name
            "upstream_value": Any,
            "match_type": str,          # exact|transformed|coerced|null_filled|not_found
            "join_key": bool,
            "all_sources": [...]        # all upstreams that have this column
        },
        ...
    ],
    "join_paths": [
        {"upstream": str, "join_keys": [str], "match_count": int},
        ...
    ],
    "findings": [str, ...],
    "risks": [str, ...],
    "samples": {
        "output_row": {col: value, ...},
        "upstream_rows": {"upstream_name": {col: value, ...}, ...}
    },
    "suggested_next_actions": [str, ...],
}
```

---

## Engine Strategy

Uses shared `engine_utils.detect_engine` (not inlined like diagnose_empty):

- **Pandas**: Boolean mask filtering with `df[mask].iloc[0]`
- **Spark**: Chained `.filter(F.col(key) == value).limit(1).collect()`
- **Mixed upstreams**: Each upstream is detected independently; engine parameter applies to `output_df`