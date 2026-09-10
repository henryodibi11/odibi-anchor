# Compliance gate protocol

> Lifecycle detail supporting the global operating contract; it is not a discoverable skill.

Use this checklist when you're about to deliver. This is the pre-delivery verification protocol
with exact commands, expected outputs, and failure recovery.

**When to load:** After code is written and tests pass, BEFORE declaring "done."

## The Delivery Sequence

Execute these in order. Each step has a hard gate — skipping causes RuntimeError.

```
task → edit → touched → preflight → test → review → gate → truthful assessment
```

### Step 1: Self-Review (REQUIRED before gate)

```python
# Review complete changeset — gate BLOCKS if this hasn't run
result = anchor("review")
# OR: anchor("diff") also satisfies the review requirement
```

**Expected output:**
```
# Session Review
## Files Changed (3)
  src/example.py (modified)
  src/odibi_anchor/_dispatcher/_gate.py (modified)
  tests/test_gate.py (created)
## Findings
  - 3 files touched, all registered via anchor("touched")
  - Preflight passed (0 issues)
  - Tests passed (12/12)
## Suggested Next Actions
  - MUST: Run anchor('gate') to finalize delivery.
```

**If review shows issues:** Fix them NOW. Do NOT proceed to gate with unresolved findings.

### Step 2: Gate (NO arguments — ever)

```python
# Gate derives ALL evidence from session state — never pass args
result = anchor("gate")
```

**Expected output (success):**
```
# Workflow Gate: PASSED ✓
## Evidence (auto-derived)
  - actions_taken: [status, memory, task, safe, touched, preflight, test, review]
  - files_changed: 3
  - obligations_paid: [preflight_context, test_context, review_context]
## Metrics
  - risk: low
  - compliance_score: 14/15
```

**Expected output (BLOCKED):**
```
RuntimeError: BLOCKED: anchor("gate") — no anchor("review") or anchor("diff") found in session.
Review your changeset before gating: anchor("review")
```

**Critical rules:**
- `anchor("gate")` takes NO arguments — it reads session_timings internally
- NEVER pass `actions_taken`, `files_changed`, or `obligations_paid` — they get stripped
- Gate verifies timing proof: tools must have ACTUALLY run, not just been claimed

### Step 3: Truthful Learning Assessment (REQUIRED after gate passes with file changes)

For structured learning, assess real Observation IDs or choose
`nothing_reusable_learned`. The complete content-quality and non-authority firewall is in
the global operating contract; gate and debt mechanics remain runtime-owned.

## Between Features: Checkpoint

After each discrete feature (before starting the next), run checkpoint:

```python
# Legacy-compatible atomic closure when genuine reusable content exists
result = anchor("checkpoint", label="phantom_ref_cleanup", learn_events=[
    {"type": "decision", "detail": "Replaced pre_join with validate+microscope across all skills"},
    {"type": "discovery", "detail": "10 phantom actions existed — skills taught non-existent tools"},
])
```

**Expected output (success):**
```
# Checkpoint 'phantom_ref_cleanup': PASS
## Steps
  - preflight: passed (0 issues)
  - test: passed (34/34)
  - gate: passed (risk: low)
  - learn: 2 events saved
```

**When checkpoint is REQUIRED:**
- After completing a discrete feature before starting the next
- Do NOT batch checkpoints to end of session — this defeats the purpose

**When checkpoint BLOCKS:**
- Historical checkpoint payloads require non-empty `learn_events` when files changed;
  use standalone gate plus structured no-learning assessment when no genuine event exists
- `skip_test=True` when `.py` files were changed → RuntimeError

## Gate Blockers (RuntimeError)

Gate checks these in order. First failure blocks — fix that one, then retry.

| Order | Check | Blocks When | Fix |
|---|---|---|---|
| 1 | Assessment debt | Previous gate passed without truthful assessment | Assess Observations or no reusable learning; follow a compatibility-only recovery prompt only when Anchor identifies persisted old state that technically requires it |
| 2 | Review | No `anchor("review")` or `anchor("diff")` in session | Run `anchor("review")` |
| 3 | Tests | Last `anchor("test")` failed or never ran for .py changes | Run `anchor("test")` and fix failures |
| 4 | Test coverage | Test target doesn't cover changed .py files | Run `anchor("test", target="tests/")` for the full suite |
| 5 | Filesystem drift safety | Unregistered Python drift matches a high-confidence known-bad pattern, or task policy forbids the changed documentation path | Revise the unsafe change, run the requested guard, register paths, and retry |

Other clean unregistered drift is automatically registered and surfaced in gate output;
ordinary output text never creates skill-loading debt.

## What NOT to Do

| Don't | Why | RuntimeError? |
|---|---|---|
| Pass arguments to `anchor("gate")` | Stripped silently — gate trusts only session state | No (stripped), but evidence won't count |
| Skip truthful assessment after gate | Next `anchor("task")` is blocked until assessment debt is paid | Yes — blocks next task |
| Batch checkpoints to session end | Defeats incremental compliance — issues compound | No, but compliance score drops |
| Run gate without review | Gate checks timing proof for review/diff | Yes — RuntimeError |
| Run gate without tests (for .py changes) | Gate verifies test coverage against changed files | Yes — RuntimeError |
| Fabricate `actions_taken` in kwargs | Keys are stripped — gate reads internal timings | No (ignored), but nothing counts |
| Assume out-of-band edits are invisible | Gate reconciles clean drift automatically and applies safety checks to unregistered Python changes | Only unsafe drift blocks |

## Pre-Gate Enforcement (blocks BEFORE gate)

These RuntimeErrors fire during normal editing — not at gate time:

| Gate | Triggers | When |
|---|---|---|
| Planning | `anchor("touched")`/`anchor("safe")`/`anchor("semantic")` without prior `anchor("task")` | Any file edit attempt |
| Spec | Consequential effect when accepted task policy requires an unreviewed Spec | Before the effect |
| Skill-load | A non-exempt substantive action begins without all task-derived direct skills registered | Before each such action |
| Edit limit | >12 file edits without `anchor("gate")` or `anchor("checkpoint")` | 13th edit |

## Quick Decision Guide

| Situation | Do This |
|---|---|
| Feature complete, .py files changed | `anchor("review")` → preflight/test → gate → truthful assessment |
| Feature complete, only .md/.json changed | `anchor("review")` → gate → truthful assessment |
| Multiple features in one session | Close each feature's gate and assessment before the next |
| Gate blocked — assessment debt | Close the prior assessment truthfully, then retry gate |
| Gate blocked — no review | `anchor("review")` then retry gate |
| Gate blocked — test failure | Fix code → `anchor("touched")` → `anchor("test")` → retry gate |
| Gate blocked — unsafe drift | Revise the known-bad/policy violation, run the requested guard, register paths, then retry |
| Read-only session (no edits) | No manufactured gate or learning obligation |

Manual closure remains supported as `review → preflight/test (for Python) → gate → assessment`.
Do not run a standalone gate and then checkpoint the same feature; a successful
checkpoint is self-contained and creates no later learn debt.

## Related invariants and references

| Authority | When | Why |
|---|---|---|
| Global self-review invariant | Before every gate | Structured self-inspection of changeset |
| Real-client scenarios | When building/modifying tools | Source-first validation before gate |
| Global immediate-verification invariant | During editing | Ensures every edit receives a focused check |
| Structured learning assessment | During closure | What to save and how to assess no reusable learning |

A successful gate creates assessment debt. Close it with `learning assess` using bounded
Observation IDs or explicitly choose `nothing_reusable_learned`.
Capture alone is not closure and content must never be invented.
