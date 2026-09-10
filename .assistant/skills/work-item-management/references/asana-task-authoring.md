# Asana task authoring

Use this reference to make Asana work consistently actionable, bounded, verifiable, and safe
to resume. It applies the `work-item-management` skill to Asana; it does not create another
skill, replace organization policy, or make Asana the authority for product source, managed
evidence, or strategic decisions.

## Authority and information boundaries

- Treat Asana as a human-facing coordination and execution surface.
- Keep source behavior and implementation authority in the target repository.
- Keep managed Problems, Specs, decisions, gates, and retained evidence in their configured
  project authority. Link them from Asana instead of copying their full contents.
- Never copy employer task names, project structures, custom fields, proprietary examples,
  incident details, internal links, or business context into a personal or public repository.
- Keep organization-specific field mappings and examples in an approved environment-local
  overlay. This generic reference does not define them.
- Search before creation. A new task requires evidence that an appropriate current task does
  not already own the outcome.
- A task recommendation, generated payload, or successful analysis is not write authority.

## Recommended 15-tool MCP baseline

Asana's official MCP tool set can change. At connection time, inspect `tools/list`, verify
every selected name and schema, and fail closed if a required tool differs or is unavailable.
For a host limited to 15 exposed tools, start with this task-management set:

### Read tools

1. `search_objects`
2. `get_task`
3. `get_tasks`
4. `get_my_tasks`
5. `search_tasks`
6. `get_project`
7. `get_projects`
8. `get_status_overview`
9. `get_attachments`
10. `get_me`
11. `get_users`

### Controlled write tools

12. `create_tasks`
13. `update_tasks`
14. `add_comment`
15. `create_project_status_update`

Do not initially expose `delete_task` or `create_project`. Do not substitute another write
tool merely to fill the limit. If portfolio, team, or another read capability becomes
material, replace a demonstrably unused read tool after review rather than expanding the
surface.

`search_tasks` depends on an Asana tier that supports advanced search. If it is unavailable,
use `search_objects` plus contextual `get_tasks`, disclose reduced duplicate-search coverage,
and do not treat this optional capability as making all task authoring unavailable.

An MCP client allowlist limits only the tools that configured host presents to this agent; it
does not create granular Asana OAuth scopes or restrict another client using the same
authorization. Official MCP authorization requests access to all tools available through the
selected workspace and authenticated user's Asana permissions. Refresh `tools/list` as the
server evolves and review additions before exposing them. Treat every write call as an
external shared effect.

## Write authority matrix

| Action | Default | Required authority |
| --- | --- | --- |
| Search and read visible Asana records | Allowed for the named task | Current task and workspace context |
| Draft a proposed task or update without sending it | Allowed | Current task context |
| Create one or more tasks | Blocked until authorized | Exact creation request, destination project/section, and reviewed fields |
| Change title or description | Blocked until authorized | Exact task and fields to change |
| Complete, reopen, reassign, reschedule, or move a task | Blocked until authorized | Explicit lifecycle/effect approval |
| Change dependencies, followers, memberships, or custom fields | Blocked until authorized | Exact fields and intended values |
| Add a comment | Blocked until authorized | Exact task and externally visible message purpose |
| Post a project status update | Blocked until authorized | Exact project, status, audience, and reviewed content |
| Bulk create or update | Blocked | Explicit bounded item set and per-item validation |
| Delete a task or create a project | Unavailable in baseline | Separate high-impact review and tool-surface change |

Do not interpret “looks good,” evidence acceptance, or approval of a draft as permission to
write unless the user also authorizes the exact Asana effect. Never use a write to discover
whether authority exists.

## Required authoring workflow

1. **Identify the destination.** Verify the authenticated user, workspace, project, section,
   and existing task IDs. Stop for ambiguity or an unexpected workspace.
2. **Search for existing ownership.** Search by outcome, system, and relevant identifiers;
   inspect plausible matches before proposing a new task.
3. **Classify the work.** Choose task, subtask, comment, or project status update according to
   the smallest coherent unit. Do not create a project through the baseline.
4. **Load local conventions.** Apply approved organization-specific fields, priorities,
   naming, and required templates from the environment-local overlay. Do not guess them.
5. **Draft the contract.** Use the universal task contract and the relevant scenario add-on
   below. Preserve unknown facts and unavailable evidence explicitly.
6. **Check readiness and authority.** Validate destination, owner, scope, acceptance evidence,
   dependencies, and the exact allowed effect. Present the proposed payload when approval is
   not already explicit.
7. **Perform one bounded write.** Send only reviewed fields. Do not combine unrelated cleanup,
   reassignment, date changes, comments, or lifecycle transitions.
8. **Re-read the result.** For task create/update/comment, fetch the exact task with
   `get_task` and compare material fields with the approved payload. For a project status,
   retain the write receipt and inspect the target through `get_status_overview`; if the exact
   created status cannot be fetched, report that limitation rather than claiming exact
   verification. A successful request alone is not proof of final state.
9. **Return the effect.** Report task/project identity, URL when available, fields changed,
   omitted/unavailable fields, and any further authority required.

## Universal task contract

Use this structure for substantive tasks. Omit genuinely irrelevant sections, but never omit
a material authority, scope, acceptance, dependency, or effect boundary.

```text
Title
<Action verb> <bounded outcome> for <system/process/scope>

Outcome
<State what must become observably true.>

Why / context
<State the supported operational or user need. Separate observation from interpretation.>

Current state
- <Observed fact or retained evidence>
- <Known limitation or unavailable fact>

Scope
In:
- <Included work>

Out:
- <Excluded work>

Acceptance criteria
- <Observable result>
- <Executed validation or comparison>
- <Required retained artifact or evidence>
- <Required failure/recovery behavior where material>

Dependencies and blockers
- <Dependency, owner, prerequisite, or explicitly none known>

Risks and constraints
- <Data, compatibility, operational, privacy, cost, or timing boundary>

Evidence and links
- <Repository, Problem, Spec, decision, run, dashboard, or approved internal reference>

Completion evidence
- <What must be attached, linked, or reported before closure>
```

