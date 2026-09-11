# Odibi Anchor workflow

> Deterministic distribution reference; it is not a discoverable skill.

Use this deterministic reference when Odibi Anchor action sequencing or signatures are needed.

## Bootstrap from a source checkout

For installed operation, first prefer `anchor portfolio prepare --config <absolute-path>
--host <host-id> --project <project-id>`. Its explicit PortfolioV1 replaces project-ID,
host-root, and state-path rediscovery and returns copy-ready immutable route/environment
inputs. Run `anchor setup-host <adapter> --target <instruction-root>` to install or reconcile
this workflow and its launcher without silently replacing user edits. The source-checkout path
below remains a development compatibility route, not a requirement for installed use.

Run the source-owned `.assistant/agent_bootstrap.py` accompanying these instructions in
the same persistent Python process that will make later `anchor()` calls. The launcher
delegates to the repository-root `agent_bootstrap.py`, which remains the only bootstrap
implementation:

```python
import runpy

bootstrap_path = "<instruction root>/.assistant/agent_bootstrap.py"
namespace = runpy.run_path(bootstrap_path)
anchor = namespace["anchor"]
ROOT = namespace["ROOT"]
MANIFEST = namespace["MANIFEST"]
orientation = namespace["ORIENTATION"]
bootstrap = namespace["BOOTSTRAP"]
assert bootstrap["success"] is True
```

The launcher resolves exactly one checkout in this order: an explicit
`ANCHOR_SOURCE_CHECKOUT` init global or environment value; its `.assistant` parent when that
is a source checkout; or that parent's fixed sibling `odibi_anchor`. An explicit
value must be absolute and non-tilde, and an invalid explicit value never falls back.
The launcher never uses cwd, scans `/Workspace/Users`, recursively searches, infers a
username, or lists repositories through an API. If these fixed locations do not resolve,
request one exact checkout path and pass it explicitly.
Distribute the complete `.assistant` tree together with `.assistant_instructions.md`;
never copy this launcher alone and treat it as a standalone bootstrap implementation.

This is an automatic host obligation, not user-prompt boilerplate. A normal task prompt
contains only the desired outcome, target/project intent, constraints, and authority.
Never ask the user to repeat this snippet or provide Databricks API/provider code. Show
the explicit invocation only to troubleshoot a bootstrap that cannot be verified.

Use a Python cell or another stateful Python interface; running the launcher as a
standalone shell subprocess loses `anchor` when that process exits. Do not use
`exec(open(...).read())`: the launcher requires `__file__` and fails actionably before
delegation when it is unavailable.

For an exact normalized `/Workspace/...` selected target—the active managed project's target, or
the Odibi Anchor checkout when no project is active—this entrypoint automatically tries the
optional Databricks runtime SDK's read-only Workspace get-status and Repos get methods. Evidence
is retained only if that target remains the effective root. Do not paste an API executor or
repository-provider constructor. Inspect
`bootstrap["repository_evidence"]`: its `kind`, `acquisition_outcome`, and `capabilities`
distinguish `local_git`, `databricks_git_folder`, and `unavailable`. Missing SDK or
denied/unavailable reads do not invalidate orientation, but they do not authorize a
source task or prove a clean Git state.

When `kind` is `databricks_git_folder`, source work uses the existing implementation
path (with non-empty relative scope), exactly:

```python
task = anchor("task", "<description>", goal="<goal>", mode="implementation",
          work_type="change", execution_mode="source_change",
          repository_scope=["src"], accept_unknown_git_state=True,
          acceptance_criteria=["<completion check>"])
```

Use `["."]` for the repository root; absolute paths are rejected. If an older agent
does not recognize this path, refresh and rerun the current bootstrap. Do not fall back
to documentation mode for source changes.

The entrypoint honors explicit non-empty absolute, non-tilde `ANCHOR_HOME` and
`ANCHOR_MEMORY_DB`. Otherwise it
uses `.odibi_anchor_state` beside the checkout and a database beneath that home,
both outside source and `.assistant`. A new default home is a new runtime/project
namespace; supply the known external `ANCHOR_HOME` when continuity is required. An unsafe
path or any exception is bootstrap failure—correct the explicit path/config and retry;
never bypass the guard or describe locating source as successful initialization.

When no valid managed project is active, the entrypoint requests the checkout as the
fallback code target; otherwise the runtime restores the active project's recorded
target. It returns structured status/audit orientation, defers bounded memory retrieval
to task acceptance, and performs no explicit project/session/task creation.
It does not create or switch managed projects. Use `anchor("project")` afterward only when
the task requires project status or an explicitly approved routing change.

### Memory operating cycle

Odibi Anchor uses **Observe, Encode, Retrieve, Apply, Evaluate, Consolidate, Govern**.
Accepted `anchor("task")` results can include bounded `memory_context` selections derived from
the accepted task and active project/trust scope. Before source edits, review and acknowledge
only those selections: report each stable memory ID with status, source/provenance, scope,
and match reason, or explicitly report no selections. Exposure is not use or correctness;
memory remains advisory. Do not replace this bounded review with a full-store scan or force
an irrelevant selection. Record actual use and evidence-backed feedback explicitly:

```python
application = anchor("memory", "apply", selection_id="sel_...", action="...", context={...})
evaluation = anchor("memory", "evaluate", application_id=application["application_id"],
                outcome="helpful", evidence={"verification": "..."})
```

Allowed outcomes are `helpful`, `not_helpful`, `harmful`, and `superseded`. Ordinary
`anchor("memory", query=...)` retrieval remains compatible. Structured `learning triage` is the
human-attested boundary for idempotent episodic-to-semantic projection; neither exposure nor
successful execution promotes memory automatically.

Inside an explicit PortfolioV1 `work` authority, an agent-supplied, assessed, evidence-backed
`workbench` reusable observation may be projected as an advisory `all`-project candidate.
Exact-project candidates still rank first. This standing boundary does not cross databases,
personal/work authorities, or infer `cross_project` scope, and it does not activate or confirm
the candidate.

Every selection needs a truthful supported disposition before closure. If bounded retrieval
is unavailable, retain that unavailable boundary and follow the runtime's fail-closed or
degraded result without widening scope or inventing evidence. Do not manufacture this review
or a learning obligation for trivial/read-only work that has no accepted durable task.

