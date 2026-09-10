# Schema design workflow

> Preserved technique source for the native owner.

Design the table schema BEFORE writing pipeline code. Wrong schema = wrong everything
downstream. This skill covers key selection, grain definition, type choices, partitioning,
and slowly changing dimension (SCD) strategies.

## When to Use This Skill

| Request | Use this skill? |
|---|---|
| "Create a new silver/gold table" | ✅ Yes — design schema first |
| "What should the grain be?" | ✅ Yes |
| "Should this be SCD1 or SCD2?" | ✅ Yes |
| "How should I partition this table?" | ✅ Yes |
| "Build the pipeline for table X" | ⚠️ Design schema first, then use data operations when mutation is material |
| "Add a column to existing table" | ❌ Usually no — use the applicable implementation workflow |

## Step 1: Define the Grain

The grain is the most important design decision. Everything else follows from it.

**The grain answers:** "What does one row represent?"

| Good grain definition | Bad grain definition |
|---|---|
| "One row per project per queue date" | "One row per project" (which version?) |
| "One row per invoice line item" | "One row per invoice" (what about multi-line?) |
| "One row per project (current state only)" | "One row per thing" (what thing?) |
| "One row per project per snapshot month" | "All the project data" (not a grain) |

### How to Choose Grain

```python
# 1. Look at the source data
anchor("profile_table", source_df, subject="catalog.schema.source")

# 2. Check candidate key uniqueness
from pyspark.sql import functions as F

candidate_keys = ["project_id", "queue_date"]
dupes = source_df.groupBy(candidate_keys).agg(F.count("*").alias("cnt")).where("cnt > 1")
print(f"Duplicate key combinations: {dupes.count()}")

# If dupes > 0, either:
# a) Add more columns to the key (finer grain)
# b) Plan dedup with explicit ordering (which row to keep)
```

### Grain Decision Matrix

| Scenario | Grain | Key columns | Notes |
|---|---|---|---|
| Snapshot data (monthly files) | Per entity per snapshot | `entity_id + snapshot_date` | History preserved |
| Transaction data | Per transaction | `transaction_id` | Natural key |
| Current state only | Per entity | `entity_id` | Overwrite old state |
| Event stream | Per event | `event_id` or `entity_id + timestamp` | Append-only |
| Aggregated metrics | Per group per period | `group_key + period` | Pre-computed |

## Step 2: Choose Column Types

### Medallion Layer Type Conventions

| Layer | String columns | Typed columns | Why |
|---|---|---|---|
| **Bronze** | ALL columns as STRING | None | Preserve raw data exactly — no cast failures |
| **Silver** | Only true strings (names, IDs) | All numeric, date, boolean | Validated types, safe for computation |
| **Gold** | Only dimensions | Fully typed | Business-ready, optimized |

### Type Casting Rules

```sql
-- NEVER in silver/gold:
CAST(raw_col AS INT)              -- Silent data loss on bad values

-- ALWAYS in silver:
TRY_CAST(NULLIF(TRIM(raw_col), '') AS INT)    -- Safe: NULL on failure, trims whitespace
TRY_CAST(NULLIF(TRIM(raw_col), '') AS DATE)   -- Safe: handles empty strings
TRY_CAST(NULLIF(TRIM(raw_col), '') AS DOUBLE) -- Safe: NULL instead of error
```

### Column Type Decision Table

| Data content | Bronze type | Silver type | Notes |
|---|---|---|---|
| IDs, codes, names | STRING | STRING | Keep as string — no arithmetic needed |
| Dates | STRING | DATE | TRY_CAST with format-aware parsing |
| Timestamps | STRING | TIMESTAMP | Include timezone handling |
| Whole numbers (counts) | STRING | INT or BIGINT | TRY_CAST, check for commas |
| Decimal numbers (money) | STRING | DECIMAL(precision, scale) | NOT DOUBLE — precision matters |
| Capacity/MW values | STRING | DOUBLE | OK for approximate values |
| Yes/No flags | STRING | BOOLEAN | Map explicitly: "Y"→true, "N"→false |
| JSON blobs | STRING | STRING | Parse with from_json() when needed |

### Metadata Columns (add to every table)

| Column | Type | Source | Purpose |
|---|---|---|---|
| `_extracted_at` | TIMESTAMP | `current_timestamp()` | When this row was ingested |
| `_source_file` | STRING | `input_file_name()` | Which file this came from |
| `_row_hash` | STRING | `sha2(concat_ws(...), 256)` | Change detection, dedup tiebreaker |

## Step 3: Design Keys

### Primary Key Selection

