# pre_merge -- Code Walkthrough

Step-by-step execution traces showing how the tool validates merge safety.

---

## Worked Example 1: Clean Merge (No Issues)

**Scenario:** Incremental batch of 50 new rows merging into a 1000-row target.
All keys unique, no nulls, schema matches. Expected: INSERT-only merge.

### Setup

```python
import pandas as pd

# Target: 1000 existing rows with ids 1-1000
target_df = pd.DataFrame({
    "id": range(1, 1001),
    "name": [f"Item_{i}" for i in range(1, 1001)],
    "value": [float(i * 10) for i in range(1, 1001)],
})

# Source: 50 NEW rows with ids 1001-1050 (no overlap)
source_df = pd.DataFrame({
    "id": range(1001, 1051),
    "name": [f"Item_{i}" for i in range(1001, 1051)],
    "value": [float(i * 10) for i in range(1001, 1051)],
})
```

### Execution Trace

**Step 1: Resolve target** — target is a DataFrame, use directly.

**Step 2: Duplicate key check**

```python
grouped = source_df.groupby(["id"], dropna=False).size().reset_index(name="_count")
dupes = grouped[grouped["_count"] > 1]
# dupes is empty — all 50 ids are unique
dupe_result = {"count": 0, "samples": [], "total_excess_rows": 0}
```

Finding: "No duplicate keys in source — MERGE ON condition is safe"

**Step 3: Null key check**

```python
null_count = source_df["id"].isna().sum()  # 0
null_result = {"total_null_key_rows": 0, "null_pct": 0.0, ...}
```

Finding: "No NULL merge keys in source"

**Step 4: Schema comparison**

```python
source_schema = {"id": "int64", "name": "object", "value": "float64"}
target_schema = {"id": "int64", "name": "object", "value": "float64"}

# All types match after normalization:
# int64 → long, object → string, float64 → double
# (long, long) → same type, (string, string) → same type, (double, double) → same type
schema_result = {"schema_compatible": True, "columns_added": [], "columns_removed": [], "columns_type_mismatch": []}
```

Finding: "Schema fully compatible — no type conflicts"

**Step 5: Key overlap analysis**

```python
source_keys = source_df[["id"]].drop_duplicates()  # 50 rows (1001-1050)
target_keys = target_df[["id"]].drop_duplicates()  # 1000 rows (1-1000)
merged = source_keys.merge(target_keys, on=["id"], how="inner")
# merged is EMPTY — no overlap

overlap_count = 0
insert_count = 50 - 0 = 50
update_count = 0
merge_behavior = "INSERT-only"
```

Finding: "All 50 source keys are new — INSERT-only merge"

**Step 6: Batch ratio**

```python
source_target_ratio = 50 / 1000 = 0.05  # well under 2.0, no risk
```

**Step 7: Build contract**

```python
summary = "OK: MERGE safe — 0 duplicate key(s) — schema compatible — INSERT-only merge (50 inserts, 0 updates)"
risks = []  # Empty!
suggested_next_actions = ["Merge looks safe — proceed with MERGE INTO"]
```

### Final Output

```python
ctx["summary"]  # "OK: MERGE safe..."
ctx["metrics"]["merge_behavior"]  # "INSERT-only"
ctx["metrics"]["source_duplicate_key_count"]  # 0
ctx["risks"]  # []
```

---

## Worked Example 2: BLOCKED — Duplicate Keys + Type Mismatch

**Scenario:** Source has duplicate ids (merge will fail) AND a string column
that targets an int column (cast will fail). Two blocking issues.

### Setup

```python
import pandas as pd

target_df = pd.DataFrame({
    "id": [1, 2, 3],
    "amount": [100, 200, 300],  # int64
    "status": ["active", "active", "closed"],
})

source_df = pd.DataFrame({
    "id": [1, 1, 2, 4],       # id=1 is DUPLICATED
    "amount": ["150", "160", "250", "400"],  # string! (object dtype)
    "status": ["active", "closed", "active", "pending"],
})
```

### Execution Trace

**Step 2: Duplicate key check**

```python
grouped = source_df.groupby(["id"], dropna=False).size().reset_index(name="_count")
#   id  _count
# 0  1       2   <-- DUPLICATE
# 1  2       1
# 2  4       1
dupes = grouped[grouped["_count"] > 1]
# 1 duplicate group: id=1 appears 2 times

dupe_result = {
    "count": 1,
    "samples": [{"id": 1, "_duplicate_count": 2}],
    "total_excess_rows": 1,  # 2 - 1 = 1 excess row
}
```

Risk: "MERGE WILL FAIL: 1 duplicate key group(s) in source — Delta MERGE requires
unique keys in source when matching target rows. Total excess rows: 1"

**Step 4: Schema comparison**

```python
source_schema = {"id": "int64", "amount": "object", "status": "object"}
target_schema = {"id": "int64", "amount": "int64", "status": "object"}

# Normalization: int64→long, object→string
# Check "amount": source=string, target=long
# Lookup: ("string", "long") in _TYPE_COMPAT
# Result: (False, "high", "MERGE will fail on non-numeric values")

schema_result = {
    "schema_compatible": False,
    "columns_type_mismatch": [{
        "column": "amount",
        "source_type": "object",
        "target_type": "int64",
        "risk": "high",
        "note": "MERGE will fail on non-numeric values",
    }],
}
```

Risk: "Type mismatch on 'amount': source=object, target=int64 — MERGE will fail on non-numeric values"

**Step 5: Key overlap**

```python
source_keys = {1, 2, 4}  # 3 distinct
target_keys = {1, 2, 3}  # 3 distinct
overlap = {1, 2}  # 2 keys in both

insert_count = 3 - 2 = 1   # id=4 is new
update_count = 2            # id=1, id=2 exist
merge_behavior = "MIXED"
```