CoALA working, episodic, semantic, and procedural memory correspond respectively to
task/session state, structured-learning evidence, curated entries, and immutable distributed
guidance. Problems, Specs, decisions, and work items retain authority. Memory cannot
grant authority, satisfy a skill, or replace verification.

Use `anchor("memory", "replay", task_window_id=..., view="inspect|verify|context")` only to
inspect hash-chained events, verify integrity, or restore a deterministic redacted context
package. It never executes recorded actions or reconstructs chain-of-thought. Use
`anchor("memory", "storage", command="inspect")` for read-only profile/path/schema/backup
diagnostics and `command="plan", destination="/absolute/path"` for a non-destructive
migration plan. Migration execution and trust-domain transfer require separate approval.

When the portfolio configures durable state, keep live SQLite on local compute. Successful
authority writes checkpoint it. `anchor state list|snapshot|restore` provides explicit recovery;
durable snapshot bytes are never opened as a live SQLite store.

**⚠️ ALWAYS assign anchor() results to a variable.** Bare calls get blocked by Databricks safety
guards. Use `result = anchor("status"); print(result)` instead. This applies to ALL anchor() calls.

If no managed project is active, ROOT reflects the explicit checkout target unless a
runtime recovery obligation deliberately neutralizes it.
Select once with `anchor("project", "use", "project-id")`, then rerun the retained
`bootstrap_path` and rebind its returned namespace when the result says
reinitialization is required, as shown in the managed-project lifecycle below.

Project artifacts always remain beneath Odibi Anchor. Use a managed project when
source can live there, or retain an external source tree without scattering artifacts:

```python
result = anchor("project", "create", name="queue-automation")
result = anchor("project", "create", name="shared-service", target="/path/to/repository")
result = anchor("project", "set_target", "shared-service", target="/new/local/clone")
```

The project ID is the stable identity across environments. `target_root` is where code
tools operate; `artifact_root` owns specs, notebooks, decisions, and Problem Records.
An existing external `target=` directory is valid and expected for a referenced project.
It is not the managed project destination and is never renamed, deleted, converted, or
scaffolded by project creation. A collision exists only when
`<ANCHOR_HOME>/workspace/projects/<project-id>` already exists or is a symlink; stop there,
preserve its unknown contents, and do not manually create `PROJECT.md` or subdirectories.

A normal owner request can therefore be only:

> Create the approved Odibi Anchor managed project `python-mastery`, referencing
> the existing external target `/Workspace/.../Python-Mastery`. Preserve every existing
> target byte and stop without changes if the managed artifact destination collides.

### Managed project creation lifecycle

Create and set-target operations mutate workspace routing authority and require explicit
bounded approval plus an active write-capable task. Selection of the exact existing project
named by the user is safe orientation: call assigned `project use`, rerun the launcher when
reinitialization is required, verify the selected ID/target, and only then create the actual
work task. Do not switch to implementation mode merely to bypass a read-only block; use it
only when project creation or retargeting is the approved task.

```python
# 1. Complete orientation and start the approved setup session.
status = anchor("status", output_format="dict")
history = anchor("audit_history", output_format="dict")
session = anchor("new_session", name="create-example-project", output_format="dict")

# 2. Establish a fully specified write-capable task.
setup_task = anchor(
    "task",
    "Create the explicitly approved managed project named example-project.",
    goal="Create one self-contained non-production project without overwriting anything.",
    mode="implementation",
    constraints=["Create exactly one project.", "Stop if the name already exists."],
    acceptance_criteria=["The returned project ID is example-project.",
                         "The project is contained beneath Odibi Anchor."],
    in_scope=["Odibi Anchor managed-project scaffolding"],
    out_of_scope=["Source edits", "table writes", "project deletion"],
    known_facts=["The user explicitly approved this exact project creation."],
    deliverables=["Created paths and project status"],
    output_format="dict",
)

# 3. Perform only the approved mutation.
created = anchor("project", "create", name="example-project", output_format="dict")

# 4. Project routing changes require a fresh bootstrap and fresh orientation.
if created.get("reinitialize_required"):
    namespace = runpy.run_path(bootstrap_path)
    anchor = namespace["anchor"]
    ROOT = namespace["ROOT"]
    MANIFEST = namespace["MANIFEST"]
    orientation = namespace["ORIENTATION"]
    session = anchor("new_session", name="verify-example-project", output_format="dict")
```

After reinitialization, inspect project status before calling `project use`; creation
normally made the new project active already. Every `init()` creates fresh in-process
enforcement state, so the mandatory sequence and task must be repeated before further
work. Never delete the project without separate explicit approval.

### Reusable project-setup smoke test (task contract v3.2)

After reinitialization and orientation, run a read-only verification task and inspect
the actual nested v3.2 schema. Do not look for obsolete flat keys such as
`required_questions`, top-level `candidate_actions`, or `handoff.context_questions`.

```python
from odibi_anchor.planning.context_selection import project_context_generators

project_status = anchor("project", "status", output_format="dict")
verification = anchor(
    "task",
    "Evaluate whether the managed project was initialized correctly.",
    goal="Verify project routing and the task-aware context contract without mutations.",
    mode="analysis",
    constraints=["Read-only verification only."],
    acceptance_criteria=["Project status identifies the expected managed project.",
                         "The v3.2 context plan and handoff agree."],
    in_scope=["Project status and task contract"],
    out_of_scope=["File edits", "table writes", "cleanup"],
    known_facts=[f"Current project root: {project_status['project_root']}"],
    inputs=[{"name": "anchor project status", "value": project_status}],
    deliverables=["PASS/FAIL report with observed paths and contract fields"],
    output_format="dict",
)

questions = verification["context_plan"]["questions"]
candidates = [
    action
    for question in questions
    for action in question["candidate_actions"]
]
prompt = verification["handoff"]["prompt_brief"]

checks = {
    "task_contract_v3_2": verification["version"] == "3.2",
    "context_plan_v1": verification["context_plan"]["schema_version"] == "1.0",
    "questions_selected": bool(questions),
    "priorities_valid": all(q["priority"] in {"required", "recommended"} for q in questions),
    "actions_registered": all(a["availability"] == "registered" for a in candidates),
    "no_completion_claim": all("satisfied" not in a for a in candidates),
    "handoff_contains_questions": all(q["question"] in prompt for q in questions),
    "compatibility_projection_exact": (
        verification["discovery"]["recommended_context_generators"]
        == project_context_generators(verification["context_plan"])
    ),
}
assert all(checks.values()), checks
```

