# Proportional planning

> Preserved proportional planning technique.

Write work requires the runtime planning entry point, but reasoning depth scales with the
task. This recipe routes to mode-specific evidence gathering without overriding the
proportionality contract in `../SKILL.md`.

## 1. Universal Rule — Always `anchor("task")`

| Situation | What to do |
|---|---|
| Multi-file feature | `anchor("task", "description", goal="...", mode="implementation")` |
| Single-file fix | `anchor("task", "description", goal="...", mode="implementation")` |
| Bug fix | `anchor("task", "description", goal="...", mode="debugging")` |
| Data pipeline work | `anchor("task", "description", goal="...", mode="implementation")` |
| Refactoring | `anchor("task", "description", goal="...", mode="implementation")` |
| Test writing | `anchor("task", "description", goal="...", mode="testing")` |
| "Quick fix" | `anchor("task", "description", goal="...", mode="implementation")` |
| New tool / empty directory | `anchor("task", "description", goal="...", mode="greenfield")` |
| Already started without planning | **STOP** — plan now, then resume |

For write work, use `anchor("task")` with the fields relevant to the task and runtime contract.
Scale the reasoning depth according to the proportionality tiers in `../SKILL.md`.
The cost of planning a "simple" task is 30 seconds. The cost of skipping planning is a full
iteration cycle when assumptions are wrong.

## 2. The Planning Sequence — Execute In Order

Use the runtime-required steps and only the reasoning elements relevant to the selected tier.

### Step 1: Orient and frame the task

Inspect the structured bootstrap orientation, then frame the goal, scope, and material unknowns
needed to establish an accepted task in Step 4.

### Step 2: Think — Understand Before Planning

Answer these out loud before calling `anchor("task")`:

```
🧠 THINK
├── What is the user actually asking? (restate in one sentence)
├── What do I know?
│   ├── Files/tables involved
│   ├── Framework functions needed (from anchor("lookup"))
│   └── Current repository and authority context
├── What do I NOT know?
│   ├── Assumptions I'm making
│   ├── Things I need to verify
│   └── Ambiguities to clarify with user
└── What is the simplest version of "done"?
```

Prioritize hypotheses or uncertainties by risk and decision value. Use an issue tree only
when complexity warrants independent material branches, and seek the smallest sufficient
confirming and disconfirming evidence.

**Rules:**
- If you can't restate the goal in one sentence → ask the user
- Verify assumptions that could materially change the approach
- "What do I NOT know" is the most important question
- Do NOT skip this step — it's the difference between good and bad planning

### Step 3: Determine Mode and Gather Evidence

Based on the task, load the appropriate mode-specific planning skill and execute its evidence checklist:

| Task type | Load skill | Key evidence |
|---|---|---|
| Building features, modifying code, fixes | [implementation](implementation.md) | Target files, function signatures, patterns, imports |
| Debugging errors or wrong data | `debugging` when diagnosis owns the outcome | Error text, stack trace, reproduction steps |
| Data pipelines, transforms, ingestion | `data-onboarding` or `data-operations` when that intent is material | Source schema, target schema, keys, grain |
| Restructuring without behavior change | [refactoring](refactoring.md) | All callers, test coverage, public API surface |
| Comparing outputs, auditing, validating data | `data-reconciliation` when proof owns the outcome | Both sides, match definition, alignment |
| Writing or expanding tests | `writing-tests` when test authoring is explicit | Code under test, existing tests, mock strategy |
| Using an external library, API, or SDK | [integration](integration.md) | Docs, API surface, method signatures, working examples |
| Upgrading library, migrating API, changing framework | [migration](migration.md) | Old→new mapping, usage inventory, breaking changes |
| New tool/library from scratch (greenfield) | [greenfield](greenfield.md) | Test corpus, domain research, library benchmark, edge cases |

**Execute the skill's evidence checklist BEFORE calling `anchor("task")`.**
The evidence you gather becomes your `known_facts`, `constraints`, and `acceptance_criteria`.

### Step 4: Call `anchor("task")` with Relevant Context

