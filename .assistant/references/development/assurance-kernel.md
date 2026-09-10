# Odibi Anchor Assurance Kernel

> Project-authored normative reference for the first Professional Assurance Model vertical slice.

## Identity and authority boundary

- Assurance schema version: `1.0`
- Core catalog version: `anchor-assurance-core/1.1`
- Canonical eleven-control core catalog SHA-256: `a64fad1c5f7fd1834daf1b087667191f4ec0a15e2e7066ce66016fba6d2c1e0e`
- Combined core and overlay catalog SHA-256: `f58e6ca37d29134e0ace3f94b96315746aa1fd324a1cd120e5f5da3e0cba5d8c`
- Mode: `shadow`

The assurance kernel is advisory. It does not block, authorize, satisfy an existing obligation,
change effect permission, change a legacy gate result, or replace the TaskProfile. The legacy gate,
effect, and authority paths remain authoritative. An assurance exception records explicit context
but never rewrites an evidence state or authorizes action.

The authority invariant is: shadow findings do not block or authorize work.

## Deterministic tier selection

Tier selection uses normalized TaskProfile consequence facts only. It never uses file count, changed
line count, repository scope, scope breadth, or phase count.

| Tier | Rule |
|---|---|
| T0 | Low-risk `read_only` work without a higher-tier trait. |
| T1 | Medium risk or `artifact_only` work. |
| T2 | High risk, `source_change`, `data_change`, or a material-change trait such as `schema-change`, `public-contract-change`, `cross-system`, `material-migration`, or `rollback-design`. |
| T3 | `critical-impact`, `destructive`, or `irreversible`; also high-risk `data_change`. |

Every plan includes `functional-suitability`. T2/T3 or mutating work adds `reliability` and
`safety`. Source changes add `maintainability`, `adaptability`, and `performance-efficiency`.
Contract or cross-system traits add `compatibility`; security domains/traits or T3 add `security`;
artifact or interaction/UI work adds `interaction-quality`. Attributes follow the package's fixed
canonical order, and controls are ordered by control ID.

## Closed core control catalog

| Control | Attribute | Applicability | Accepted evidence |
|---|---|---|---|
| AK-001 | Functional suitability | Every plan | Accepted task context plus current successful result-bearing verification or a mode-appropriate inspection. |
| AK-002 | Compatibility | Compatibility selected | Matching current caller-required evidence whose referenced evidence IDs resolve. |
| AK-003 | Maintainability | Maintainability selected | Current unfiltered successful test evidence retaining target and zero exit code. |
| AK-004 | Safety | Safety selected | Current successful review and preflight evidence. |
| AK-005 | Reliability | Reliability selected | Current successful output/outcome evidence retaining source and observation time. |
| AK-006 | Safety | T3 and mutating | Current successful recovery/rollback evidence, with advisory exception context allowed. |

All six controls have `advisory` disposition. Every assessment contains AK-001 through AK-006;
non-applicable controls contain no evidence IDs and no exception ID.

Core catalog version 1.1 also contains the five result-backed T1 controls
`Anchor-T1-TESTS`, `Anchor-T1-STATIC-RATCHET`, `Anchor-T1-OUTPUT-CONTRACT`,
`Anchor-T1-DISTRIBUTION`, and `Anchor-T1-SCOPE-INTEGRITY`. They retain their separate
result-backed evaluation and rollout path; adding them is the reason the immutable core catalog
identity advanced from 1.0 to 1.1. Conditional standards overlays have their own `1.0.0` catalog
identity while remaining part of the single exported `CONTROL_CATALOG`.

## Evidence normalization and evaluation

Caller-required evidence and control-derived evidence remain separate lanes. Ledger evidence retains
its ID, kind, status, source, UTC observation time, and provenance. Dispatcher timings receive stable
session-sequence IDs. Pre-task and superseded observations are stale; invocation-only or unsupported
observations are unavailable; malformed evidence is ignored and cannot pass. State precedence is:

```text
failed > stale > unavailable > missing > satisfied
```

Evidence is current only within the accepted task verification epoch. A test filtered by a marker,
or missing its target or explicit zero exit code, cannot satisfy AK-003. Missing referenced IDs cannot
satisfy AK-002. Timestamps and exception bounds must be explicit UTC ISO-8601 values.

## Contracts and lifecycle integration

`AssurancePlan`, `AssuranceAssessment`, `ControlResult`, and `AssuranceException` are frozen strict
contracts with exact-field JSON serializers. Unknown fields, catalog controls, schema versions,
states, dispositions, and non-UTC timestamps are rejected.

Task assurance is staged from the exact normalized TaskProfile and injected privately into the task
planner. It is projected as top-level `assurance` output and committed to session state only after
task readiness acceptance. A rejected or errored replacement task retains the previous accepted
assurance state. Task reset clears profile and assurance state together.

After the legacy gate completes, the gate refreshes assurance from current evidence and adds only
`metrics.assurance_shadow` plus an advisory finding. If assurance planning, normalization, or
evaluation fails, task or gate output reports `mode=shadow`, `status=degraded`, and bounded
diagnostics. The legacy result and all authority decisions remain unchanged.

## Scope limit

This slice intentionally has no score, aggregate pass/fail, blocking disposition, compliance export,
provider adapter, framework crosswalk, exception workflow, policy promotion, or new native skill.
Those capabilities require later reviewed slices.
