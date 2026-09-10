# Schema Migrate Tool — Usage

## Signature

```python
schema_migrate_context(
    schema_diff_ctx: dict,
    target_table: str,
    drop_columns: bool = False,
    dry_run: bool = True,
    rename_threshold: float = 0.8,
    subject: str | None = None,
    output_format: str = "dict",
) -> dict | str
```

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `schema_diff_ctx` | dict | Yes | — | Output from `anchor("schema_diff", old_df, new_df)` |
| `target_table` | str | Yes | — | Fully qualified table name for DDL (`"catalog.schema.table"`) |
| `drop_columns` | bool | No | `False` | If True, generates executable DROP COLUMN statements |
| `dry_run` | bool | No | `True` | If True, generates code without executing |
| `rename_threshold` | float | No | `0.8` | Similarity threshold (0.0–1.0) for rename detection |
| `subject` | str | No | target_table | Display label |
| `output_format` | str | No | `"dict"` | `"dict"` or `"markdown"` |

## Basic Workflow

```python
# Step 1: Detect schema differences
diff = anchor("schema_diff", old_df, new_df)

# Step 2: Generate migration plan (dry run)
ctx = anchor("schema_migrate", diff, "catalog.schema.orders")

# Step 3: Review the plan
for add in ctx["migration_plan"]["add_columns"]:
    print(add["ddl"])

# Step 4: Apply (copy DDL to a SQL cell and run)
```

## Rename Detection

```python
# Default threshold (0.8) — catches obvious renames
ctx = anchor("schema_migrate", diff, "catalog.schema.orders")

# Lower threshold (0.6) — catches more aggressive renames
ctx = anchor("schema_migrate", diff, "catalog.schema.orders", rename_threshold=0.6)

# Disable rename detection entirely
ctx = anchor("schema_migrate", diff, "catalog.schema.orders", rename_threshold=1.1)
```

## Enabling Column Drops

```python
# Default: DROP statements are commented-out warnings
ctx = anchor("schema_migrate", diff, "catalog.schema.orders")
# ctx["migration_plan"]["drops"][0]["action"] == "warning_only"

# Explicit: generate executable DROP statements
ctx = anchor("schema_migrate", diff, "catalog.schema.orders", drop_columns=True)
# ctx["migration_plan"]["drops"][0]["action"] == "drop"
```

## Result Structure

| Field | Type | Description |
|-------|------|-------------|
| `kind` | str | Always `"schema_migrate"` |
| `summary` | str | One-line plan description |
| `metrics.columns_added` | int | Number of ADD COLUMN operations |
| `metrics.columns_removed` | int | Number of columns to drop |
| `metrics.type_casts` | int | Number of type change operations |
| `metrics.renames` | int | Number of detected renames |
| `metrics.unsafe_casts` | int | Number of type casts flagged as unsafe |
| `metrics.dry_run` | bool | Whether this was a dry run |
| `migration_plan.add_columns` | list | ADD COLUMN details with DDL |
| `migration_plan.type_casts` | list | ALTER COLUMN details with safety flag |
| `migration_plan.renames` | list | RENAME COLUMN details with similarity score |
| `migration_plan.drops` | list | DROP COLUMN details (guarded) |
| `risks` | list[str] | Unsafe operations requiring attention |
| `suggested_next_actions` | list[str] | What to do after reviewing the plan |

### migration_plan Entry Format

**Add column:**
```python
{"column": "email", "type": "STRING", "default": "NULL",
 "ddl": "ALTER TABLE catalog.schema.orders ADD COLUMN `email` STRING;",
 "pyspark": "spark.sql('ALTER TABLE catalog.schema.orders ADD COLUMN `email` STRING')"}
```

**Type cast:**
```python
{"column": "amount", "from": "INT", "to": "BIGINT", "safe": True,
 "ddl": "ALTER TABLE catalog.schema.orders ALTER COLUMN `amount` SET DATA TYPE BIGINT;",
 "pyspark": "spark.sql('ALTER TABLE ... ALTER COLUMN `amount` SET DATA TYPE BIGINT')"}
```

**Rename:**
```python
{"old_name": "customer_name", "new_name": "cust_name", "similarity": 0.85,
 "ddl": "ALTER TABLE catalog.schema.orders RENAME COLUMN `customer_name` TO `cust_name`;"}
```

## Direct Import

```python
import sys
sys.path.append("/Workspace/Users/user@example.com/odibi_anchor/src")
sys.path.append("/Workspace/Users/user@example.com/odibi_anchor")

from tools.schema_migrate_tool.schema_migrate_impl import schema_migrate_context

ctx = schema_migrate_context(
    schema_diff_ctx=diff,
    target_table="catalog.schema.orders",
    dry_run=True,
)
```
