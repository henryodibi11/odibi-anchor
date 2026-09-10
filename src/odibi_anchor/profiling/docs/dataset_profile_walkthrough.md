# dataset_profile_context — Code Walkthrough

This module turns a Pandas or Spark DataFrame into a compact structured profile:
nulls, types, stats, top values, risks, and suggested next actions. It is the
foundation of `anchor("profile_table")`. (Note: `anchor("profile")` has been removed; use `anchor("profile_table")` for all profiling.)

**Source:** `src/odibi_anchor/profiling/dataset_profile_context.py` (1,218 lines)

---

## Module Overview

### Dual-Engine Architecture

The module supports two execution paths:

- **Pandas engine** (`_dataset_profile_context_pandas`) — production-ready, full feature set
- **Spark engine** (`_dataset_profile_context_spark`) — uses native Spark aggregations
  (`approx_count_distinct`, single-pass null counts, capped `collect`)

Engine resolution is handled by `_resolve_engine(df, engine)` which checks:
1. If `engine="pandas"` → validates `isinstance(df, pd.DataFrame)` → returns `"pandas"`
2. If `engine="spark"` → validates via `_is_spark_dataframe(df)` → returns `"spark"`
3. If `engine="auto"` → checks `isinstance` for pandas first, then spark (pandas preferred)

### Anchor Contract Output

Every context builder returns the same top-level shape:

```python
{
    "kind": "dataset_profile_context",
    "subject": str,          # human-readable dataset name
    "summary": str,          # one-line natural language summary
    "metrics": dict,         # row_count, column_count, missing_cell_pct, ...
    "columns": list[str],    # profiled column names in order
    "column_profiles": dict, # per-column profile dicts keyed by column name
    "findings": list[dict],  # typed observations (shape, highest_null, potential_keys, ...)
    "risks": list[dict],     # severity-coded quality risks
    "samples": dict,         # {"data_preview": [...]} or empty dict
    "suggested_next_actions": list[str],  # actionable recommendations
    "parameters": dict,      # echo of resolved parameters
}
```

### Frame-Aware Augmentation

When a `frame` kwarg is passed (a `ContextFrame` instance), the module appends
memory-based gotchas to findings:

```python
if frame is not None:
    if hasattr(frame, "memory_context") and frame.memory_context.has_gotcha_for(subject):
        gotchas = frame.memory_context.get_gotchas_for(subject)
        for g in gotchas[:3]:
            findings.append(f"MEMORY GOTCHA: {g}")
```

This is advisory-only (wrapped in bare `except: pass`) and limited to 3 gotchas.

---

## Worked Example 1: `_infer_column_type` with Ambiguous Data

### Function Signature

```python
def _infer_column_type(series: pd.Series) -> str:
```

### Scenario

A pandas Series with dtype `object` containing mixed values: mostly integers
but one text value that prevents automatic numeric detection.

### Step-by-Step Trace

```
Input: pd.Series(["123", "456", None, "789", "abc"], dtype=object)

Step 1: Check native dtype shortcuts
  - is_bool_dtype? No
  - is_integer_dtype? No (dtype=object)
  - is_float_dtype? No
  - is_numeric_dtype? No
  - is_datetime64_any_dtype? No
  - CategoricalDtype? No
  → Fall through to string-based inference

Step 2: Prepare non-null values
  non_null = series.dropna()  → ["123", "456", "789", "abc"] (4 values)
  as_text = non_null.astype(str).str.strip()  → ["123", "456", "789", "abc"]
  normalized = as_text.str.lower()  → ["123", "456", "789", "abc"]

Step 3: Boolean check
  normalized_non_empty = normalized[normalized != ""]  → 4 values
  normalized_non_empty.isin(BOOLEAN_LIKE_VALUES).all()? No ("123" not in set)
  → Not boolean_like

Step 4: Numeric parse attempt
  numeric_parsed = pd.to_numeric(as_text, errors="coerce")
    → [123.0, 456.0, 789.0, NaN]  ("abc" → NaN)
  parse_ratio = _safe_ratio(3, 4) = 0.75
  0.75 >= NUMERIC_PARSE_THRESHOLD (0.9)? No
  → Not numeric_integer_like or numeric_float_like

Step 5: Datetime parse attempt
  datetime_parsed = pd.to_datetime(as_text, errors="coerce")
    → [NaT, NaT, NaT, NaT]  (none parse as dates)
  parse_ratio = _safe_ratio(0, 4) = 0.0
  0.0 >= DATETIME_PARSE_THRESHOLD (0.8)? No
  → Not datetime_like

Step 6: Cardinality check
  distinct_count = non_null.nunique() = 4
  is_low_card = (4 <= LOW_CARDINALITY_MAX_DISTINCT (50))
                AND (_safe_ratio(4, 4) = 1.0 <= _LOW_CARDINALITY_RATIO (0.5))? No (1.0 > 0.5)
  → Not categorical

Step 7: Default fallback
  → Return "text"

Output: "text"
```

