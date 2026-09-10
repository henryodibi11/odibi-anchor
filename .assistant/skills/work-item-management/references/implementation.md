# Implementation planning reference

> Preserved implementation planning technique.

Evidence-gathering checklist for implementation tasks. Execute this checklist BEFORE calling
`anchor("task")`. Every item you complete becomes a `known_fact` or `constraint` in your plan.

Use this local reference when implementation planning is material to the managed work item.

## Evidence Checklist — Execute In Order

### 1. Identify Target Files

```python
anchor("map")  # or anchor("map", focus_file="path/to/suspected/file.py")
```

**Record as known_facts:**
- Exact file paths you will modify
- Exact file paths of files that depend on your targets
- Module structure (what imports what)

**If you can't identify the target files, STOP.** Ask the user for clarification.
Do not guess — wrong file = wasted iteration.

### 2. Read Target Files

For EVERY file you plan to modify, read it completely:

```python
readFile("src/module.py")
```

**Record as known_facts:**
- Current function signatures (name, params, return type)
- Existing patterns (how similar things are done in this file)
- Import statements already present
- Class structure if modifying a class
- Lines of code / complexity estimate

**Rule:** You must have read the file to claim you understand it.
"I know what's in that file" without having read it is fabrication.

### 3. Look Up Framework Functions

For every framework function you plan to use:

```python
anchor("lookup", "function_name")
```

**Record as known_facts:**
- Correct import path
- Full function signature (all params with types and defaults)
- Return type and shape
- Any gotchas from the lookup output

**Rule:** Do not guess function signatures. The framework has 162+ functions.
The one you think takes `(df, key)` might take `(df, *, key_columns: list)`.

### 4. Check Conventions

```python
anchor("convention", action="new_function", function_name="your_planned_function")
```

**Record as constraints:**
- Naming conventions for this module
- Parameter conventions (df-in/df-out, keyword-only args, etc.)
- Return value conventions
- Import style conventions

### 5. Define Acceptance Criteria

Before calling `anchor("task")`, write your acceptance criteria explicitly:

| Criterion | How to verify |
|---|---|
| Function produces correct output | Unit test with known input → expected output |
| Existing tests still pass | `anchor("test")` |
| No regressions in callers | `anchor("impact", target="file.py")` shows no broken deps |
| Code follows conventions | `anchor("preflight")` passes |
| Syntax is valid | `anchor("touched")` returns `syntax_check: "passed"` |
| Reviewable without library docs | Non-domain-expert can understand every function by reading only the file |

**Rule:** If you can't define how to verify "done", you don't understand the task yet.
Go back to the THINK step in the planning skill.

### 6. Identify Risks

| Risk category | What to check | Example |
|---|---|---|
| Breaking changes | Does this function have callers? | `anchor("impact")` — if callers exist, signature changes break them |
| Side effects | Does this modify state? | Writes to disk, modifies globals, mutates input df |
| Data contracts | Does output schema change? | Adding/removing columns breaks downstream consumers |
| Test coverage | Are there tests for this code? | If no tests, you're flying blind on regressions |
| Complexity | Is this a 5-line or 50-line change? | >20 lines = consider splitting |
| Library opacity | Does this use a library reviewers won't know? | Plan domain primer + inline annotations DURING impl, not after |

## Output — What You Feed to `anchor("task")`

After completing this checklist, you should have:

```python
known_facts = [
    "Target file: src/tables.py (read, 342 lines, 12 functions)",
    "Function to modify: apply_transform_context() at line 200",
    "Current signature: def apply_transform_context(df, plan_ctx, *, checkpoint=True)",
    "Returns: dict with keys 'df', 'steps_applied', 'checkpoints'",
    "Uses: pyspark.sql.DataFrame, no pandas",
    "Convention: df-in/dict-out, keyword-only after first positional",
    "Past learning: unpersist() must check is_cached first (gotcha from 2026-05-15)",
    "Callers: 2 callers found via anchor('impact') — both pass checkpoint=True",
]

constraints = [
    "Must not change existing function signature (callers depend on it)",
    "Must use TRY_CAST, never CAST for type conversions",
    "Must return same dict shape — adding keys is ok, removing is not",
]

acceptance_criteria = [
    "New parameter accepted without breaking existing callers (default value)",
    "Unit test covers new behavior with at least 2 edge cases",
    "anchor('preflight') passes with no warnings",
    "All existing tests still pass",
]

in_scope = ["Add max_retries parameter to apply_transform_context()"]
out_of_scope = ["Refactoring other functions in tables.py", "Performance optimization"]

risks = [
    "If default value chosen poorly, existing callers may see behavior change",
    "New parameter increases function complexity — keep implementation minimal",
]
```

## Post-Acceptance Memory Review

After `anchor("task")` accepts the task and before source edits, inspect only its bounded, scoped
`memory_context`. Acknowledge selected IDs, status, provenance, scope, and match reasons—or no
selections. Do not force irrelevant advice or scan the full store. Treat relevant gotchas,
decisions, prior successful patterns, and failure patterns as advisory context to verify, not as
pre-task evidence or authority.

Continue the accepted work item

When task policy requires a formal Spec, do not start consequential implementation
until `anchor("spec", "status", name)` shows it is reviewed and ready.
NEVER skip the evidence checklist — gate checks that planning preceded implementation. with these kwargs.
## Formal Spec compliance
If accepted task policy requires a formal Spec, use `writing-specs`, include its
compliance requirements, and review the exact linked Spec to a rating ≥ good
before consequential effects. Do not infer a Spec requirement from mode alone.
