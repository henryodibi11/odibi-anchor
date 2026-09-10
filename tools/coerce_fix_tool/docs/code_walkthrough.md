# Coerce Fix Tool — Code Walkthrough

## Worked Example 1: Case Mismatch Fix

Scenario: `coerce_check` classified column `"name"` as `"case"` mismatch.
The old DataFrame has uppercase names; the new DataFrame has mixed case.

### Input

```python
import pandas as pd

new_df = pd.DataFrame({
    "id": [1, 2, 3, 4, 5],
    "name": ["alice", "Bob", "CHARLIE", "diana Prince", "EVE"],
})

# coerce_check output (abbreviated):
coerce_ctx = {
    "kind": "coerce_check",
    "column_results": {
        "name": {
            "dominant_category": "case",
            "total_mismatches": 3,
            "categories": {"case": 3},
        }
    }
}
```

### Execution Trace

**1. Validation**
```
df is not None ✔
coerce_ctx is dict with "column_results" ✔
case_target = "upper" (default) ✔
detect_engine(df) = "pandas" ✔
```

**2. Determine columns to fix**
```
column_results keys: ["name"]
target_cols = ["name"]  (no columns= filter provided)
```

**3. Build fix plan**
```
column "name":
  dominant_category = "case"
  "case" ∈ _FIXABLE_CATEGORIES ✔
  operation = _OPERATION_LABELS["case_upper"] = "UPPER"

fixes_planned = [{"column": "name", "category": "case", "operation": "UPPER"}]
skipped = []
```

**4. Apply fix** (dry_run=False)
```python
fixed_df = df.copy()  # Never mutate original

before = fixed_df["name"].copy()
# ["alice", "Bob", "CHARLIE", "diana Prince", "EVE"]

_apply_fix_pandas(series, category="case", case_target="upper")
→ _fix_case(series, target="upper")
→ series.astype(str).where(series.notna(), other=pd.NA).str.upper()

after = ["ALICE", "BOB", "CHARLIE", "DIANA PRINCE", "EVE"]
```

**5. Count affected rows**
```
before_str: ["alice", "Bob", "CHARLIE", "diana Prince", "EVE"]
after_str:  ["ALICE", "BOB", "CHARLIE", "DIANA PRINCE", "EVE"]

Rows changed: indices 0,1,3 ("alice"→"ALICE", "Bob"→"BOB", "diana Prince"→"DIANA PRINCE")
rows_affected = 3
```

Note: "CHARLIE" and "EVE" are already uppercase, so they don't count as affected.

**6. Capture samples** (`_capture_samples(before, after, n=5)`)
```
before: ["alice", "Bob", "diana Prince"]
after:  ["ALICE", "BOB", "DIANA PRINCE"]
```

**7. Assemble output**
```python
{
    "kind": "coerce_fix",
    "subject": "coerce_fix",
    "summary": "Fixed 1 column(s): name (case→UPPER). 3 values corrected.",
    "metrics": {
        "columns_fixed": 1,
        "columns_skipped_genuine": 0,
        "total_values_corrected": 3,
        "by_category": {"case": 3},
    },
    "findings": ["name: UPPER (3 rows)"],
    "risks": [],
    "samples": {
        "name": {
            "category": "case",
            "before": ["alice", "Bob", "diana Prince"],
            "after": ["ALICE", "BOB", "DIANA PRINCE"],
        }
    },
    "fixes_applied": [{"column": "name", "category": "case", "operation": "UPPER", "rows_affected": 3}],
    "skipped": [],
    "df": <corrected DataFrame>,
    "suggested_next_actions": [
        "Verify fixes: anchor('diff', old_df, fixed_df, keys=[...])",
        "Re-run coerce_check: anchor('coerce_check', old_df, fixed_df, keys=[...], columns=[...])",
    ],
}
```

---

## Worked Example 2: Date Format Normalization

Scenario: `coerce_check` classified column `"date"` as `"date_format"` mismatch.
Source has dates in multiple formats; target needs ISO `%Y-%m-%d`.

### Input

```python
new_df = pd.DataFrame({
    "id": [1, 2, 3, 4],
    "date": ["01/15/2024", "2024-02-20", "March 3, 2024", "04-10-24"],
})

coerce_ctx = {
    "kind": "coerce_check",
    "column_results": {
        "date": {
            "dominant_category": "date_format",
            "total_mismatches": 3,
            "categories": {"date_format": 3},
        }
    }
}
```

### Execution Trace