```python
anchor("task", "description",
    goal="...",
    mode="implementation",  # or "debugging", "testing"
    known_facts=[
        "Target file: src/module.py (read at step 3)",
        "Function signature: def process(df: DataFrame) -> DataFrame",
        "Existing pattern: uses window functions for dedup",
    ],
    constraints=[
        "Must not break existing callers",
        "Must use TRY_CAST, never CAST",
    ],
    acceptance_criteria=[
        "All tests pass",
        "Row count matches source within 1%",
    ],
    in_scope=["modify process() to add date filter"],
    out_of_scope=["refactoring unrelated functions"],
    risks=["Schema drift if upstream changes column names"],
    guardrails={"max_files": 3, "must_test": True},
)
```

Provide the fields needed for a truthful, ready plan; do not add irrelevant content to fill a quota.

Once `anchor("task")` returns an accepted task, review only its bounded `memory_context` before
source edits. Acknowledge selected IDs and metadata or an empty selection set; do not substitute
a full-store scan or force irrelevant advice. Its `required_skills` identify the direct native
guidance selected from the accepted task. Read each listed SKILL.md and register it with
`anchor("skill_loaded", "name")` before substantive work.

When the accepted task policy requires a formal Spec, persist it with
`anchor("spec", "persist", task_result)` and review it with
`anchor("spec", "review", "NAME")`. Ordinary planning does not imply a formal Spec.
Description AND goal must each be ≥10 characters — RuntimeError if shorter.
Provide the decision-critical evidence from Step 3 without filling irrelevant fields.

### Step 5: Reach Readiness — Hard Floor at 40%

Readiness score ≥ 40% is the current runtime gate. Failed-readiness tasks (passed=False) do NOT satisfy
the planning gate — `anchor("task")` must return both `error=None` and `passed=True`.

Resolve material readiness gaps that could change the decision, scope, or safe approach.
Do not gather context solely to maximize a score.

**Iteration pattern:**
1. Read the `recommended_clarifications` and `evidence_gaps` from the planner output
2. For each gap, fill it using this priority order:
   - **Read the actual source file** (`readFile`) — concrete evidence, always best
   - **Run `anchor("lookup")` or `anchor("map")`** — framework/codebase facts
   - **State it as a `known_fact` or `constraint`** in the next `anchor("task")` call
   - **If genuinely unknowable, ask the user** — do NOT fabricate
3. Re-run `anchor("task")` with the additional kwargs filled in
4. Repeat until the runtime gate passes and material gaps are resolved or explicitly bounded

**Common gaps and how to resolve them:**

| Gap | Resolution |
|---|---|
| "Existing code patterns" | `readFile` the target file, pass structure as `known_facts` |
| "Target file paths" | `anchor("map")`, pass paths as `known_facts` |
| "Output contract" | `anchor("lookup", "function_name")` or read the module's return dict |
| "Stop conditions" | Add `constraints=["Stop if X", "Pause if Y"]` |
| "Prior work" | State "No prior work" or "Continues from session on DATE" as `known_fact` |
| "Resources needed" | List files/tables in `known_facts` |

If material uncertainty remains after focused evidence gathering, state it explicitly and
ask only when it presents a genuine decision.

### Step 6: Critique the Plan

Before presenting to the user, self-critique:

```
🔍 CRITIQUE
├── Am I solving the right problem? (re-read user's request)
├── Am I over-engineering?
│   ├── Could this be fewer steps?
│   ├── Am I adding abstraction that isn't needed?
│   └── Would a simpler approach work?
├── Am I under-engineering?
│   ├── Nulls, duplicates, dirty data handled?
│   ├── Idempotent on re-run?
│   └── Missing framework functions I should use?
├── What could go wrong?
│   ├── Data risk: unexpected nulls, schema drift
│   ├── Logic risk: wrong keys, wrong join type
│   └── Silent failure: code runs, produces wrong results
└── If a senior engineer reviewed this, what would they question?
```

**Rules:**
- Report material critique findings honestly; do not invent one
- "What would a senior engineer question?" is the strongest self-check
- If critique reveals a fundamental issue → go back to Step 2

### Step 7: Present Plan to User — Obtain Approval When Risk Requires It

```
Here's my plan for [task]:

**Goal:** [one sentence]

**Steps:**
1. ...
2. ...
3. ...

**Key decisions:**
- [decision]: because [reason]

**Risks:**
- [risk]: mitigated by [approach]

**Files to touch:**
- [file1.py] — [what changes]
- [file2.py] — [what changes]

Ready to proceed, or want to adjust anything?
```

