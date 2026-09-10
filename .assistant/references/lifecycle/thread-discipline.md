# Thread discipline

> Lifecycle detail supporting the global operating contract; it is not a discoverable skill.

Keeps threads focused, short, and transparent. Prevents scope creep, context rot, and autonomous decision-making without user consent.

Use this lifecycle detail with Odibi Anchor action documentation when extended thread guidance is needed.

## 1. Thread Scope Rules

| Rule | Threshold | Action |
|---|---|---|
| One coherent task per thread | Default | Keep approved related work together; hand off an independent outcome when separation improves review or safety |
| Multi-feature session | Independent outcomes need coordination | Use a session notebook when durable cross-thread coordination is useful |
| File or module breadth | Work no longer has one coherent review boundary | Split or checkpoint at the smallest safe boundary |
| Thread length | Context degradation threatens correctness | Prepare a concise handoff rather than imposing an arbitrary action quota |
| New request | It conflicts with or materially expands the approved outcome | Finish or safely checkpoint current work, then hand off the new scope |

### Scope Change Detection

If you're editing a file that was NOT in your plan's `in_scope`, STOP and evaluate:

| Signal | What it means | Action |
|---|---|---|
| Editing a file not mentioned in `anchor("task")` plan | Scope is creeping | Ask user: "This file wasn't in the plan. Should I add it or defer?" |
| Second `anchor("task")` call in same session | New task emerged | Save current state, hand off the new task |
| User asks for something unrelated to current goal | Scope change | Complete or checkpoint current task first |
| Fix reveals a second bug in a different module | Scope creep | Hand off bounded evidence; retain learning only if reusable |

**Structural check:** `anchor("safe")` now detects if no `anchor("task")` was run this session and emits a MUST warning.
Use `anchor("status")` to see if your session is on track — it shows planning, testing, and thread length compliance.

**Check mid-session:**
```python
from odibi_anchor._utils._session_state import _SESSION_TIMINGS
print(f"Actions so far: {len(_SESSION_TIMINGS)}")
```

If threshold exceeded, tell the user:
> "This thread has {N} actions across {M} files. Recommend handing off to a fresh thread to avoid context degradation. Want me to prepare a handoff?"

## 2. User Involvement Gates (Anti-Vibe-Coding)

The user is part of the process, not a rubber stamp. Ask only for genuine decisions,
approval-boundary effects, or materially ambiguous scope.

| Gate | What to show | Then |
|---|---|---|
| **After planning** | Plan summary at the depth warranted by risk | Continue within existing approval; ask if a genuine decision remains |
| **After implementation** | Concise changes and verification | Continue to gate unless review is an explicit approval boundary |
| **Before shared effects** | Proposed irreversible or shared action | Obtain the approval required by the existing effect boundary |
| **On material uncertainty** | Genuine decision options and tradeoffs | Follow the existing human-input guidance; don't invent alternatives |

**Hard rule:** Ask before an unresolved architecture decision changes the approved scope,
compatibility contract, or effect boundary. Continue when the architecture choice is already
specified and approved.

**Anti-patterns to avoid:**
- Hiding a material departure from the approved plan until after implementation
- Silently choosing between approaches when the choice changes an unresolved material tradeoff
- "I went ahead and refactored X while I was in there" — never do this
- Seeking approval for routine commits already covered by the approved delivery boundary

## 3. Handoff Protocol

### Terminal notification

When the thread reaches its final completed, safely stopped, or blocked outcome, call
`notify_human()` exactly once if the configured Slack variables are present. Include the
task, terminal status, result, verification, and PR or blocker. Do not notify at an
intermediate checkpoint or while approved work remains. After delivery, still return
the normal final response through the execution harness. Catch `HumanInputError` and
report its class in that final response; notification failure does not undo completed
work.

For a completed task, deliver a concise outcome, changed scope, verification, and residual
concerns. Use the detailed protocol below only for resumable work where another thread
would otherwise need to re-derive state.

When handing off resumable work (context getting long, blocked, or scope exceeded):

```python
# 1. Capture what changed
anchor("session_diff")

# 2. Keep genuine reusable findings as session evidence. Close a learning assessment
# only after a successful gate creates the obligation.

# 3. Hand off with detailed state
anchor("snapshot", mode="handoff", summary="task description",
    state="in_progress",  # or "blocked", "complete"
    decisions=[
        "Decision 1 and why",
        "Decision 2 and why",
    ],
    next_steps=[
        "Specific testable step 1",
        "Specific testable step 2",
    ],
    artifacts=[
        {"path": "path/to/file1.py", "role": "source", "note": "modified"},
        {"path": "path/to/file2.py", "role": "test", "note": "modified"},
    ],
    evidence_chain=[
        {"tool": "pytest", "summary": "focused tests pass", "status": "verified"},
    ],
)
```

