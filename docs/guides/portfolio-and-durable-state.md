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

For Databricks, configure `local_state_root` as a stable user-specific local-compute base and
`durable_root` on approved durable storage. On shared/serverless compute, use a base such as
`/tmp/odibi-anchor-hodibi`, not a generic path. The managed preparation path derives the physical
runtime root from the effective UID and a non-reversible OS-account fingerprint without mutating
the portfolio. This prevents a recycled numeric UID from reopening stale local state. It
atomically migrates an accessible pre-0.3.20 base once; if the base belongs to a prior compute
identity, it leaves that directory untouched and restores the current identity from the latest
verified v2 snapshot. Never append ephemeral UIDs to the portfolio yourself. Never use a live
SQLite database under `/Workspace`, `/Volumes`, or `/dbfs`. `repository` and
`artifact_namespace` are optional metadata; exact host/project targets are routing authority.

To bound snapshot accumulation, opt into portfolio-wide automatic retention through the
guarded public API. Do not edit the TOML or snapshot storage directly:

```python
from odibi_anchor.portfolio import (
    load_portfolio_document,
    validate_portfolio,
    write_portfolio,
)

config_path = "/absolute/private/path/anchor.toml"
host_id = "databricks-work"
document = load_portfolio_document(config_path)
portfolio = document["portfolio"]
portfolio.setdefault("durability", {})["retention"] = {
    "days": 1,
    "minimum_snapshots": 3,
}
validation = validate_portfolio(portfolio, host_id=host_id)
assert validation["status"] == "valid", validation
written = write_portfolio(
    config_path,
    portfolio,
    expected_sha256=document["sha256"],
)
assert written["validation_status"] == "valid", written
```

After each successful durable checkpoint, Anchor retains every snapshot from the last
`days` and always retains at least the newest `minimum_snapshots`. It removes expired
manifests first, then removes only database or artifact blobs no retained manifest
references. Omitting the retention policy preserves all snapshots. After changing the policy,
start a fresh Python process and rerun the managed launcher before checkpointing so it loads the
new values. Let the next successful managed checkpoint enforce the policy; never manually delete
snapshot manifests or blobs.

Install or reconcile the packaged instructions once per host instruction root:

```bash
anchor setup-host databricks --target /Workspace/Users/name
anchor setup-host amp --target /absolute/path/to/repository
```

The Databricks adapter uses a Workspace Files-compatible resource profile: it installs the
complete mandatory contract, skills, and authored references, but leaves the deeply nested
third-party snapshot cache in the installed wheel. Anchor's runtime reference APIs continue
to read that cache from the package. Other adapters install the complete packaged resource
tree.

Anchor owns only files listed with hashes in
`.odibi-anchor-host-guidance.json`. Identical reruns are no-ops and unchanged managed
files can be upgraded. User edits and unknown collisions fail closed. A pre-existing
`AGENTS.md` or `CLAUDE.md` that already points to `.assistant_instructions.md` is
preserved as compatible user-owned guidance.

If a legacy Anchor installation is missing that manifest, setup adopts only files whose
SHA-256 matches a known released Anchor artifact. Recognized files are upgraded, customized
Anchor operating instructions are preserved as compatible user-owned guidance, and any other
differing byte still stops setup before publication.

Prepare one exact runtime after validation:

```bash
anchor portfolio prepare \
  --config /absolute/private/path/anchor.toml \
  --host databricks-work --project project
```

The result registers the exact route when absent, performs no remote snapshot reads when complete
local state already exists, restores only the newest verified authority snapshot when local state
is absent, and returns copy-ready environment plus bootstrap inputs. On Databricks, cold restore
downloads one latest manifest and only its referenced database and artifact payloads; historical
payloads are not fetched. It does not mutate the caller's environment.
`.active_project` is never consulted.

## Durable state lifecycle

