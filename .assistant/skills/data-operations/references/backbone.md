# Data Operations — Shift-Left Write Safety

> Preserved technique source for the native owner.

Use this workflow for an authorized transform, join, merge, write, or refresh. It owns
mutation safety; the conditional data/platform profile supplies applicable engineering
guidance but grants no data access, effect, compute, or approval authority.

## Applicable assurance overlay

When structured selection activates `data.quality`, use
`.assistant/references/assurance/standards-overlays.md` for the applicable outcomes and
result-evidence contract. This workflow remains the procedure owner; the pointer creates no
obligation by itself.

## Establish the write contract

Before mutation, identify the approved source and destination, grain, keys, schema,
freshness, ownership, access boundary, write mode, rerun behavior, and rollback or
recovery plan. Confirm that the active task and environment authorize the exact effect.

Profile enough of each input to test assumptions about nulls, duplicates, malformed
values, drift, and control totals. For joins, evaluate key quality, cardinality, fanout,
and unmatched rows on both sides. For merges, prove source-key uniqueness and define
matched, unmatched, and deletion behavior.

## Shift-left sequence

```text
contract and profile
        ↓
join or merge preflight
        ↓
quality and effect prediction
        ↓
bounded authorized write
        ↓
destination and rerun verification
```

Predict expected row-count or control-total movement before writing. Explain tolerances
and legitimate changes; equal counts alone do not prove correctness.

## Execute the effect safely

- Use the active environment's approved mutation surface. When a Odibi Anchor action
  owns the write, route the effect through that action so its controls can observe it.
- Keep transformation and validation inspectable before the write.
- Prefer idempotent or checkpointed operations with bounded targets and explicit partial-
  failure behavior.
- Do not broaden a target, create compute, alter access, or bypass a blocked check merely
  because the operation is technically possible.

## Verify after the write

Check the destination's schema, grain, key uniqueness, null behavior, row counts or
control totals, material aggregates, freshness, and intended deletions. Re-run or simulate
the rerun contract when idempotency is a requirement. Retain the executed method, exact
scope, result, and limitations.

If verification fails, stop further dependent writes, preserve evidence, and use the
approved recovery or rollback path. Do not describe a partial or unverified write as
successful.

## Composition

Use [planning checks](planning.md) for a multi-stage mutation. Compose with onboarding,
schema design, or reconciliation only when that workflow owns a separate material
outcome; skill composition never widens effect authority.
