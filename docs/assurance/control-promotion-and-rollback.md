# Control promotion, exceptions, and rollback

Promotion is a separately authorized policy change for one exact `control_id@version`. The pure
`assess_promotion` function assesses retained evidence; it does not mutate policy or perform a
promotion. Changing control semantics creates a new version and resets qualification.

## Promotion sequence

Only one forward step is valid: `shadow -> warn -> block`. A packet is held when any required
denominator, cohort, coverage cell, guardrail, or approval is missing. Aggregate improvement never
overrides a failed criterion, unavailable evidence, or critical outcome.

`shadow -> warn` requires the complete matrix and sealed holdout, at least five real applicable
tasks spanning two tiers and both scopes, policy-compliant outcomes and ceremony, no critical
escape, required genuine agent/host coverage, and two independent approvals: assurance reviewer
and non-implementing maintainer.

`warn -> block` additionally requires at least 20 applicable real tasks over 14 elapsed days,
successful remediation instructions, zero unexcepted critical false positives, zero critical
escapes, no open regression, and rollback demonstrated in a clean installed distribution.
Synthetic records test these rules but never satisfy them.

## Request or review an exception

An exception must be signed and contain an ID, exact control version and mode, narrow repository,
task, and subject scope, rationale, owner, compensating control/evidence, requester, independent
approver, issue link, creation and expiry, and use count. Maximum lifetime is 30 days. Renewal is a
new independently reviewed record.

Exceptions cannot grant effects or authority, convert unavailable evidence to pass, suppress
scorecard reporting, or apply to future control versions. Expired, malformed, broad, unsigned, or
self-approved records fail closed. Review reports must show outcomes both with and without active
exceptions.

## Automatic rollback

An affected blocked control returns to warn on:

- policy or digest mismatch;
- evaluator leakage;
- an unexcepted critical false positive;
- a critical escaped defect attributable to the control;
- unavailable evidence treated as pass; or
- expired approval.

Other guardrail breach can return warn to shadow according to policy. Rollback changes only the
affected exact control version's mode. It retains all runs, judgements, scorecards, comparisons,
exceptions, decisions, approvals, and policy history; existing safety gates remain in force.
Re-promotion requires a new packet and new approvals.

## Approval procedure

1. Reproduce score construction from retained raw records and verify all denominator provenance.
2. Adjudicate every critical outcome and a random sample of non-critical outcomes while blinded.
3. Verify exact control version, tier/scope independence, scenario-family split, elapsed time,
   real-task status, agent/host identities, leakage scan, exception scope, and installed rollback.
4. Return `APPROVE`, `HOLD`, or `BLOCK` with findings. Two qualifying `APPROVE` decisions only make
   the packet eligible for a separately authorized policy change; they do not perform that change.

Unavailable evidence or coverage is reported as unavailable or pending. It is never inferred from
a synthetic test, another transport, a demonstration, or an aggregate score.
