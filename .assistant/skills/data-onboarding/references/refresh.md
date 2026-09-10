# Data Onboarding Refresh

> Preserved refresh technique.

Handle recurring data loads — new version of a source you've already onboarded.
This skill covers what's **different** from first-time onboarding.

## When to Load

- New month's file from the same source
- Updated export replacing a prior version
- Recurring pipeline getting a new batch
- **NOT** for first-time onboarding — that's `data-onboarding`

## Step 1: Schema Drift Detection

Run schema comparison **first**, before anything else:

```python
ctx = anchor("schema_diff", old_df, new_df)
```

This catches:
- Column additions (new columns in this month's file)
- Column removals (columns dropped from the export)
- Type changes (column that was STRING now comes as DOUBLE)

**Decision gate:** If schema changed, **stop and update the pipeline** before loading.
Schema drift in a recurring source means the upstream system changed — don't silently absorb it.

**If schema_diff shows issues, use the composed workflow to fix them systematically:**

```python
anchor("evolve", new_df, target_schema=old_df, subject="source_name")
# → Auto-chains: schema_diff → schema_migrate → coerce_fix → validate
```

## Step 2: Diff — What Changed?

```python
ctx = anchor("diff", old_df, new_df, keys=["primary_key"])
```

**Or use the composed workflow** for automated multi-step reconciliation:

```python
anchor("reconcile", old_df, new_df, keys=["primary_key"])
# → Auto-chains: diff → partition_check → watermark → schema_diff
```

Read the `changed_column_counts` — for each column you get:
- `null_to_value` — previously null, now populated
- `value_to_null` — previously populated, now null
- `value_changed` — different non-null value

**High `value_changed` on a column → suspicious.** Run `coerce_check` before trusting the changes.

## Step 3: Coerce Check — Are Changes Real?

For columns with `value_changed > 0`:

```python
ctx = anchor("coerce_check", old_df, new_df, keys=["primary_key"],
         columns=["suspect_col1", "suspect_col2"])
```

The `dominant_category` tells you what kind of "change" it really is:
- `whitespace` — trailing spaces, tabs
- `case` — uppercase/lowercase drift
- `unicode` — zero-width spaces, smart quotes, en-dashes
- `numeric_representation` — `1.0` vs `1` vs `1.00`
- `date_format` — `2024-01-15` vs `01/15/2024`
- `genuine` — actual data change

**If mostly representation issues → fix upstream, not in the data.**

### CRM Dogfooding Lesson

Reference case: CRM contact refresh showed 4,742 "changes." Breakdown:
- **4,742 unicode** mismatches (zero-width spaces in names)
- **664 null_to_value** (newly populated fields)
- **3 whitespace** differences

Most "changes" aren't real. Always coerce_check before reacting to a high diff count.

## Step 4: Load Strategy Decision

| Scenario | Strategy | Tool |
|---|---|---|
| Full replacement (drop old, load new) | Validated overwrite | For Delta, `anchor("apply_sql", ..., mode="table")` after quality/pre-merge passes |
| Additive (new rows only) | Append with dedup | For Delta, build validated SQL and execute through `anchor("apply_sql")` |
| Merge (update existing + add new) | MERGE/upsert | For Delta, guarded `anchor("apply_sql")` with explicit keys |
| SCD Type 2 (preserve history) | Version rows | For Delta, validated close-old/insert-new SQL through `anchor("apply_sql")` |

**Choosing:**
- Overwrite when the source is always a complete snapshot and you don't need history
- Append when the source only contains new records (event logs, transactions)
- MERGE when the source contains updates to existing records plus new ones
- SCD Type 2 when you need to preserve the history of changes over time

## Step 5: Post-Load Verification

After loading, verify the result:

1. **Count comparison:** old target count → new target count. Is the delta explainable?
2. **Quality gate:**
   ```python
   anchor("quality", new_target_df, keys=["primary_key"])
   ```
   Expect zero duplicate keys.
3. **Spot-check:** Sample rows that changed and verify the changes are correct.

## Gotchas for Recurring Loads

- **Column order changes between files** (Excel exports love this) — match by name, not position
- **New columns added silently** — `schema_diff` catches this; don't skip Step 1
- **Source system coercion changes** — same data, different formatting between exports
- **Timestamp columns updating on every export** — `Updated_Timestamp` refreshes even for unchanged rows; exclude from diff or expect noise
- **File naming patterns** — ensure you're reading the NEW file, not re-processing the old one

## Phase Gate

After completing the refresh workflow, return to `data-onboarding` SKILL.md
for any remaining pipeline or schema work.
