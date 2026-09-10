# Incident response workflow

> Preserved technique source for the native owner.

Production is broken. Users are affected. The goal is to restore service FIRST, understand
root cause SECOND, and prevent recurrence THIRD. This is a different mode from normal
development — speed and blast radius matter more than code elegance.

## Applicable assurance overlay

When structured selection activates `operations.reliability`, use
`.assistant/references/assurance/standards-overlays.md` for applicable outcomes and result
evidence. `change-safety.high-risk` also applies only at T3 with a structured consequence trait
or declared consequence effect. This pointer creates no obligation by itself.

## When to Use This Skill

| Situation | Use this skill? |
|---|---|
| Pipeline is failing in production right now | ✅ Yes |
| Data is wrong in a production table | ✅ Yes |
| Scheduled job stopped running | ✅ Yes |
| Bug that will affect production next run | ⚠️ Partially — use if urgent, normal planning if not |
| Bug in development code | ❌ No — use debugging |

## Phase 1: Triage (< 5 minutes)

### Assess Impact

Answer these IMMEDIATELY:

| Question | How to check | Why it matters |
|---|---|---|
| What's broken? | Error logs, user report | Scope the incident |
| Who's affected? | Downstream consumers, dashboards | Determines urgency |
| When did it start? | Job history, Delta table history | Narrows root cause window |
| Is it getting worse? | Recurring failures, data corruption spreading | Determines if you need to stop the bleed first |

### Classify Severity

| Severity | Definition | Response time | Example |
|---|---|---|---|
| **P1 — Critical** | Production data wrong, users making decisions on bad data | Fix NOW | Gold table has wrong aggregations, dashboard shows garbage |
| **P2 — High** | Pipeline failing, data stale but not wrong | Fix today | Bronze ingestion failing, silver table not updating |
| **P3 — Medium** | Degraded but functional | Fix this week | Slow performance, partial data, non-critical pipeline |
| **P4 — Low** | Minor issue, workaround exists | Fix when convenient | Cosmetic, logging noise, non-blocking |

### Decide: Stop the Bleeding or Fix Root Cause?

| Situation | Action |
|---|---|
| Bad data is actively being consumed | **Stop the bleed** — disable pipeline, revert to last good state |
| Pipeline failing but old data is still correct | **Fix root cause** — old data is safe, take time to fix properly |
| Failure is intermittent | **Observe** — collect more data before acting |

## Phase 2: Stop the Bleeding (if needed)

### Revert to Last Known Good State

```sql
-- Delta time travel: restore table to before the incident
RESTORE TABLE catalog.schema.table TO VERSION AS OF [version_number]

-- Find the right version
DESCRIBE HISTORY catalog.schema.table LIMIT 10
```

### Disable the Failing Pipeline

```python
# If it's a scheduled job:
# 1. Pause the job in Databricks Workflows UI
# 2. Or disable the trigger

# If it's writing bad data:
# 1. Revoke write permissions temporarily
# 2. Or add a circuit breaker (empty source → skip write)
```

### Communicate

Tell affected stakeholders:
- What's broken
- What you're doing about it
- When you expect resolution
- Whether current data is reliable

## Phase 3: Diagnose Root Cause

Now that the bleeding has stopped, investigate properly.

### Narrow the Window

```python
# When did it last work?
# Check Delta history for last successful write
spark.sql("DESCRIBE HISTORY catalog.schema.table LIMIT 10").show(truncate=False)

# What changed between "last worked" and "first failed"?
# Check: code changes, config changes, upstream data changes, cluster changes
```

### Common Production Failure Causes

| Category | Examples | How to check |
|---|---|---|
| **Upstream change** | Schema drift, column rename, new null values | Compare current source schema to expected |
| **Data volume spike** | 10x more rows than usual, OOM | Count source rows, check file sizes |
| **Config change** | Wrong table name, missing secret, changed path | Check all config values against production |
| **Code regression** | Recent deploy broke something | Check git log for recent changes |
| **Infrastructure** | Cluster size, DBR version change, network | Check cluster config, Databricks workspace events |
| **Permissions** | Service principal lost access, token expired | Check access to source tables, storage |
| **Dependency** | Package version changed, API deprecated | Check installed versions, API status |