Report package source, contract version, project ID/type/root/artifact root/target root,
created structure, every check above, warnings, and manual steps. `called_this_session`
means invocation only and must never be reported as evidence completeness.

### Durable Problem Records

Durable Problem Records project the canonical proportional reasoning contract from
the global operating contract; they do not define a second reasoning method. For ambiguous,
high-impact, or multi-session work, keep one living record instead of generating separate
analysis and handoff documents:

```python
result = anchor("problem", "create", title="Reduce queue processing delays")
result = anchor("problem", "update", "PRB-2026-0001", issue={...}, hypothesis={...})
result = anchor("problem", "update", "PRB-2026-0001", evidence={"source": "...", ...})
result = anchor("problem", "resume", "PRB-2026-0001")
```

The record frames the supported decision and boundaries, disaggregates material branches
when complexity warrants, prioritizes uncertainty, plans the smallest sufficient confirming
and disconfirming evidence, analyzes sourced evidence, and synthesizes recommendations,
residuals, and reversal conditions. Use `link_spec` only when a recommendation produces
an implementation contract; inconclusive investigations can close without a spec.

Tasks can create or resume the record directly:

```python
task = anchor("task", "investigate queue latency", goal="choose a remedy",
          mode="analysis", create_problem=True)
task = anchor("task", "implement accepted remedy", goal="reduce p95 latency",
          mode="implementation", problem="PRB-2026-0001")
spec = anchor("spec", "from_problem", "PRB-2026-0001", name="QUEUE_CAPACITY")
```

Tool findings are offered as evidence candidates but are not persisted automatically.
Snapshots point to a compact resume projection. `anchor("save")` and selected entries from the
compatibility-only historical `anchor("learn")` route, used only when old persisted state
technically requires it, are tagged with the active Problem Record rather than copying it.

Rigor is progressive: level 0 direct tasks bypass a record, level 1 analysis uses a
compact record, and level 2 high-impact/migration/multi-session work uses all stages.
The gates verify traceability only: provenance, evidence-backed hypothesis outcomes,
evidence and uncertainty behind recommendations, and critical-hypothesis disposition.
They do not score decomposition quality or require arbitrary branch counts.

To bring existing Markdown artifacts into a managed project without deleting sources:

```python
preview = anchor("project", "migrate", "project-id", target="/legacy/root")
result = anchor("project", "migrate", "project-id", target="/legacy/root", dry_run=False)
```

Migration preserves relative paths under `problems/`, `specs/`, and `decisions/`,
reports conflicts, never overwrites them, and never removes originals.

### Explicit external targets

Use `root=` only for a caller-supplied target path. Prefer a referenced managed project so
the logical project and artifacts remain stable across environments. Never infer a
private workspace path from a repository nickname.

| If task identifies... | Set `root=` to |
|---|---|
| An explicit external repository or directory | The exact caller-supplied path |
| Odibi Anchor itself | The discovered repository root |
| A bundled tool implementation | Its path beneath `tools/` in the discovered repository |
| unclear / general question | Use the active managed project; otherwise ask for an explicit target root |

## Planning & Hard Rules

See `.assistant_instructions.md` — planning requirements, readiness iteration loop, and hard rules are always active without loading a reference as a skill.

## Native skills that may apply

| When task involves... | Direct skill when materially required |
|---|---|
| Writing new code, building features, creating notebooks | Applicable repository standards |
| Explicitly writing tests or fixtures | `skills/writing-tests/SKILL.md` |
| Building production pipelines with mutation | `skills/data-operations/SKILL.md` |
| Multi-session work, task tracking, backlog management | `skills/work-item-management/SKILL.md` |
| Authoring transformers or pipeline notebooks | `skills/data-operations/SKILL.md` when mutation is material |
| Onboarding or profiling a new source | `skills/data-onboarding/SKILL.md` |
| Onboarding new data source (file, API, unfamiliar table) | `skills/data-onboarding/SKILL.md` |

## Gate Triggers — STOP and call before proceeding

| About to... | MUST call first | Why |
|---|---|---|
| Trace a row's origin through upstreams | `anchor("trace_row", output_df, keys=[...], values={...}, upstream={...})` | Composed workflow: explain_row → pre_join → case_file |
| Fix schema/type mismatches systematically | `anchor("evolve", df, target_schema=target_df, subject="name")` | Composed workflow: schema_diff → schema_migrate → coerce_fix → validate |
| Compare table versions or audit changes | `anchor("reconcile", table="catalog.schema.table", keys=["id"])` | Composed workflow: delta_diff → partition_check → watermark → schema_diff |
| Start any implementation task | `anchor("task", goal=..., mode="implementation")` | Pass the runtime readiness gate and resolve or bound material gaps proportionally |
| Make a small single-file fix | `anchor("task", "description", goal="...", mode="implementation")` | Use bounded context and focused verification; do not manufacture architecture ceremony |
| Plan a feature as a spec | `anchor("task", goal=..., mode="spec_creation")` then `anchor("spec", "persist", result)` | Persists plan to `specs/FEATURE/SPEC.md` |
| Validate a spec is still fresh | `anchor("spec", "validate", "FEATURE")` | Checks files, imports, dependencies |
| Execute a spec mechanically | `anchor("spec", "execute", "FEATURE")` | Validates first, then returns phase-by-phase plan |
| Review a spec before execution | `anchor("spec", "review", "FEATURE")` | Audits completeness, standards, test coverage (8 checks incl. skill cross-ref) |
| Register a skill as loaded | `anchor("skill_loaded", "skill-name")` | Required before each non-exempt substantive action — RuntimeError if skipped |
| Edit any code file | `anchor("map")` | Understand structure before changing it |
| Use framework functions | `anchor("lookup", "what you need")` | Prevents wrong signatures — framework has 162+ functions |
| Query unfamiliar table | `anchor("profile_table", df, subject="table")` | Shape, nulls, cardinality, grain, freshness in one call |
| Assess data quality deeply | `anchor("profile_table", df)` → `anchor("microscope")` → `anchor("case_file")` | Full investigation chain; load `skills/data-onboarding/SKILL.md` only for new-source onboarding |
| Compare two tables | `anchor("diff")` → check `value_changed` per column → `anchor("coerce_check")` on suspicious columns | diff tells WHAT changed, coerce_check tells WHY |
| Join two tables | `anchor("validate", left_df, rules=[...])` + `anchor("profile_table", right_df)` | Check key overlap, nulls, fanout risk before join |
| Write/persist data | `anchor("quality", df, subject="table", keys=[...])` | Catches duplicate keys, nulls, anomalies |
| Debug an error | `anchor("trace", "error text")` | Structured root cause extraction |
| Use unfamiliar function | `anchor("lookup", "function_name")` | Correct import path + signature |
| Modify file structure | `anchor("impact", target="file.py")` | AST-aware dependency analysis |
| Validate business rules | `anchor("validate", df, rules=[...])` | Semantic checks quality_gate doesn't cover |
| Clean/transform raw data | `anchor("transform", profile_or_ctx)` | Auto-detects profiler output; generates reviewable plan |
| Execute transform plan | `anchor("apply_transform", df, plan_ctx)` | Applies transforms with checkpoint/rollback — no exec() |
| Close session learning | Capture supported reusable Observations, or assess `nothing_reusable_learned` | Avoids fabricated learning |

