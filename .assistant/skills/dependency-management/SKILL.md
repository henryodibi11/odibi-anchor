---
name: dependency-management
description: Evaluate, add, remove, pin, upgrade, or troubleshoot dependencies and verify lock, metadata, compatibility, and environment effects; do not use for general coding standards or import-flow comprehension.
---

# Dependency management

## When to load
Load for package additions/removals, upgrades, CVEs, version conflicts, import failures, pins, or dependency metadata changes. Follow [the preserved decision workflow](references/backbone.md).

## When NOT to load
Do not load for general code standards or merely tracing imports through existing code.

## Workflow
Establish need and supported environments; inspect current declarations and resolution; compare maintained alternatives; bound compatibility/security/licensing risk; make the smallest metadata change; regenerate only owned lock artifacts; verify clean installation, imports, tests, and packaging.

## Enforcement
Self-enforced. Global instructions apply. Installation capability does not authorize environment mutation.
