---
name: writing-tests
description: Design or implement deterministic tests, fixtures, coverage, and validation strategy for explicit test creation, repair, or missing regression coverage; do not use for generic review/gating or routine verification after edits.
---

# Writing tests

## When to load
Load for explicit test creation/repair/strategy, fixture design, coverage gaps, or regression-test authoring. Follow [test standards](references/backbone.md), [risk-based planning](references/planning.md), and [real-client scenarios](references/scenarios.md).

## When NOT to load
Do not load for generic review/gating or merely running focused verification after an edit.

## Workflow
Translate behavior and failure risk into assertions; choose the lowest useful test level; isolate deterministic fixtures; test public outcomes and material boundaries; include regression and negative cases; avoid implementation-coupled mocks; prove tests fail for the intended defect and pass for the fix. Apply the task's runtime `capture_guidance`; skipped, blocked, and unavailable checks are never passing evidence.

## Enforcement
Self-enforced. Global instructions apply. Test presence, invocation, or coverage percentage alone is not behavioral proof.
