# Refactoring planning

> Preserved behavior-lock and incremental-proof technique.

Evidence-gathering checklist for refactoring tasks. Execute this checklist BEFORE calling
`anchor("task")`. Every item you complete becomes a `known_fact` or `constraint` in your plan.

Use this local reference when refactoring planning is material to the managed work item.

## Core Principle

Refactoring changes structure, not behavior. If the output of any function changes,
it's not a refactoring — it's a modification. Use [implementation](implementation.md) instead.

## Evidence Checklist — Execute In Order

### 1. Map the Impact Surface

```python
anchor("impact", target="file_or_function_to_refactor.py")
anchor("map", focus_file="file_to_refactor.py")
```

**Record as known_facts:**
- Every file that imports from the target
- Every function that calls the target function(s)
- Every module that depends on the target module
- The full dependency tree (who depends on what)

**Rule:** If you don't know every caller, you cannot safely refactor.
A "simple rename" that misses one caller is a production break.

### 2. Read ALL Affected Files

For EVERY file in the impact surface (not just the target):

```python
readFile("src/target_module.py")
readFile("src/caller_1.py")
readFile("src/caller_2.py")
readFile("tests/test_target.py")
```

**Record as known_facts:**
- Current structure of the target (classes, functions, imports)
- How each caller uses the target (which functions, which parameters)
- Import patterns used by callers (from X import Y vs import X)
- Test coverage (which functions have tests, which don't)

### 3. Define Behavioral Invariants

Before changing anything, define what MUST NOT change:

| Invariant | How to verify |
|---|---|
| Function X returns same output for same input | Existing test passes |
| Module's public API (exported names) | `dir(module)` unchanged |
| Callers don't need to change their code | All caller files untouched |
| No new dependencies introduced | Import list doesn't grow |
| Performance doesn't degrade | Timing within 10% of baseline |

**Record as constraints:**
- List of behavioral invariants
- Which tests verify each invariant
- What's in the public API that must be preserved

### 4. Assess Test Coverage

```python
anchor("test", changed_files=["target_file.py"])
```

**Record as known_facts:**
- Which functions have tests
- Which functions have NO tests (these are high-risk to refactor)
- Test file locations
- Test count and pass/fail status

**If test coverage is low (<50% of functions), record as risk:**
- "Low test coverage — refactoring without tests means regressions are undetectable"
- Consider: write tests FIRST, then refactor (two separate planned tasks)

### 5. Design the Refactoring Steps

Every refactoring should be a sequence of small, independently verifiable steps.
Each step should leave the code in a working state.

**Patterns:**

| Refactoring type | Step sequence |
|---|---|
| **Extract function** | 1. Write new function with extracted body, 2. Replace original body with call, 3. Run tests |
| **Rename** | 1. Add alias (old name → new name), 2. Update all callers to new name, 3. Remove alias, 4. Run tests |
| **Move to module** | 1. Copy to new module, 2. Add re-export from old module, 3. Update callers, 4. Remove re-export, 5. Run tests |
| **Split module** | 1. Create new module, 2. Move functions one at a time (with re-exports), 3. Update callers, 4. Clean re-exports, 5. Run tests |
| **Consolidate** | 1. Identify shared pattern, 2. Write unified version, 3. Replace one caller at a time, 4. Run tests after each |

**Rule:** Never make all changes at once. One function, one caller, one test run.

### 6. Bound Historical Context

**Record as known_facts:**
- Current authoritative records describing fragile areas
- Dependencies verified from the current code and impact map

After task acceptance, review only its bounded `memory_context` for advisory prior refactoring
patterns. Do not issue a pre-task full-store/tag scan or force selected advice into the plan.

## Output — What You Feed to `anchor("task")`

After completing this checklist, you should have:

```python
known_facts = [
    "Target: src/dispatcher.py — extract _resolve_action() from dispatch() (52-line function)",
    "Callers: bootstrap.py (line 45), safe_change_context.py (line 120), 3 test files",
    "Import pattern: all callers use 'from odibi_anchor.dispatcher import dispatch'",
    "Public API: dispatch() is the only exported function — must keep same signature",
    "Test coverage: 8 tests cover dispatch() — all passing",
    "No tests for internal logic (the part being extracted)",
    "_resolve_action() will be module-private (underscore prefix)",
]

constraints = [
    "dispatch() signature must not change: dispatch(action, *args, **kwargs)",
    "dispatch() return value must not change for any input",
    "No new dependencies — _resolve_action() uses only existing imports",
    "All 8 existing tests must pass without modification",
    "Callers must not need any changes",
]

acceptance_criteria = [
    "dispatch() delegates to _resolve_action() for action resolution",
    "_resolve_action() is <20 lines (extracted cleanly)",
    "dispatch() is <35 lines (down from 52)",
    "All 8 existing tests pass",
    "No caller files modified",
    "anchor('preflight') passes",
]

in_scope = ["Extract _resolve_action() from dispatch()"]
out_of_scope = ["Renaming dispatch()", "Changing dispatch() behavior", "Adding new features"]

risks = [
    "If extraction changes subtle control flow (early returns, exceptions), behavior may change",
    "Mitigation: run tests after extraction, before any cleanup",
]
```

Continue the accepted work item using the gathered evidence.
## Formal Spec compliance
If accepted task policy requires a formal Spec, use `writing-specs`, include its
compliance requirements, and review the exact linked Spec to a rating ≥ good
before consequential effects. Do not infer a Spec requirement from mode alone.
