# Code comprehension workflow

> Preserved technique source for the native owner.

Use this workflow when understanding or explaining existing behavior is the outcome.
It does not authorize a source change, diagnose a defect, or replace applicable
repository and reference guidance.

## Bound the question

Identify the audience, requested depth, relevant revision, and decision the explanation
must support. Ask only when different interpretations would materially change the work.
Otherwise state the chosen scope and proceed.

## Map before tracing

Locate the smallest authoritative surface that can answer the question:

- entry points and public contracts;
- implementations, dependencies, and callers;
- inputs, outputs, state, and side effects;
- configuration and runtime boundaries;
- tests and retained behavior evidence.

Use repository-native search and mapping tools. Tool output locates evidence; it does not
establish behavior until the relevant source is read.

## Trace behavior

Follow execution order from an entry point through the requested outcome. At each
material step, record:

1. the received contract and assumptions;
2. the decision or transformation;
3. the returned value or state transition;
4. side effects and external calls;
5. failure and recovery behavior.

Trace the normal path first, then the branches that can change the user's conclusion.
For pipelines, also track grain and schema changes. For stateful code, identify who owns,
mutates, persists, and clears the state.

## Verify the model

- Compare the explanation with tests, executable configuration, and supported runtime
  behavior where available.
- Predict one normal and one boundary outcome, then verify both against source or an
  authorized read-only check.
- Search callers before claiming a contract is private, unused, or safe to change.
- Separate observed facts, interpretation, and unresolved uncertainty.

## Explain for the audience

Lead with purpose, inputs, outputs, and the main flow. Add a focused call tree, data-flow
diagram, or state table only when it reduces prose. Name files and symbols that support
each consequential claim. Explain design rationale only when source, tests, decisions,
or history provide evidence; label inference as inference.

## Change-impact handoff

When comprehension informs a later source change, identify affected contracts, callers,
tests, compatibility risks, effects, and applicable house profiles. Establish a separate
write-capable task before editing. The read-only explanation itself grants no authority.

## Stop conditions

Stop and report the evidence gap when the relevant revision, generated source,
dependency implementation, runtime, or permission is unavailable and the gap could
change the conclusion. Do not fill missing behavior with a plausible story.
