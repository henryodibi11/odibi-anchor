---
name: data-onboarding
description: Ingest and profile a new CSV, Excel, JSON, API, or table source and validate its contract, quality, and refresh setup; do not use for ongoing joins or writes, schema-only design, or source-to-target reconciliation proof.
---

# Data onboarding

## When to load
Load for a new dataset, unfamiliar source, first ingestion, format handling, profiling, quality acceptance, or initial refresh design. Start with [the onboarding backbone](references/backbone.md), then use [CSV](references/csv.md), [Excel](references/excel.md), [JSON](references/json.md), [profiling](references/profiling.md), [quality](references/quality.md), or [refresh](references/refresh.md) as needed.

## When NOT to load
Do not load for ongoing joins/writes, schema-only design, or reconciliation as the deliverable.

## Workflow
Confirm provenance and format; inspect before parsing; establish grain, keys, types, volume, nulls, and anomalies; define acceptance rules; preserve raw input; make ingestion repeatable and idempotent; verify freshness and drift handling before handoff.

## Enforcement
Self-enforced. Global instructions apply. Access and write effects still require explicit authority.
