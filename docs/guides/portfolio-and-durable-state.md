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
`/tmp/odibi-anchor-<user>`, not a generic path. The managed preparation path derives the physical
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

### Recording the portfolio for a host

The managed launcher selects its portfolio in this order:

1. the `ANCHOR_PORTFOLIO_CONFIG` launcher init global;
2. the `ANCHOR_PORTFOLIO_CONFIG` environment variable;
3. the host binding sidecar `<instruction-root>/.odibi-anchor-host-binding.json`;
4. the instruction-root default `<instruction-root>/.odibi-anchor/anchor.toml`.

Record the exact portfolio once per instruction root, especially when it does not live at the
default path:

```bash
anchor setup-host databricks --target /Workspace/Users/name \
  --portfolio /Workspace/Users/name/.odibi-anchor/anchor.toml [--host databricks-work]
```

The Python equivalent is `setup_host(target, adapter=..., portfolio_config=..., host_id=...)`.
Setup validates that the portfolio declares the host, with this adapter and this instruction root,
before it publishes anything. It writes the sidecar only after guidance is verified, through the
Workspace API for `/Workspace` targets. Without `--host`, exactly one matching host must exist. The
sidecar is canonical JSON naming the absolute portfolio path, the host and the instruction root.
It is not listed in the guidance manifest, so older Anchor versions neither manage nor reject it.
Rerunning with a different portfolio replaces the binding and reports the previous one.

The launcher validates the sidecar strictly: canonical JSON, an absolute portfolio path, its own
instruction root, and a portfolio host that declares that root. It then passes the recorded host
to bootstrap. Failures are explicit and list every searched path:

- `managed_host_binding_invalid`: the sidecar is malformed, or names another root or host.
- `managed_portfolio_ambiguous`: the sidecar and an existing instruction-root default name different
  portfolios. The owner chooses one, then reruns `setup-host --portfolio` or passes
  `ANCHOR_PORTFOLIO_CONFIG`.
- `managed_portfolio_not_found`: the selected portfolio is missing. A sidecar-selected portfolio
  never falls back to an `ANCHOR_HOME` installed runtime.

A launcher whose instruction root is itself named `.assistant` (a nested
`.assistant/.assistant` install) is reported as a nested install. The error names the probable
intended launcher and that launcher's default portfolio. The launcher never switches to either one
automatically. Hosts without a sidecar keep the v0.3.24 search exactly. Never copy, move or
hand-edit a portfolio or the sidecar to work around discovery.

Host setup for the `databricks` adapter also installs the source-checkout launcher
`agent_bootstrap.py` at the instruction root, next to `.assistant/agent_bootstrap.py`. The
installed file set is unchanged in this release. The root launcher works only beside a `src/`
checkout. Anywhere else it fails immediately with `source_launcher_outside_checkout` and a
copy-ready call to `.assistant/agent_bootstrap.py`, which is the only launcher for managed hosts.

### Reconciling host guidance drift

When managed files differ from the manifest, plain `setup-host` (and therefore bootstrap) stops
before writing anything. It raises `host_guidance_drift`, which lists every drifted file with its
classification, and its next operation is the read-only reconcile plan:

```bash
anchor setup-host databricks --target /Workspace/Users/name --reconcile
```

Reconcile classifies every managed path:

| Classification | Meaning | Applied action |
|---|---|---|
| `current` | equals the active package | keep |
| `released_version` | equals the manifest hash or bytes shipped by a released Anchor version | replace, or delete when no longer packaged |
| `unmanaged_edit` | matches neither | replace or delete only with explicit approval |
| `missing` | absent | install, or drop from the manifest |

`--reconcile` alone is a dry run that returns the full plan and writes nothing. To apply it, add
`--apply`. Unmanaged edits also need `--approve-replace-edited`. The Python equivalent is
`setup_host(target, adapter=..., reconcile=True, dry_run=False, approve_replace_edited=True)`.
Applying the plan:

1. backs up every replaced or deleted file, the previous manifest and a `BACKUP.json` receipt to
   `<instruction-root>/.odibi-anchor-host-backups/<utc>/`, then reads each backup back;