The live database and managed project artifact tree stay on local compute. Substantive authority
writes automatically checkpoint both when `ANCHOR_DURABLE_ROOT` and `ANCHOR_AUTHORITY_ID` are
configured. High-frequency recoverable bookkeeping (`touched`, `skill_loaded`, `task_rebind`,
`new_session`, and irrelevant memory dispositions) remains local until the next substantive
checkpoint instead of publishing a global snapshot per call. The configured retention scan runs
at terminal learning or an explicit checkpoint/snapshot action rather than at every intermediate
checkpoint. Global memory status changes remain immediately durable. Database and artifact-bundle
bytes are copied as opaque immutable
files to `<durable_root>/<authority_id>/snapshots/`; a canonical checksummed manifest
is published last. Remote listing verifies canonical manifests and referenced payload presence
and size without downloading historical payload bytes. Restore or content reuse downloads the
selected payloads and verifies SHA-256 before trusting them. Restore copies durable bytes to local staging, verifies SHA-256,
logical content, SQLite integrity, and safe artifact paths locally, then publishes only to absent
local destinations. Legacy v1 database-only snapshots remain readable; v2 restores require both
destinations so a partial state cannot be presented as complete. When a v2 snapshot moves to a
different configured local state root, restore verifies each continuity owner's canonical managed
path and original route fingerprint, then rebases only local paths and derived checksums in staging.
The immutable snapshot is never modified, and ambiguous continuity fails before publication.

Each live database is bound to one configured work authority. Preparation refuses an
existing unowned database or a database owned by another authority. After making an
independent backup and confirming the intended owner, explicitly adopt legacy local state
with `ensure_database_authority(database, authority_id=..., trust_domain="work",
initialize=True)`; an explicit manual `state snapshot` also records that identity before
copying the database. Automatic preparation never guesses ownership.

Manual inspection and recovery are available without opening SQLite on durable
storage:

```bash
anchor state list --durable-root /Volumes/catalog/schema/anchor \
  --authority enterprise-analytics-ai --databricks
anchor state snapshot --database /tmp/odibi-anchor/.agent_memory.db \
  --artifacts /tmp/odibi-anchor/workspace/projects \
  --durable-root /Volumes/catalog/schema/anchor --authority enterprise-analytics-ai --databricks
anchor state restore --database /tmp/odibi-anchor/.agent_memory.db \
  --artifacts /tmp/odibi-anchor/workspace/projects \
  --durable-root /Volumes/catalog/schema/anchor --authority enterprise-analytics-ai --databricks
```

### Restore classification

Preparation reports `restore.classification` and initializes empty local state only on
confirmed first use:

| Classification | Meaning | Preparation |
| --- | --- | --- |
| `restored` | The newest verified snapshot was restored. | Continues. |
| `local_present` | The local database already exists, so nothing is restored. | Continues. |
| `no_lineage` | The durable root is confirmed reachable and holds no authority marker or snapshot. | Initializes a new database. |
| `not_configured` | No durable root is configured for this host. | Initializes a new database. |
| `durable_root_unavailable` | The local directory or Databricks Volume root cannot be confirmed to exist or be reachable. | Fails closed. |
| `durable_lineage_missing` | `<durable_root>/<authority_id>/AUTHORITY.json` shows snapshots were published, but no valid snapshot remains. | Fails closed. |

Every successful snapshot publication writes the authority marker if it is absent, including
the next checkpoint of a lineage created before v0.3.24. The marker sits outside `snapshots/`,
so older readers ignore it, and its bytes are identical for every writer of the same
authority. Until a pre-v0.3.24 lineage is checkpointed again, an emptied lineage cannot be told
apart from first use. Deleting the whole `<durable_root>/<authority_id>` directory, marker
included, also looks like first use. A local durable root is confirmed only as an existing
directory, so an unmounted mount point that leaves an empty directory is classified
`no_lineage`. On Databricks, a Files API `NotFound` on the snapshot directory counts as
first use only after a Files API metadata request confirms the Volume itself
(`/Volumes/<catalog>/<schema>/<volume>`). A missing or unreachable Volume is
`durable_root_unavailable`. A subdirectory that does not exist yet inside an existing Volume is
first use, so a mistyped subdirectory starts a new lineage at the next checkpoint.

