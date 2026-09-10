# compare_ops — Code Walkthrough

**Module:** `compare_ops.py` (233 lines)  
**Purpose:** `cross_check` (structural validation) + `detect_deletes` (CDC soft-delete detection).

---

## How It Works

`compare_ops` provides two lightweight comparison functions that complement the heavier `diff_ops`:

1. **`cross_check`** — Quick structural validation between source and target tables (row counts, column presence, schema compatibility)
2. **`detect_deletes`** — Finds rows that existed in a previous version but are missing from the current version (soft-delete detection for CDC patterns)

These are simpler than `diff_tables_by_key` — they answer "are these structurally compatible?" and "what disappeared?" without full row-level comparison.

---

## Worked Example 1: cross_check

### Input

```
source: 100 rows, columns [id, name, amount]
target: 95 rows, columns [id, name, amount, email]
```

### Step-by-Step Execution

**Step 1: Row count check**

`_check_row_count(source=100, target=95)`:

```python
result = {
    "check": "row_count",
    "passed": False,
    "details": "Row count mismatch: source=100, target=95 (delta: -5)"
}
```

**Step 2: Column presence check**

`_check_columns(source_cols=["id", "name", "amount"], target_cols=["id", "name", "amount", "email"])`:

```python
missing_in_target = set(source_cols) - set(target_cols)  # empty set
extra_in_target = set(target_cols) - set(source_cols)    # {"email"}

result = {
    "check": "columns",
    "passed": False,  # extra columns present
    "details": "Extra columns in target: ['email']",
    "missing_in_target": [],
    "extra_in_target": ["email"]
}
```

**Step 3: Schema compatibility check**

`_check_schema(source, target)` — for common columns, compare dtypes:

| Column | Source dtype | Target dtype | Compatible? |
|---|---|---|---|
| id | int64 | int64 | Yes |
| name | object | object | Yes |
| amount | float64 | float64 | Yes |

```python
result = {
    "check": "schema",
    "passed": True,
    "details": "All common columns have compatible types"
}
```

### Output

```python
{
    "checks": [
        {"check": "row_count", "passed": False, "details": "Row count mismatch: 100 vs 95"},
        {"check": "columns", "passed": False, "details": "Extra columns in target: ['email']"},
        {"check": "schema", "passed": True, "details": "All common columns compatible"}
    ],
    "all_passed": False,
    "summary": "2/3 checks failed: row_count, columns"
}
```

---

## Worked Example 2: detect_deletes

### Input

```
previous: 100 rows (ids 1-100)
current: 95 rows (ids 1-95, missing ids 96-100)

keys=["id"]
threshold=0.10  # max 10% deletes allowed
```

### Step-by-Step Execution

**Step 1: Anti-join to find missing rows**

Perform a left-anti join: rows in `previous` that have no matching key in `current`.

- Pandas: `merge(how="left", indicator=True)` then filter `_merge == "left_only"`
- Spark: `previous.join(current, on=keys, how="left_anti")`

Result: 5 rows (ids 96, 97, 98, 99, 100)

**Step 2: Compute delete metrics**

```python
deleted_count = 5
total_previous = 100
delete_rate = deleted_count / total_previous  # 0.05 (5%)
```

**Step 3: Compare to safety threshold**

```python
delete_rate = 0.05
threshold = 0.10
0.05 < 0.10  → within safe bounds (allowed)
```

If `delete_rate > threshold`, the function raises a warning and does NOT mark rows as deleted (safety guard against mass deletes from bad data).

**Step 4: Generate soft-delete output**

Add `_is_deleted=True` marker to the deleted rows for CDC processing:

```python
deleted_rows = previous[previous["id"].isin([96, 97, 98, 99, 100])].copy()
deleted_rows["_is_deleted"] = True
```

### Output

```python
{
    "deleted_count": 5,
    "total_previous": 100,
    "delete_rate": 0.05,
    "threshold": 0.10,
    "within_threshold": True,
    "deleted_keys": [{"id": 96}, {"id": 97}, {"id": 98}, {"id": 99}, {"id": 100}],
    "deleted_df": DataFrame  # 5 rows with _is_deleted=True column
}
```

---

## Python Patterns

- **No Anchor standard contract** — unlike other context builders, `compare_ops` returns direct dict/DataFrame results rather than the standard `{kind, subject, metrics, findings, risks, samples}` structure
- **`left_anti` join (Spark) / `indicator=True` merge (Pandas)** — the anti-join pattern efficiently finds rows present in one table but not another, without materializing the full outer join
- **Threshold safety guard** — prevents mass-delete scenarios (e.g., empty source file accidentally processed as "everything was deleted"); configurable per-call
- **Lightweight by design** — at 233 lines, this module handles quick structural checks that don't need the full complexity of `diff_ops` (1,971 lines)
- **Complementary to diff_ops** — use `cross_check` first for a fast pass/fail, then `diff_tables_by_key` for detailed row-level comparison only when needed