## Quick Reference — Full Tool Table

| Action | Call |
|---|---|
| Map codebase | `anchor("map")` or `anchor("map", focus_file="path")` |
| Find framework function | `anchor("lookup", "description or name")` |
| Explore table | `anchor("profile_table", df, subject="catalog.schema.table")` |
| Profile table deeply | `anchor("profile_table", df, subject="name", level="deep")` |
| Full structural profiler | `anchor("profile_table", df, subject="catalog.schema.table")` |
| Column deep-dive | `anchor("microscope", df, "column_name", output_format="dict")` |
| Row investigation | `anchor("case_file", df, column="col", filter="nulls\|outliers\|top:N\|where:EXPR")` |
| Quality gate before write | `anchor("quality", df, subject="table", keys=["id"])` |
| Validate rules | `anchor("validate", df, rules=[{"type":"not_null","columns":["id"]}])` |
| Change impact | `anchor("impact", target="file.py")` |
| Error trace | `anchor("trace", "traceback text")` |
| Known error lookup | `anchor("known_error", "error text")` |
| Schema comparison | `anchor("schema_diff", old_df, new_df)` |
| Table diff (rows) | `anchor("diff", old_df, new_df, keys=["id"])` — includes null_to_value, value_to_null, value_changed per column |
| Pre-join validation | `anchor("validate", df, rules=[{"column": "id", "rule": "not_null"}, ...])` — check key nulls, duplicates, type compatibility |
| Classify value mismatches | `anchor("coerce_check", old_df, new_df, keys=["id"], columns=["col"])` — whitespace/case/unicode/numeric/date vs genuine |
| Table contract | `anchor("contract", df, subject="table", candidate_key_columns=["id"])` |
| Generate transform plan | `anchor("transform", profile_or_ctx, subject="table")` |
| Apply transform plan | `anchor("apply_transform", df, plan_ctx, checkpoint=True)` |
| Rollback transform step | `anchor("rollback", result, to="dedup")` or `anchor("rollback", result, to=3)` |
| Free Spark checkpoints | `anchor("unpersist", result)` |
| Find duplicates | `anchor("duplicate", df, ["id", "date"])` |
| Resolve imports | `anchor("import_resolve", symbol="ClassName")` |
| Convention check | `anchor("convention", action="new_function", function_name="...")` |
| Explicit bounded memory search | `anchor("memory", query="...")` when the accepted task context is insufficient |
| Full task plan | `anchor("task", "description", goal="...", mode="implementation")` |
| Preflight lint | `anchor("preflight", changed_files=[...])` |
| Test focus | `anchor("test", changed_files=[...])` |
| Run tests (pytest) | `anchor("test")` or `anchor("test", target="file.py", mark="fast")` |
| Session diff | `anchor("session_diff")` or `anchor("session_diff", stat=True)` |
| Safe edit (guardrailed) | `anchor("safe", target=file, action=..., function=...)` — auto-chains: known_bad → semantic_edit → touched → preflight → test_focus |
| AST edit | `anchor("semantic", target=file, action="add_import", ...)` |
| Check obligations | `anchor("gate")` — no args needed, derives ALL evidence from session state |
| Save knowledge | `anchor("save", entry_type="discovery", content="...", tags=[...])` |
| Review task memory | inspect bounded `task_result["memory_context"]`; query further only when insufficient |
| Memory stats (explicit memory-governance tasks only) | `anchor("memory_stats")` |
| Known-bad guardrail | `anchor("known_bad", changed_files=[...])` |
| Register file change | `anchor("touched", "path/to/file.py")` or `anchor("touched", "new.py", created=True)` |
| Register skill loaded | `anchor("skill_loaded", "writing-tests")` — register a required direct native skill after reading its SKILL.md |
| Session file list | `anchor("session_files")` |
| Trace row lineage | `anchor("trace_row", output_df, keys=["id"], values={"id": 42}, upstream={"source": df1, "dim": df2})` — composed: explain_row → pre_join → case_file |
| Schema evolution | `anchor("evolve", df, target_schema=target_df, subject="orders")` or `anchor("evolve", df, coerce_ctx=coerce_check_result)` — composed: schema_diff → schema_migrate → coerce_fix → validate |
| Reconcile table versions | `anchor("reconcile", table="catalog.schema.table", keys=["id"])` or `anchor("reconcile", old_df, new_df, keys=["id"])` — composed: delta_diff → partition_check → watermark → schema_diff |
| Anti-pattern config | `anchor("config")` — view/edit suppress settings (persists to `.anchor_config.json`) |
| Assess session learning | Real Observation IDs or `nothing_reusable_learned`; follow the global learning firewall |
| Handoff to next session | `anchor("snapshot", mode="handoff", summary="task", state="in_progress", decisions=[...])` |
| Session snapshot | `anchor("snapshot", decisions=[...], next_steps=[...])` |
| Session status | `anchor("status")` — zero-param read-only health dashboard (current-process session/task continuity, files, obligations, risk, file history) |
| Memory-governance tag audit | `anchor("memory_tags", tags=["protective"])` — only for an explicitly authorized bounded audit |
| Checkpoint | `anchor("checkpoint", label="feature", learning_assessment={"outcome": "nothing_reusable_learned"})` or capture and assess real observations |
| New session template | `anchor("new_session", name="feature_name", features=3, inline=False)` — generates a pre-structured notebook; default inline mode creates no file |

