# Delta Diff Tool — Code Walkthrough

## Worked Example 1: Standard Version Diff

Scenario: Compare `catalog.schema.orders` at version 5 (current) vs version 4 (previous).
The last operation was a MERGE that added 10 rows, removed 2, and updated 50.

### Execution Trace

**1. Get Delta history**
```python
spark.sql("DESCRIBE HISTORY catalog.schema.orders LIMIT 50")
```

Returns (newest first):
```python
history = [
    {"version": 5, "timestamp": datetime(2026, 6, 8, 14, 0), "operation": "MERGE",
     "userName": "pipeline@ExampleCo.com", "operationMetrics": {...}},
    {"version": 4, "timestamp": datetime(2026, 6, 7, 10, 0), "operation": "WRITE",
     "userName": "pipeline@ExampleCo.com", "operationMetrics": {...}},
    # ... older versions
]
current_version = 5
```

**2. Resolve versions**
```python
old_version = None, versions_ago = 1
→ _resolve_version(None, history, current_version=5, versions_ago=1)
→ 5 - 1 = 4

new_version = None
→ _resolve_version(None, history, current_version=5)
→ 5 (latest)

resolved_old = 4, resolved_new = 5
```

**3. Read both snapshots**
```python
old_df = spark.read.format("delta").option("versionAsOf", 4).table("catalog.schema.orders")
new_df = spark.read.format("delta").option("versionAsOf", 5).table("catalog.schema.orders")
```

**4. Compute diff** (`_compute_diff_metrics`)

With `keys=["order_id"]`:

```python
# Key-based set operations
old_keys_df = old_df.select("order_id").distinct()   # 1000 keys
new_keys_df = new_df.select("order_id").distinct()   # 1008 keys

added_keys_df = new_keys_df.subtract(old_keys_df)    # 10 new keys
removed_keys_df = old_keys_df.subtract(new_keys_df)  # 2 removed keys
common_keys_df = old_keys_df.intersect(new_keys_df)  # 988 common keys

added_count = 10
removed_count = 2
```

**5. Find changed rows among common keys**
```python
# Join old and new on common keys
old_common = old_df.join(common_keys_df, "order_id", "inner").alias("old")
new_common = new_df.join(common_keys_df, "order_id", "inner").alias("new")

# Build change condition (NULL-safe) for all non-key columns:
change_conds = [
    ~F.col("old.status").eqNullSafe(F.col("new.status")),
    ~F.col("old.amount").eqNullSafe(F.col("new.amount")),
    ~F.col("old.updated_at").eqNullSafe(F.col("new.updated_at")),
    # ... all non-key columns
]
any_changed = change_conds[0] | change_conds[1] | ...  # OR all conditions

changed_df = joined.filter(any_changed)
changed_count = 50
unchanged_count = 988 - 50 = 938
```

**6. Identify which columns changed**
```python
# For each non-key column, count rows where it differs:
for col in non_key_compare:
    col_diff_count = joined.filter(~F.col(f"old.{col}").eqNullSafe(F.col(f"new.{col}"))).count()

# Result:
changed_columns = ["status", "amount"]  # Only these two had any differences
```

**7. Classify changes per column** (`_classify_column_changes`)

For `"status"` column:
```python
changed_rows = matched.filter(~F.col("old.status").eqNullSafe(F.col("new.status")))

counts = changed_rows.agg(
    F.sum(F.when(old_col.isNull() & new_col.isNotNull(), 1).otherwise(0)).alias("null_to_value"),
    F.sum(F.when(old_col.isNotNull() & new_col.isNull(), 1).otherwise(0)).alias("value_to_null"),
    F.sum(F.when(old_col.isNotNull() & new_col.isNotNull(), 1).otherwise(0)).alias("value_changed"),
).collect()[0]

# Result:
"status": {"null_to_value": 5, "value_to_null": 0, "value_changed": 45}
"amount": {"null_to_value": 0, "value_to_null": 2, "value_changed": 3}
```