Wait for explicit approval when the plan contains a genuine architecture, scope,
destructive-effect, or materially ambiguous decision. Otherwise follow the established
execution boundary. Include residual uncertainty, stop conditions, and reversal conditions
where relevant.

### Step 8: Execute Within the Approved Boundary

After any required approval is obtained, or when no genuine approval decision is present:
1. Apply the global editing invariant
2. Follow the approved plan step by step
3. After each step: verify it worked
4. If something unexpected → mini-critique before continuing
5. Apply the global review/gate/learning firewall when ready to deliver

## 3. Exemptions (Narrow)

### Audit-then-fix pattern
When a preceding audit phase has already read all source files and established full evidence:
- `anchor("task")` at **75%+** is acceptable if `known_facts` includes "Full source audit completed"
- The audit itself IS the evidence-gathering phase — repeating it is waste
- Greenfield or unfamiliar work still requires deeper evidence proportional to its risk

### Self-editing exception
When modifying `anchor("safe")`, `anchor("semantic")`, or `semantic_edit_context.py` itself:
- `executeCode` with raw file I/O is the ONLY option
- Still MUST plan with `anchor("task")` first — no exemption from planning, only from tool choice

## 4. Batch vs Independent Features

| Type | How to detect | Protocol |
|---|---|---|
| **Related fixes** (same root cause) | Would fixing one but not others leave system broken? If yes → related | Can batch `touched`/`preflight` to end of batch |
| **Independent features** | Numbered targets, separate acceptance criteria, different files | MUST `anchor("checkpoint", label="target_N")` between EACH |

**Rule:** No batching independent targets to end-of-session. Checkpoint between each.
Each independent feature gets its own proportional planning and closure cycle.

### Multi-feature sessions — create a session notebook

When the user requests 2+ independent features, create a structured session notebook BEFORE
starting the first feature:

```python
anchor("new_session", name="feature_name", features=3, inline=False)
# Generates pre-structured notebook at ANCHOR_ROOT/sessions/{project}/YYYY-MM-DD_name.ipynb
# Contains: per-feature planning slots, checkpoint markers, progress tracker
```

**When to suggest it:**
- User says "I need to do X, Y, and Z"
- Task decomposes into 2+ independent features during planning
- Multi-session work where progress tracking matters

**When to skip it:**
- Single-feature task
- Research/analysis session
- Quick fix with one clear target

## 5. Anti-Patterns — Things You Must Never Do

| Anti-pattern | Why it's wrong | What to do instead |
|---|---|---|
| Skip to code because "it's simple" | You don't know it's simple until you plan | Plan. Every time. |
| Call `anchor("task")` with just description + goal | Under-specified plan = gaps = iterations | Front-load all kwargs from evidence gathering |
| Proceed below 40% readiness | Hard RuntimeError floor at 40% | Resolve decision-critical gaps until the runtime gate passes |
| Hide a material decision from the user | The decision boundary cannot be reviewed | Present options and request approval |
| Start coding then plan retroactively | Planning after coding is rationalization, not planning | STOP, plan now, then resume |
| Batch planning for multiple features | Each feature has different evidence needs | Separate planning sequence per feature |
| Skip the THINK step | Most planning failures start with misunderstanding the goal | Always restate the goal in one sentence |
| Guess at file paths or function signatures | Wrong assumptions cascade through the plan | `readFile`, `anchor("map")`, `anchor("lookup")` — verify everything |

## 6. Prior Session Learn Debt

If a previous session ended with learning debt, `anchor("task")` is blocked until truthful
assessment clears it. Assess real Observation IDs, choose `nothing_reusable_learned`, or
capture genuine reusable content before assessment. RuntimeError, not a warning.
Content quality and the non-authority firewall are owned by
[memory-saving](memory-saving.md).

## 7. Planning Gate Enforcement

The planning gate checks both `error=None` AND `passed=True`. Tasks that fail readiness
(passed=False) do not satisfy the gate. All of these tools require an active planning gate
(RuntimeError if not): safe, semantic, gate, preflight, checkpoint, touched, save, handoff,
apply_transform, confirm, reject, archive, import_md.

Config mutations (suppress_category, suppress_id, file_override) also require planning.
Read-only `anchor("config")` is fine without planning.

Recovered active learning debt blocks a fresh task/session/project switch. First assess it
with real Observation IDs or `nothing_reusable_learned`.