### Restore ownership and recovery

Restore creates the managed projects destination with an exclusive directory creation. It
records that directory's device and inode in an exclusively created
`.odibi-anchor-restore-owner.json` marker with a per-invocation nonce, then copies into the
directory. If the destination already exists or appears before the claim, restore stops with
`restore_destination_conflict` and changes nothing at the destination. If copying or proof verification fails,
restore removes only the files and directories it created, and only while the directory
identity and marker still match. It never deletes a path on the strength of its type or a
prior absence check.

The database is still published last. Just before publication, restore records the staged
database's SHA-256 in the marker. If a later failure leaves the owned tree in place, the
marker stays as the incomplete-restore record. This covers a failed database publication, a
failed cleanup, and a crash before the marker is removed. Preparation and snapshot then report
`restore_incomplete` with copy-ready recovery operations:

```bash
anchor state resume --durable-root /Volumes/catalog/schema/anchor \
  --authority enterprise-analytics-ai --databricks \
  --database /tmp/odibi-anchor/.agent_memory.db \
  --artifacts /tmp/odibi-anchor/workspace/projects
anchor state abandon --durable-root /Volumes/catalog/schema/anchor \
  --authority enterprise-analytics-ai --databricks \
  --database /tmp/odibi-anchor/.agent_memory.db \
  --artifacts /tmp/odibi-anchor/workspace/projects
```

The Python equivalents are `odibi_anchor.durability.resume_restore(...)` and
`abandon_restore(...)` with the same keyword arguments as `restore_latest`. `resume`
re-verifies the tree against the same recorded snapshot manifest, even if a newer snapshot
exists. It fills in only absent entries, refuses any differing or unexpected entry, and then
publishes the database. If the database already holds exactly the bytes this restore
published, whether it was hard-linked or copied into place, `resume` only removes the marker. A
partially written copy does not match, so `resume` refuses and the owner must inspect that
database file; Anchor does not remove it. `abandon` moves the tree, including any foreign entries, to a
sibling `projects.restore-abandoned-<timestamp>-<nonce>/projects` path and deletes nothing.
Both refuse when the directory identity, marker, or requested arguments do not match the
record. `resume` also refuses when a database it did not publish exists. `abandon` refuses when
this restore already published the database, because `resume` finalizes that case. When
`resume` cannot succeed, for example because foreign or differing entries are present, the
error offers only `abandon`. A process crash during copy leaves the same record. `resume` fills
in entries that were never written, but a partially written file differs from the snapshot, so
`resume` refuses and `abandon` is the recovery. If the recorded manifest is missing or no longer
verifies, the error offers only `abandon`; transport errors are raised unchanged so `resume` can
be retried. Directory ownership uses device and inode numbers, so a
filesystem that renumbers them across a remount turns the record into an unowned directory, and
both operations refuse.

These are local filesystem primitives: exclusive creation, identity comparison, and atomic
rename. They detect concurrent changes and keep cleanup from reaching another writer's
content, but they are not distributed locks. Path checks and the following operation are not
one atomic step. Power-loss durability depends on the host filesystem honoring `fsync`. The
Databricks Files API offers no compare-and-swap. A concurrent upload of identical bytes is
accepted after a byte-for-byte readback, and anything else fails closed.

Initial authority is one active compute replica at a time. Use one active writer per durable
authority, and do not restore while another agent, process, thread, or person can create or
modify the destination. Immutable route isolation does not make simultaneous writers on
separate Databricks computes safe. Multi-replica writes require a later lease/CAS service or
transactional server database.

Within one explicit `work` authority, assessed evidence-backed `workbench` observations
may become advisory `all`-project candidates and are retrieved alongside exact-project
memories. Cross-project widening is never inferred, candidates remain non-authoritative,
and personal/work authorities must use separate configurations and stores.
