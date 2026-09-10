---
name: authoring-governed-memories
description: Authors precise, scoped, evidence-backed memory candidates when reusable knowledge should be preserved and manages their lifecycle; do not use for raw notes, task authority, or full-store audits.
---

# Authoring governed memories

Use when a reusable fact, constraint, preference, failure pattern, or operating insight
should improve future tasks. A memory is a compact retrieval index into authoritative
evidence, not a replacement for source, policy, a Spec, decision, Work Item, or skill.

## When NOT to load

Do not load for raw task notes, transient observations, store-wide governance audits,
or when an authoritative source, Spec, decision, Work Item, skill, or reference should
own the information instead.

## Classify before capture

- Put implementation authority in approved Specs, Work Items, decisions, and owner input.
- Put broad repeatable procedures in a skill or reference.
- Put reproducible detail in source, tests, reports, or notebooks.
- Put transient findings in the task journal or learning observation.
- Create a memory candidate only when the claim is reusable, likely to affect a future
  decision, and small enough to retrieve without importing its source artifact.

## Check overlap and scope before capture

1. Search bounded memory in the active project and trust domain using the proposed claim's
   concrete nouns, operational consequence, tags, and relevant files. Page through the
   filtered results when the first bounded page is insufficient; never widen to another
   project or trust domain merely to find a match.
2. Compare claims semantically, not by exact wording. Claims are equivalent only when they
   assert the same operational consequence under compatible applicability, limits, freshness,
   and contradiction conditions. Shared keywords or a common topic do not make them equivalent.
3. If an equivalent memory exists, do not create a competing candidate. Route the new
   observation through the configured learning/consolidation lifecycle so it preserves its own
   source, revision, environment, evidence, and uncertainty as recurrence for the existing
   canonical claim. Never rewrite the earlier provenance or increment a counter as a substitute
   for evidence. If no supported public attachment or consolidation surface exists, retain the
   observation and report the canonical memory ID plus the blocked consolidation need; do not
   manufacture the link or create a duplicate as a workaround.
4. Keep materially different, narrower, contradictory, or differently scoped claims separate
   and record their relationship when supported. Quarantine or supersede through the governed
   lifecycle rather than silently merging disagreement.
5. Default to project-local applicability. Propose cross-project applicability only when the
   claim is portable, sanitized, supported outside one project where appropriate, and contains
   no private or proprietary detail. Scope and authority are independent: widening applicability
   does not activate or confirm a candidate, and confirmation does not make a claim global.

## Authoring contract

1. Write one atomic, falsifiable claim. State the operational consequence rather than a
   vague topic or activity log.
2. Assign the narrowest correct project, repository, component, or trust scope. Never
   widen private, employer, client, or proprietary knowledge into another trust domain.
3. Retain provenance: authoritative source identity, relevant paths or records, observed
   revision or time, evidence method, and known limits or freshness trigger.
4. Add retrieval cues that future agents are likely to use: concrete nouns, failure
   symptoms, subsystem names, and stable tags. Do not keyword-stuff.
5. Screen the proposed content and evidence for credentials, personal data, proprietary
   content, sensitive paths, and copied material without reuse rights. Never store secrets.
6. Capture through the public learning/memory lifecycle as candidate-only. Creation,
   recurrence, retrieval, application, evaluation, successful task completion, and their
   counters are evidence and telemetry only; none activates or confirms a memory.

## Verification and lifecycle

- For a mechanically provable claim, attach only a supported typed verifier whose exact
  request, source, inputs, assertions, bounds, and environment are independently checked.
- For preferences, conventions, policy, risk acceptance, or procedural authority, require
  explicit governed owner evidence; never simulate it with a code test or infer it from
  repeated use. On an interactive Windows host with no Slack configuration, the command opens
  a local Yes/No owner-presence dialog. In a single-user Databricks session it first returns an
  exact challenge without granting authority; stop until the owner personally sends that phrase
  in a new Genie message, then pass only that reply as `in_session_approval`. If configured Slack
  is unavailable, the owner may explicitly choose this lane with
  `provider="databricks_in_session"`; never fall back automatically. This lower-assurance
  lane records an in-session assertion, not authenticated identity; workspace login or Genie tool
  approval alone is insufficient. Complete Slack configuration remains the default stronger
  remote authenticated path. Request candidate activation with
  `anchor("memory", "promotion", command="request_owner_activation", memory_id=...)`; after
  activation, request confirmation separately with
  `anchor("memory", "promotion", command="request_owner_confirmation", memory_id=...)`.
- Record applications and evidence-backed evaluations when a selected memory materially
  influenced work. These immutable lifecycle records—not legacy mutable counters—inform
  retrieval ranking. Verify against current authority even when the memory is active.
- For mechanically provable claims, use the supported typed verifier promotion path. For
  owner-governed claims, use only the owner-presence commands above. `anchor("confirm", ...)`
  is blocked legacy compatibility, not a promotion path. Authors and agents cannot promote
  from retrieval, application, evaluation, recurrence, task success, or counter thresholds.
- Quarantine a contradicted, unsafe, unverifiable, or wrongly scoped claim through the
  public lifecycle. Supersede a valid but replaced claim with explicit lineage to its
  replacement. Correcting an active or confirmed memory also appends an authority-withdrawal
  event atomically; a later reactivation requires a new governed owner decision. Never delete
  or rewrite history merely to make retrieval look clean.
- Load `auditing-memory-governance` for store-wide review, seed or migration changes,
  reconciliation, retrieval-quality audits, or disputed promotion evidence.

## Quality check

Before capture, confirm: one claim; future decision value; bounded overlap search completed;
equivalent recurrence routed to its canonical claim; narrow project/trust scope; any proposed
cross-project applicability independently justified; searchable wording; authoritative provenance;
disconfirming evidence considered; limits and revalidation trigger; no secret or trust-boundary
leak; candidate-only authority. If any check fails, improve the source artifact or do not create
a memory.

## Return

Report the candidate claim, scope, provenance, evidence and counter-evidence, retrieval cues,
freshness rule, sensitivity result, lifecycle action, and any authority still required.

## Enforcement

Self-enforced. Odibi Anchor's task, capture, verification, disposition, gate, and
learning contracts remain authoritative; this skill grants no mutation or promotion right.