**1. Category dispatch**
```
dominant_category = "date_format"
"date_format" ∈ _FIXABLE_CATEGORIES ✔
operation = "parse to ISO date"
```

**2. Apply fix** (`_fix_date(series, target_fmt="%Y-%m-%d")`)

For each value, calls `pd.to_datetime(str(val)).strftime("%Y-%m-%d")`:

```
"01/15/2024"     → pd.to_datetime("01/15/2024")    → 2024-01-15 → "2024-01-15"
"2024-02-20"     → pd.to_datetime("2024-02-20")    → 2024-02-20 → "2024-02-20"
"March 3, 2024"  → pd.to_datetime("March 3, 2024") → 2024-03-03 → "2024-03-03"
"04-10-24"       → pd.to_datetime("04-10-24")      → 2024-04-10 → "2024-04-10"
```

**3. Count affected**
```
before: ["01/15/2024", "2024-02-20", "March 3, 2024", "04-10-24"]
after:  ["2024-01-15", "2024-02-20", "2024-03-03",    "2024-04-10"]

Changed: indices 0, 2, 3 ("2024-02-20" was already in target format)
rows_affected = 3
```

**4. Edge case: unparseable value**

If a value like `"not a date"` is encountered:
```python
try:
    return pd.to_datetime(str(val)).strftime(target_fmt)
except (ValueError, TypeError):
    return val  # Leave unchanged
```

The original value is preserved — no data loss on parse failure.

---

## Worked Example 3: Wrong-Side Detection

Scenario: User accidentally passes the clean (old) DataFrame instead of the
dirty (new) one. All fix operations produce 0 changes.

### Input

```python
old_df = pd.DataFrame({
    "id": [1, 2],
    "name": ["ALICE", "BOB"],  # Already clean
})

coerce_ctx = {
    "kind": "coerce_check",
    "column_results": {
        "name": {
            "dominant_category": "case",
            "total_mismatches": 2,
            "categories": {"case": 2},
        }
    }
}

# User calls coerce_fix with the WRONG DataFrame:
result = anchor("coerce_fix", old_df, coerce_ctx)  # Should be new_df!
```

### Execution Trace

```
_fix_case(["ALICE", "BOB"], target="upper")
→ ["ALICE", "BOB"]  (no change — already uppercase)

rows_affected = 0
total_values_corrected = 0
```

**Wrong-side detection triggers:**
```python
zero_effect_cols = ["name"]  # All planned fixes = 0 effect
total_values_corrected == 0  # Global zero

risks.append(
    "⚠ All fixes produced 0 changes. You may be fixing the wrong "
    "DataFrame. coerce_check compares old↔new — try passing the OTHER "
    "side to coerce_fix (the one that has the formatting issues)."
)
```

The output `risks` array alerts the user to swap DataFrames.

---

## Key Python Patterns

### Strategy Pattern (Category → Handler Function)

```python
_FIXABLE_CATEGORIES = frozenset(
    {"whitespace", "case", "whitespace+case", "unicode",
     "numeric_representation", "date_format"}
)

def _apply_fix_pandas(series, category, case_target, date_target):
    if category == "whitespace":
        return _fix_whitespace(series)
    elif category == "case":
        return _fix_case(series, target=case_target)
    elif category == "whitespace+case":
        return _fix_case(_fix_whitespace(series), target=case_target)
    # ... etc.
```

Each category maps to exactly one handler. Compound categories (whitespace+case)
chain handlers in sequence.

### Unicode Cleanup with `re` and `unicodedata`

```python
import re
import unicodedata

_INVISIBLE_CHARS_RE = re.compile(r"[\u200b\u200c\u200d\ufeff\u00ad]")

def _clean(val):
    # Remove zero-width characters
    cleaned = _INVISIBLE_CHARS_RE.sub("", str(val))
    # Normalize to NFC (composed form)
    return unicodedata.normalize("NFC", cleaned)
```

Targets: zero-width space (\u200b), zero-width non-joiner (\u200c),
zero-width joiner (\u200d), BOM (\ufeff), soft hyphen (\u00ad).

### Null-Safe Operations

```python
def _fix_case(series, target="upper"):
    s = series.astype(str).where(series.notna(), other=pd.NA)
    if target == "upper":
        return s.str.upper()
    return s.str.lower()
```

All fix functions preserve NaN/NA values by:
1. Converting to string only where non-null (`where(notna())`)
2. Operating on the string accessor
3. Null positions remain as pd.NA throughout
