# Self-review checklist

Individual edits can each be correct and the combination can still be wrong.

You verified each edit in isolation. Each one passed. But edit #3 added a function call,
edit #1 never imported it. Edit #5 renamed a parameter, edit #7 still uses the old name.
Edit #2 changed return type from `list` to `dict`, but the caller in a file you didn't
re-read still unpacks it as a list. Every edit was "correct." The changeset is broken.

`verify-every-edit` catches per-edit errors. Self-review catches **cross-edit errors** —
the ones that only become visible when you look at the total diff as a single unit.

## The Non-Negotiable Sequence

Before every `anchor("gate")`, this exact sequence. No exceptions. No reordering.

### Step 1: RUN `anchor("diff")`

Get the complete session diff. Every file, every hunk, in one view.

### Step 2: READ the entire diff

Top to bottom. Every file. Every hunk. Not a skim — a read. You're looking at
the changeset as a reviewer would, not as the author who "knows what they changed."

### Step 3: CHECK for these specific issues

**Cross-file consistency:**
- **Missing imports** — added usage but forgot the import
- **Unused imports** — removed usage but left the import
- **Inconsistent naming** — renamed in one place, old name in another
- **Partial refactors** — renamed a function in definition but not all call sites
- **Cross-file consistency** — changed a return type but callers still expect old type
- **Forgotten test updates** — changed behavior but tests still assert old behavior

**Code hygiene:**
- **Copy-paste artifacts** — duplicated code with old variable names still in it
- **Debug artifacts** — print statements, hardcoded values, commented-out code left behind
- **Missing docstring updates** — changed function signature but docstring describes old params
- **Off-by-one in the changeset** — edited the wrong function, edited the wrong file

**Universal anti-patterns (from real bugs caught in this codebase):**
- **Boundary condition mismatch** — error message says "Maximum: N" but the check uses `>=` (blocks at N, effective limit is N-1). Verify `>` vs `>=` matches the stated limit
- **Unbound variables** — variable assigned inside a conditional branch but used outside it. Initialize before the branch or restructure
- **Nested ternary one-liners** — chained inline conditionals that extract from nested dicts. Break into named intermediate variables
- **Dead code branches** — conditions that can never be reached due to earlier `continue`/`return`. If Strategy 2 falls through, Strategy 3's guard is always true — delete the unreachable else
- **Single-value where list expected** — function accepts a string but the downstream consumer needs a list of strings (e.g., `cmd.append(target)` when target could be multiple paths). Accept both types or document which
- **Stale test assertions** — tests that assert implementation details (e.g., exact source patterns like `"MANIFEST = _json.load("`) break silently when the implementation is refactored. Assert behavior or public API, not internal wiring. After any refactor, grep tests for old patterns
- **Unmocked guards** — mocking a function but not the guard that controls whether it's called. If production code checks `shutil.which("tool")` before calling `_run_tool()`, mocking only `_run_tool` does nothing when the tool isn't installed — mock the guard too

**Conditional portability and platform checks:**
- **Python path serialization** — when Python code serializes relative paths as portable keys,
  URLs, manifests, or cross-platform output, prefer `path.relative_to(root).as_posix()`.
  Ordinary `Path.relative_to()` calls that remain path objects do not need normalization.
- **Python text persistence** — when Python code writes repository or interchange text, pass
  `encoding="utf-8"` unless repository tooling or an external contract requires another encoding.
- **Environment portability** — when code or tests are intended to run outside a configured
  Databricks workspace, reject hardcoded `/Workspace/...`, `/dbfs/...`, and mount paths in favor
  of configuration or portable resolution. Explicit Databricks deployment resources may use
  platform paths when the repository contract requires and tests them.
- **Databricks/Spark checks** — apply DBFS, Unity Catalog, Spark Connect, notebook, and runtime
  checks only when changed paths, dependencies, or the canonical task domain establish that
  platform. Generic prose mentioning Spark or a table does not establish applicability.

### Step 4: VERIFY the changeset tells a coherent story

Could someone reading ONLY the diff understand what was done and why? If the diff
looks like three unrelated changes, you have a scope problem. If the diff contradicts
itself, you have a consistency problem. Both are blockers.

### Step 5: PROCEED to `anchor("gate")`