| Rule | Why |
|---|---|
| Must be NOT NULL | NULL keys break joins, dedup, and merge |
| Must be unique (at the defined grain) | Duplicate keys = corrupt downstream |
| Must be stable (doesn't change for same entity) | Changing keys break relationships |
| Must be minimal (fewest columns needed) | Simpler joins, smaller index |
| Prefer natural keys over surrogate | Natural keys are self-documenting |

### Key Patterns by Table Type

| Table type | Key pattern | Example |
|---|---|---|
| Dimension (Type 1) | Natural business key | `project_id` |
| Dimension (Type 2/SCD) | Natural key + version | `project_id + effective_from` |
| Fact (transactional) | Transaction ID | `invoice_id + line_number` |
| Fact (snapshot) | Entity + snapshot period | `project_id + snapshot_month` |
| Bridge/junction | Composite of both sides | `project_id + queue_id` |
| Aggregate | Group key + period | `region + fiscal_quarter` |

### Foreign Key Relationships

Document how tables relate:

```
dim_project (project_id) ←── fact_queue_position (project_id)
dim_queue (queue_id) ←── fact_queue_position (queue_id)
dim_date (date_key) ←── fact_queue_position (queue_date)
```

## Step 4: Plan Partitioning and Clustering

### Partitioning (for large tables)

| Strategy | When to use | Example |
|---|---|---|
| **Date partition** | Time-series data, incremental loads | `PARTITIONED BY (queue_date)` |
| **Category partition** | Few distinct values, queries filter by it | `PARTITIONED BY (iso_region)` |
| **No partition** | Small table (<1GB), or queries scan everything | Default |

**Rules:**
- Partition column should have **low cardinality** (hundreds, not millions)
- Partition should match the **most common filter** in queries
- Avoid over-partitioning — 10,000+ partitions = small file problem

### Clustering / Z-ORDER (Delta)

```sql
-- Optimize for point lookups on project_id
OPTIMIZE catalog.schema.table ZORDER BY (project_id)

-- Multi-column for range queries
OPTIMIZE catalog.schema.table ZORDER BY (project_id, queue_date)
```

**When to Z-ORDER:**
- Column is frequently used in WHERE clauses
- Column has high cardinality (unlike partitioning)
- Table is large enough that file skipping matters (>1GB)

## Step 5: Choose SCD Strategy

### SCD Decision Matrix

| Scenario | SCD Type | Implementation | Trade-off |
|---|---|---|---|
| Only need current state | **Type 1** (overwrite) | MERGE with update | Simple, no history |
| Need full history | **Type 2** (versioned rows) | Add `effective_from`, `effective_to`, `is_current` | Complex, table grows |
| Need current + previous | **Type 3** (previous column) | Add `previous_status` column | Simple, limited history |
| Need change log | **Type 4** (history table) | Separate current and history tables | Clean separation |

### SCD Type 2 Schema Pattern

```sql
CREATE TABLE dim_project (
    project_id STRING NOT NULL,        -- Natural key
    project_name STRING,
    status STRING,
    capacity_mw DOUBLE,
    -- SCD2 tracking columns
    effective_from DATE NOT NULL,       -- When this version became active
    effective_to DATE,                  -- NULL = current version
    is_current BOOLEAN NOT NULL,        -- Quick filter for current state
    _row_hash STRING NOT NULL,          -- Change detection
    _extracted_at TIMESTAMP NOT NULL
)
USING DELTA
```

## Step 6: Document the Schema

### Schema Design Document Template

```markdown
# Schema: [table_name]

**Layer:** Bronze | Silver | Gold
**Grain:** [one row per ___]
**Primary key:** [columns]
**SCD type:** Type 1 | Type 2 | N/A
**Partition:** [column] | None
**Z-ORDER:** [columns] | None

## Columns

| Column | Type | Nullable | Description | Source |
|---|---|---|---|---|
| project_id | STRING | NO | Business key | source.project_id |
| queue_date | DATE | NO | Date entered queue | TRY_CAST(source.raw_date) |
| capacity_mw | DOUBLE | YES | Nameplate capacity | TRY_CAST after comma removal |
| _extracted_at | TIMESTAMP | NO | Ingestion timestamp | current_timestamp() |

## Relationships

- FK: project_id → dim_project.project_id
- FK: queue_date → dim_date.date_key

## Business Rules

1. [Rule about this table]
2. [Another rule]

## Expected Volume

- Current: [X rows]
- Growth: [Y rows/month]
- Retention: [Z months/years/forever]
```

## Integration with odibi-anchor

```python
# Design schema before building pipeline
anchor("task", "design silver schema for queue positions",
    goal="define grain, keys, types, and partitioning for silver queue table",
    mode="planning")

# Profile source data to inform decisions
anchor("profile_table", source_df, subject="catalog.schema.source")
anchor("profile_table", source_df, subject="catalog.schema.source", level="deep")

# Verify key uniqueness
anchor("quality", df, subject="catalog.schema.table", keys=["project_id", "queue_date"])

# After design is complete, persist the decision in the owning Spec/decision artifact.
# Capture learning only if current evidence supports a reusable observation.
```

## Anti-Patterns

| Don't | Do instead |
|---|---|
| Skip grain definition | Define grain FIRST — everything depends on it |
| Use DOUBLE for money | Use DECIMAL(precision, scale) — precision matters |
| Assume keys are unique | Verify with GROUP BY + HAVING COUNT > 1 |
| Partition on high-cardinality column | Partition on low-cardinality, Z-ORDER on high |
| Use CAST instead of TRY_CAST | TRY_CAST returns NULL on failure — safer |
| Design schema while writing code | Design schema BEFORE writing any pipeline code |
| Skip metadata columns | Always add _extracted_at and _source_file |

NEVER design schema without profiling the source first — `anchor("profile_table")` reveals true grain.
NEVER assume key uniqueness — `anchor("duplicate", df, keys)` proves it or disproves it.
