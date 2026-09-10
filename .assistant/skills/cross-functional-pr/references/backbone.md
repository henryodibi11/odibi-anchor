# Cross-functional PR workflow

> Preserved technique source for the native owner.

Use this workflow for an explicit PR or reviewer handoff spanning audiences such as
product, data, operations, security, and documentation. Repository contribution rules
own the format. Resolved house profiles guide source quality but do not certify readiness.

## Define the audience and decision

Name who must review, what each reviewer can decide, and which facts are outside their
usual context. A good handoff is a compact teaching package: it supplies enough domain
and system context to evaluate the change without hiding consequential uncertainty.

## Build the review story

1. **Outcome:** State the user or operator impact and why the change is needed.
2. **Scope:** Name changed contracts and deliberate non-goals; separate refactoring from
   behavior change.
3. **Approach:** Explain the owning boundary and material alternatives or tradeoffs.
4. **Risk:** Describe compatibility, data, security, operational, rollout, and rollback
   implications that actually apply.
5. **Evidence:** Map acceptance claims to checks that ran, with scope and result.
6. **Questions:** Ask reviewers for the decisions that require their expertise.

Use plain domain language for infrastructure or library behavior that a target reviewer
may not know. Explain non-obvious rationale near the code or contract that owns it; do not
duplicate generic tutorials or require every reader to understand irrelevant internals.

## Evidence table

| Claim | Evidence to provide |
| --- | --- |
| Behavior changed as intended | Focused behavioral check and relevant regression result |
| Compatibility preserved | Caller, schema, API, or migration evidence as applicable |
| Data output is trustworthy | Grain, keys, controls, and representative comparison |
| Operation is safe | Effect boundary, rollback, observability, and failure-mode evidence |
| Documentation is current | Updated source plus validated links, commands, or examples |

Record failed, skipped, partial, and unavailable checks explicitly. A screenshot, test
count, PR template checkbox, or prose assertion is not a substitute for decisive output.

## Cross-functional impacts

Include only applicable sections, but make omissions deliberate:

- consumer and product behavior;
- data contracts, quality, retention, or backfill;
- access, secrets, privacy, or threat boundaries;
- deployment order, feature exposure, monitoring, and rollback;
- documentation, support, ownership, and follow-up work;
- concurrent branches, migrations, or dependencies that constrain merge order.

## Readiness review

Before handoff, verify that repository requirements and applicable house profiles were
followed, the final diff matches the narrative, and every readiness statement is backed
by an actual result. Surface authority conflicts instead of converting advisory guidance
into a blocker or claiming conformance that was not assessed.

Do not use this workflow for independent PR review. Preparing evidence and judging it
must remain separate responsibilities when independent review is required.
