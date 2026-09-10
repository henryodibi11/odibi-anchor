# Debugging-planning reference

> Preserved evidence-first diagnostic planning technique.

Evidence-gathering checklist for debugging tasks. Execute this checklist BEFORE calling
`anchor("task")`. Every item you complete becomes a `known_fact` or `constraint` in your plan.

Use this reference only after `debugging` owns the requested diagnostic outcome.

Use the owning debugging skill's backbone for domain-specific diagnostic sequences
(row count mismatch, null investigation, duplicate investigation, etc.).

## Determine Bug Type

| Type | Signal | Evidence focus |
|---|---|---|
| **Code error** (exception/traceback) | Stack trace, error message | Error text, file:line, input state |
| **Wrong data** (no error, wrong output) | Values don't match expectations | Expected vs actual, which stage diverges |
| **Performance** (too slow, OOM) | Timeout, memory error | Data size, operation type, execution plan |

## Evidence Checklist — Code Errors

### 1. Capture the Exact Error

```python
anchor("trace", "paste full traceback here")
```

**Record as known_facts:**
- Exact error type and message (e.g., `KeyError: 'project_id'`)
- File and line number where it occurred
- The full call chain (which function called which)

**Rule:** Copy-paste the exact error. Do not paraphrase.
"It threw a KeyError" is useless. `KeyError: 'project_id' at silver_queue.py:45` is actionable.

### 2. Check Known Error Patterns

```python
anchor("known_error", "exact error text")
```

**Record as known_facts:**
- Has this error been seen before? If yes, what was the fix?
- Does the current error match the known pattern's exact inputs and call path?

Treat a known pattern as a hypothesis until current evidence verifies that it matches. After task
acceptance, the bounded task `memory_context` may add advisory failure patterns; do not query the
full store or force a prior fix.

### 3. Read the Failing Code

```python
readFile("path/to/failing_file.py")
```

**Record as known_facts:**
- The function that throws the error (full source)
- What inputs it expects (parameter types, dict keys)
- What it does with the inputs before the failure point

### 4. Trace the Input

Work backwards from the error to find what produced the bad input:

```python
anchor("map", focus_file="path/to/failing_file.py")  # Who calls this function?
```

**Record as known_facts:**
- What calls the failing function
- What data/arguments the caller passes
- Where the caller gets its data from

### 5. Identify the Root Cause

The root cause is NOT "line 45 threw KeyError." The root cause is WHY the key is missing.

| Surface error | Root cause pattern |
|---|---|
| KeyError / missing column | Upstream schema changed, column renamed, or dict key misspelled |
| TypeError / wrong type | Caller passes string where int expected, or None where value expected |
| ValueError | Data doesn't match expected format (date parsing, enum values) |
| AttributeError | Object is None (missing initialization) or wrong type (duck typing failure) |

**Record as known_facts:**
- Root cause (not just the error, but WHY it happened)
- What needs to change to fix it
- What else might be affected by the same root cause

### 6. Define Fix Scope

**Record as constraints:**
- Fix the root cause, not the symptom (no try/except to silence errors)
- If the root cause is upstream, fix upstream (don't patch downstream)
- If multiple places have the same bug, fix all of them (check with `anchor("map")`)

**Record as acceptance_criteria:**
- Error no longer occurs with the same input
- No new errors introduced (existing tests pass)
- Root cause cannot recur (guard added, or source fixed)

## Evidence Checklist — Wrong Data (No Error)

### 1. Define Expected vs Actual

**Record as known_facts:**
- What the output SHOULD look like (expected row count, values, schema)
- What the output ACTUALLY looks like (actual row count, values, schema)
- Specific examples of wrong rows/values

**Rule:** "The data looks wrong" is not specific enough.
"Expected 1,234 rows, got 987. Missing rows have project_status='Withdrawn'." is actionable.

### 2. Isolate the Stage

Run counts at each pipeline stage to find where data diverges:

```python
# Count at each stage
source_count = spark.table("source").count()
after_step1 = df_step1.count()
after_step2 = df_step2.count()
target_count = spark.table("target").count()
print(f"Source: {source_count} → Step1: {after_step1} → Step2: {after_step2} → Target: {target_count}")
```

**Record as known_facts:**
- The exact stage where counts/values diverge
- The operation at that stage (JOIN, FILTER, CAST, etc.)
- Before/after comparison at that stage

### 3. Load the Debugging Playbook

```
Load skills/debugging/SKILL.md
```

Use the triage decision tree to match your symptom to a diagnostic sequence.
Execute the diagnostic sequence for your specific symptom.

**Record as known_facts:**
- Diagnostic findings from the playbook
- Root cause identified
- Fix strategy from the common culprits table

## Output — What You Feed to `anchor("task")`

After completing this checklist, you should have:

```python
known_facts = [
    "Error: KeyError: 'project_id' at src/silver_queue.py:45",
    "Root cause: upstream bronze table renamed column from 'project_id' to 'proj_id'",
    "Callers: pipeline_notebook calls process_queue() which calls the failing function",
    "Known pattern: similar rename happened with 'status' column on 2026-05-10",
    "Fix: update column reference in silver_queue.py line 45",
    "Also affected: silver_queue.py line 72 references same column",
]

constraints = [
    "Fix root cause, not symptom — no try/except to catch KeyError",
    "Check all column references in silver_queue.py, not just line 45",
    "Must verify with actual data, not just syntax check",
]

acceptance_criteria = [
    "Pipeline runs end-to-end without error",
    "Output row count matches expected (1,234 rows)",
    "All existing tests pass",
    "New test covers the renamed column scenario",
]
```

Continue the accepted task using the gathered evidence.
