---
name: code-comprehension
description: Map unfamiliar code, dependencies, behavior, and change impact when asked to explain a flow or locate implementation; do not use for defect diagnosis, schema design, or implementation planning.
---

# Code comprehension

## When to load
Load to explain code behavior, trace a flow, locate implementation, map dependencies, or assess impact before change. Follow [the preserved mapping workflow](references/backbone.md).

## When NOT to load
Do not load for root-cause defect diagnosis, schema design, or implementation planning.

## Workflow
Bound the question; locate entry points and contracts; trace calls and data; distinguish observed behavior from inference; map callers, side effects, tests, and impact; report uncertainty with evidence. Route durable findings with the task's runtime `capture_guidance`.

## Enforcement
Advisory. Global instructions apply. Loading this skill is not evidence that its workflow or any gate passed.
