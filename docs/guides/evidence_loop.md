# The Evidence Loop Pattern

## Concept

The evidence loop is a structured way to go from vague intent to confident execution:

```
Plan → Discover Gaps → Gather Evidence → Re-Plan → Execute
```

Each pass through `task_execution_context` identifies what you DON'T know,
recommends tools to fill gaps, and scores your readiness.

## Step-by-Step

### Pass 1: Initial Planning

```python
from odibi_anchor.planning import task_execution_context

ctx = task_execution_context(
    task="Migrate bronze.readings to silver with dedup.",
    mode="implementation",
    goal="Idempotent merge, no duplicate keys.",
)

# Score: 40/100 — too vague to execute
print(ctx["status"])  # "needs_detail"
print(ctx["discovery"]["evidence_gaps"])
# → ["Unknown schema structure", "Unknown key uniqueness", "Unknown null patterns"]
```

### Pass 2: Gather Evidence

Use recommended context generators to fill gaps:

```python
from odibi_anchor.profiling import dataset_profile_context
from odibi_anchor.validation import quality_gate_context, duplicate_key_context
from odibi_anchor.tables import table_contract_summary

# Profile the source
profile = dataset_profile_context(bronze_df)

# Check key integrity
dupes = duplicate_key_context(bronze_df, keys=["asset_id", "reading_date"])

# Get table contract
contract = table_contract_summary(bronze_df, candidate_key_columns=["asset_id", "reading_date"])
```

### Pass 3: Re-Plan with Evidence

```python
ctx2 = task_execution_context(
    task="Migrate bronze.readings to silver with dedup.",
    mode="implementation",
    goal="Idempotent merge, no duplicate keys.",
    evidence=[
        {"name": "profile", "status": "passed", "summary": "450K rows, 12 cols, 2% nulls in value"},
        {"name": "duplicate_keys", "status": "failed", "summary": "1,247 duplicate keys found"},
        {"name": "table_contract", "status": "passed", "summary": "Grain: asset_id + reading_date"},
    ],
    constraints=["Dedup before merge — keep latest by ingest_ts"],
    acceptance_criteria=["Zero duplicate keys post-merge", "No null keys written"],
    risks=["1,247 existing duplicates need resolution strategy"],
    in_scope=["Bronze dedup", "Silver merge"],
)

# Score: 90/100 — ready to execute
print(ctx2["status"])  # "ready"
```

## Key Principles

1. **Never execute at score < 50** — you're guessing
2. **Evidence gaps map to context generators** — the tool tells you which to run
3. **Each pass should improve score by 15-30 points** — if not, you're adding noise
4. **Two passes is usually enough** — three means the task is too big, split it
5. **Evidence has status** — "passed"/"failed"/"partial" changes the plan
