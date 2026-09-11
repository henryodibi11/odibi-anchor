# Odibi Anchor quick reference

> Deterministic distribution reference; it is not a discoverable skill.

## Code-Modifying Sequence (RuntimeError where the runtime enforces it)

Project routing: `odibi_anchor.launch(...)` binds one immutable managed project and exact
target. A unique exact target match can supply the project ID; ambiguity requires an explicit
ID. `workspace/.active_project` is not routing authority. Run `anchor doctor` before startup
when the home, route, host, or task implications are uncertain.

Preferred configured startup: `anchor portfolio prepare --config <absolute-config> --host
<host-id> --project <project-id>`. Host setup: `anchor setup-host
<amp|claude|databricks|chatgpt> --target <instruction-root>`. Both return one exact next
operation and never infer a global active project.

Substantial problem: `anchor("problem", "create", title="...")`; capture stable `I*`,
`H*`, and `E*` entries; resume with `anchor("problem", "resume", "PRB-...")`.

```
1. bootstrap         → use installed `launch()` or run `.assistant/agent_bootstrap.py` in-process; retain anchor and structured orientation
2. structured orientation → inspect returned status and prior gate evidence; task memory is deferred
3. anchor("new_session", name="feature_name", inline=True) → initialize this logical session
4. anchor("task", "describe intended work", goal="state intended outcome", mode="implementation",
           acceptance_criteria=["state how completion will be verified"])
                     → plan plus bounded task-aware memory_context selections
5. review task memory → before source edits, acknowledge bounded selections or none
6. read requirements → read each SKILL.md in task output's required_skills
7. anchor("skill_loaded")→ register each: anchor("skill_loaded", "skill-name")
8. anchor("spec","persist")→ when accepted task policy requires a formal Spec
9. anchor("spec","review")→ required Spec must achieve rating ≥ "good"
10. anchor("known_bad", changed_files=[...]) → required before Python edits
11. edit + touched   → verify each edit, then `anchor("touched", "path")`
12. anchor("preflight") → required for Python changes
13. anchor("test")      → required for Python changes
14. anchor("review")    → required for changed files
15. anchor("gate")      → no args, derives evidence from session state
16. learning assess → real Observation IDs or `nothing_reusable_learned`
Repeat for the next independently planned source-change cycle. Gate resets planning.
Steps 7-10 are runtime-enforced when applicable to the accepted task; steps 5-6 are required
guidance review before source edits.
```

With an active Databricks Git Folder provider, step 4 must also pass
`work_type="change", execution_mode="source_change", repository_scope=["src"],
accept_unknown_git_state=True`. Scope paths are relative (`["."]` or `["src"]`), never
absolute. A stale agent must refresh and rerun bootstrap, not use documentation mode.

Read-only work gathers bounded evidence and synthesizes an answer without manufactured
edit, test, gate, or learning steps.

## Memory Cycle

**Observe → Encode → Retrieve → Apply → Evaluate → Consolidate → Govern.** Task acceptance
automatically retrieves bounded selections with reasons and stable IDs; retrieval is not
application. Before source edits, report each bounded selection's memory ID, status,
source/provenance, scope, and reason—or report none. Do not scan the full store or force use.
If a selection affects work, call `anchor("memory", "apply", selection_id=...,
action=..., context={...})`, then `anchor("memory", "evaluate", application_id=...,
outcome="helpful|not_helpful|harmful|superseded", evidence={...})`. Ordinary
`anchor("memory", query=...)` remains compatible.

Working memory is task/session state; episodic memory is structured-learning evidence;
semantic memory is curated entries; procedural memory is immutable distributed guidance.
Authority remains in Problems, Specs, decisions, and work items. Memory cannot authorize,
satisfy skills, or replace verification. Human-attested `learning triage` is the only
episodic-to-semantic publication boundary.

An explicit `work` authority may share assessed evidence-backed `workbench` observations as
advisory `all`-project candidates within that one authority database. Personal/work boundaries
remain separate; candidates never become authority or verification.

Forensics are read-only: `anchor("memory", "replay", task_window_id=...,
view="inspect|verify|context")` restores recorded, redacted context—not execution or
chain-of-thought. Diagnose storage with `anchor("memory", "storage", command="inspect")`; create
a non-destructive move plan with `anchor("memory", "storage", command="plan",
destination="/absolute/path")`. Actual migration and cross-trust-domain transfer require
separate authority.

With durable state configured, successful authority writes checkpoint local SQLite. Use
`anchor state list|snapshot|restore`; never open the durable snapshot as live SQLite.

## Hard Limits (RuntimeError)

| Rule | Limit |
|---|---|
| Ungated file edits | Max 12; the 13th blocks until anchor("gate") or checkpoint |
| Research calls without planning | Max 8 anchor() calls without anchor("task") |
| Write-task planning | Use `anchor("task")` with the relevant context; scale depth to risk and complexity |
| anchor("task") without current structured orientation | RuntimeError |
| Readiness below 40% | RuntimeError |
| known_bad before .py edits | REQUIRED — RuntimeError if skipped |
| Tests pass for .py changes | REQUIRED at gate — RuntimeError if failed |
| Learning assessment after gate (when files changed) | REQUIRED; no-learning is valid |
| Prior session learn debt | Blocks anchor("task") until cleared |
| Config mutations without planning | RuntimeError |
| Task policy requires a Spec but exact linked Spec is not reviewed | RuntimeError — persist and review the required Spec |
| Required skills not loaded | RuntimeError — `anchor("skill_loaded", "name")` for each |
| Spec review rating < good | RuntimeError — `anchor("spec", "review")` must pass |

