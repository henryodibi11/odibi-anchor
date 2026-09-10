# safe_change_context — Code Walkthrough

## Purpose

`safe_change_context` is the composable edit pipeline. Instead of the agent sequencing
five separate tool calls (known_bad → semantic_edit → touched → preflight → test_focus),
this builder chains them all into a single decision-point call. The agent decides *what*
to change; the pipeline handles safety, application, and verification.

It is invoked by the dispatcher as `anchor("safe", target="...", action="...")` and is the
**recommended** path for all `.py` file edits. `apply=False` (dry-run) is the default.

## Public Entrypoints

| Name | Signature | Role |
| --- | --- | --- |
| `safe_change_context` | `(root, *, target, action, function, verify, test, guardrail, apply, subject, output_format, frame, **edit_kwargs)` | Main builder |
| `render_safe_change_report` | `(ctx: dict) -> str` | Renders context dict as markdown |

## Pipeline Steps

```
Step 0a  Auto-map (if map not yet called this session)
Step 0b  known_bad_change_context (guardrail=True by default)
           -> if status=="block": return early with is_safe=False, skip edit
Step 1   semantic_edit_context(apply=False) — dry-run
Step 1b  known_bad_change_context(proposed_diff=diff_preview)
           — post-dry-run content guardrail, static anti-pattern scan on actual diff
Step 1c  Manifest constraint checks (.anchor_manifest.json):
           i.  forbidden_patterns  — substring match on added lines
           ii. sensitive_columns   — regex match on string literals in added lines
           iii.read_only_catalogs  — write-pattern detection near catalog names
           iv. max_table_rows_local — toPandas()/collect() near local limits
           v.  Blast radius check  — frame.code_context.codebase_map reverse graph
Step 2   Apply to disk (if apply=True and has_changes and not has_edit_error)
           File locking: fcntl.flock(LOCK_EX | LOCK_NB) on Unix
                         msvcrt.locking(LK_NBLCK) on Windows
           Lock sidecar: {target}.lock file
           Snapshot baseline: _SESSION_DIFF_BASELINES[target] saved before apply
Step 2b  touched() — syntax check + session tracking
Step 3   preflight_context(changed_files=[target]) (if verify=True and has_changes)
Step 4   test_focus_context(changed_files=[target]) (if test=True and has_changes)
```

## Overall Safety Assessment

| Condition | `metrics.is_safe` |
| --- | --- |
| has_changes and no edit error and preflight passed | True |
| edit error OR no changes | False |
| preflight failed | False |
| guardrail blocked | False (early return) |

## Output Contract

```python
{
    "kind": "safe_change_context",
    "subject": str,
    "summary": str,
    "metrics": {
        "action": str,
        "target": str,
        "has_changes": bool,
        "applied": bool,
        "preflight_passed": bool,
        "affected_test_files": int,
        "is_safe": bool,           # overall safety signal
        "function_not_found": bool,
        "guardrail_status": str,   # "ok" | "warn" | "block" | "skipped"
        "touched_syntax_check": str | None,
        "lock_conflict": bool,
        "static_patterns_matched": int,
        "manifest_violations": int,
        "sensitive_column_refs": int,
    },
    # Sub-results (None if step was skipped):
    "edit": dict,          # full semantic_edit_context output
    "preflight": dict,     # full preflight_context output
    "test_focus": dict,    # full test_focus_context output
    "guardrail": dict,     # full known_bad_change_context output
    "diff_preview": str,
    "findings": list[str],
    "risks": list[str],
    "samples": {},
    "suggested_next_actions": list[str],
}
```

## Worked Examples

### Example 1 — Dry-Run Rename Function (Default: apply=False)

```
Input:
  anchor("safe", target="lib/extractor.py",
     action="rename_function",
     function="extract_excel_structure",
     new_name="extract_sheet_structure")

Execution:
  Step 0b: known_bad_change_context(changed_files=["lib/extractor.py"],
                                    action="rename_function")
    -> status="ok" (no matching gotchas)
  Step 1: semantic_edit_context(apply=False, ...)
    -> diff_preview = "--- a/lib/extractor.py
+++ b/lib/extractor.py
@@ -12 @@
-def extract_excel_structure(...
+def extract_sheet_structure(..."
    -> metrics.has_changes = True
  Step 1b: known_bad_change_context(proposed_diff=diff_preview, ...)
    -> static_patterns check on diff -> no matches -> status="ok"
  Step 1c: manifest checks -> no forbidden patterns matched
  apply=False -> skip write
  Step 3: preflight_context(changed_files=["lib/extractor.py"])
    -> lint on current file (before apply) -> is_safe=True
  Step 4: test_focus_context -> affected_test_files=2

Output:
  metrics.is_safe = True
  metrics.applied = False
  metrics.guardrail_status = "ok"
  suggested_next_actions[0] = "MUST: Review diff, then apply with safe_change_context(..., apply=True)"
  diff_preview = "... full diff ..."
```

### Example 2 — Apply Blocked by Guardrail

```
Input:
  anchor("safe", target="lib/models.py",
     action="modify_logic",
     function="build_invoice",
     apply=True)

  Memory contains gotcha: {
    content: "build_invoice returns a malformed dict when discount=None — always set default",
    confidence: 0.9,
    confirmation_count: 3,
    tags: ["lib/models.py"]
  }

Execution:
  Step 0b: known_bad_change_context(changed_files=["lib/models.py"])
    score = 2.5 (file match + action match)
    confidence >= 0.8 AND confirmation_count >= 2 -> blocking = [gotcha]
    status = "block"
  -> early return (skip semantic_edit, apply, preflight, test_focus)

Output:
  metrics.is_safe = False
  metrics.has_changes = False
  metrics.applied = False
  metrics.guardrail_status = "block"
  risks = ["BLOCKED: ... known-bad pattern ..."]
  suggested_next_actions = ["MUST: Review blocking gotcha before proceeding",
                             "MUST: Address known-bad pattern, then retry with guardrail=False"]
```

## Notable Implementation Patterns

- **Two-phase guardrail**: step 0b checks memory before dry-run; step 1b re-scans the
  actual diff for static anti-patterns after dry-run. This means a change can pass the
  pre-edit check but still produce a warning after the content is generated.
- **Baseline snapshot before apply**: `_SESSION_DIFF_BASELINES[target]` is saved from disk
  before writing, so session diff comparisons always show the real before/after rather than
  the post-apply state.
- **File lock sidecar**: the lock is acquired on `{target}.lock`, not the target itself.
  This prevents concurrent writes without conflicting with editors that have the main file
  open. The lock is held only during the write operation (not the full pipeline).
- **Blast radius threshold**: if the project's codebase map (via `frame`) shows the target
  has 3+ dependents in the reverse dependency graph, a HIGH BLAST RADIUS warning is injected
  into `manifest_violations`. The threshold of 3 reflects the point at which test coverage
  verification becomes mandatory rather than advisory.
- **SILENT-OK for non-critical steps**: `auto-map` errors and `test_focus` failures are
  caught and silenced. These steps are advisory; only guardrail block and edit errors are
  surfaced as hard failures.
- **`suggested_next_actions` planning check**: if no `task` timing entry exists in
  `_SESSION_TIMINGS`, the first suggested action is a MUST to run `anchor("task")` before
  editing. This enforces the plan-before-edit constraint even when `safe` is called directly.
