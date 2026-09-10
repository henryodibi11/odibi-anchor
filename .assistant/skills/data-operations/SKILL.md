---
name: data-operations
description: Safely transform, join, merge, write, or refresh operational data with preflight and post-write validation; do not use for first ingestion, schema-only design, or reconciliation as the sole deliverable.
---

# Data operations

## When to load
Load for ETL, transformations, joins, merges, writes, refresh execution, or destructive/data-mutation safety. Follow [the write-safety sequence](references/backbone.md) and [planning checks](references/planning.md).

## When NOT to load
Do not load for first ingestion, schema-only design, or a read-only reconciliation deliverable. Compose with data reconciliation only when reconciliation includes mutation.

## Workflow
Define grain and contracts; profile inputs; validate join cardinality and merge keys; predict row/count/null effects; use bounded idempotent writes; preserve rollback; validate destination schema, quality, counts, and rerun behavior.

## Enforcement
System hooks may govern writes. Global instructions apply. Skill loading never authorizes mutation or proves a gate.
