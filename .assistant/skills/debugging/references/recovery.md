# Error recovery workflow

> Preserved non-incident recovery technique.

When your edits cause cascading failures, tests go from passing to broken, or you realize
you're on the wrong path. This skill prevents the #1 agent failure mode: **digging deeper
into a bad approach instead of stepping back.**

## Triage — How Bad Is It?

| Severity | Signal | Response |
|---|---|---|
| **Syntax error** | `anchor("touched")` returns `syntax_check: FAILED` | Fix the specific syntax error — usually trivial |
| **Single test failure** | One test fails, others pass | Likely a real bug in your change — debug normally |
| **Cascading failures** | 3+ tests fail that were passing before your change | STOP — you broke something fundamental |
| **Nothing works** | Import errors, module won't load, all tests fail | STOP — likely a structural break (bad import, circular dep) |
| **Wrong approach** | Code works but you realize the approach is wrong | STOP — don't polish bad architecture |

## The STOP Protocol

When you hit cascading failures or realize you're on the wrong path:

### Step 1: STOP Editing

**Do NOT** make another edit to try to fix the cascade. Every edit on top of a broken
foundation makes the mess worse. Put down the keyboard.

### Step 2: Assess the Damage

```python
# What did I change?
anchor("session_files")  # All files touched this session

# What's the current state?
anchor("status")  # Obligations, risk level, compliance
```

**Answer these questions:**
1. How many files did I change?
2. Which change broke things? (Usually the most recent one)
3. Are the failures related to my change, or did I uncover a pre-existing issue?
4. Can I isolate the break to one specific edit?

### Step 3: Decide — Fix Forward or Rollback

| Situation | Action |
|---|---|
| One file changed, clear what's wrong | **Fix forward** — correct the specific error |
| Multiple files changed, unclear what broke | **Rollback to last known good state** |
| 3+ failed attempts to fix the same issue | **Rollback** — you're in a fix loop |
| Approach is fundamentally wrong | **Rollback everything** — re-plan from scratch |
| Pre-existing issue uncovered | **Stash your changes**, fix the pre-existing issue first |

### Step 4a: Fix Forward (When Appropriate)

Only if the break is isolated and you understand the root cause:

1. Identify the exact line/change that caused the break
2. Fix ONLY that issue — do not "improve" surrounding code
3. Run tests after the fix
4. If tests still fail → switch to Rollback (Step 4b)

**Rule:** Maximum 2 fix-forward attempts. After 2 failures, rollback.

### Step 4b: Rollback

```python
# Option 1: Undo last edit (if it was the only change)
# Use your editor's undo or git checkout for the specific file

# Option 2: Check session diff to see all changes
anchor("session_diff")  # Shows what changed since session start

# Option 3: Full file rollback
# git checkout -- path/to/broken_file.py
```

After rollback, verify:
1. Tests pass again (back to known good state)
2. No partial changes left in other files
3. Session state is clean

### Step 5: Re-Plan

After recovering, do NOT just retry the same approach:

```
🔄 RECOVERY PLAN
├── What went wrong?
│   ├── Wrong assumption: [what I assumed that was false]
│   ├── Missing context: [what I didn't know]
│   └── Approach flaw: [why the approach was wrong]
├── What's different now?
│   ├── [New fact learned from the failure]
│   └── [Constraint discovered]
└── New approach:
    └── [How to do it differently]
```

Then re-run `anchor("task")` with the new understanding as `known_facts`.

## Fix Loops — Detection and Escape

A **fix loop** is when you keep editing the same code to fix errors your edits introduced.

### How to Detect

You're in a fix loop if:
- You've edited the same file 3+ times for the same issue
- Each "fix" introduces a new error
- You're adding try/except or type checks to silence errors instead of fixing root cause
- You've been working on the same error for >10 minutes without progress

### How to Escape

1. **STOP immediately** — do not make another edit
2. **Rollback** to the last state where tests passed
3. **Read the code you were editing** — actually read it, don't skim
4. **Identify the root cause** — not the symptom, the actual problem
5. **Plan the fix as a single coherent change** — not incremental patches
6. **Make one edit, run tests, done** — if tests fail again, rollback and ask the user

## Common Recovery Scenarios

### Broke an Import Chain
**Symptom:** `ImportError` or `ModuleNotFoundError` cascading through multiple files
**Cause:** Usually a circular import, missing `__init__.py` update, or renamed module
**Fix:**
1. Check the import that changed
2. Verify the module/function exists at the expected path
3. Check for circular imports (A imports B imports A)
4. If circular: restructure to break the cycle (extract shared types to a third module)

### Broke Function Signature
**Symptom:** `TypeError: unexpected keyword argument` or `missing required argument` in callers
**Cause:** Changed a function signature without updating all callers
**Fix:**
1. `anchor("impact", target="changed_file.py")` — find all callers
2. Update every caller, or revert the signature change
3. Add backward-compatible defaults if possible

### Broke Data Contract
**Symptom:** `KeyError`, wrong column names, missing fields downstream
**Cause:** Changed output shape (dict keys, DataFrame columns, return type)
**Fix:**
1. Check what the callers expect (read the calling code)
2. Either match the old contract or update all consumers
3. Never silently change output shape

### Tests Pass but Output Is Wrong
**Symptom:** No errors, but results are incorrect
**Cause:** Logic error in your change — hardest to catch
**Fix:**
1. Add a test that asserts the correct output
2. Run it — it should fail (confirming the bug)
3. Fix the logic
4. Test passes — done

### Approach Is Fundamentally Wrong
**Symptom:** The more you code, the more problems emerge. Everything feels forced.
**Cause:** Wrong mental model, wrong decomposition, or wrong tool choice
**Fix:**
1. Rollback ALL changes from this approach
2. State explicitly what's wrong with the approach
3. Re-plan from scratch with the new understanding
4. Present the revised plan to the user before coding again

## Integration with odibi-anchor

```python
# After recovering, capture only a supported reusable observation; otherwise
# close learning with nothing_reusable_learned.

# Re-plan with new understanding
anchor("task", "description",
    goal="...",
    known_facts=["Previous approach failed because: ...",
                 "New constraint discovered: ..."],
    mode="implementation")
```

## Anti-Patterns

| Don't | Do instead |
|---|---|
| Make another quick edit to fix the cascade | STOP — assess damage first |
| Add try/except to silence errors | Fix the root cause |
| Keep going because "I'm almost there" | If you've been fixing for 10 min, you're not almost there |
| Blame the tests | Tests were passing before your change — the change is wrong |
| Skip re-planning after recovery | The failure taught you something — capture it in the new plan |
| Rollback and immediately retry the same approach | Something was wrong with the approach — change it |
