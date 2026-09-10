# validation_summary_context — Code Walkthrough

## Module Overview

`validation_summary_context` evaluates business validation rules against a DataFrame and produces a promotion safety assessment. It supports **6 rule types**, runs on a **dual engine** (pandas auto-selected for DataFrames under 500K rows, Spark for larger), applies **severity escalation** to determine `is_promotion_safe`, and generates **paste-ready fix expressions** for every failing rule.

**Rule types:** `not_null`, `accepted_values`, `range`, `unique`, `regex`, `custom`

**Severity levels:** `blocker` (prevents promotion) → `warning` (flags but allows promotion) → `info` (informational only)

**Source:** `src/odibi_anchor/validation/validation_summary_context.py` (932 lines)

---

## Worked Example 1: not_null Rule Evaluation

**Input:** DataFrame with 1000 rows; column `email` has 30 null values.

**Rule:** `{"type": "not_null", "column": "email"}`

```python
# Step 1: Resolve severity
_resolve_severity("not_null", "email", severity_map=None)
# → _DEFAULT_SEVERITIES["not_null"] = "blocker"

# Step 2: Build failure mask
mask = df["email"].isna()          # 30 True values

# Step 3: Compute metrics
failed_count = 30
failed_rate  = 30 / 1000  # 0.03

# Step 4: Determine status
# severity="blocker", failed_count > 0 → status="FAIL"

# Step 5: Generate fix expression
fix_expr = 'df = df.dropna(subset=["email"])'

# Step 6: Sample 5 failing rows as evidence
samples = df[mask].head(5)
```

**Rule result:**
```python
{
    "rule_id":      "not_null__email",
    "status":       "FAIL",
    "severity":     "blocker",
    "failed_count": 30,
    "failed_rate":  0.03,
    "fix_expr":     'df = df.dropna(subset=["email"])'
}
```

`is_promotion_safe: False` — a blocker rule failed.

---

## Worked Example 2: accepted_values Rule

**Input:** DataFrame column `status` has values `["A", "B", "C", "X", "Y"]`.

**Rule:** `{"type": "accepted_values", "column": "status", "values": ["A", "B", "C"]}`

```python
# Step 1: Resolve severity
# _DEFAULT_SEVERITIES["accepted_values"] = "warning"

# Step 2: Build failure mask
mask = ~df["status"].isin(["A", "B", "C"])  # 50 True (X and Y rows)

# Step 3: Metrics
failed_count = 50
failed_rate  = 0.05

# Step 4: severity="warning" → status="WARN" (does not block promotion)
```

**Rule result:**
```python
{
    "rule_id":      "accepted_values__status",
    "status":       "WARN",
    "severity":     "warning",
    "failed_count": 50,
    "failed_rate":  0.05,
    "fix_expr":     'df = df[df["status"].isin(["A", "B", "C"])]'
}
```

`is_promotion_safe: True` — only `blocker` rules prevent promotion. Warnings are flagged but do not block.

---

## Worked Example 3: _build_quarantine_call

**Input:** 2 failed rules — `not_null` on `email`, `range` on `amount` (valid range: 0–10000).

```python
# Step 1: Build compound failure filter
compound = (
    df["email"].isna()
    | (df["amount"] < 0)
    | (df["amount"] > 10000)
)

# Step 2: Generate quarantine expressions
quarantine_call = (
    'quarantine_df = df[df["email"].isna() | '
    '((df["amount"] < 0) | (df["amount"] > 10000))]\n'
    'clean_df = df[~(df["email"].isna() | '
    '((df["amount"] < 0) | (df["amount"] > 10000)))]'
)
```

The `quarantine_call` string is paste-ready Python — copy it directly into a notebook cell to isolate failing rows without manual filter construction.

---

## Worked Example 4: from_manifest Auto-Rules

**Input:** `from_manifest=True`, `root="/project"` containing `.anchor_manifest.json`.

```python
# .anchor_manifest.json excerpt:
# {"columns": {"email": {"nullable": false}, "id": {"unique": true}}}

# Step 1: Load manifest
manifest = load_manifest(root="/project")

# Step 2: Generate rules from column metadata
generated_rules = [
    {"type": "not_null", "column": "email"},   # nullable=false
    {"type": "unique",   "column": "id"},       # unique=true
]

# Step 3: Evaluate generated rules against df (same pipeline as manual rules)
```

Using `from_manifest=True` ensures validation rules stay in sync with the project manifest — no manual rule maintenance required.

---

## Python Patterns

| Pattern | Location | Purpose |
|---|---|---|
| `Literal` type alias | Module header | `Engine = Literal["auto", "pandas", "spark"]` — constrains engine param |
| `_DEFAULT_SEVERITIES` dict | Module-level constant | Maps `rule_type → default severity`; overridable via `severity_map=` |
| `_rule_id()` | Private helper | Generates stable IDs: `"not_null__email"`, `"range__amount"` |
| `_build_fix_expr()` | Private helper | Returns paste-ready Python per rule type (dropna, isin filter, clip, etc.) |
| `severity_map` param | Public API | User override: `{"not_null": "warning"}` downgrades blocker to warning |
| `_resolve_severity()` | Private helper | Merges `_DEFAULT_SEVERITIES` with caller's `severity_map`; caller always wins |