2. reinstalls from the active package, with the same rollback as plain setup;
3. verifies every hash and the manifest.

Bootstrap never reconciles. It stays fail-closed, and it never replaces an edited file. The
released-version table is generated from release tags with
`python scripts/generate_released_guidance_hashes.py --write`. Regenerate it after each release
tag. Bytes recorded in a host's manifest are recognized even when they are missing from the table.

### Planning a fresh compute launch

Before launching a project on a new compute identity, inspect the launch without changing anything:

```bash
anchor doctor --fresh-compute --config <absolute portfolio> --host <host-id> --project <project-id>
```

The Python equivalent is `doctor(fresh_compute=True, config_path=..., host_id=..., project_id=...)`.
The plan reports each step with a `status` (`ok`, `action_required`, `blocked` or `not_evaluated`)
and its exact `next_operation`:

1. `portfolio_discovery`: what the launcher at the host's instruction root would select, including
   sidecar validation, ambiguity and nested installs.
2. `host_guidance`: the reconcile dry run, and whether bootstrap would accept the host as it is.
3. `durable_lineage`: `local_present`, `not_configured`, `no_lineage`, `restored` (bootstrap restores
   the named latest snapshot), `durable_root_unavailable`, `durable_lineage_missing` or
   `restore_incomplete`.
4. `route_comparison`: the managed descriptor that launch would use, read from local state or, in
   memory, from the latest snapshot. It is compared with the portfolio target by the route
   classifier (`match`, `unregistered`, `descriptor_damaged`, or a `route_target_conflict`
   classified as `probable_move` or `ambiguous`).
5. `launch_inputs`: the exact launcher and init globals.

The plan's `next_operation` is the first blocked or pending step's operation, otherwise the
launch. Doctor never writes to the portfolio, host guidance, local runtime state or durable
storage. On Databricks, remote snapshot reads stage downloads in self-deleting temporary
directories. It never suggests copying local state from another compute.

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

## Package version policy and bootstrap telemetry

On Databricks, the managed launcher requires the latest stable release from the package index
by default. To control upgrades, or to start without package-index access, pin an exact release.
The launcher reads the first pin it finds, in this order:

1. the `ANCHOR_PACKAGE_VERSION` launcher init global;
2. the `ANCHOR_PACKAGE_VERSION` environment variable;
3. the optional portfolio field `hosts.<id>.package_version`, for the host whose
   `instruction_root` matches the launcher;
4. otherwise, the latest stable release.

With a pin, the launcher requires `installed == pin`, does not call the package index, and
prints the exact `%pip install` command on a mismatch. Pins must be exact `MAJOR.MINOR.PATCH`
releases of 0.3.24 or newer, because older launchers ignore pins. Anchor versions before 0.3.24
reject the `package_version` portfolio field as unknown, so upgrade every host before adding it.

Remote bootstrap calls are bounded. `ANCHOR_WORKSPACE_API_TIMEOUT_SECONDS` (default 30) bounds
Workspace API calls during host-guidance reconciliation, and `ANCHOR_VERSION_CHECK_TIMEOUT_SECONDS`
(default 5) bounds the package-index lookup. Both accept values above 0 and up to 600. A timeout
fails closed with `error_code="bootstrap_phase_timeout"`, naming the phase, layer, elapsed time
and item. No integrity check is skipped.

The managed startup packet includes `timings` (schema `odibi-anchor-bootstrap-timings-v1`). It
records elapsed milliseconds and an outcome for each phase, including launcher-side portfolio
discovery and the version check, host guidance with the slowest files, local-state identity,
restore, route binding, repository provider, init and orient. A failed bootstrap carries the same
summary as `exc.bootstrap_timings`. To measure cold and warm bootstraps on a target compute, run
`python scripts/bootstrap_benchmark.py --launcher <instruction-root>/.assistant/agent_bootstrap.py
--project-id <project-id> --runs 3`. Use `--isolation in-process --runs 1` when notebook
credentials are not inherited by child processes. The helper performs no writes beyond the
launcher's normal bootstrap effects.

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
