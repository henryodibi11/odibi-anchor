---
name: debugging
description: Reproduce, isolate, diagnose, fix, and verify non-incident defects, regressions, stack traces, or wrong results; do not use for live incident command or performance-only investigation.
---

# Debugging

## When to load
Load for failures, regressions, exceptions, wrong results, silent data defects, or root-cause diagnosis. Use [the diagnostic backbone](references/backbone.md), [evidence-first planning](references/planning.md), [logging](references/logging.md), and [recovery](references/recovery.md) as needed.

## When NOT to load
Do not load for active incident command or when latency/resource performance is the primary question.

## Workflow
Reproduce the smallest failure; preserve exact inputs and environment; separate observations from hypotheses; localize by boundaries and controlled comparisons; seek confirming and disconfirming evidence; fix the cause with the smallest change; verify regression, nearby behavior, and recovery.

## Operational evidence
Capture evidence before committing to a diagnosis. Use the narrow collector that answers
the known question: `spark_diagnose`, `uc_context`, `delta_changes`, `run_diff`,
`environment_diff`, `observe_table`, or `table_trend`. Collector statuses such as
`unavailable`, `denied`, and `failed` are material uncertainty, never affirmative evidence.
Distinguish local, fixture, and live provenance.

## Enforcement
Self-enforced. Global instructions apply. Logging and successful reruns alone do not prove root cause.