Only if all checks pass. If anything fails, fix it first. Then re-run `anchor("diff")`
and review again. Do not fix-and-gate without re-reviewing.

## The Checklist

Run through this before every gate. Every box must be checked.

```
[ ] Every new usage has a corresponding import
[ ] No orphaned imports from deleted code
[ ] Names are consistent across all changed files
[ ] Tests cover the new/changed behavior
[ ] No debug artifacts (print, hardcoded paths, TODO hacks)
[ ] Docstrings match current signatures
[ ] Return types match what callers expect
[ ] The diff makes sense as a unit
[ ] Boundary checks (>= vs >) match their error messages
[ ] No variables used outside the branch where they're assigned
[ ] No nested ternaries deeper than one level
[ ] No unreachable code after continue/return
[ ] Functions that accept paths handle both str and list
[ ] Tests assert behavior, not implementation details
[ ] Mocks cover guards (shutil.which, os.path.exists) not just the guarded function
[ ] Applicable Python path serialization and text encoding are portable
[ ] Applicable environment/Databricks/Spark checks follow concrete repository or task evidence
```

## Anti-Rationalization: Self-Review Edition

| What you're thinking | Why it's wrong | What to do instead |
|---|---|---|
| "The diff is small, I don't need to review it" | Small diffs have small review cost. A one-line diff with a wrong import crashes just as hard. | Review it. 10 seconds. |
| "I verified each edit individually" | Per-edit verification checks each edit against its file. Self-review checks edits against EACH OTHER. Different failure modes. | Per-edit verification ≠ self-review. Both are required. |
| "I'll review at the end" (multiple features) | By "the end" the diff is 200 lines across 12 files and you won't actually review it. You'll skim. | Review per feature, before each gate. |
| "The tests will catch any issues" | Tests catch behavioral regressions. They don't catch unused imports, stale docstrings, inconsistent naming, or debug artifacts. | Tests catch runtime errors. Self-review catches everything else. |
| "I know what I changed" | You know what you INTENDED to change. The diff shows what you ACTUALLY changed. Those diverge more often than you think. | Read the diff. Trust the diff over your memory. |
| "The gate will catch problems" | Gate checks compliance and test results. It doesn't read your diff for cross-file consistency. That's YOUR job. | Gate is not a substitute for review. Gate checks process. You check correctness. |
| "It's just a rename/refactor" | Renames are the #1 source of partial refactors — changed in definition, missed in 2 of 7 call sites. | Renames ESPECIALLY need self-review. Search the diff for both old and new names. |
| "I'm running out of context, skip the review" | Gating broken code to save context means the NEXT session inherits your mess. You're not saving time, you're transferring cost. | If context is tight, review is MORE important — you can't afford a fix-up session. |

## The Meta-Rule

```
If you wrote the code, you're the worst person to judge it —
and also the only person available.

Self-review is how you overcome author bias.
The diff doesn't care what you intended. It shows what you did.
Read it like a stranger would.
```

## Anchor Self-Review Workflow

```python
# Step 1: See all changes before gate
result = anchor("review")
# → Shows diffstat, untested files, uncovered changes, gate-readiness

# Step 2: Check specific file diff
result = anchor("session_diff", target="src/transformers/dedup.py")
# → Shows exact lines changed this session

# Step 3: Verify no drift from intent
result = anchor("diff", old_df, new_df, keys=["id"])
# → For data changes: shows row-level differences

# Step 4: Gate (runs review internally)
result = anchor("gate")
```

**NEVER skip `anchor("review")` before `anchor("gate")`** — gate checks that review was performed.
**NEVER deliver without reading the diff** — author bias means you remember intent, not implementation.
**NEVER assume your edit did what you intended** — read back the changed lines explicitly.

### Self-Review Checklist (run mentally on every diff)

| Check | Question | Tool |
|---|---|---|
| Intent match | Does the diff do what I intended, nothing more? | `anchor("session_diff")` |
| Accidental deletions | Did I remove code I didn't mean to? | `anchor("session_diff")` |
| Test coverage | Are changed functions covered by tests? | `anchor("review")` |
| Naming | Are new names self-documenting? | Manual read |
| Edge cases | Did I handle nulls, empty, error paths? | `anchor("validate", df, rules=[...])` |
