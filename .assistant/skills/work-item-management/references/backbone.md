# Work-item management

> Preserved technique source for the native owner.

## When to load

Load when planning, drafting, creating, updating, commenting on, or closing work in
Asana, Azure DevOps, Jira, or another team work-management system. Also load when a
specification must be decomposed into team-visible tickets or delivery evidence must be
linked back to an existing ticket.

## When NOT to load

Do not load for a private direct task that does not need team coordination. Do not turn
every agent execution step into a ticket. Do not use this guidance to administer users,
teams, workspaces, projects, billing, or destructive provider operations.

## Enforcement

Provider reads may be used to resolve context and detect duplicates. Any team-visible
mutation—including task creation, field/status changes, assignment, comments, and
closure—requires explicit bounded approval. Planning or “prepare this for Asana” means
draft, not publish. A request naming the exact reviewed write set can approve that set;
it does not authorize later unrelated mutations.

`anchor("work_item")` writes only local managed artifacts. It never invokes a provider.
External tools remain separately permissioned. `anchor("work_item", "approve", ...)`
records approval only after it exists in the conversation; it does not manufacture it.

## Decide the artifact depth

| Work | Artifact behavior |
|---|---|
| Read-only, direct, low-risk, or private | No work item by default |
| Ordinary team-visible outcome | One work-item draft |
| High-risk team-visible work without another complexity trigger | One required draft |
| Multi-phase, full-rigor, or cross-system team-visible work | Outcome-level work-item set |
| Existing assigned ticket | Link/import it; do not create a duplicate |
| Explicit caller evidence requirement | Create or link the required work item |

Use the BPS kernel to define complex outcomes and boundaries, not to add ceremony to a
small ticket. Keep implementation details in the specification. A ticket should state
what outcome another teammate can understand, own, review, and validate.

## Draft workflow

1. Inspect the destination project and related tasks.
2. Search for duplicates before creating anything.
3. Create a local draft with `anchor("work_item", "create", ...)`.
4. Include outcome, context, scope, non-goals, acceptance criteria, validation, risks,
   dependencies, and links to the Problem Record/specification.
5. Use `preview` to produce the provider, exact operation set, and draft fingerprint.
6. Show the user a concise publish preview: creates, updates, comments, destination,
   assignees, dates, dependencies, and intentionally omitted unknowns.
7. Obtain explicit approval. Never invent dates, owners, priority, or approval.
8. Record the bounded approval against the exact fingerprint.
9. Invoke only the approved provider operations.
10. Record returned provider IDs/URLs with `record_publish`.

If a material draft field changes after approval, preview and approve again. One
approval may cover a clearly enumerated batch, but each local artifact must retain the
scope and receipt relevant to it.

## Provider-neutral ticket quality

Every publishable work item needs:

- a concise outcome-oriented title;
- why the work matters and the current state;
- in-scope and out-of-scope boundaries;
- observable acceptance criteria;
- validation expected before completion;
- known dependencies and material risks;
- links rather than duplicated specification/Problem Record content; and
- no fabricated owner, due date, status, priority, or external state.

After delivery, add the PR URL, tests and target-environment validation, residual risks,
and follow-ups. A local draft is not evidence that an external ticket exists. Only a
provider receipt with an ID and URL may support a `work-item` attestation.

## Asana profile

When Asana is the target, [Asana task authoring](asana-task-authoring.md) is the sole
provider-specific tool, authoring, approval, and verification profile. Read and apply it
before drafting or performing an Asana effect. Keep this backbone's provider-neutral
approval workflow and failure behavior; do not maintain a competing tool list or map unknown
tool names as equivalent.

## Failure behavior

- MCP unavailable: retain the local draft and provide a manual publication preview.
- Duplicate found: link or update it after approval; do not create another.
- Ambiguous destination/assignee/date: leave it unknown and ask only if publication
  depends on it.
- Provider write partially succeeds: record only returned receipts, report the exact
  remainder, and request approval again before compensating writes.
- Receipt cannot be verified: do not claim publication or PR work-item evidence.
