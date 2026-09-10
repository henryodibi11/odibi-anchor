# Anchor House Engineering Standard — Data and Platform Profile

> Profile: `anchor-house-engineering/v1` (data/platform). Conditional advisory guidance;
> it grants no data access, mutation authority, compute permission, or evidence status.

This profile supplements the house hub only for the components activated below. Apply
repository and approved project contracts first, especially for grain, keys, naming,
access, retention, environments, and write controls.

## Applicability

| Component | Activate only when | Do not infer from |
| --- | --- | --- |
| Generic data | Canonical domain or task metadata identifies data work or a data change. | A word such as “table” in prose. |
| DataFrame | Changed paths, dependencies, or configuration identify a DataFrame API. | Generic Python work. |
| Spark | Canonical technology, dependencies, configuration, or changed paths identify Spark. | SQL or data work alone. |
| SQL | SQL changed paths or canonical technology/configuration identifies SQL. | A datastore mentioned only as context. |
| Databricks | Explicit task traits, repository configuration, or changed Databricks asset paths. | Spark use by itself. |
| Unity Catalog | Explicit Unity Catalog configuration, assets, or task traits. | Any catalog-like noun. |
| Notebook layout | A notebook asset or explicit notebook task for the detected platform. | Ordinary source modules. |

Inactive components contribute no rules. Resolution should report the activated
components and their evidence; uncertainty does not activate a platform default.

## Generic data rules

- **[Applies: generic data]** Define grain, business meaning, keys, null semantics,
  expected schema, and freshness before transforming or persisting data.
- **[Applies: generic data]** Profile inputs enough to challenge assumptions about
  uniqueness, cardinality, missingness, malformed values, and drift.
- **[Applies: joins]** Validate key quality and expected cardinality on both sides;
  predict fanout and unmatched behavior before execution.
- **[Applies: deduplication]** Use a complete, deterministic ordering with a stable
  tie-breaker. State which record wins and test ties, nulls, and reruns.
- **[Applies: writes or refreshes]** Design idempotent reruns, bounded effects,
  checkpoints or rollback, and an explicit response to partial failure.
- **[Applies: persisted outputs]** Compare schema, grain, key quality, row counts or
  control totals, and important aggregates against pre-write expectations. Explain
  legitimate differences rather than treating counts alone as proof.
- **[Applies: pipelines]** Separate reads, pure transformations, validation, and writes
  so that data decisions are reviewable before effects occur.

## DataFrame and Spark rules

- **[Applies: DataFrame]** Name important stages by meaning when that makes schema and
  grain changes easier to inspect; avoid parallel variables that obscure ownership.
- **[Applies: DataFrame]** Select columns deliberately at contracts and make join,
  null, and duplicate behavior explicit instead of relying on engine defaults.
- **[Applies: Spark]** Account for lazy execution when placing validation, metrics,
  caching, and writes. Avoid repeated actions unless their evidence is worth the cost.
- **[Applies: Spark]** Prefer engine-native expressions for scalable transforms; use a
  user-defined function only when the native API cannot express the required behavior
  and the tradeoff is measured or documented.
- **[Applies: Spark]** Make partitioning, ordering, and merge assumptions explicit where
  they affect correctness, repeatability, or material cost.

## SQL and ingestion rules

- **[Applies: SQL]** Use names and query structure that expose logical stages, grain,
  join conditions, filters, and tie-breakers. Follow the repository's dialect and format.
- **[Applies: untrusted textual ingestion on a supporting engine]** Normalize blank
  text and use tolerant conversion such as `TRY_CAST`; retain or measure rejected values
  so malformed input is visible.
- **[Applies: validated values or engines without tolerant conversion]** `CAST` remains
  valid when the input contract supports it and failure behavior is intentional.
- **[Applies: SQL writes]** Keep read/transform logic inspectable before DDL or DML, and
  use the environment's authorized mutation path and transaction semantics.

## Databricks, Unity Catalog, and notebooks

- **[Applies: Databricks]** Use repository-approved workspace, compute, dependency,
  secret, and deployment patterns. Availability of a platform does not authorize use.
- **[Applies: Unity Catalog]** Follow local catalog, schema, ownership, privilege, and
  object-naming policy. A three-part object name is a house default only when local
  policy is silent and the active context is unambiguous.
- **[Applies: platform notebook]** Keep purpose, parameters, dependencies, operations,
  and verification easy to locate. A read → validate → transform → write → verify cell
  flow is a default, not a required cell count or substitute for modular source code.
- **[Applies: platform notebook]** Put environment-specific names and tunables in the
  repository's established configuration surface; do not create a second configuration
  source merely to match an example.

## Golden examples

These examples explain two conditional rules; they are not templates or gates.

### Example 1 — Principle: deterministic deduplication

```sql
WITH ranked AS (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY customer_id
        ORDER BY source_updated_at DESC, ingestion_id DESC
    ) AS record_rank
    FROM staged_customers
)
SELECT * EXCEPT (record_rank)
FROM ranked
WHERE record_rank = 1;
```

The second ordering field resolves equal timestamps; the owning data contract must
identify an appropriate stable tie-breaker.

### Example 2 — Principle: tolerant external-text conversion

```sql
SELECT
    raw_amount,
    TRY_CAST(NULLIF(TRIM(raw_amount), '') AS DECIMAL(18, 2)) AS amount
FROM external_rows;
```

This is appropriate only when the engine supports tolerant conversion and malformed
external text is expected. Measure null conversions or rejected rows at the boundary.
