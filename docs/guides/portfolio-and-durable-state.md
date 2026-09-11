# Portfolio, host setup, and durable state

Anchor's PortfolioV1 is a user-owned routing configuration. It records one work
authority, named hosts, stable projects, host-specific target roots, and advisory
personas. It contains no credentials and has no global active-project selector.

Create an explicit scaffold, complete its reported missing fields, and validate it:

```bash
anchor portfolio scaffold \
  --config /absolute/private/path/anchor.toml \
  --host databricks-work --adapter databricks \
  --target-root /Workspace/Users/name/project --project project \
  --authority enterprise-analytics-ai
anchor portfolio validate --config /absolute/private/path/anchor.toml --host databricks-work
```

For Databricks, configure `local_state_root` on local compute (for example
`/tmp/odibi-anchor`) and `durable_root` on approved durable storage. Never use a
live SQLite database under `/Workspace`, `/Volumes`, or `/dbfs`. `repository` and
`artifact_namespace` are optional metadata; exact host/project targets are routing
authority.

Install or reconcile the packaged instructions once per host instruction root:

```bash
anchor setup-host databricks --target /Workspace/Users/name
anchor setup-host amp --target /absolute/path/to/repository
```

Anchor owns only files listed with hashes in
`.odibi-anchor-host-guidance.json`. Identical reruns are no-ops and unchanged managed
files can be upgraded. User edits and unknown collisions fail closed. A pre-existing
`AGENTS.md` or `CLAUDE.md` that already points to `.assistant_instructions.md` is
preserved as compatible user-owned guidance.

Prepare one exact runtime after validation:

```bash
anchor portfolio prepare \
  --config /absolute/private/path/anchor.toml \
  --host databricks-work --project project
```

The result registers the exact route when absent, restores the newest verified
authority snapshot only when the local database is absent, and returns copy-ready
environment plus bootstrap inputs. It does not mutate the caller's environment.
`.active_project` is never consulted.

## Durable SQLite lifecycle

The live database stays on local compute. After each successful authority write—including
task acceptance, evidence/memory writes, and terminal closure—Anchor automatically snapshots
it when `ANCHOR_DURABLE_ROOT` and `ANCHOR_AUTHORITY_ID` are configured. Snapshot bytes are copied as opaque immutable
files to `<durable_root>/<authority_id>/snapshots/`; a canonical checksummed manifest
is published last. Restore copies durable bytes to local staging, verifies SHA-256,
logical content, and SQLite integrity locally, then publishes only to an absent local
destination.

Manual inspection and recovery are available without opening SQLite on durable
storage:

```bash
anchor state list --durable-root /Volumes/catalog/schema/anchor --authority enterprise-analytics-ai
anchor state snapshot --database /tmp/odibi-anchor/.agent_memory.db \
  --durable-root /Volumes/catalog/schema/anchor --authority enterprise-analytics-ai --databricks
anchor state restore --database /tmp/odibi-anchor/.agent_memory.db \
  --durable-root /Volumes/catalog/schema/anchor --authority enterprise-analytics-ai
```

Initial authority is one active compute replica at a time. Immutable route isolation
does not make simultaneous writers on separate Databricks computes safe. Multi-replica
writes require a later lease/CAS service or transactional server database.

Within one explicit `work` authority, assessed evidence-backed `workbench` observations
may become advisory `all`-project candidates and are retrieved alongside exact-project
memories. Cross-project widening is never inferred, candidates remain non-authoritative,
and personal/work authorities must use separate configurations and stores.