### Title rules

- Start with a concrete action such as `Validate`, `Reconcile`, `Investigate`, `Repair`,
  `Document`, `Migrate`, or `Decide`.
- Name the bounded object or outcome; avoid “work on,” “handle,” “miscellaneous,” and vague
  system-only titles.
- Do not place credentials, sensitive identifiers, incident details, or long status text in
  the title.
- Do not claim a root cause, solution, priority, or readiness state that evidence has not
  established.

### Acceptance-criteria rules

- Describe observable outcomes, not implementation activity such as “code completed.”
- Include success, material failure, and recovery behavior when relevant.
- Name the required validation without claiming it has already passed.
- Keep policy decisions and assumptions separate from demonstrated requirements.
- Never use a line-count estimate or an agent's confidence as acceptance evidence.

## Scenario add-ons

Add only the section matching the task's material outcome.

### Data pipeline or transformation

```text
Data contract
- Source and target identities:
- Expected grain and keys:
- Processing window or watermark:
- Schema and null/duplicate expectations:
- Control totals or reconciliation:
- Allowed write destination:
- Idempotency and rollback/recovery:
```

### Defect or regression

```text
Observed behavior
- Symptom and environment:
- Expected behavior:
- Reproduction evidence:
- First known occurrence or unknown:
- Affected and unaffected scope:
- Regression test and recovery evidence required:
```

Do not place a presumed root cause in the title or description as fact. Record it as a
hypothesis until supported.

### Investigation or decision

```text
Decision to support
- Exact decision owner:
- Leading hypotheses or options:
- Evidence that can confirm and disconfirm each:
- Stop condition:
- Explicitly not authorized: implementation unless separately approved
```

### Incident follow-up

```text
Follow-up boundary
- Sanitized incident/problem reference:
- Control or recovery gap being addressed:
- Corrective or preventive outcome:
- Verification and reversal condition:
- Owner and due-date rationale:
```

Use the `incident-response` skill for an active incident. This add-on is for separately
authorized follow-up work after stabilization.

### Documentation or runbook

```text
Documentation contract
- Audience:
- Task or decision the reader must complete:
- Authoritative behavior sources:
- Commands/examples to verify:
- Failure modes and operating boundaries:
- Drift owner:
```

### Refactoring or technical debt

```text
Preservation contract
- Behavior that must remain unchanged:
- Complexity or recurring cost being reduced:
- Supported reason to act now:
- Characterization and regression evidence:
- Explicit non-goals:
```

## Task, subtask, comment, or status update

- **Task:** one independently valuable outcome with its own acceptance evidence and owner.
- **Subtask:** a necessary bounded contribution whose value and closure depend on one parent
  outcome. Link the parent and avoid duplicating its full contract.
- **Comment:** progress, evidence, blocker, decision request, or handoff on an existing task.
  Do not silently rewrite stable task scope through comments.
- **Project status update:** an audience-level summary of supported progress, risk, decisions,
  and next milestones. It is not a substitute for task-level evidence.

Use this comment/update structure:

```text
State: on track, at risk, blocked, or completed
Since last update: <supported change>
Evidence: <checks, artifacts, or links>
Risk/blocker: <owner and impact, or none known>
Next: <bounded next action and authority needed>
```

## Definition of ready

A task is ready only when:

- the outcome and destination are unambiguous;
- current authority permits the work;
- in-scope and out-of-scope boundaries are explicit;
- acceptance criteria are observable;
- dependencies, blockers, owner, and material constraints are known or explicitly unknown;
- required source/evidence links are available or their absence is a stated blocker; and
- no sensitive material is being copied outside its authorized environment.

If these conditions are not met, leave the task in the organization's appropriate intake or
draft state rather than inventing readiness.

## Definition of done

Do not mark a task complete until:

- every acceptance criterion has an evidence disposition;
- required checks actually ran and their results are linked or summarized truthfully;
- artifacts and external effects were re-read after writing;
- skipped, failed, and unavailable evidence remains explicit;
- residual risk, follow-up ownership, and reopening conditions are recorded where material;
  and
- the final Asana task state matches the supported lifecycle outcome.

Do not close a task merely because implementation stopped, a branch merged, or no more work is
planned. Publication, deployment, operational acceptance, and task closure may be separate
transitions.

## Consistency and anti-duplication rules

- Keep the description as the stable work contract; use comments for progress and evidence.
- If scope or acceptance criteria change after work starts, record the decision and why rather
  than silently rewriting history.
- Link one canonical source for each artifact. Do not paste stale copies into several tasks.
- Preserve exact IDs and URLs in handoffs; names alone can be ambiguous.
- Use organization-defined custom fields only after reading their current definitions.
- Do not infer priority from urgency language, due dates from estimates, or assignees from
  prior tasks.
- Do not create placeholder subtasks merely to make a plan look complete.
- Do not use task count, comment volume, or completion status as proof of delivered value.

## Terminal return after an Asana write

In addition to the global terminal contract, report:

```text
Asana effect:
- Workspace and project identity
- Task/project GID and URL when available
- Tool called and exact effect performed
- Fields created or changed
- Post-write re-read result
- Fields intentionally not changed
- Duplicate search performed
- Additional Asana effect or owner decision still required
```

Never expose credentials, access tokens, sensitive descriptions, or unauthorized task content
in the terminal return.
