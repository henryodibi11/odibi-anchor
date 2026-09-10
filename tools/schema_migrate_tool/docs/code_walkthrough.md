# Schema Migrate Tool — Code Walkthrough

## Worked Example 1: Simple ADD COLUMN

Scenario: `schema_diff` shows a new column `"email"` (STRING) was added.

### Input

```python
schema_diff_ctx = {
    "kind": "schema_diff",
    "added_columns": [{"name": "email", "type": "string"}],
    "removed_columns": [],
    "type_changes": [],
    "likely_renames": [],
}

target_table = "catalog.schema.customers"
```

### Execution Trace

**1. Parse schema_diff**
```
added: ["email" (string)]
removed: []
type_changes: []
likely_renames: []
```

**2. No rename detection needed** (no removed columns to pair)

**3. Generate DDL for add_columns**
```python
ddl = "ALTER TABLE catalog.schema.customers ADD COLUMN `email` STRING;"
pyspark = "spark.sql('ALTER TABLE catalog.schema.customers ADD COLUMN `email` STRING')"
```

**4. Safety assessment**
```
ADD COLUMN is always safe → no risks
```

**5. Output**
```python
{
    "kind": "schema_migrate",
    "subject": "catalog.schema.customers",
    "summary": "Migration plan: 1 ADD COLUMN. Dry run — no changes applied.",
    "metrics": {
        "columns_added": 1,
        "columns_removed": 0,
        "type_casts": 0,
        "renames": 0,
        "drops": 0,
        "unsafe_casts": 0,
        "dry_run": True,
    },
    "migration_plan": {
        "add_columns": [{
            "column": "email",
            "type": "STRING",
            "default": "NULL",
            "ddl": "ALTER TABLE catalog.schema.customers ADD COLUMN `email` STRING;",
            "pyspark": "spark.sql('ALTER TABLE catalog.schema.customers ADD COLUMN `email` STRING')",
        }],
        "type_casts": [],
        "renames": [],
        "drops": [],
    },
    "findings": ["1 column to add: email (STRING)"],
    "risks": [],
    "suggested_next_actions": [
        "Review DDL above and run in a SQL cell",
        "After applying: anchor('delta_diff', 'catalog.schema.customers', keys=[...]) to verify",
    ],
}
```

---

## Worked Example 2: Rename Detection via SequenceMatcher

Scenario: `schema_diff` shows `"customer_name"` removed and `"cust_name"` added.
The tool should detect this as a rename.

### Input

```python
schema_diff_ctx = {
    "kind": "schema_diff",
    "added_columns": [{"name": "cust_name", "type": "string"}],
    "removed_columns": [{"name": "customer_name", "type": "string"}],
    "type_changes": [],
    "likely_renames": [],
}

target_table = "catalog.schema.orders"
rename_threshold = 0.8
```

### Execution Trace

**1. Rename detection**

For each (removed, added) pair where types are compatible:
```python
from difflib import SequenceMatcher

ratio = SequenceMatcher(None, "customer_name", "cust_name").ratio()
# "customer_name" (13 chars) vs "cust_name" (9 chars)
# Longest common subsequences: "cust", "_name"
# ratio = 2 * matching_chars / total_chars = 2 * 9 / 22 ≈ 0.818

0.818 > threshold 0.8 → classified as RENAME
```

**2. Remove from add/remove lists, add to renames**
```
renames: [{"old_name": "customer_name", "new_name": "cust_name", "similarity": 0.818}]
add_columns: []  (consumed by rename)
drops: []  (consumed by rename)
```

**3. Generate RENAME DDL**
```python
ddl = "ALTER TABLE catalog.schema.orders RENAME COLUMN `customer_name` TO `cust_name`;"
```

**4. Output**
```python
{
    "migration_plan": {
        "add_columns": [],
        "type_casts": [],
        "renames": [{
            "old_name": "customer_name",
            "new_name": "cust_name",
            "similarity": 0.818,
            "ddl": "ALTER TABLE catalog.schema.orders RENAME COLUMN `customer_name` TO `cust_name`;",
        }],
        "drops": [],
    },
    "findings": [
        "1 likely rename: customer_name → cust_name (similarity 0.82)",
    ],
    "risks": [
        "Verify rename: customer_name → cust_name (auto-detected at 82% similarity)",
    ],
}
```

**Edge case: similarity below threshold**

If `rename_threshold=0.9`, then 0.818 < 0.9 and the pair would be classified as:
- `add_columns: [{"column": "cust_name", ...}]`
- `drops: [{"column": "customer_name", "action": "warning_only", ...}]`

---

## Worked Example 3: Unsafe Type Cast

Scenario: Column `"amount"` changed from BIGINT to INT (narrowing cast).

### Input

```python
schema_diff_ctx = {
    "kind": "schema_diff",
    "added_columns": [],
    "removed_columns": [],
    "type_changes": [{"name": "amount", "old_type": "bigint", "new_type": "int"}],
    "likely_renames": [],
}
```

### Execution Trace

**1. Check _SAFE_CASTS matrix**
```python
_SAFE_CASTS[("bigint", "int")] = False  # narrowing → unsafe
```

**2. Generate DDL with safety flag**
```python
{
    "column": "amount",
    "from": "BIGINT",
    "to": "INT",
    "safe": False,
    "ddl": "-- UNSAFE: ALTER TABLE catalog.schema.orders ALTER COLUMN `amount` SET DATA TYPE INT;",
    "pyspark": "# UNSAFE cast BIGINT→INT: possible data truncation for values > 2^31",
}
```

**3. Risk generated**
```python
risks = [
    "UNSAFE type cast: amount BIGINT→INT — values exceeding INT range (2^31) will overflow. "
    "Verify max(amount) < 2147483647 before applying."
]
```

---

## Key Python Patterns

### difflib.SequenceMatcher for Fuzzy Rename Matching

```python
from difflib import SequenceMatcher

def _detect_renames(added, removed, threshold=0.8):
    renames = []
    used_added = set()
    used_removed = set()

    for rem in removed:
        best_match = None
        best_ratio = 0.0
        for add in added:
            if add["name"] in used_added:
                continue
            # Types must be compatible
            if add["type"] != rem["type"]:
                continue
            ratio = SequenceMatcher(None, rem["name"], add["name"]).ratio()
            if ratio > best_ratio and ratio >= threshold:
                best_ratio = ratio
                best_match = add

        if best_match:
            renames.append({"old": rem["name"], "new": best_match["name"], "similarity": best_ratio})
            used_added.add(best_match["name"])
            used_removed.add(rem["name"])

    return renames
```

### _SAFE_CASTS Dictionary for Type Compatibility

```python
_SAFE_CASTS: dict[tuple[str, str], bool] = {
    # Widening (always safe)
    ("int", "bigint"): True,
    ("int", "double"): True,
    ("float", "double"): True,
    ("int", "string"): True,

    # Narrowing (potential data loss)
    ("bigint", "int"): False,
    ("double", "float"): False,
    ("string", "int"): False,
    ("double", "int"): False,
}
```

Lookup is `O(1)`. Unknown pairs default to `False` (conservative).

### f-String DDL Generation

```python
def _generate_add_ddl(target_table: str, column: str, col_type: str) -> str:
    return f"ALTER TABLE {target_table} ADD COLUMN `{column}` {col_type.upper()};"

def _generate_rename_ddl(target_table: str, old_name: str, new_name: str) -> str:
    return f"ALTER TABLE {target_table} RENAME COLUMN `{old_name}` TO `{new_name}`;"
```

Backtick quoting handles column names with spaces or reserved words.
