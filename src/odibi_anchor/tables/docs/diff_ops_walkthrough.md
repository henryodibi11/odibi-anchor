# diff_tables_by_key — Code Walkthrough

**Module:** `diff_ops.py` (1,971 lines)  
**Purpose:** Compare two DataFrames by business key — added/removed/changed/unchanged with per-column change detail.

---

## How It Works

`diff_tables_by_key` takes two DataFrames (old and new versions of the same logical table), joins them on one or more business key columns, and classifies every row into one of four categories:

- **added** — exists in new but not in old
- **removed** — exists in old but not in new
- **changed** — exists in both but at least one non-key column differs
- **unchanged** — exists in both with all values identical

For changed rows, it produces a per-column breakdown showing exactly which columns differ, enabling targeted MERGE INTO statements.

---

## Worked Example 1: Basic Diff with Changes

### Input

```
old_df (3 rows):
  id=1, name="Alice",   amount=100
  id=2, name="Bob",     amount=200
  id=3, name="Charlie", amount=300

new_df (3 rows):
  id=1, name="Alice",   amount=150
  id=2, name="Bob",     amount=200
  id=4, name="Diana",   amount=400

keys=["id"]
```

### Step-by-Step Execution

**Step 1: Outer merge on key columns**

The function performs an outer join on `id`, producing 4 rows (ids 1, 2, 3, 4). Each row gets suffixed columns (`name_x`/`name_y`, `amount_x`/`amount_y`) and a `_merge` indicator.

**Step 2: Classify each row**

| id | _merge | Classification | Reason |
|---|---|---|---|
| 1 | both | **changed** | `amount_x=100` ≠ `amount_y=150` |
| 2 | both | **unchanged** | All values match |
| 3 | left_only | **removed** | Missing from new_df |
| 4 | right_only | **added** | Missing from old_df |

For id=1 (changed), the function builds `changed_columns=["amount"]` by comparing each `col_x` vs `col_y` pair.

**Step 3: Compute metrics**

```python
metrics = {
    "added": 1,
    "removed": 1,
    "changed": 1,
    "unchanged": 1,
    "total_old": 3,
    "total_new": 3,
    "change_rate": 0.333,  # changed / total rows in both
}
```

**Step 4: Generate merge_expr SQL**

For Delta MERGE INTO operations, the function generates:

```sql
MERGE INTO target USING source ON target.id = source.id
WHEN MATCHED AND (target.amount <> source.amount) THEN UPDATE SET amount = source.amount
WHEN NOT MATCHED THEN INSERT (id, name, amount) VALUES (source.id, source.name, source.amount)
```

### Output

```python
{
    "kind": "diff",
    "subject": "table_comparison",
    "metrics": {"added": 1, "removed": 1, "changed": 1, "unchanged": 1, ...},
    "findings": ["1 row changed (column: amount)", "1 row added", "1 row removed"],
    "risks": [],
    "samples": {
        "added": [{"id": 4, "name": "Diana", "amount": 400}],
        "removed": [{"id": 3, "name": "Charlie", "amount": 300}],
        "changed": [{"id": 1, "changed_columns": ["amount"], "old_amount": 100, "new_amount": 150}],
    },
    "merge_expr": "MERGE INTO target USING source ON ...",
}
```

---

## Worked Example 2: Schema Drift Detection

### Input

```
old_df columns: ["id", "name", "amount"]
new_df columns: ["id", "name", "amount", "email"]

keys=["id"]
```

### Step-by-Step Execution

**Step 1: Resolve comparable columns**

`_resolve_compare_columns` computes the intersection of non-key columns:
- old non-key columns: `{"name", "amount"}`
- new non-key columns: `{"name", "amount", "email"}`
- common (comparable): `["name", "amount"]`

**Step 2: Detect schema drift**

`"email"` is in new_df but not in old_df. This is noted in findings:

```python
findings.append("Schema drift detected — 1 column added in new version: ['email']")
```

**Step 3: Assess risk**

Schema drift doesn't block the diff but produces a risk entry:

```python
risks.append("Schema drift detected — 1 column added in new version. "
             "Diff comparison limited to common columns only.")
```

**Step 4: Proceed with common columns only**

The diff runs on `["name", "amount"]` only. The `email` column is excluded from change detection but noted for the caller.

### Output

```python
{
    "kind": "diff",
    "metrics": {"added": 0, "removed": 0, "changed": 0, "unchanged": 3,
                "schema_drift": {"columns_added": ["email"], "columns_removed": []}},
    "findings": ["Schema drift detected — 1 column added in new version: ['email']"],
    "risks": ["Schema drift detected — diff limited to common columns"],
}
```

---

## Python Patterns

- **Outer merge with `_merge` indicator column** — uses Pandas `merge(how="outer", indicator=True)` to get `"left_only"`, `"right_only"`, `"both"` markers for classification
- **Per-row changed_columns list** — for rows with `_merge == "both"`, iterates over suffixed column pairs (`col_x` vs `col_y`) and builds a list of column names that differ
- **`merge_expr` SQL generation** — constructs Delta MERGE INTO syntax with appropriate WHEN MATCHED/NOT MATCHED clauses based on actual changed columns
- **Dual-engine dispatch** — detects whether inputs are Spark or Pandas DataFrames and routes to the appropriate comparison logic
- **Capped evidence samples** — limits sample rows in output to prevent memory issues on large diffs (configurable via `max_samples` parameter)
- **Null-safe comparison** — uses `IS DISTINCT FROM` semantics (two NULLs are considered equal, NULL vs value is a change)