## Gate Rules

- Call `anchor("gate")` with NO arguments
- Gate strips ALL caller-supplied kwargs
- Gate derives files_changed, files_created, actions_taken from session state
- Gate checks: test pass, test target coverage, filesystem drift, mode mismatch
- ALL gate failures are RuntimeError, not warnings

## Planning Gate

The runtime action contract is authoritative; use `anchor("help", "action")` when uncertain.
Safe orientation includes `status`, `audit_history`, `new_session`, `help`,
`skills`, and `tools`. Bounded context collection such as `map` and `lookup` may occur
before task acceptance. Substantive actions—including `trace`, `trace_row`, `evolve`,
`reconcile`, `profile_table`, source edits, data operations, and delivery gates—require an
accepted task and any direct skills it resolves.

## Edit Protocol

```
1. Edit ONE thing
2. Read it back (3 seconds) — does it do what you intended?
3. anchor("touched", "path") — check syntax_check result
4. If FAILED → fix NOW, do not continue
5. THEN edit the next thing
```

Never batch edits without intermediate verification.
Edit in dependency order: leaf → middle → top → tests.

## When to Ask the User

**Ask when unresolved and material:** Ambiguous requirements, architecture or scope
decisions, destructive effects, business rules, or contradictory evidence that changes the approach.

**NEVER ask:** Which file to read, which anchor() tool, how to fix syntax, import paths,
variable names, whether to run tests, implementation details within approved plan.

**State-and-proceed (medium risk):** Technical choice within plan, error handling strategy,
test case selection, default values, naming with clear convention.

## Scope Rules

- One task per thread
- Editing a file not in your plan? ASK before proceeding
- Found a second bug? Hand off the bounded evidence; capture learning only if reusable.
- "While I'm in here..." → NO. Hand it off. Focus on the original task.
- Keep a thread bounded and hand off when another session would otherwise need to re-derive material state.

## Anti-Rationalization Quick Check

If you're thinking any of these, STOP — you're about to cut a corner:

- "This is simple, I don't need to plan" → Plan. 30 seconds.
- "I know what's in that file" → Read it. 5 seconds.
- "I know this library" → Read the docs. 5 minutes.
- "This edit is trivial, no need to verify" → Verify. 3 seconds.
- "I'll verify at the end" → Verify now. Errors compound.
- "While I'm in here..." → Hand it off; retain it only if the learning firewall passes.
- "I can fix this with one more edit" → STOP. Rollback. Re-plan.
- "The user is waiting, skip steps" → Wrong work fast > right work normal speed? No.
- "I need to invent a learning" → Don't. Assess `nothing_reusable_learned`.
- "This is too small for a gate" → `anchor("gate")`. 5 seconds.

## Learning Closure

Use one truthful route:

```python
# No bounded reusable observation
anchor("learning", "assess", outcome="nothing_reusable_learned")

# Reusable observation: capture content first, then assess returned IDs
observation = anchor("learning", "capture", ...)
anchor("learning", "assess", outcome="observations_recorded",
   observation_ids=[observation["item"]["item_id"]])
```

`assess` accepts a disposition and IDs, not observation content. Legacy `anchor("learn",
session_events=[...])` is compatibility-only for historical recovery that technically
requires the old payload. Do not use memory as a substitute for project evidence or defect authority:

- reusable cross-project lesson → structured Observation
- project-specific run result or environment deviation → managed evidence artifact
- reproducible source defect → Problem Record or authorized Work Item

After writing a managed artifact, re-read the exact path or record before claiming persistence.
Conversation history is not a durable fallback. Learning records never substitute for review
or gate evidence.

## Checkpoint Format

```python
anchor("checkpoint", label="feature_name",
   learning_assessment={"outcome": "nothing_reusable_learned"})
```
When observations exist, pass genuine `learning_captures` and assess their returned
IDs. `learn_events=` remains compatibility-only; do not invent an event.

## New Session Template

```python
anchor("new_session", name="feature_name", features=3, inline=False)
# Generates pre-structured notebook at ANCHOR_ROOT/sessions/{project}/YYYY-MM-DD_name.ipynb
```

Omit `inline=False` for the default logical inline session, which does not create a file.

## Skill loading

Load only the direct native skills named by the accepted task context.
`anchor("skill_loaded", "name")` returns the complete resolved `SKILL.md` content and then
registers the skill; preview-only discovery does not satisfy the gate. There is no
always-load skill. Registration proves delivery, not comprehension or correct application.

## Context Budget

- Load only the skills relevant to the task. Unneeded guidance consumes context and obscures the selected owner.
- Compose skills only when each owns a material part of the requested outcome.
- References beneath a skill provide progressive detail; they are not loadable skill names.

After gate: capture only real Observations, then `anchor("learning", "assess", outcome="observations_recorded", observation_ids=[...])`;
or assess `outcome="nothing_reusable_learned"`.
