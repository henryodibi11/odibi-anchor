---
name: schema-design
description: Design or evolve data/API schemas, grain, keys, constraints, compatibility, and migration implications; do not use for ingestion execution, dependency metadata, or reconciliation execution.
---

# Schema design

## When to load
Load for schema or contract design/evolution, grain and key choices, constraints, compatibility, partitioning, or migration implications. Follow [the detailed design workflow](references/backbone.md).

## When NOT to load
Do not load for ingest execution, package/dependency metadata, or reconciliation execution.

## Workflow
Define consumers and grain first; choose stable keys and explicit types/nullability; encode invariants and ownership; model evolution and backward/forward compatibility; assess volume, access, privacy, and lifecycle; specify migration, validation, rollback, and deprecation conditions.

## Enforcement
Self-enforced. Global instructions apply. A schema document does not authorize migration.