**8. Final output**
```python
{
    "kind": "delta_diff",
    "subject": "catalog.schema.orders",
    "summary": "Version 4→5 (MERGE by pipeline@ExampleCo.com at 2026-06-08 14:00:00): "
               "10 added, 2 removed, 50 changed, 938 unchanged",
    "metrics": {
        "table": "catalog.schema.orders",
        "old_version": 4,
        "new_version": 5,
        "added_count": 10,
        "removed_count": 2,
        "changed_count": 50,
        "unchanged_count": 938,
        "changed_column_counts": {
            "status": {"null_to_value": 5, "value_to_null": 0, "value_changed": 45},
            "amount": {"null_to_value": 0, "value_to_null": 2, "value_changed": 3},
        },
        "schema_changed": False,
    },
    "findings": [
        "10 rows added (new keys not in previous version)",
        "2 rows removed (keys present in old but not new)",
        "50 rows changed across 2 column(s) (status, amount)",
        "  status: 50 changes — 45 value changes, 5 null→value fills",
        "  amount: 5 changes — 3 value changes, 2 value→null clears",
    ],
    "risks": [],  # <5% removed, so no warning
    "samples": {
        "added_rows": [{"order_id": 1001, "status": "active", ...}, ...],
        "removed_rows": [{"order_id": 500, "status": "cancelled", ...}, ...],
        "changed_rows": [{"old.order_id": 42, "old.status": None, "new.status": "active", ...}],
    },
    "suggested_next_actions": [
        "Classify change types: anchor('coerce_check', old_df, new_df, keys=['order_id'], columns=['status', 'amount'])",
        "Investigate removed rows: anchor('case_file', old_df, filter='where:order_id IN (...)')",
        "Profile new rows: anchor('profile_table', new_df.filter(...), subject='orders new rows')",
    ],
}
```

---

## Worked Example 2: With coerce_check Integration

Scenario: Same table, but `include_coerce_check=True` to distinguish genuine changes
from formatting artifacts.

### Execution Trace (Post-Diff)

After computing `changed_columns = ["status", "amount"]`:

```python
include_coerce_check = True
changed_columns = ["status", "amount"]

# Runs coerce_check automatically:
from odibi_anchor.tables.coercion_classifier import coercion_check_context

coerce_results = coercion_check_context(
    old_df, new_df, keys=["order_id"],
    columns=["status", "amount"],
    output_format="dict",
)
```

Suppose coerce_check finds:
- `status`: dominant_category = "case" (old has "ACTIVE", new has "active")
- `amount`: dominant_category = "genuine" (actual value changes)

This is added to the output, distinguishing that 45 of the "status" changes
are just case formatting differences, not real data mutations.

---

## Key Python Patterns

### Delta Time Travel via Spark Options

```python
def _read_delta_version(spark, table: str, version: int):
    return (
        spark.read.format("delta")
        .option("versionAsOf", version)
        .table(table)
    )
```

This leverages Delta Lake's transactional log to reconstruct the exact table state
at any version without needing separate backup copies.

### NULL-Safe Comparison with pyspark.sql.functions

```python
from pyspark.sql import functions as F

# Standard equality: NULL == NULL → NULL (unknown)
# eqNullSafe: NULL <=> NULL → True

changed = matched.filter(
    ~F.col("old.status").eqNullSafe(F.col("new.status"))
)
```

The `eqNullSafe` operator implements SQL's `<=>` semantics, treating two NULLs as equal.
Negating with `~` gives "these values differ (including null transitions)".

### Counter Pattern for Column Change Aggregation

```python
# Single-pass aggregation with conditional sums:
counts = changed_rows.agg(
    F.sum(F.when(old_col.isNull() & new_col.isNotNull(), 1).otherwise(0)).alias("null_to_value"),
    F.sum(F.when(old_col.isNotNull() & new_col.isNull(), 1).otherwise(0)).alias("value_to_null"),
    F.sum(F.when(old_col.isNotNull() & new_col.isNotNull(), 1).otherwise(0)).alias("value_changed"),
).collect()[0]
```

A single Spark action (one `collect()`) retrieves all three classification counts
per column, avoiding multiple passes over the data.

### Timestamp-Based Version Resolution

```python
def _resolve_version(version_spec, history, current_version, versions_ago=None):
    # ISO string → find latest version at or before that time
    target_ts = datetime.fromisoformat(version_spec)
    candidates = [h for h in history if h["timestamp"] <= target_ts]
    # History sorted newest-first, so candidates[0] is the closest version
    return candidates[0]["version"]
```

This enables point-in-time queries: "Show me what changed since last Monday"
without needing to know the exact version number.