### Alternate Path: When Numeric Dominates

If the input were `["123", "456", "789", "012"]` (all numeric strings):
- Step 4 would yield `parse_ratio = 4/4 = 1.0 >= 0.9` → True
- Then check `(parsed_non_null % 1 == 0).all()` → True (all integers)
- Output: `"numeric_integer_like"`

---

## Worked Example 2: `_profile_column` with Real Data

### Function Signature

```python
def _profile_column(
    series: pd.Series,
    *,
    total_rows: int,
    top_values_limit: int,
    high_null_threshold: float,
    high_cardinality_threshold: float,
) -> dict[str, Any]:
```

### Scenario

A 100-row column of customer names (dtype=object) with some nulls.

### Step-by-Step Trace

```
Input: series with 100 rows, dtype=object
  Values: ["Alice", "Bob", "Alice", None, "Charlie", None, "Bob", ...]
  Parameters: total_rows=100, top_values_limit=5,
              high_null_threshold=0.5, high_cardinality_threshold=0.9

Step 1: Null statistics
  null_count = int(series.isna().sum()) = 5
  non_null_count = 100 - 5 = 95
  null_pct = _safe_ratio(5, 100) = 0.05

Step 2: Distinct count
  distinct_count = int(series.nunique(dropna=True)) = 45
  distinct_pct = _safe_ratio(45, 95) = 0.4737...

Step 3: Type inference via _infer_column_type
  - dtype is object → falls through native checks
  - non_null: 95 values, all strings
  - Boolean check: fails (names aren't boolean-like)
  - Numeric parse: fails (names aren't numbers)
  - Datetime parse: fails (names aren't dates)
  - Cardinality check:
    distinct_count = 45 <= LOW_CARDINALITY_MAX_DISTINCT (50)? Yes
    _safe_ratio(45, 95) = 0.47 <= _LOW_CARDINALITY_RATIO (0.5)? Yes
  → Return "categorical"

Step 4: Null-like value detection
  _detect_null_like_values(series):
    - dtype is object → proceed
    - values = series.dropna().astype(str)
    - Check each value.strip().lower() against NULL_LIKE_VALUES frozenset
    - None of "Alice", "Bob", etc. match
  → Return []

Step 5: Top values
  _top_values(series, 5):
    non_null = series.dropna()  → 95 values
    counts = non_null.value_counts().head(5)
    → [{"value": "Alice", "count": 8, "pct": 0.084211},
       {"value": "Bob", "count": 6, "pct": 0.063158},
       {"value": "Charlie", "count": 5, "pct": 0.052632}, ...]

Step 6: Column stats (for categorical/text types)
  _column_stats(series, "categorical"):
    - Not numeric, not datetime, not boolean
    - dtype is object → compute text stats
    text = series.dropna().astype(str)  → 95 values
    lengths = text.str.len()  → [5, 3, 5, 7, 3, ...]
    → {"min_length": 3, "max_length": 9, "mean_length": 5.2}

Step 7: Quality flags
  _column_quality_flags(profile, high_null_threshold=0.5, high_cardinality_threshold=0.9):
    - total_count == 0? No
    - null_pct == 1.0? No
    - null_pct (0.05) >= high_null_threshold (0.5)? No
    - is_constant? No (45 distinct)
    - distinct_pct (0.47) >= high_cardinality_threshold (0.9)? No
    - null_like_values? Empty → No
    - inferred_type "categorical" in {"numeric_integer_like", ...}? No
    - is_unique? No (45 distinct != 100 rows, and has nulls)
  → Return []

Output:
  {"name": "customer_name", "dtype": "object", "inferred_type": "categorical",
   "total_count": 100, "non_null_count": 95, "null_count": 5,
   "null_pct": 0.05, "distinct_count": 45, "distinct_pct": 0.473684,
   "is_unique": False, "is_constant": False, "null_like_values": [],
   "top_values": [...], "stats": {"min_length": 3, ...}, "quality_flags": []}
```

