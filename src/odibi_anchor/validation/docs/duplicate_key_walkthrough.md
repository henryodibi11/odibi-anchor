# duplicate_key_context — Code Walkthrough

## Module Overview

`duplicate_key_context` summarizes **duplicate-key risk** — “does this dataset violate its expected grain?” It surfaces whether the DataFrame is unique on a given key set, counts excess rows, and identifies the worst offenders. Supports both pandas and Spark via a **dual-engine** architecture with flexible key input (`str` or `Sequence[str]`).

**Source:** `src/odibi_anchor/validation/duplicate_key_context.py` (867 lines)

---

## Worked Example 1: Basic Duplicate Detection

**Input:** DataFrame with 1000 rows, `keys=["customer_id"]`.

```python
# Step 1: Normalize keys
keys = _normalize_keys("customer_id")        # → ["customer_id"]

# Step 2: Validate columns exist
_validate_columns_exist(df, ["customer_id"]) # → OK

# Step 3: Build group counts
groups = _build_group_counts(
    df, ["customer_id"], treat_nulls_as_duplicates=True
)
# df.groupby(["customer_id"]).size() → 950 unique groups

# Step 4: Filter for duplicates (size > 1)
duplicate_groups = groups[groups > 1]   # 30 groups

# Step 5: Compute metrics
unique_key_count           = 950
duplicate_key_count        = 30   # groups with > 1 row
duplicate_row_count        = 80   # total rows in duplicate groups
excess_duplicate_row_count = 50   # 80 - 30 = extra rows beyond one-per-key
max_rows_per_key           = 5

# Step 6: Build top-5 offender samples
samples = _build_duplicate_key_samples(groups, df, ["customer_id"], top_n=5)
```

**Output (key fields):**
```python
{
    "is_grain_violated":         True,
    "unique_key_count":          950,
    "duplicate_key_count":       30,
    "duplicate_row_count":       80,
    "excess_duplicate_row_count": 50,
    "max_rows_per_key":          5,
    "top_duplicate_keys":        [...]   # top 5 worst offenders
}
```

---

## Worked Example 2: _normalize_keys Flexibility

`_normalize_keys` accepts both string and sequence inputs and validates them upfront. All validation happens before any DataFrame operations.

```python
_normalize_keys("customer_id")              # → ["customer_id"]
_normalize_keys(["order_id", "line_item"])  # → ["order_id", "line_item"]

_normalize_keys("")                         # → ValueError("Keys must be non-empty")
_normalize_keys([])                         # → ValueError("Keys must be non-empty")
_normalize_keys(["id", "id"])              # → ValueError("Duplicate key columns: ['id']")
```

Using a union type (`str | Sequence[str]`) keeps the public API flexible while centralizing validation in a single private helper. Callers never need to wrap a string in a list.

---

## Worked Example 3: treat_nulls_as_duplicates

**Input:** DataFrame with 5 rows where `customer_id` is `None`.

```python
# treat_nulls_as_duplicates=True (default):
# → 5 null rows grouped as one key in _build_group_counts()
# → counted as 1 duplicate group with 5 rows
# → null_key_row_count = 5 (also reported separately)

# treat_nulls_as_duplicates=False:
# → null rows excluded from groupby entirely
# → NOT counted in duplicate_key_count
# → null_key_row_count = 5 (still reported in output)
```

The default (`True`) is conservative — null keys are ambiguous grain violations and flagged by default. Callers who know nulls are expected can opt out with `treat_nulls_as_duplicates=False`.

---

## Worked Example 4: _json_safe_value Serialization

`_json_safe_value` converts non-JSON-serializable types recursively so output dicts can be safely serialized or stored in context objects.

```python
_json_safe_value(numpy.int64(42))            # → int(42)
_json_safe_value(Decimal("99.99"))           # → float(99.99)
_json_safe_value(pd.Timestamp("2024-01-15")) # → "2024-01-15T00:00:00"
_json_safe_value(pd.NaT)                     # → None
_json_safe_value(float("nan"))               # → None
_json_safe_value({"nested": numpy.int64(1)}) # → {"nested": 1}  (recursive)
```

Handling is recursive: dicts and lists are traversed depth-first. This ensures nested sample structures (e.g., `top_duplicate_keys`) are always JSON-safe.

---

## Python Patterns

| Pattern | Location | Purpose |
|---|---|---|
| `str \| Sequence[str]` union | `keys` param type | Accepts both `"id"` and `["id", "date"]` — no caller wrapping required |
| `_is_missing()` | Private helper | Checks None, NaN, NaT, empty string in one function — avoids scattered `.isna()` calls |
| `_json_safe_value()` (recursive) | Private helper | Handles nested dicts/lists, numpy types, pandas types — safe for JSON serialization |
| `_rate()` | Private helper | Safe division: `0` denominator → `0.0` (no `try/except`, no `ZeroDivisionError`) |
| `render_duplicate_key_result()` | Public helper | Extracts samples from nested dict structure for display and reporting |
| Dual-engine dispatch | Orchestrator | Pandas for small DataFrames, Spark for large — uniform output shape regardless of engine |