**Step 7: Build contract**

```python
status = "BLOCKED"  # dupe_count > 0 or type mismatches
summary = "BLOCKED: MERGE unsafe — 1 duplicate key(s) — schema incompatible — MIXED merge (1 inserts, 2 updates)"

suggested_next_actions = [
    'Deduplicate source before merge: source_df.dropDuplicates(["id"])',
    'Fix type for \'amount\': source_df.withColumn("amount", F.col("amount").cast("int64"))',
]
```

### Final Output

```python
ctx["summary"]  # "BLOCKED: MERGE unsafe..."
ctx["metrics"]["source_duplicate_key_count"]  # 1
ctx["metrics"]["schema_compatible"]  # False
ctx["risks"]  # 2 risk entries (dupes + type mismatch)
ctx["suggested_next_actions"]  # Fix commands
```

---

## Worked Example 3: WARNING — Null Keys + Large Batch Ratio

**Scenario:** Source has 5000 rows (target has 1000), and 200 rows have NULL keys.
Merge won't fail, but rows will be lost and batch looks like a full reload.

### Setup

```python
import pandas as pd
import numpy as np

target_df = pd.DataFrame({
    "id": range(1, 1001),
    "value": range(1000),
})

# Source: 5000 rows, 200 with null id
ids = list(range(1, 4801)) + [None] * 200
source_df = pd.DataFrame({
    "id": ids,
    "value": range(5000),
})
```

### Execution Trace

**Step 2: Duplicate key check** — All non-null ids unique → 0 duplicates.

**Step 3: Null key check**

```python
any_null_mask = source_df[["id"]].isna().any(axis=1)
rows_with_any_null = 200

null_result = {
    "total_null_key_rows": 200,
    "null_pct": 200 / 5000 = 0.04,  # 4%
    "per_column": {"id": {"null_count": 200, "null_pct": 0.04}},
}
```

Risk: "200 source row(s) have NULL merge keys — these will NOT match any target
row (NULL != NULL in MERGE ON)"

**Step 5: Key overlap**

```python
source_distinct = 4800  # non-null distinct keys
overlap_count = 1000    # ids 1-1000 exist in target
insert_count = 3800     # ids 1001-4800 are new
update_count = 1000
merge_behavior = "MIXED"
```

**Step 6: Batch ratio**

```python
source_target_ratio = 5000 / 1000 = 5.0  # > 2.0!
```

Risk: "Source is 5.0x larger than target (5,000 vs 1,000) — verify this isn't a
full reload instead of an incremental batch"

**Step 7: Build contract**

```python
status = "WARNING"  # risks exist but no dupes or type issues
risks = [
    "200 source row(s) have NULL merge keys...",
    "Source is 5.0x larger than target...",
]
suggested_next_actions = [
    'Filter NULL keys: source_df.where(F.col("id").isNotNull())',
]
```

### Final Output

```python
ctx["summary"]  # "WARNING: MERGE safe..."
ctx["metrics"]["source_null_key_count"]  # 200
ctx["metrics"]["source_target_ratio"]    # 5.0
ctx["metrics"]["merge_behavior"]         # "MIXED"
```

---

## Key Python Patterns

### 1. Type Normalization

```python
def _normalize_type(type_str):
    t = str(type_str).lower().strip()
    if "(" in t:
        t = t[:t.index("(")]  # "decimal(10,2)" → "decimal"
    aliases = {"integer": "int", "bigint": "long", "object": "string", ...}
    return aliases.get(t, t)
```

Handles both Spark types (`"bigint"`, `"decimal(10,2)"`) and pandas dtypes
(`"int64"`, `"object"`, `"float64"`).

### 2. Type Compatibility Lookup with Fallback

```python
def _check_type_compatibility(source_type, target_type):
    src_norm = _normalize_type(source_type)
    tgt_norm = _normalize_type(target_type)

    if src_norm == tgt_norm:
        return {"compatible": True, "risk": "none", "note": "same type"}

    key = (src_norm, tgt_norm)
    if key in _TYPE_COMPAT:
        return dict from matrix

    # Check reverse (downcast)
    rev_key = (tgt_norm, src_norm)
    if rev_key in _TYPE_COMPAT:
        # Reverse of safe upcast = risky downcast
        return {"compatible": True, "risk": "medium", ...}

    # Unknown pair — warn, don't block
    return {"compatible": True, "risk": "medium", "note": "unknown type pair..."}
```

Three-level fallback: exact match → reverse lookup → unknown (conservative warn).

### 3. Duplicate Detection (Pandas)

```python
grouped = df.groupby(keys, dropna=False).size().reset_index(name="_count")
dupes = grouped[grouped["_count"] > 1]
total_excess = int(dupes["_count"].sum() - dupe_count)
```

`dropna=False` ensures null keys are still grouped (important for accurate count).
`total_excess_rows` = how many extra rows beyond one-per-key.

### 4. Deferred Contract Import

```python
def pre_merge_context(...):
    import sys, os
    _src_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "src",
    )
    if _src_path not in sys.path:
        sys.path.insert(0, _src_path)

    from odibi_anchor._utils.contract import build_base_context, ...
```

Unlike pre_join (which imports at module top), pre_merge adds `src/` to sys.path
inside the function. This allows the module to be imported without odibi_anchor
on the path — useful for standalone testing.

### 5. Summary Status Logic

```python
status = "BLOCKED" if (dupe_count > 0 or type_mismatches) else (
    "WARNING" if risks else "OK"
)
```

Three clear states: BLOCKED (will fail), WARNING (won't fail but issues), OK (safe).