`status.runtime.task_context_present` reports only whether this process currently holds an
accepted task profile; a remembered project or task-window ID is not source authority. For drift
after a bounded Databricks task baseline, run `known_bad` for Python paths, then acknowledge each
intended path with `touched`; unrelated drift remains blocked until acknowledged.

## Valid entry_types for anchor("save")

`gotcha`, `decision`, `pattern`, `convention`, `failure_pattern`, `discovery`, `tool_call`, `preference`


## Learning compatibility

Legacy `anchor("learn")` remains available only as a compatibility-only route for historical
recovery that technically requires the old persisted payload. Use canonical `learning capture/assess`
for normal closure. The content-quality and non-authority firewall is stated in the global
operating contract.

## Workflow Loops

| Trigger | Sequence |
|---|---|
| Modify code | plan → map → safe(apply=True) → checkpoint | *(safe auto-chains: known_bad → edit → touched → preflight → test_focus)* |
| Build feature | plan → map → convention → implement → touched → test → checkpoint |
| Debug error | plan → trace → map → fix → test → gate |
| Compare / reconcile tables | plan → pre_join (if joining) → diff → (if value_changed > 0) coerce_check → fix coercion → diff again |
| Trace row lineage | `anchor("trace_row", output_df, keys=[...], values={...}, upstream={...})` — auto-chains: explain_row → pre_join (if multiple upstreams) → case_file (problematic upstreams) |
| Schema evolution | `anchor("evolve", df, target_schema=target_df, subject="name")` — auto-chains: schema_diff → schema_migrate → coerce_fix → validate |
| Reconcile table/version changes | `anchor("reconcile", table="...", keys=[...])` — auto-chains: delta_diff (or diff) → partition_check → watermark → schema_diff |
| Onboard new source | Load `skills/data-onboarding/SKILL.md`, then use its source-format references as needed |
| End session | close applicable gate, assess genuine learning, snapshot only resumable work |
| Orient mid-session | status | *(zero-parameter, <100ms, read-only)* |

**Tool chaining**: Every tool now emits `suggested_next_actions` with explicit `anchor()` references.
After each tool completes, read its suggestions to know the natural next step:
- `anchor("task")` → suggests `anchor("map")`, `anchor("lookup")`, `anchor("convention")` based on mode
- `anchor("map")` → suggests `anchor("known_bad")`, `anchor("convention")`, `anchor("impact")`, `anchor("lookup")`
- `anchor("trace")` → suggests `anchor("known_error")`, `anchor("map")`, then fix→touched→preflight→test→gate
- `anchor("quality")` → suggests `anchor("diff")`, `anchor("validate")`, `anchor("profile_table")`, `anchor("touched")`
- `anchor("validate")` → suggests `anchor("quality")`, `anchor("profile_table")`, `anchor("touched")`→gate
- `anchor("gate")` → creates assessment debt; close it truthfully before the next task
- `anchor("checkpoint")` → suggests next feature or fix guidance on failure
- `anchor("status")` → suggests next obligation to fulfill (MUST/SHOULD)

**Between-feature checkpoint** (STOP and verify after completing each discrete feature):

**Preferred (one command):**
```python
anchor("checkpoint", label="feature_name",
   learning_assessment={"outcome": "nothing_reusable_learned"})
# Options: skip_test=True (doc-only), test_target="tests/specific.py" (focused)
```

**Manual fallback (if checkpoint unavailable):**
```
✓ Did I orient?     → inspect structured bootstrap orientation
✓ Did I review memory? → acknowledge accepted task memory_context before source edits
✓ Did I plan?       → anchor("task") before first edit, with context proportional to the work
✓ Did I touch?      → anchor("touched", "path") after each file edit
✓ Did I preflight?  → anchor("preflight", changed_files=[...]) before moving on
✓ Did I test?       → anchor("test") confirms coverage
✓ Did I gate?       → anchor("gate") before next feature — no kwargs needed
```
If any answer is NO, go back and do it before starting the next feature.
Do NOT batch these to the end of a multi-feature session — they catch errors
between features that compound if left unchecked.

**Gate call pattern** (end of every code-modifying workflow):
```python
# Gate derives ALL evidence from session state — just call with no args
anchor("gate")
# Gate auto-detects: files_changed, files_created, actions_taken from session registry
# Do NOT pass actions_taken, files_changed, files_created, obligations_paid, or skip_timing_verification
```

**Gate hard blocks (all RuntimeError):**
- Tests must pass for .py changes — RuntimeError if tests haven't run or last test failed
- Test target cross-reference — RuntimeError if test target doesn't cover changed .py files
- Filesystem drift safety — clean unregistered changes are auto-registered; known-bad Python drift or policy-forbidden documentation paths block
- Mode mismatch — RuntimeError if readonly mode (e.g., "analysis") but files were modified
- Ungated edit limit — max 12 file edits before gate/checkpoint; the 13th blocks
- known_bad REQUIRED before .py edits — RuntimeError if skipped
- Spec REQUIRED only when accepted task policy says so — exact linked Spec must be persisted
- Direct skills REQUIRED by accepted task intent/policy — RuntimeError if not registered via `anchor("skill_loaded")`
- Required Spec review rating must be ≥ good — RuntimeError if not reviewed

## Static Anti-Pattern Scanner (v0.5.0)

`anchor("safe")` now runs a **static anti-pattern scan** on proposed code changes post-dry-run.
25 rules across 11 categories detect common pitfalls before code is applied.

### Categories

| Category | Example patterns |
|---|---|
| performance | `.collect()`, `.toPandas()`, `.rdd`, row-at-a-time UDF, `.count()` for emptiness |
| correctness | `CAST()` without `TRY_CAST`, bare `except:`, mutable default args |
| migration | `/dbfs/`, `/mnt/` paths |
| reliability | `time.sleep()` in production, `.cache()` without unpersist |
| security | hardcoded credentials, string-concatenated SQL |
| portability | `display()` in modules |
| compatibility | `SparkContext`, `.rdd` (Spark Connect incompatible) |
| data_safety | unconditional `mode('overwrite')`, cross joins |
| maintainability | `SELECT *`, `import *`, TODO/FIXME, global state mutation |
| observability | `print()` in production (use logging) |

### Suppress Configuration

Configure via `anchor("config")` or `.anchor_config.json` at project root:

