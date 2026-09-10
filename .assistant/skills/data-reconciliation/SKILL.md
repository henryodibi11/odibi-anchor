---
name: data-reconciliation
description: Define grain, keys, mappings, tolerances, and controls to prove source-to-target completeness and correctness; do not use for generic profiling/onboarding or ordinary ETL, though it may compose with data operations for mutation.
---

# Data reconciliation

## When to load
Load for reconciliation, control totals, source-to-target proof, expected-difference classification, or unexplained deltas. Follow [the preserved reconciliation workflow](references/backbone.md).

## When NOT to load
Do not load for generic profiling/onboarding or ordinary ETL. Compose with data operations only when the work actually mutates data.

## Workflow
Freeze source/target scope and as-of time; define grain, key alignment, mappings, filters, and tolerances; compare population, uniqueness, totals, nulls, and distributions; classify expected versus unexplained differences; retain reproducible discrepancy evidence; conclude pass/fail with residuals and owners.

## Enforcement
Self-enforced. Global instructions apply. Matching headline counts alone is not reconciliation proof.
