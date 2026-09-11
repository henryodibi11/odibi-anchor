# Quickstart: One Normal Odibi Anchor Workflow

Odibi Anchor should be mostly invisible to the user. Ask for the outcome normally;
the agent follows this workflow and keeps the ceremony proportional to the task.

## 1. Ask for the outcome

Examples:

> Fix the failing pipeline and leave it PR-ready.

> Review this notebook for correctness and explain any risks.

> Turn these notes into a technical specification. Do not implement it yet.

You do not need to choose tools, name context “eyes,” or describe internal contracts.
The agent should ask only when intent is materially unclear or approval is required.

> **Owner workflow:** after one-time agent/runtime setup, asking for the outcome is the
> normal interaction. The remaining steps document setup and the workflow the agent
> owns behind the scenes; they are not commands the owner must supervise for each task.

## 2. Make the runtime available

```bash
# Planning only (zero deps)
pip install -e .

# With pandas features
pip install -e ".[pandas]"

# Everything
pip install -e ".[all,dev]"
```

### Databricks Free Edition

Install the exact public release and its qualified Databricks SDK dependency, then restart
Python. No Anchor source checkout is required:

```python
%pip install "odibi-anchor[databricks]==<version>"
dbutils.library.restartPython()
```

Run doctor, scaffold or validate the private PortfolioV1, install host guidance, and call
`prepare_portfolio_runtime()` before launch. Apply its returned environment exactly. See
[Getting started](getting-started.md) for the copy-ready sequence. Normal user prompts then
contain only outcome, target/project intent, constraints, and authority.

The explicit entrypoint is a host troubleshooting/fallback equivalent, not user-prompt
boilerplate:

```python
import runpy

namespace = runpy.run_path("<instruction root>/.assistant/agent_bootstrap.py")
anchor = namespace["anchor"]
orientation = namespace["ORIENTATION"]
assert namespace["BOOTSTRAP"]["success"] is True
repository_evidence = namespace["BOOTSTRAP"]["repository_evidence"]
```

The entrypoint recognizes an exact normalized `/Workspace/...` selected target—the active managed
project's target, or the Odibi Anchor checkout when no project is active—and, when the
optional Databricks runtime SDK is available, auto-configures read-only Workspace get-status and
Repos get identity attestation. Evidence is retained only if that target remains the effective
root. No per-process executor/provider snippet is needed. An unavailable or denied attestation
does not break orientation, but its structured acquisition outcome must not be treated as clean
Git evidence.

The packaged launcher uses the installed distribution. Explicit source checkout discovery
is a development-only fallback; it never derives authority from cwd or scans a user tree.

## 3. Bootstrap and orient

For an installed package, prefer PortfolioV1 preparation and `odibi_anchor.startup.launch()`.
The packaged entrypoint above returns bounded structured status/audit orientation; bounded memory arrives with an accepted task. Retain its namespace and create a session or task only
when the requested work requires that lifecycle. In notebooks, always assign `anchor()`
results to variables.

## 4. Frame the task and use only relevant context

```python
task = anchor(
    "task",
    "Fix the failing pipeline and leave it PR-ready.",
    goal="Restore correct pipeline execution with focused verification.",
    mode="implementation",
    deliverables=["smallest correct fix", "test evidence", "PR-ready handoff"],
    output_format="dict",
)

for question in task["context_plan"]["questions"]:
    print(question["priority"], question["question"])
```

The context plan selects questions that may matter. Candidate actions are possible
ways to gather evidence—not proof that a capability works or that the question has
been answered. Do not run every candidate. Gather the smallest relevant evidence set
that can affect the decision, and report missing or unsupported evidence honestly.

## 5. Work, verify, and leave a useful result

The agent works normally in the current environment, then:

1. verifies the changed behavior with the narrowest meaningful check;
2. reviews the actual changes and evidence;
3. preserves specifications, decisions, or findings only when they have future value;
4. runs the applicable Odibi Anchor delivery gate;
5. leaves a concise handoff and PR-ready material when code changed.

Odibi Anchor does not authorize external writes. Project creation, production
data writes, work-item publication, commits, and pushes retain their explicit approval
boundaries.

## Keep the process proportional

Proportionality changes the depth of evidence and durable artifacts, not the
agent-owned orientation, task, and delivery workflow. “Answer directly” means no
user-visible ceremony and no unnecessary permanent project.

| Work | Expected treatment |
|---|---|
| Disposable question | Lightweight internal workflow; answer directly; no permanent project |
| Typo or tiny documentation edit | Orient, frame, edit, focused check, applicable delivery gate |
| Small bug | Reproduce, fix, test, review |
| Feature or multi-session work | Spec, decisions, implementation, tests, handoff |
| Incident or data write | Evidence, impact, approval boundaries, outcome verification |

If a small task produces more framework ceremony than useful work, record that as
dogfood friction rather than making the user supervise internal commands.

## Managed projects

Existing projects are restored automatically. Create a new managed project only when
the work has its own objective, lifecycle, or durable artifacts. Project creation
requires explicit bounded approval, an implementation-mode task, and reinitialization
when the result reports `reinitialize_required=True`. After reinitialization, repeat
orientation; the remembered project should already be active.

## Optional: direct context generators

```python
from odibi_anchor.planning import task_execution_context, quick_context

# Quick brief (80% case)
brief = quick_context(
    "Merge bronze readings into silver, dedup by asset_id + date.",
    goal="Clean merge with no key violations.",
    mode="implementation",
)
print(brief)
```

### The five-question pattern

After calling `task_execution_context()`:

1. **Is it ready?** → `ctx["status"]` + `ctx["readiness"]["score"]`
2. **What's missing?** → `ctx["readiness"]["missing_details"]`
3. **Which evidence questions matter?** → `ctx["context_plan"]["questions"]`
4. **Which actions might help?** → each question's nested `candidate_actions` (registration is not runtime capability or evidence)
5. **Can I hand this off?** → `ctx["handoff"]["prompt_brief"]`

## Optional: data diagnostics

Use these only when the task actually requires data evidence. The
**[Tool Picker](tool_picker.md)** maps plain-English symptoms to focused actions.

### Validate before writing

```python
from odibi_anchor.validation import quality_gate_context

result = quality_gate_context(df, keys=["asset_id", "reading_date"])

# status is "pass", "warn", or "fail"
# is_write_safe is the binary decision
if result["metrics"]["is_write_safe"]:
    # Continue through the project's approved persistence path.
    pass
else:
    print(f"Status: {result['status']}")
    print(result["recommendation"])
    # Apply combined fix
    print(result["fix_all_expr"])
    # Or check individual failures:
    for check in result["checks"]:
        if check["status"] == "fail":
            print(f"  FIX: {check['fix_expr']}")
```

### Compare before and after

```python
from odibi_anchor.tables import diff_tables_by_key, schema_diff_context

# Row-level diff
diff = diff_tables_by_key(old_df, new_df, keys=["id"])
m = diff["metrics"]
print(f"Added: {m['added_key_count']}, Changed: {m['changed_key_count']}, Removed: {m['removed_key_count']}")

# Percentage metrics also available
print(f"Changed: {m['changed_key_pct']:.1%} of keys")

# Ready-to-paste MERGE statement (when status="ok")
if diff.get("merge_expr"):
    print(diff["merge_expr"])

# Schema diff
changes = schema_diff_context(old_df, new_df)
print(changes["summary"])
if changes["metrics"]["is_breaking_change"]:
    print("BREAKING: ", changes["write_safety"]["blockers"])
```