### Handoff state must include:

| Field | Required | Content |
|---|---|---|
| Exact current state | ✓ | What works, what doesn't, what's partially done |
| Files modified | ✓ | Full relative paths |
| Decisions made | ✓ | What was decided AND why |
| Next steps | ✓ | Specific, testable actions for the next thread |
| Compiled details | ✓ | Function signatures, error messages, parameter values — anything the next thread would otherwise have to re-derive |

**Rule:** The next thread should not need to re-derive material non-obvious state.

## 4. Context Degradation Prevention

| Trigger | Action |
|---|---|
| Material non-obvious finding | Retain bounded session evidence; evaluate reuse at closure |
| Re-reading the same file | Improve resumable notes if material state was missing |
| Mid-session | Keep only evidence needed for the current decision or handoff |
| Session end | Assess genuine reusable learning or close with `nothing_reusable_learned` |

**Record a genuinely reusable finding only when warranted:**
```python
# Preserve evidence now; authority remains candidate-only until assessed.
observation = anchor("learning", "capture", observation_type="reusable_practice", ...)
# At closure, assess the returned Observation ID with the rest of the task evidence.
```

**Rule:** Retain a bounded finding only when it is genuinely reusable; routine work creates no item.

## 5. Scope Reduction Patterns

Before starting any task, ask:

> "Can this be done in fewer steps?"

| Pattern | Apply when | Action |
|---|---|---|
| Single-fix focus | Fix reveals a second issue | Save second issue to memory, hand off — don't scope-creep |
| Minimal diff | Editing a file | Change only what's needed — don't refactor neighbors |
| Smallest testable unit | Planning implementation | Break into pieces that can each be verified |
| One thing well | Tempted to fix three things | Fix one, verify, then decide if the thread has capacity for more |

**Anti-patterns:**
- "While I'm in here, I'll also fix..." — NO. Hand it off.
- "This would be cleaner if I also refactored..." — NO. Separate thread.
- "I found two more bugs" — Hand off bounded evidence and focus on the original task.

## 6. Commit Convention

After `anchor("gate")` passes, draft a commit message. Request approval only when commit or
push remains an unresolved effect boundary; otherwise proceed within existing authorization.

**Format:** Conventional commits — `type(scope): description`

**Types:** `fix`, `feat`, `refactor`, `test`, `docs`, `chore`, `perf`

**Template:**
```
type(scope): concise description of what changed

Files: list of changed files
Tests: X passed, Y failed
Gate: risk_level=low|medium|high, obligations status
```

**Example:**
```
fix(memory): align FTS5 query tokenization with OR semantics

Files: src/odibi_anchor/codebase/_memory_db.py
Tests: 93 passed, 0 failed
Gate: risk_level=low, all obligations paid
```

**Rules:**
- Never push without the authorization required by the existing effect boundary
- Body must list: files changed, tests run, gate status
- Scope should match the module or component touched
- If gate failed or has warnings, include them in the body

## 7. Bootstrap Non-Negotiable

Before substantive Anchor-governed work, execute the repository-root
`agent_bootstrap.py` through the canonical same-process recipe in
`references/odibi-anchor/workflow.md`. Resolve one exact checkout from explicit
or bounded host repository metadata; never derive it from cwd or scan a user tree.
Successful structured bootstrap/orientation—not merely locating source—is the
precondition.

| Scenario | Action |
|---|---|
| Anchor-governed code work | Bootstrap before substantive work and retain the returned dispatcher |
| User provides plan from another thread | Bootstrap FIRST, then load and execute the plan |
| "Just a quick fix" | Bootstrap FIRST — no exceptions |
| Anchor-requested read-only exploration | Bootstrap, use bounded orientation, and avoid synthetic write-task ceremony |
| Ordinary non-Anchor read-only question | Gather proportional evidence without manufacturing Anchor lifecycle actions |

**Pattern:** `bootstrap → load plan from user → execute`

If required bootstrap fails, report the actual failure. Do not continue as though Anchor
initialized, and do not use a subprocess whose state disappears before later calls.

## Enforcement Summary

This discipline is supported by Odibi Anchor tooling:

| Mechanism | What it catches |
|---|---|
| `_SESSION_TIMINGS` length check | Thread too long (§1) |
| User approval gates (§2) | Autonomous decisions without consent |
| `anchor("snapshot", mode="handoff")` protocol (§3) | Lost context between threads |
| Bounded evidence + conditional Observation capture (§4) | Context degradation without forced memory creation |
| Scope reduction questions (§5) | Scope creep before it starts |
| `anchor("gate")` + commit template (§6) | Unreviewed commits |
| Bootstrap check (§7) | Operating without context |

Before ending or switching a thread, close any post-gate obligation through explicit
structured assessment, including truthful no-learning. Never invent closure content merely
to satisfy the lifecycle.
