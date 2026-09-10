# Agent context and deterministic next operations

Odibi Anchor derives a bounded context envelope from current authority so an agent does not
have to reconstruct project, task, lifecycle, repository, capability, evidence, or policy state
from prose. The envelope is read-only: it reports what verified sources establish and never
installs tools, repairs the environment, or invents semantic intent.

## Default response

Lifecycle responses include a compact `agent_context` with:

- current state and blocker;
- verified project and accepted-task authority;
- current lifecycle obligations;
- one `next_operation`: an executable `anchor(...)` call, semantic-input preparation, an explicit
  runtime-bootstrap operation, or a truthful terminal state; and
- a packaged skill or reference pointer when that operation needs one.

Use `anchor("context", view="summary")` for evidence, artifact, and capability counts, or
`anchor("context", view="full")` for their bounded provenance. Compact remains the default so
routine calls do not flood the model context.

Statuses are deliberately small and truthful: `verified`, `attested`, `derived`, `missing`,
`unverified`, `unavailable`, `not_applicable`, `stale`, and `conflicting`. Mutable
`.active_project` metadata is never route authority; only the immutable `RouteBinding` can verify
project, target, and artifact roots.

## Prepare a high-friction operation

Static MCP schemas cannot change per project. Use the deterministic preparation path instead of
calling help and reconstructing parameters:

```python
prepared = anchor(
    "prepare",
    operation="work_item.create",
    inputs={
        "title": "Harden parser",
        "outcome": "Malformed input is rejected",
    },
)
```

The result always groups fields as:

- `required_missing` — semantic input that must be supplied;
- `recommended_missing` — applicable context that materially improves correctness;
- `prefilled_verified` — values derived from immutable authority, with provenance; and
- `not_applicable` — excluded fields, with reasons.

The initial bounded set covers task/context creation, Problem creation/update and evidence
capture, Work Item creation/update, gate qualification, handoff preparation, and explicit
learning assessment. Preparation never invents task intent or a learning outcome. Literal
placeholders such as `TBD`, `...`, `<goal>`, and `${goal}` fail before mutation. Explicit
`unknown`, `unavailable`, or `not_applicable` values remain valid when truthful.

## Baseline and capability facts

Source-change task acceptance records an immutable repository baseline and a qualification bound
to that baseline and task window. Qualification reports exactly one outcome:
`smoke_passed`, `existing_failure_reproduced`, `unrelated_failure`, `unavailable`, `unsafe`, or
`not_applicable`. Explicit repository configuration has precedence over bounded checked-in
conventions; an inferred unsafe command is never executed. A checked-in positive outcome defines
a check method, not a result for a new task: current retained evidence must be supplied before it
can be attested. Later authority drift makes the qualification stale.

Capability projection is likewise nonmutating. It distinguishes available, missing,
incompatible, unverified, unavailable, and not-applicable Python, package, Git, repository,
direct-Python, CLI, MCP, and execution-surface facts.

## Canonical implementation handoff

For a managed project with an accepted task and immutable route, planning completion can persist a
self-contained handoff without restating facts already known to Odibi Anchor:

```python
handoff = anchor("snapshot", mode="handoff", output_format="dict")
```

The versioned packet derives the objective and supplied business reason, scope and exclusions,
source baseline/current state, linked managed records and decisions, evidence, acceptance and
validation obligations, policy, risks/questions, expected artifacts, definition of done, and one
copy-ready first implementation action. Missing semantic fields are explicit. The packet is
content-addressed under `artifact_root/notebooks/handoffs/` and recorded as a managed artifact.

Before using a retained packet, re-derive its authority with
`validate_canonical_handoff(packet, session_state, route_binding)`. A changed route, managed
record, or repository state returns `stale`, identifies the conflicts, and withholds the first
action. The default recommendation is a fresh implementation thread in the same environment;
Odibi Anchor does not infer token or thread-length thresholds. Persisted baselines exclude
task-start file bytes, freshness includes current changed-content fingerprints and exact linked
record hashes, and handoff directories must remain contained non-symlink paths.

Direct calls to `planning.handoff_context` and unbound legacy snapshots retain their existing
caller-authored compatibility contract.