```python
# View current settings
anchor("config")

# Suppress entire category
anchor("config", suppress_category="maintainability")

# Suppress specific rule
anchor("config", suppress_id="todo_fixme_hack")

# File-level override (tests get relaxed rules)
anchor("config", file_override="tests/**", suppress_categories=["performance"])

# Remove suppression
anchor("config", unsuppress_category="maintainability")
anchor("config", unsuppress_id="todo_fixme_hack")
anchor("config", remove_override="tests/**")
```

### Config file structure (`.anchor_config.json`)

```json
{
  "anti_patterns": {
    "suppress_categories": ["maintainability"],
    "suppress_ids": ["print_instead_of_log"],
    "file_overrides": {
      "tests/**": {"suppress_categories": ["performance", "portability"]},
      "scripts/*": {"suppress_ids": ["todo_fixme_hack"]}
    }
  }
}
```

**Precedence:** Explicit kwargs > file_overrides (glob match) > project-level config.
`None` = use config defaults. `[]` = explicitly suppress nothing (overrides config).

### How it integrates with anchor("safe")

1. **Pre-edit** — `known_bad` checks memory DB for prior failure matches (file + action)
2. **Post-dry-run** — `known_bad` re-runs with `proposed_diff` for static pattern scanning
3. Static matches → `status: "warn"` (never block), surfaced in findings + risks
4. New metrics in return dict: `static_patterns_matched` (int), `content_guardrail` (sub-result)

## Session Timing & Context (v0.4.0)

Every `anchor()` call is automatically timed. Session metadata is captured at boot.

### What's captured automatically:
- **Timing**: Wall-clock milliseconds per action (via `time.perf_counter()`)
- **Session context**: DBR version, cluster name, spark version, notebook path

### Accessing session data:
```python
session = anchor("session_files", output_format="dict")

# Timing
session["timings"]        # [{action, elapsed_ms, error}, ...]
session["total_time_ms"]  # cumulative wall-clock
session["total_actions"]  # count of anchor() calls this session

# Environment
session["session_context"]  # {dbr_version, spark_version, cluster_name, notebook_path, cluster_id}
```

### AST cache integration:
`anchor("touched", "path")` now automatically calls `ast_cache_invalidate(path)`.
This means subsequent calls to `anchor("map")`, `anchor("impact")`, or `anchor("preflight")` will
re-parse the edited file instead of using stale cached AST data.

## Delta Table Metadata (v0.4.0)

`anchor("profile_table", df, subject="catalog.schema.table")` now fetches Delta metadata automatically
when the subject looks like a qualified table name (contains `.`) and Spark is available.

### What's captured:
- **DESCRIBE DETAIL**: num_files, size_bytes, partitioning, clustering, location, created_at, last_modified
- **DESCRIBE HISTORY (LIMIT 5)**: Recent operations with metrics (rowsInserted, rowsUpdated, rowsDeleted)

### Freshness enrichment:
When column-based freshness detection fails (no temporal columns), the tool falls back to
Delta log's `last_modified` timestamp — zero data scanning required.

### Output:
```python
ctx = anchor("profile_table", df, subject="catalog.schema.table", output_format="dict")
ctx["delta_metadata"]  # {format, num_files, size_bytes, partitioning, clustering, recent_operations, ...}
ctx["freshness"]       # may have source="delta_metadata" when column detection failed
```

For Pandas DataFrames or subjects without `.` in the name, `delta_metadata` is `None`.

## Task Planner Mode-Aware Suggestions (v0.4.0)

`anchor("task")` now emits mode-specific `anchor()` action references in its `suggested_next_actions`.
Instead of generic advice, each mode tells you exactly which tools to run:

| Mode | Key suggestions |
|---|---|
| `planning` | bounded `task_result["memory_context"]`, then `anchor("map")` as needed |
| `implementation` | `anchor("map")`, `anchor("lookup")`, `anchor("convention")`, `anchor("touched")`, `anchor("preflight")`, `anchor("test")`, `anchor("gate")` |
| `testing` | `anchor("profile_table")`, `anchor("validate")`, `anchor("gate")` |
| `debugging` | `anchor("trace")`, `anchor("known_error")`, `anchor("map")`, `anchor("touched")`→`anchor("gate")` |
| `review` | `anchor("map")`, `anchor("consistency")`, `anchor("impact")` |

## Enforcement Mechanisms (v0.4.1)

Three layers ensure preflight and known_bad compliance:

### Layer 1: Auto-preflight on touched (instant feedback)
`anchor("touched", "file.py")` now runs `compile()` on .py files immediately.
Returns `syntax_check: "passed"` or `"FAILED"` with error details:
```python
result = anchor("touched", "src/my_module.py")
# {'registered': '...', 'syntax_check': 'passed', ...}

result = anchor("touched", "src/broken.py")
# {'registered': '...', 'syntax_check': 'FAILED', 'syntax_error': "Line 5: invalid syntax"}
```
This catches typos instantly — no need to wait for gate.

### Layer 2: Gate obligation enforcement (blocks delivery)
`anchor("gate")` derives ALL evidence from session state. It auto-detects files changed,
files created, actions taken, and obligations paid from `_SESSION_TIMINGS`. Gate strips
any caller-supplied kwargs and rebuilds evidence from scratch. Do NOT pass `actions_taken`,
`files_changed`, `files_created`, `obligations_paid`, or `skip_timing_verification`.

`known_bad` is REQUIRED (RuntimeError) before .py edits — not optional.

### Layer 2b: Gate hard blocks (all RuntimeError)
- Tests must pass for .py changes
- Test target must cover changed .py files (cross-reference check)
- Filesystem drift reconciliation (clean drift auto-registers; unsafe known-bad/policy cases block)
- Mode mismatch (readonly mode but files were modified)
- Ungated edit limit: 12 file edits max without a successful gate/checkpoint

### Post-gate learning assessment
When gate PASSES with `files_changed > 0`, truthful learning assessment is required before
the next gate. Structured learning is a two-step route: capture genuine observation content
with `anchor("learning", "capture", ...)`, then assess the returned item IDs with
`outcome="observations_recorded"`. If no bounded reusable observation exists, assess
`nothing_reusable_learned` without IDs. `assess` never accepts observation content. Legacy
`anchor("learn")` remains a compatibility-only historical-recovery route where the old payload
is technically required; historical payload validation is unchanged. Do not fabricate content.

