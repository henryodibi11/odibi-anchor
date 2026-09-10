---
name: performance-investigation
description: Measure, profile, attribute, and verify latency, throughput, memory, resource, or cost regressions; do not use for correctness debugging unless performance is the primary question.
---

# Performance investigation

## When to load
Load for a slow service/query/pipeline, benchmark regression, OOM/resource pressure, skew, bottleneck, or cost problem. Follow [the profiling workflow](references/backbone.md).

## When NOT to load
Do not load for correctness defects unless performance is the primary question, or for an active incident where containment owns the outcome.

## Workflow
Define workload and metric; establish reproducible baseline and variance; profile before changing; attribute time/resources to a component; test one hypothesis at a time; compare equivalent runs; verify correctness and report tradeoffs and residual bottlenecks.

## Enforcement
Advisory. Global instructions apply. Anecdotes and single noisy runs are not performance proof.