---

## Worked Example 3: Full Pipeline End-to-End

### Scenario

A 1000-row DataFrame with 3 columns: `id` (int), `name` (str), `amount` (float).
Two rows have null `name` values.

### Step-by-Step Trace

```
Input: pd.DataFrame({
    "id": range(1, 1001),
    "name": ["Alice", "Bob", None, "Charlie", ...],  # 2 nulls, 30 distinct
    "amount": [150.5, 22.0, 300.75, ...]              # 0 nulls
})
Call: dataset_profile_context(df, subject="demo.orders")

═══ Phase 1: Validation & Engine Resolution ═══

Step 1: validate_output_format("dict") → OK

Step 2: _validate_parameters(engine="auto", sample_limit=5, ...)
  - All params in valid range → OK

Step 3: _resolve_engine(df, "auto")
  - isinstance(df, pd.DataFrame)? Yes
  → Return "pandas"

═══ Phase 2: Pandas Implementation ═══

Step 4: _validate_dataframe_columns(df)
  - Check for duplicated column names → none found → OK

Step 5: _resolve_columns(df, None)
  - columns=None → use all: ["id", "name", "amount"]

Step 6: _profile_column for each column:

  "id":
    null_count=0, non_null_count=1000, distinct_count=1000
    is_unique = (1000 > 0 and 0 == 0 and 1000 == 1000) → True
    inferred_type: is_integer_dtype(int64) → "numeric_integer"
    stats: {min: 1, max: 1000, mean: 500.5, median: 500.5, std: 288.8, p25: 250.75, p75: 750.25}
    quality_flags: ["potential_key"]

  "name":
    null_count=2, non_null_count=998, distinct_count=30
    inferred_type: 30 <= 50 and 30/998=0.03 <= 0.5 → "categorical"
    stats: {min_length: 3, max_length: 9, mean_length: 5.4}
    quality_flags: []

  "amount":
    null_count=0, non_null_count=1000
    inferred_type: is_float_dtype(float64) → check (values % 1 == 0).all()
      → False (150.5, 300.75 have fractional parts)
      → "numeric_float"
    stats: {min: 5.0, max: 999.99, mean: 150.5, std: 45.2, ...}
    quality_flags: []

═══ Phase 3: Aggregate Metrics ═══

Step 7: _build_metrics(profile_df, column_profiles)
  row_count = 1000
  column_count = 3
  total_cells = 3000
  missing_cells = 0 + 2 + 0 = 2
  missing_cell_pct = _round_pct(2/3000) = 0.000667
  duplicate_row_count = profile_df.duplicated().sum() = 0
  type_counts = {"numeric_integer": 1, "categorical": 1, "numeric_float": 1}
  potential_key_columns = ["id"]
  all_null_columns = []
  constant_columns = []
  high_null_columns = []
  high_cardinality_columns = []

═══ Phase 4: Findings, Risks, Actions ═══

Step 8: _build_findings(column_profiles, metrics)
  findings = [
    {"type": "shape", "message": "Profiled 1,000 rows across 3 columns."},
    {"type": "highest_null_columns", "message": "Highest-null columns: name (0.2%)",
     "columns": ["name"]},
    {"type": "potential_key_columns", "message": "Potential single-column keys: id",
     "columns": ["id"]},
  ]

Step 9: _build_risks(column_profiles, metrics, "demo.orders")
  - row_count > 0 → no empty_dataset risk
  - column_count > 0 → no no_columns risk
  - all_null_columns empty → skip
  - high_null_columns empty → skip
  - duplicate_row_count == 0 → skip
  risks = []  (clean dataset)

Step 10: _build_suggested_next_actions(column_profiles, metrics, risks)
  - potential_key_columns not empty → "Validate the potential key column(s)..."
  - no high-null or all-null risk codes → skip those
  - Always append: "Use this profile to draft validation tests..."
  - Always append: "MUST: Run anchor('transform', profile_ctx)..."
  actions = [
    "Validate the potential key column(s) against expected business grain.",
    "Use this profile to draft validation tests for nullability, uniqueness, ...",
    "MUST: Run anchor('transform', profile_ctx) to auto-generate a cleanup plan...",
    "SKILL: Load skills/data-onboarding/SKILL.md ...",
  ]

═══ Phase 5: Samples & Summary ═══

Step 11: _build_samples(profile_df, 5)
  → First 5 rows as list of dicts, all values passed through _json_safe()

Step 12: _build_summary("demo.orders", metrics, risks)
  → "demo.orders has 1,000 rows and 3 profiled columns; 0.1% cells are null
     and no major risks detected."

═══ Final Output ═══

{
    "kind": "dataset_profile_context",
    "subject": "demo.orders",
    "summary": "demo.orders has 1,000 rows and 3 profiled columns; 0.1% cells are null and no major risks detected.",
    "metrics": {"row_count": 1000, "column_count": 3, ...},
    "columns": ["id", "name", "amount"],
    "column_profiles": {"id": {...}, "name": {...}, "amount": {...}},
    "findings": [...],
    "risks": [],
    "samples": {"data_preview": [{"id": 1, "name": "Alice", "amount": 150.5}, ...]},
    "suggested_next_actions": [...],
    "parameters": {"engine": "pandas", "sample_limit": 5, ...},
}
```

