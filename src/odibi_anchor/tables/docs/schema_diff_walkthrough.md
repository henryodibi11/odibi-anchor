# schema_diff_context — Code Walkthrough

**Module:** `schema_diff_context.py` (1,392 lines)  
**Purpose:** Compare schemas — compatibility assessment, breaking changes, rename suggestions, write safety.

---

## How It Works

`schema_diff_context` compares the schemas of two DataFrames (or schema definitions) and produces a comprehensive compatibility assessment. It detects:

- **Added columns** — present in new but not old
- **Removed columns** — present in old but not new
- **Type changes** — same column name, different data type
- **Reordered columns** — same columns, different positions
- **Likely renames** — fuzzy-matched columns that look like renames

The output includes a `write_safety` assessment that tells callers whether they can safely write the new schema to a target table.

---

## Worked Example 1: Compatible Schema Change

### Input

```
old_df schema: id(int64), name(object), amount(float64)
new_df schema: id(int64), name(object), amount(float64), email(object)
```

### Step-by-Step Execution

**Step 1: Extract schemas**

`_extract_schema_pandas` converts each DataFrame's dtypes into a list of `_SchemaField` dataclass instances:

```python
# Each field is:
_SchemaField(name="id", dtype="int64", position=0, nullable=False)
_SchemaField(name="name", dtype="object", position=1, nullable=True)
_SchemaField(name="amount", dtype="float64", position=2, nullable=True)
# new_df also has:
_SchemaField(name="email", dtype="object", position=3, nullable=True)
```

**Step 2: Compare by column name**

For each column in old schema, check if it exists in new schema with same type:

| Column | Old Type | New Type | Status |
|---|---|---|---|
| id | int64 | int64 | unchanged |
| name | object | object | unchanged |
| amount | float64 | float64 | unchanged |
| email | (absent) | object | added |

**Step 3: Assess compatibility**

No removals, no type changes → `compatibility = "compatible"`

Compatibility levels:
- `"identical"` — schemas are exactly the same
- `"compatible"` — only additive changes (new columns)
- `"breaking"` — removals or type changes

**Step 4: Compute write_safety**

```python
write_safety = {
    "can_write": True,
    "blockers": [],
    "warnings": ["1 column added: ['email']"]
}
```

### Output

```python
{
    "kind": "schema_diff",
    "subject": "schema_comparison",
    "metrics": {
        "columns_added": 1,
        "columns_removed": 0,
        "type_changes": 0,
        "compatibility": "compatible"
    },
    "findings": ["1 column added: email (object)"],
    "risks": [],
    "samples": {
        "added": [{"name": "email", "dtype": "object", "position": 3}]
    },
    "write_safety": {"can_write": True, "blockers": [], "warnings": ["1 column added"]}
}
```

---

## Worked Example 2: Rename Detection

### Input

```
old_df schema: customer_name(object), id(int64)
new_df schema: cust_name(object), id(int64)
```

### Step-by-Step Execution

**Step 1: Initial comparison**

- `customer_name` is in old but not in new → potentially removed
- `cust_name` is in new but not in old → potentially added
- `id` exists in both with same type → unchanged

**Step 2: Normalize column names**

`_normalize_column_name` strips prefixes, suffixes, and normalizes separators:

```python
_normalize_column_name("customer_name") → "customer_name"
_normalize_column_name("cust_name") → "cust_name"
```

**Step 3: Fuzzy matching**

Compare normalized names using string similarity:
- `"customer_name"` vs `"cust_name"` → similarity = 0.75
- Below exact-match threshold but above ignore threshold

**Step 4: Substring/abbreviation check**

`"cust_name"` appears to be an abbreviation of `"customer_name"` ("cust" is a common abbreviation of "customer").

**Step 5: Generate rename suggestion**

```python
likely_renames = [
    {
        "old_name": "customer_name",
        "new_name": "cust_name",
        "confidence": 0.8,
        "reason": "Substring/abbreviation match"
    }
]
```

### Output

```python
{
    "kind": "schema_diff",
    "metrics": {
        "columns_added": 1,
        "columns_removed": 1,
        "likely_renames": 1,
        "compatibility": "breaking"  # removal = breaking
    },
    "findings": [
        "1 column removed: customer_name",
        "1 column added: cust_name",
        "Likely rename detected: customer_name → cust_name (confidence: 0.8)"
    ],
    "risks": ["Column removal is a breaking change for downstream consumers"],
    "likely_renames": [
        {"old_name": "customer_name", "new_name": "cust_name", "confidence": 0.8}
    ],
    "write_safety": {
        "can_write": False,
        "blockers": ["Column 'customer_name' was removed — downstream consumers may break"],
        "warnings": []
    }
}
```

---

## Python Patterns

- **`@dataclass(frozen=True)` for `_SchemaField`** — immutable and hashable, enabling set operations for fast comparison
- **`Literal` type aliases** — `Engine = Literal["pandas", "spark"]` for engine/output_format parameters
- **Position-based reorder detection** — compares `field.position` across old/new to detect column reordering without content changes
- **`write_safety` sub-dict** — separates blockers (must fix before write) from warnings (proceed with caution), enabling programmatic go/no-go decisions
- **Fuzzy matching with confidence scores** — uses string similarity plus abbreviation heuristics to suggest renames rather than requiring exact matches
- **Dual-engine support** — extracts schema from both Pandas dtypes and Spark StructType, normalizing to a common representation