### Diagnostic Tools

Use the narrow operational collector that answers the current question rather than
collecting everything. Compose the resulting evidence with `anchor("incident_snapshot", ...)`.
Persist aggregates and metadata only—never raw table or change-data-feed rows,
credentials, signed URLs, or unbounded plans/logs. Link the durable snapshot to the
active Problem Record so later agents can inspect the evidence instead of reconstructing
it. A collector status of `unavailable`, `denied`, or `failed` records uncertainty; it
does not prove that the system is healthy.

```python
# Check source data health
anchor("profile_table", source_df, subject="catalog.schema.source")

# Check for known errors
anchor("known_error", "exact error text from logs")
anchor("known_bad", error_text="pipeline failure description", task_type="incident")

# Check recent changes
anchor("session_diff")  # If you have a session
```

## Phase 4: Fix

### Rules for Production Fixes

1. **Minimal change** — fix the bug, nothing else. No refactoring, no improvements.
2. **Test before deploying** — even under pressure, run at least one verification.
3. **Reversible** — prefer fixes you can undo (config change > code change > schema change).
4. **No heroics** — if you're not confident, escalate. A bad fix on top of a bad situation is worse.

### Fix Verification

```python
# After fixing:
# 1. Run the pipeline on a sample or recent batch
# 2. Compare output to expected (row count, values, schema)
# 3. Check downstream tables/dashboards

# For data fixes:
# Compare restored data to previous good version
anchor("diff", current_df, expected_df, keys=["primary_key"])
```

## Phase 5: Post-Mortem

After the incident is resolved, document it. This is NOT optional — it prevents recurrence.

### Post-Mortem Template

```markdown
# Incident: [Brief Title]

**Date:** YYYY-MM-DD
**Duration:** [start time] to [resolution time]
**Severity:** P1/P2/P3/P4
**Affected:** [what was impacted]

## Timeline

| Time | Event |
|---|---|
| HH:MM | [First sign of problem] |
| HH:MM | [Incident detected by ...] |
| HH:MM | [Bleeding stopped by ...] |
| HH:MM | [Root cause identified] |
| HH:MM | [Fix deployed] |
| HH:MM | [Verified resolved] |

## Root Cause

[What actually went wrong — be specific]

## Fix Applied

[What you changed to resolve it]

## Prevention

| Action | Owner | Status |
|---|---|---|
| [What to do to prevent recurrence] | [Who] | [Done/TODO] |
| [Monitoring/alert to add] | [Who] | [Done/TODO] |
```

### Assess Learning

```python
# Only when the incident evidence supports a reusable observation:
observation = anchor("learning", "capture", observation_type="near_miss", ...)
anchor("learning", "assess", outcome="observations_recorded",
   observation_ids=[observation["item"]["item_id"]])
# Otherwise assess nothing_reusable_learned without fabricating content.
```

## Integration with odibi-anchor

```python
# Incident response uses relaxed planning — speed matters
anchor("task", "production fix: pipeline failure in silver_queue",
    goal="restore pipeline functionality and data accuracy",
    mode="debugging",
    known_facts=["Error: ...", "Started failing at: ...", "Severity: P2"],
    constraints=["Minimal change only", "Must be reversible"],
    acceptance_criteria=["Pipeline runs successfully", "Output matches expected"])

# After fix
anchor("gate")
anchor("learning", "capture", ...)
anchor("learning", "assess", outcome="observations_recorded", observation_ids=[...])
```

## Anti-Patterns

| Don't | Do instead |
|---|---|
| Start fixing before understanding impact | Triage first — 2 minutes of assessment saves hours |
| Make multiple changes at once | One change, verify, then next |
| Skip communication | Tell stakeholders what's happening |
| "Improve" code while fixing | Fix the bug, nothing else |
| Skip post-mortem because "it's fixed" | If you don't document it, it WILL happen again |
| Panic-deploy without testing | Even a quick smoke test catches obvious mistakes |