Apply the authority boundary before retaining a finding. Cross-project lessons belong in
learning, project-specific execution results and environmental deviations belong in managed
evidence artifacts, and reproducible source defects belong in a Problem Record or authorized
Work Item. Memory is not a replacement for project authority. After any managed-artifact
write, re-read the exact path or record in the active process before claiming persistence;
conversation history is not durable evidence of the write.

### Auto-compliance audit (v0.6.1)
When old persisted state technically requires the compatibility-only historical
`anchor("learn")` route and it fires with `files_changed > 0`, a
compliance audit auto-runs:
- Scores session 0-10 objectively from `_SESSION_TIMINGS` ordering
- Detects gaps: missing planning, wrong ordering, no preflight, untouched files, etc.
- Preserves the historical compatibility record; new work uses structured audit evidence
- Inspect historical compliance with `anchor("audit_history")`, not a semantic-memory query
- Zero ceremony — fires automatically, never breaks learn on failure

### Auto-tag enrichment (v0.6.2)
Compliance entries are automatically tagged for queryability:
- `file:<relative_path>` — one tag per changed file (query: "what happened when I touched X?")
- `task:implementation|quick-fix|testing|docs-only` — inferred from session actions
- `quality:perfect|poor` — score-based quality flag
- `had-errors`, `long-session`, `quick-session` — contextual flags

Use `anchor("audit_history")` for compliance trends. Deep inspection of compatibility-era
semantic rows requires an explicitly authorized memory-governance audit.

### Behavioral auto-tags (v0.6.3)
- **`recurring`** — Applied at learn time when existing entries have similar content (FTS5 match).
  It is recurrence evidence only and grants no priority or lifecycle disposition.
- **`protective`** — Applied when `known_bad` matches a memory entry (the entry prevented an error).
  It is application evidence only and grants no priority or lifecycle disposition.
- **`time-sink`** — Applied when session spends >5min per feature (total_time_ms / max(1, checkpoints)).
  Identifies tasks that consistently run long — useful for workload estimation.

### Signal interpretation
Recurrence, application, exposure, and attention remain evidence or bookkeeping. Only the
explicit human-attested lifecycle may dispose a candidate; follow the global learning firewall.

### Extended checks (v0.6.2) — scoring now 0-15
In addition to original 10 compliance checks:
- **Error ratio** (>30% = thrashing, indicates wrong approach)
- **Retry loops** (same action 3+ consecutive = stuck, should pivot)
- **Tool diversity** (<3 unique actions with >2 files = editing without verifying)
- **Session duration** (>300s without orientation = drift risk)
- **First-edit latency** (edit before read/plan = cowboy coding)

### Layer 3: Hard rules (agent instruction enforcement)
Rules #11 and #12 in `.assistant_instructions.md`:
- **#11**: NEVER skip `anchor("preflight")` after editing .py files — gate will FAIL
- **#12**: ALWAYS run `anchor("touched", "path")` immediately after every file edit
- **#13**: NEVER claim `obligations_paid` without actually running the tool — gate verifies timing proof

### Layer 4: Research session enforcement
8+ `anchor()` calls without `anchor("task")` raises RuntimeError. Warning starts at 3+.

### Layer 5: Prior session learn debt
If a previous session ended with assessment debt, `anchor("task")` is blocked until real
Observation IDs or `nothing_reusable_learned` clear it. A compatibility-only historical
recovery route is surfaced by Anchor only when persisted old state technically requires it.

### Layer 6: Per-feature planning reset
After a successful gate/checkpoint, a fresh `anchor("task")` is required for the next feature.
All planning-gated tools (safe, semantic, gate, preflight, checkpoint, touched, save,
handoff, apply_transform, confirm, reject, archive, import_md) raise RuntimeError without
an active planning gate.

### Layer 7: Spec requirement gate
A persisted Spec is required when the accepted task's current policy disposition is
`required` or formal Spec creation is explicit. Generic implementation and planning do
not alone imply a Spec.

Sequence: `anchor("task")` → `anchor("spec", "persist", result)` → then file edits are unblocked.

### Layer 8: Skill-load gate
The accepted TaskProfile/context and Spec policy disposition feed one requirements
resolver. All resulting direct skills must be loaded via
`anchor("skill_loaded", "name")` before each non-exempt substantive action proceeds.
RuntimeError if any required direct skill is missing.

| Mode | Required skills |
|---|---|
| `implementation`, `migration`, `planning`, `greenfield` | `writing-specs` only for required Spec policy or explicit formal Spec intent |
| `spec_creation` | `writing-specs` |
| `testing` | `writing-tests` only for explicit test authoring, repair, or strategy |
| `debugging` | `debugging` |
| `review` | `code-comprehension` when material; `cross-functional-pr` only for explicit PR intent |
| `analysis` | `code-comprehension` when material |
| data mutation | `data-operations`; compose with explicit onboarding or reconciliation owners |

Workflow: `anchor("skill_loaded", "name")` returns the complete resolved SKILL.md and registers
it → repeat for each required skill → apply the task-specific constraints. Preview-only
discovery does not satisfy the gate; registration proves delivery, not comprehension.

### Layer 9: Spec review gate
When accepted task policy requires a formal Spec, the exact linked Spec must be
persisted and `anchor("spec", "review")` must return rating ≥ "good" before
consequential effects proceed.

Review now includes **Check 8: standards_cross_ref** — verifies the spec references all
required skill files for its mode. Missing skill references fail the review.

## Transform Pipeline

### Full workflow (zero-friction)
```python
profile_ctx = anchor("profile_table", df, subject="bronze.invoices")
plan_ctx = anchor("transform", profile_ctx, subject="bronze.invoices")  # auto-detects profiler output
result = anchor("apply_transform", df, plan_ctx)
cleaned_df = result["df"]
```

### Key parameters

| Function | Parameter | Default | Effect |
|---|---|---|---|
| `transform_plan_context` | `include_cast` | `True` | Generate type-cast steps |
| | `include_dedup` | `True` | Generate deduplication step |
| | `include_drop_constant` | `False` | Drop all-null/constant columns |
| | `custom_overrides` | `None` | `{"col": "skip"}` excludes column from all steps |
| `apply_transform_context` | `checkpoint` | `True` | Store df state before each step for rollback |
| | `steps` | `None` | `[1, 3]` — execute only specific steps |
| | `dry_run` | `False` | Validate without mutating |
| | `min_confidence` | `0.0` | Skip steps below threshold |
| | `spark_persist` | `"cache"` | Spark checkpoint mode: `none`, `cache`, `local_checkpoint` |
| | `auto_unpersist` | `False` | Auto-release Spark checkpoints after execution (fire-and-forget) |
| | `max_checkpoints` | `None` | Limit retained checkpoints; oldest evicted (FIFO) when exceeded |

