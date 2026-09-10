# Data reconciliation reference

> Preserved technique source for the native owner.

Evidence-gathering checklist for data reconciliation tasks. Execute this checklist BEFORE calling
`anchor("task")`. Every item you complete becomes a `known_fact` or `constraint` in your plan.

Use this reference only after `data-reconciliation` owns the requested proof outcome.

## When to Use This Mode

| Scenario | Example |
|---|---|
| Comparing pipeline output to a reference (Excel, legacy system) | "Does our gold table match Sophie's Excel?" |
| Verifying counts across pipeline stages | "Bronze has 45k rows but gold only has 38k — where did 7k go?" |
| Auditing after a pipeline change | "We changed the dedup logic — verify nothing broke" |
| Cross-system reconciliation | "PJM portal shows 1,234 active projects — do we match?" |
| Pre-go-live validation | "Before switching from manual Excel to pipeline, prove they match" |

## Evidence Checklist — Execute In Order

### 1. Define the Two Sides

Every reconciliation has exactly two sides: **source of truth** and **system under test**.

| Side | What to record |
|---|---|
| **Source of truth** | Where is the "correct" answer? (Excel file, legacy system, external portal, manual count) |
| **System under test** | What are you validating? (your pipeline output, gold table, report) |

**Record as known_facts:**
- Source of truth: exact location, format, date/version, who produced it
- System under test: exact table/file, when it was last run
- Are they from the same point in time? (stale data = false mismatches)

**Rule:** If the source of truth is ambiguous, STOP. Ask the user.
"Compare our data to theirs" means nothing without knowing which "theirs" is authoritative.

### 2. Define "Match" — What Does Correct Look Like?

Reconciliation without a definition of match is just looking at numbers and hoping.

| Match type | Definition | How to check |
|---|---|---|
| **Row count match** | Same number of rows after applying same filters | `count()` both sides |
| **Key coverage match** | Same set of business keys present | `LEFT ANTI JOIN` both directions |
| **Value match** | Same values for the same key | `JOIN` on key, compare columns |
| **Aggregate match** | Sums/counts/averages agree within tolerance | `GROUP BY` + compare |
| **Schema match** | Same columns, same types | Side-by-side schema comparison |

**Record as known_facts:**
- Which match types are required (all? just counts? just key coverage?)
- Match tolerance (exact match? within 1%? within 5 rows?)
- Which columns matter (all columns? just key + a few value columns?)

**Record as acceptance_criteria:**
- Specific pass/fail thresholds for each match type
- e.g., "Row count within ±5", "All key columns match exactly", "Amounts within $0.01"

### 3. Understand Both Sides

#### Source of Truth

```python
# If it's a table:
truth_df = spark.table("catalog.schema.truth_table")
anchor("profile_table", truth_df, subject="truth_table")

# If it's an Excel file:
truth_df = spark.read.format("csv").option("header", True).load("path/to/file.csv")
anchor("profile_table", truth_df, subject="source_of_truth")
```

**Record as known_facts:**
- Row count
- Column names (watch for naming differences: `Project ID` vs `project_id`)
- Key column(s) and their cardinality
- Any filters already applied (e.g., "Sophie's Excel excludes Withdrawn projects")
- Data freshness / snapshot date
- Known quirks (duplicate rows, extra header rows, trailing whitespace)

#### System Under Test

```python
test_df = spark.table("catalog.schema.gold_table")
anchor("profile_table", test_df, subject="gold_table")
```

**Record as known_facts:**
- Row count
- Column names
- Key column(s) and cardinality
- Filters applied in the pipeline
- Last pipeline run time
- Any known data quality issues

### 4. Align the Two Sides

Before comparing, you must make the two sides comparable. They almost never are out of the box.

| Alignment issue | How to detect | How to fix |
|---|---|---|
| Different column names | Side-by-side schema comparison | Create a column mapping dict |
| Different filters | Count mismatch before any comparison | Apply same filters to both sides |
| Different grain | One has duplicates the other doesn't | Dedup or aggregate to same grain |
| Different types | String "1234" vs integer 1234 | Cast both to same type |
| Different date ranges | One side has more history | Filter both to same date range |
| Whitespace/case differences | Keys don't join but look similar | TRIM + UPPER on join keys |
| NULL handling | One side has NULLs where other has blanks | NULLIF(TRIM(col), '') on both sides |

**Record as known_facts:**
- Column mapping (truth column → test column)
- Filters to apply to make sides comparable
- Type casts needed
- Any grain alignment needed

**Record as constraints:**
- "Must apply same filters to both sides before comparing"
- "Must align column names using mapping: [mapping]"
- "Must handle NULL vs blank: NULLIF(TRIM(...), '') on both sides"

### 5. Plan the Comparison Sequence

**Use the composed workflow** to automate the diff → partition_check → watermark → schema_diff chain:

```python
anchor("reconcile", truth_df, test_df, keys=["primary_key"])
# → Auto-chains: delta_diff (or diff) → partition_check → watermark → schema_diff
```

Or design as numbered, verifiable steps — from broadest to most specific:

```
Step 1: Row count comparison (both sides, same filters)
        → Pass: counts within tolerance. Fail: investigate funnel.

Step 2: Key coverage (LEFT ANTI JOIN both directions)
        → Pass: 0 orphan keys. Fail: list orphan keys, investigate.

Step 3: Value comparison on key columns (JOIN, compare critical columns)
        → Pass: 0 mismatches. Fail: sample mismatches, categorize root cause.

Step 4: Aggregate comparison (SUM/COUNT of value columns by group)
        → Pass: within tolerance. Fail: identify which groups diverge.

Step 5: Document results (match report with counts per check)
```

**Record as acceptance_criteria:**
- One pass/fail criterion per step with specific threshold
- Final deliverable: reconciliation report showing match status per check

### 6. Anticipate Common False Mismatches

These cause "mismatches" that aren't real bugs — check for them before reporting issues.

**Use `anchor("coerce_check")` to classify false mismatches automatically:**
```python
# After anchor("diff") shows value_changed > 0 on certain columns:
ctx = anchor("coerce_check", old_df, new_df, keys=["id"],
         columns=["suspect_col1", "suspect_col2"])
# → Classifies each mismatch: whitespace, case, unicode, numeric_representation, date_format, genuine
# → representation_pct tells you what fraction are false positives
# → suggested_fix tells you exactly what to apply (TRIM, UPPER, strip zero-width chars, CAST)
```

| False mismatch | Root cause | How to confirm |
|---|---|---|
| Count off by a few rows | Timing — source of truth is stale | Check data freshness dates |
| Keys present in one but not other | Different source data (CRM extract date) | Compare source extract timestamps |
| Values slightly different | Rounding (float vs decimal) | Check with tolerance: `ABS(a - b) < 0.01` |
| Duplicates in source of truth | Manual Excel has copy-paste duplicates | Dedup truth before comparing |
| NULL vs blank mismatch | One system stores '' and other stores NULL | Normalize both with NULLIF |
| Case sensitivity | "Active" vs "ACTIVE" | `anchor("coerce_check")` detects as `case` category |
| Whitespace / zero-width chars | Trailing spaces, Unicode U+200B | `anchor("coerce_check")` detects as `whitespace` or `unicode` |
| Float suffix | "2026.0" vs "2026" | `anchor("coerce_check")` detects as `numeric_representation` |

**Record as known_facts:**
- Which false mismatch patterns are likely for this comparison
- How you'll distinguish real mismatches from noise

### 7. Bound Historical Context

**Record as known_facts:**
- Current authoritative findings for this data
- Current source-of-truth contracts and observed quirks

After task acceptance, review only its bounded `memory_context` for advisory prior findings.
Do not issue a pre-task full-store/tag scan or force selected advice into the proof.

## Output — What You Feed to `anchor("task")`

After completing this checklist, you should have:

```python
known_facts = [
    "Source of truth: Sophie's Excel (PJM tab, downloaded 2026-05-18, 1,247 rows)",
    "System under test: analytics_dev.gold.queue_positions_pjm (last run 2026-05-19, 1,234 rows)",
    "Row count gap: 13 rows — truth has more",
    "Sophie's Excel filters: position_type='existing', Project Status NOT IN ('Withdrawn','Operating')",
    "Our pipeline filters: same logic in gold view",
    "Column mapping: Sophie's 'Queue Pos' → our 'queue_position', Sophie's 'MW' → our 'capacity_mw'",
    "Sophie's Excel has 72 NULL Project Status rows — 36 are duplicated SPP ERAS-2025 IDs",
    "Our CRM has no duplicates (Salesforce dedup applied upstream)",
    "Past learning: all count gaps in previous audit were CRM source differences, not logic bugs",
]

constraints = [
    "Must apply same filters to both sides before comparing",
    "Must TRIM + UPPER join keys (Sophie's data has trailing whitespace)",
    "Must handle NULL Project Status — Sophie keeps them, verify we do too",
    "Do not report CRM source differences as pipeline bugs",
]

acceptance_criteria = [
    "Row count gap fully explained (each missing/extra row attributed to a cause)",
    "Key coverage: <5 orphan keys after accounting for CRM differences",
    "Value match: capacity_mw within $0.01 for all matched keys",
    "Status values match after case normalization",
    "Reconciliation report produced with per-check pass/fail",
]

in_scope = ["PJM EXISTING tab reconciliation"]
out_of_scope = ["NEW tab", "CRM_NOT_QUEUE tab", "Other ISOs"]

risks = [
    "Sophie's Excel may be from different CRM extract — dates must match",
    "Floating point comparison on MW capacity needs tolerance",
]
```

**Recommended execution:** Use `anchor("reconcile", truth_df, test_df, keys=["primary_key"])` to automate the comparison sequence, then document results.

Continue the accepted task

NEVER reconcile without profiling both sides first — `anchor("profile_table")` on source AND target.
NEVER skip `anchor("diff", old, new, keys=[...])` — manual comparison misses edge cases. with these kwargs.
## Formal Spec compliance
If accepted task policy requires a formal Spec, use `writing-specs`, include its
compliance requirements, and review the exact linked Spec to a rating ≥ good
before consequential effects. Do not infer a Spec requirement from mode alone.
