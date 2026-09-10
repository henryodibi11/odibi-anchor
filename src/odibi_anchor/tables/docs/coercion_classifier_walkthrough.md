# coercion_check_context — Code Walkthrough

**Module:** `coercion_classifier.py` (431 lines)  
**Purpose:** Classify WHY string values differ — whitespace, case, unicode, numeric, date_format, or genuine.

---

## How It Works

`coercion_check_context` takes two DataFrames (joined on business keys) and classifies value mismatches into categories that explain the root cause. Instead of just reporting "these values differ", it tells you *why* they differ, enabling targeted fixes.

Categories:
- **whitespace** — values match after trimming
- **case** — values match after case-folding
- **unicode** — values match after Unicode normalization (NFC)
- **numeric** — values represent the same number in different formats
- **date_format** — values represent the same date in different formats
- **genuine** — values are truly different (no coercion resolves the mismatch)

---

## Worked Example 1: Whitespace + Case Classification

### Input

```
old_df: id=1, name="  Alice  " | id=2, name="Bob"
new_df: id=1, name="ALICE"     | id=2, name="Bob"

keys=["id"], columns=["name"]
```

### Step-by-Step Execution

**Step 1: Inner join on keys**

Join old_df and new_df on `id`, producing paired values with `__old` / `__new` suffixes:

| id | name__old | name__new |
|---|---|---|
| 1 | "  Alice  " | "ALICE" |
| 2 | "Bob" | "Bob" |

**Step 2: Filter to mismatches only**

Row id=2 is identical → excluded. Only id=1 proceeds to classification.

**Step 3: Classify the pair (id=1)**

`_classify_pair("  Alice  ", "ALICE")` runs through the hierarchy:

1. **Whitespace check**: Strip both → `"Alice"` vs `"ALICE"` → still differ → not whitespace-only
2. **Case check**: Case-fold stripped values → `"alice"` vs `"alice"` → **match!**
3. **Result**: Category = `"case"` (after stripping, only case differs)

Note: The classification hierarchy is ordered. If stripping alone resolves it, category is `"whitespace"`. If case-folding after stripping resolves it, category is `"case"`.

**Step 4: Map to suggested fix**

```python
_SUGGESTED_FIXES["case"] = "df[col].str.upper()"  # or .str.lower()
```

**Step 5: Tally results per column**

Using `Counter`:
```python
column_results["name"] = Counter({"case": 1})
```

### Output

```python
{
    "kind": "coercion_check",
    "subject": "name_comparison",
    "metrics": {
        "total_mismatches": 1,
        "categories": {"case": 1},
        "columns_checked": ["name"]
    },
    "findings": [
        "Column 'name': 1 mismatch classified as 'case' (fix: df[col].str.upper())"
    ],
    "risks": [],
    "samples": {
        "case": [{"id": 1, "old": "  Alice  ", "new": "ALICE", "category": "case"}]
    }
}
```

---

## Worked Example 2: Date Format Classification

### Input

```
old_df: id=1, invoice_date="2024-01-15" | id=2, invoice_date="2024-03-20"
new_df: id=1, invoice_date="01/15/2024" | id=2, invoice_date="03/20/2024"

keys=["id"], columns=["invoice_date"]
```

### Step-by-Step Execution

**Step 1: Inner join and filter mismatches**

Both rows differ (different string representations), so both proceed to classification.

**Step 2: Classify pair (id=1)**

`_classify_pair("2024-01-15", "01/15/2024")` runs through:

1. **Whitespace check**: No leading/trailing whitespace → not whitespace
2. **Case check**: No alphabetic chars differ → not case
3. **Unicode check**: `unicodedata.normalize("NFC")` → no change → not unicode
4. **Numeric check**: Not parseable as numbers → not numeric
5. **Date format check**: `_same_date_different_format`:
   - Parse `"2024-01-15"` with `_DATE_FORMATS` → `date(2024, 1, 15)`
   - Parse `"01/15/2024"` with `_DATE_FORMATS` → `date(2024, 1, 15)`
   - Same date! → Category = `"date_format"`

**Step 3: Classify pair (id=2)**

Same logic: `"2024-03-20"` and `"03/20/2024"` both parse to `date(2024, 3, 20)` → `"date_format"`

**Step 4: Tally and suggest fix**

```python
column_results["invoice_date"] = Counter({"date_format": 2})
_SUGGESTED_FIXES["date_format"] = "Standardize to ISO format: pd.to_datetime(col).dt.strftime('%Y-%m-%d')"
```

### Output

```python
{
    "kind": "coercion_check",
    "metrics": {
        "total_mismatches": 2,
        "categories": {"date_format": 2},
        "columns_checked": ["invoice_date"]
    },
    "findings": [
        "Column 'invoice_date': 2 mismatches classified as 'date_format'",
        "Suggested fix: Standardize to ISO format: pd.to_datetime(col).dt.strftime('%Y-%m-%d')"
    ],
    "risks": [],
    "samples": {
        "date_format": [
            {"id": 1, "old": "2024-01-15", "new": "01/15/2024", "category": "date_format"},
            {"id": 2, "old": "2024-03-20", "new": "03/20/2024", "category": "date_format"}
        ]
    }
}
```

---

## Python Patterns

- **`Counter` for category tallying** — each column gets a `Counter` that accumulates category hits per pair, enabling concise reporting
- **`_SUGGESTED_FIXES` dict** — maps each category to a human-readable fix string, making the output actionable without consulting docs
- **`_INVISIBLE_CHARS` frozenset** — contains zero-width Unicode characters (ZWJ, ZWNJ, BOM, etc.) for unicode category detection
- **`unicodedata.normalize("NFC")` for unicode comparison** — canonical decomposition + composition normalizes equivalent Unicode representations
- **Inner join with `__old`/`__new` suffixes** — pairs values for row-by-row comparison without polluting original column names
- **Ordered classification hierarchy** — cheapest checks first (whitespace → case → unicode → numeric → date_format → genuine), short-circuiting on first match
- **`_DATE_FORMATS` list** — ordered collection of `strptime` format strings covering common date representations
