---
name: auditing-memory-governance
description: Audits memory seeds, lifecycle migrations, reconciliation, provenance, trust scope, diagnostics, retrieval quality, and promotion candidates; do not use for ordinary implementation, routine task-memory review, or generic learning capture.
---

# Auditing memory governance

Use only when memory itself is changed or reviewed: reviewed seeds, lifecycle/schema
migration, reconciliation, provenance or trust scope, diagnostics anomalies, cross-thread
harvest, stale/duplicate/quarantined records, retrieval quality, or future promotion
decisions. Ordinary substantive tasks follow the global bounded-review contract without
loading this skill.

## When NOT to load

Do not load for ordinary implementation, routine bounded task-memory review, generic
learning capture, or merely because a task bootstrap returned memory selections.

## Bounded audit workflow

1. State the audit question, active project/trust domain, authorized mutations, and stop
   conditions. Keep employer/private sources outside independent or public scope.
2. Review the accepted task's bounded `memory_context`. Report each selected memory ID,
   status, source/provenance, scope, and match reason, or report no selections. Every
   selection has candidate-only advisory authority; it is not verification.
3. Build the smallest evidence set that can confirm and disconfirm the claim. Prefer, in
   order: current authority records and approvals; runtime/schema and public API behavior;
   executed tests and integrity checks; immutable lifecycle, terminal, or reconciliation
   records; provenance documents; candidate narrative.
4. Use public Odibi Anchor inspection surfaces: scoped task memory, diagnostics,
   storage inspection, replay verification, seed inspection, and hygiene dry-runs. Inspect
   only relevant bounded IDs and cohorts. Never scan the entire store by default and never
   mutate SQLite directly.
5. Challenge eligibility, lineage, project/trust isolation, redaction, stale/rejected/
   retired/quarantined/superseded exclusion, duplicate amplification, idempotency, migration
   rollback, diagnostic denominators, and retrieval false positives/negatives. For apparent
   overlap, compare the operational claim, applicability, limits, freshness, and contradiction
   conditions—not keywords alone. Classify each relationship as equivalent recurrence,
   materially distinct, contradictory, or superseding. Consolidate equivalent recurrence only
   through the public lifecycle into one canonical claim while retaining every source revision,
   evidence reference, environment, uncertainty, and immutable lineage. Never consolidate
   across trust domains or use consolidation to widen applicability or authority. For seeds or
   cross-thread harvest, verify source rights, public provenance, and absence of secrets,
   personal data, employer content, and proprietary implementation.
   Confirm retrieval ranks the complete eligible project-scoped corpus before applying its
   output bound, FTS exactly mirrors source rows, and diagnostics count immutable lifecycle
   rows with mathematically consistent denominators.
6. Separate observed facts, interpretation, unavailable evidence, and uncertainty. If
   bounded retrieval or another required surface is unavailable, preserve that boundary and
   follow existing fail-closed/degraded policy; never infer rows, widen trust, or invent
   evidence.
7. Make approved changes only through the owning public lifecycle/API. Verify the
   installed/public path as well as helper behavior. Creation, recurrence, retrieval,
   application, evaluation, successful execution, and their counters never confirm or
   promote a candidate. Mechanically provable claims require a supported typed verifier.
   Preferences, conventions, policy, and procedural authority require distinct governed owner
   requests: `request_owner_activation` for candidate→active, then
   `request_owner_confirmation` for active→confirmed. `anchor("confirm", ...)` remains blocked
   legacy compatibility and is not an authority lane. Without complete Slack configuration,
   an interactive Windows host uses a visible local owner-presence dialog. A single-user
   Databricks session uses a separate prepare→owner-message→approve sequence with exact challenge
   binding. Its receipt records a lower-assurance in-session assertion; workspace authentication,
   Genie execution approval, or an agent-copied challenge does not prove owner approval.
   Corrections to active or confirmed memories must append a promotion withdrawal atomically;
   any later owner reactivation is a distinct durable authority event.
   Treat applicability scope as a separate decision: project-local is the default;
   cross-project use requires portable, sanitized evidence and never follows automatically from
   recurrence, consolidation, activation, or confirmation.
8. Verify gate-time typed verification does not depend only on retrieval luck: require a fair
   bounded sweep of eligible project-local structured-learning candidates and active memories,
   prioritizing claims with fewer prior verifier runs.
9. Dispose every task selection. When one influenced work, record application and an
   evidence-backed evaluation; otherwise use the truthful supported disposition. Close
   learning with genuine observations or `nothing_reusable_learned`; never force a lesson.

## Return

Report trust boundary, inspected IDs/cohorts, evidence and provenance, adversarial checks,
mutations and authority, dispositions/evaluations, installed/public verification,
unavailable boundaries, and the decision or next authority required.

## Enforcement

Self-enforced. The global task, memory-disposition, review, gate, and learning contracts
remain authoritative; this skill adds no mutation permission.
