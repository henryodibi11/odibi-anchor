# Evidence-oriented logging

> Preserved evidence-oriented logging technique.

Pipeline observability through centralized logging and structured print statements.
Does NOT cover alerting (Teams webhooks, email) — that's a separate concern.

## When to Log

| Event | Log? | Why |
|---|---|---|
| Pipeline start | YES | Establishes run context, timing baseline |
| Row count after read | YES | Confirms data arrived, catches empty source early |
| Row count after transform | YES | Tracks data flow, detects unexpected drops |
| Write success | YES | Proves persistence happened |
| Pipeline completion | YES | Duration, final row counts, success/failure |
| Each intermediate step | NO | Noise — only log boundaries, not internals |
| Debug info in production | NO | Use display() during development, remove before PR |

## Structured Pipeline Logging

```python
import logging

logger = logging.getLogger(__name__)

# Emit one structured completion event. Configure the handler/sink per project.
logger.info(
    "pipeline_complete",
    extra={"notebook_path": NB_PATH, "status": "Success", "error_message": None},
)
```

### Event Fields

| Column | Type | Source |
|---|---|---|
| Notebook_Path | STRING | `dbutils.notebook...notebookPath()` |
| Status | STRING | "Success" or "Failure" |
| Error_Message | STRING | Exception text (nullable) |
| Timestamp | TIMESTAMP | Auto-generated |

### Setup Pattern

```python
# ── Observability Setup ─────────────────────────────────
import logging
import time

NB_PATH = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
logger = logging.getLogger(__name__)

start_time = time.time()
```

## Structured Print Statements (Notebooks)

For notebook pipelines, structured prints ARE the logging. They show in job run output and are searchable.

### Pattern

```python
# ── Read ────────────────────────────────────────────────
df = reader.read(...)
print(f"📥 Read: {df.count():,} rows from {source_table}")

# ── Transform ───────────────────────────────────────────
df = deduplicate(df, keys=CONFIG["dedup_keys"], order_by=CONFIG["dedup_order"])
print(f"🔄 Dedup: {df.count():,} rows remaining")

df = hash_columns(df, columns=hash_cols, output_column="row_hash")
print(f"🔄 Hash: added row_hash column")

# ── Write ───────────────────────────────────────────────
saver.save(df, ...)
print(f"📤 Write: {df.count():,} rows → {target_table}")

# ── Summary ─────────────────────────────────────────────
duration = time.time() - start_time
print(f"✅ Pipeline complete in {duration:.1f}s")
```

### Emoji Convention

| Emoji | Meaning | Use for |
|---|---|---|
| 📥 | Data in | Read operations |
| 📤 | Data out | Write operations |
| 🔄 | Transform | Dedup, hash, join, filter |
| ✅ | Success | Final completion |
| ⚠️ | Warning | Non-fatal quality issues (nulls in non-key cols) |
| ❌ | Failure | Assertion failures, missing data |

## Pipeline Wrapper (Production)

For scheduled pipelines that need centralized logging + error capture:

```python
# ── Config ──────────────────────────────────────────────
PIPELINE = "silver_queue_positions"
start_time = time.time()

try:
    # ... pipeline logic with print statements ...

    # ── Log Success ─────────────────────────────────────
    duration = time.time() - start_time
    logger.info("pipeline_complete", extra={"notebook_path": NB_PATH, "status": "Success"})
    print(f"✅ {PIPELINE} complete in {duration:.1f}s")

except Exception as e:
    duration = time.time() - start_time
    logger.exception("pipeline_failed", extra={"notebook_path": NB_PATH})
    print(f"❌ {PIPELINE} failed after {duration:.1f}s: {e}")
    raise  # Always re-raise — don't swallow
```

## Ingestion Metadata (added to DataFrames)

```python
from pyspark.sql import functions as F

# Adds: Created_Timestamp, Updated_Timestamp
now = F.current_timestamp()
df = df.withColumn("Created_Timestamp", now).withColumn("Updated_Timestamp", now)
```

These columns track WHEN each row was written. Use for:
- Debugging stale data
- Incremental processing (`WHERE Updated_Timestamp > last_run`)
- Audit trail

## What NOT to Log

- PII (names, emails, SSNs) — never in print or log tables
- Full DataFrames — use `.limit(5).display()` during dev, remove before PR
- Secrets or connection strings
- Internal function call traces (unless debugging)
- Repetitive per-row messages (log aggregates, not individual rows)

## Rules

- Every pipeline has `start_time = time.time()` at the top
- Every read/write gets a row count print
- Every production pipeline emits structured completion/failure events to its configured sink
- Print statements use the emoji convention for scannability
- Failed pipelines log the error AND re-raise — never swallow silently
- Remove all `display(df)` and debug prints before PR
- NEVER log without `anchor("touched")` tracking the file afterward
- NEVER skip structured prints — job run output is your only debugging surface
- NEVER swallow exceptions silently — always re-raise after logging

## Anchor Integration

Use `anchor("log")` to record pipeline milestones during investigation sessions:

```python
# Step 1: Log milestones during debugging/investigation
result = anchor("log", "pipeline", "bronze_queue_pjm loaded 6454 rows")
result = anchor("log", "decision", "Switching to LEFT JOIN to preserve orphan rows")
result = anchor("log", "quality", "12% nulls in mw_capacity — confirmed normal for early-stage projects")

# Step 2: Review session log
result = anchor("session_log")

# Step 3: After adding logging code to a notebook
result = anchor("touched", "notebooks/bronze_queue_pjm.py")
result = anchor("preflight")
result = anchor("gate")
```

### When to use `anchor("log")` vs print statements:

| Situation | Use | Why |
|---|---|---|
| Pipeline notebook observability | `print(f"📥 Read: {count:,} rows")` | Shows in job run output |
| Investigation session milestones | `anchor("log", "category", "message")` | Persists in session state |
| Recording a decision mid-session | `anchor("log", "decision", "why...")` | Supports later `learning capture/assess` review |
| Tracking data quality observations | `anchor("log", "quality", "finding...")` | Structured, reviewable |