---

## Python Patterns

### 1. `frozenset` for Constant Lookup Sets

```python
NULL_LIKE_VALUES: frozenset[str] = frozenset({
    "", " ", "-", "--", ".", "..", "#n/a", "#na", "#null", "#value!",
    "missing", "n/a", "na", "nan", "nat", "nil", "none",
    "not available", "null", "tbd", "unknown",
})
```

**Why:** `frozenset` provides O(1) membership testing (`value in NULL_LIKE_VALUES`),
is immutable (prevents accidental mutation), and is hashable (can be used as a dict
key or set element). The module uses four such sets: `NULL_LIKE_VALUES`,
`BOOLEAN_LIKE_VALUES`, `TRUE_LIKE_VALUES`, `FALSE_LIKE_VALUES`.

### 2. `pd.api.types` for Robust Dtype Checking

```python
if pd.api.types.is_object_dtype(series):
    ...
if pd.api.types.is_integer_dtype(series):
    ...
```

**Why:** `pd.api.types.is_object_dtype` handles both classic `object` dtype and
the newer `pd.StringDtype()` (nullable string). Using `series.dtype == "object"`
would miss `StringDtype` columns. Similarly, `is_integer_dtype` handles both
`int64` and nullable `Int64` (capital-I).

### 3. `_json_safe` — Recursive Type Converter

```python
def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        numeric = float(value)
        return None if np.isnan(numeric) else numeric
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
```

**Why:** Profile output must be JSON-serializable (for LLM context windows,
file persistence, and API responses). NumPy types (`np.int64`, `np.float64`),
`Decimal`, `pd.Timestamp`, and `pd.NaT` are all non-serializable by default.
This recursive converter handles nested structures (dicts, lists) and converts
every leaf to a JSON-safe primitive. The `pd.isna()` check is wrapped in
`try/except` because it raises `TypeError` for non-scalar values like dicts.

### 4. `_safe_ratio` — Division Without Try/Except

```python
def _safe_ratio(numerator: int | float, denominator: int | float) -> float:
    if denominator == 0:
        return 0.0
    return float(numerator) / float(denominator)
```

**Why:** The module computes ratios everywhere (null_pct, distinct_pct, top_value pct).
Using `try/except ZeroDivisionError` is slower and less explicit. This guard-clause
pattern is clear, fast, and used 15+ times across the module. Returning 0.0 (not NaN
or None) means downstream code never needs null checks on percentage fields.

---

## Key Design Decisions

| Decision | Rationale |
| --- | --- |
| Pandas-first, Spark-second | Pandas is faster for small-medium data; Spark path avoids `.toPandas()` for large data |
| `_LOW_CARDINALITY_RATIO = 0.5` | Balances false positives (calling numbers "categorical") with detection sensitivity |
| `NUMERIC_PARSE_THRESHOLD = 0.9` | Allows up to 10% unparseable values before giving up on numeric classification |
| `_round_pct` rounds to 6 decimal places | Avoids floating-point noise while preserving precision for small percentages |
| Quality flags as strings, not enums | Keeps output JSON-serializable and extensible without import dependencies |
| `high_null_threshold` as parameter | Different domains have different null tolerance (financial data vs logs) |
| Frame augmentation is silent-fail | Advisory feature — profiler must work standalone without context frames |