### Rollback
```python
result = anchor("apply_transform", df, plan_ctx)

# Via dispatcher
before_dedup = anchor("rollback", result, to="dedup")     # by action name
before_step3 = anchor("rollback", result, to=3)           # by step order
original     = anchor("rollback", result, to="start")     # full rollback

# Or direct import
from odibi_anchor.tables import rollback
before_dedup = rollback(result, to="dedup")
```

### Spark checkpoints
```python
# Default: cache() + count() materializes checkpoint
result = anchor("apply_transform", spark_df, plan_ctx, spark_persist="cache")

# Truncates lineage (best for long pipelines with many steps)
result = anchor("apply_transform", spark_df, plan_ctx, spark_persist="local_checkpoint")

# No materialization (lazy — recomputes on rollback)
result = anchor("apply_transform", spark_df, plan_ctx, spark_persist="none")

# Free memory when rollback is no longer needed
anchor("unpersist", result)  # releases all cached checkpoints

# Or auto-release immediately (fire-and-forget — no rollback needed)
result = anchor("apply_transform", spark_df, plan_ctx, auto_unpersist=True)
# result["df"] is ready, checkpoints already freed

# Limit checkpoint memory — keep only last 3 (oldest evicted automatically)
result = anchor("apply_transform", spark_df, plan_ctx, max_checkpoints=3)
# result["checkpoints_evicted"] lists evicted step orders
# rollback still works for retained steps; KeyError for evicted ones
```

Valid action names: `standardize_columns`, `null_cleanup`, `cast`, `boolean_cast`, `date_parse`, `drop_constant`, `dedup`.


## Architecture: Session State Singleton (v0.5.1)

`src/odibi_anchor/_utils/_session_state.py` is the shared singleton for ALL mutable session state.

### Problem (historical)
The old `exec(open('agent_init.py').read())` pattern created a namespace split: mutable state in notebook
globals was invisible to `import`-based modules. This is resolved by the import-based bootstrap
(`from odibi_anchor.bootstrap import init`).

### Solution
Session state lives in a proper package module (`_session_state.py`). The import-based bootstrap
and all other modules resolve to the **same `sys.modules` entry**. The mutable objects
(`_SESSION_FILES_CHANGED`, `_SESSION_FILES_CREATED`, `_SESSION_TIMINGS`, `_SESSION_DIFF_BASELINES`)
are truly shared.

### Key exports:
```python
from odibi_anchor._utils._session_state import (
    _SESSION_FILES_CHANGED,   # set[str] — files modified this session
    _SESSION_FILES_CREATED,   # set[str] — files created this session
    _SESSION_TIMINGS,         # list[dict] — per-action timing records
    _SESSION_DIFF_BASELINES,  # dict[str,str] — pre-edit file content for diff
    touched,                  # register file change + syntax check + baseline capture
    get_diff,                 # compute unified diff against baselines
    get_state,                # full session state snapshot
    record_timing,            # record action timing
)
```

### How `touched()` works:
1. Validates non-empty path (raises `ValueError` otherwise)
2. **Snapshots file content** into `_SESSION_DIFF_BASELINES` (BEFORE registering change)
3. Adds path to `_SESSION_FILES_CHANGED` (and `_SESSION_FILES_CREATED` if `created=True`)
4. Invalidates AST cache via `ast_cache_invalidate(path)`
5. Runs `compile()` syntax check on `.py` files → returns `syntax_check: "passed"` or `"FAILED"`

### Safe edit chain (updated):
```
anchor("safe") → known_bad → semantic_edit → _session_state.touched() → preflight → test_focus
```
`touched()` is called from `_session_state` (shared singleton), NOT a per-module copy.

## Memory Evidence Lifecycle

Legacy `sessions_seen` and `use_count` remain readable history but do not rank,
promote, or retain entries. Immutable application/evaluation evidence informs retrieval ranking;
it does not grant backlog, scope, permission, policy, implementation, or promotion authority.
Typed verifiers and governed owner activation/confirmation are the available promotion lanes;
compatibility `confirm` calls return `confirmation_blocked` without changing candidate status.

### How it works:
1. **Candidate fetch**: reads the complete eligible active-project corpus before bounded ranking.
2. **Selection**: task acceptance records only final returned IDs in immutable lifecycle tables.
3. **Evidence**: application and evidence-backed evaluation are separate events.
4. **Correction**: excludes an entry from normal retrieval and atomically withdraws any prior
   activation/confirmation authority; owner reactivation is a distinct governed event.
5. **Progression**: successful gates add a fair bounded project-local typed-verifier sweep so
   eligible structured-learning memories do not depend only on retrieval selection.

No gate-wide or exposure-based bulk confirmation occurs.

## Environment

- **Compute**: DBR 17.3 LTS, USER_ISOLATION
- **FRAMEWORK_ROOT**: Optional reusable project/team library path. Pass it explicitly to `anchor("lookup")`; Odibi Anchor does not require a specific framework.
- **Architecture**: Medallion (bronze=string, silver=typed, gold=aggregated)
- **Memory**: SQLite + FTS5 at `.agent_memory.db` (portable single-file, 344 entries)
  - Exposure, use, evaluation, and counters never promote. Authority requires a supported
    typed verifier or separate governed owner-presence activation and confirmation requests.
    Task use remains recorded through disposition/application/evaluation.
  - Session registry: `anchor("touched")` tracks file changes, auto-feeds into `anchor("gate")`
  - `anchor("touched")` also auto-invalidates AST cache for the edited file
- **Session context**: Captured at boot — DBR version, Spark version, cluster name, notebook path
- **Timing**: Every `anchor()` call timed automatically; accessible via `anchor("session_files")`

- **Structured learning**: `anchor("learning", "capture|assess|safe_stop|list|show|insights|triage|export|backup", ...)`.
  Capture, gate, or closure assessment creates the task's local SQLite obligation as needed,
  so observations can be recorded when encountered. Assessment uses real Observation IDs or
  `nothing_reusable_learned`. Legacy `learn` remains compatibility-only. State is local—no cloud replication.
