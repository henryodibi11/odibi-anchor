# Changelog

All notable changes to Odibi Anchor are documented here. This project follows [Semantic Versioning](https://semver.org/).

## [0.3.28] - 2026-10-10

Faster cold starts and fewer wasted agent round trips.

### Changed

- **Restore unpacking is linear.** Validating a restored artifact bundle compared each file against
  every earlier entry, so time grew with the square of the file count (a 25.5 s step in a live
  Databricks cold boot). Collision, duplicate, traversal and limit checks are unchanged; 2,000
  files now validate in about 0.15 s instead of 12 s. Restore fill no longer sorts the whole tree.
- **Guidance metadata receipt on repeat boots.** On Databricks, the third and later boots on the
  same compute compare Workspace file metadata (one listing per guidance directory) with a
  compute-local receipt instead of re-reading every guidance file. The first boot on a compute adds
  no calls, the second records the receipt, and any difference falls back to full content
  verification. The startup packet reports `guidance.verification` (`content` or
  `metadata_receipt`); metadata equality is not a content hash. Receipts must be owned by the
  current user and not group- or world-writable, or they are ignored.
- **Every missing prerequisite at once.** `anchor("task")` and `anchor("learning", "capture")`
  refusals keep their first message but list every independent problem in
  `context.problems`. Task refusals point to the matching `prepare` operation; capture refusals
  give a corrected call when only unknown fields were wrong.
- **Start-then-check for long test runs.** `anchor("test", ..., wait_seconds=N)` (0–90) returns a
  `running` packet with a copy-ready `poll=True` call when the run outlives the wait; finalization
  and workflow measurement happen in the polling call. A `request_id` is reserved before the run
  starts, so a concurrent duplicate never runs twice; after a restart, polling fails closed. A
  poll is refused if any target file changed during the run, even if its bytes were restored.
  Running requests left by another task window are stopped when the request table is full.

### Fixed

- **Clearer edge failures.**
  - The launcher and `doctor` install with `pip --no-cache-dir`, and the launcher explains that a
    just-published release can take minutes to appear (never add `--pre`).
  - A rejected delivery approval names each failed check (reply text, owner, transport, message
    ids) without echoing the reply (`delivery_approval_mismatch`).
  - Delivery readback failures report the endpoint, HTTP status and retry-after
    (`destination_readback_unavailable`).
  - `anchor("doctor")` and other module functions called as actions point to the exact import
    (`module_function_not_action`).
  - The `stale_plan` refusal and `help("workflow")` give the rework order that avoids a
    dirty-worktree refusal.

## [0.3.27] - 2026-10-10

Hotfix for a Databricks serverless install failure.

### Fixed

- **No protobuf downgrade on Databricks serverless.** The `databricks` and `all` extras pinned
  `protobuf<6` (added in 0.3.4 after a non-blocking resolver warning). On current serverless,
  `%pip install "odibi-anchor[databricks]==…"`, the command the launcher and `doctor` print,
  downgraded protobuf 6.33.5 to 5.29.6, which breaks Spark Connect and crashed the kernel. The
  extras no longer mention protobuf; `databricks-sdk` declares its own supported range
  (`>=4.25.8,<7`). An environment whose preinstalled protobuf falls outside that range may see pip
  upgrade it with a non-blocking resolver warning, as before 0.3.4.
- For 0.3.26 and earlier on serverless, install without the extra
  (`%pip install "odibi-anchor==0.3.26"`): the base package has no dependencies, and serverless
  already provides a qualified `databricks-sdk`.

## [0.3.26] - 2026-10-10

Recovery and agent-experience release: the supported descriptor repair and target move for #29
and #30, deterministic host discovery, and the lifecycle friction found while self-hosting (#39).

### Added

- **Descriptor repair (#29).** `anchor portfolio repair-descriptor --config C --host H --project P
  --expected-sha256 S [--approve]` (also `odibi_anchor.startup.repair_portfolio_descriptor` and
  `anchor("project", "repair-descriptor", ...)`). It rebuilds only the route fields from the
  portfolio target and keeps the Markdown body byte for byte. A dry run returns the exact text;
  an approved repair writes a backup, an atomic hash-checked replacement and a receipt, then
  re-reads the result. Refusals are `descriptor_repair_refused` with a classification.
  `managed_descriptor_damaged` from portfolio preparation now offers the copy-ready dry run.
- **Route-field protection.** The gate blocks a task edit that changes or damages the bound
  `PROJECT.md` route fields with `managed_descriptor_route_change`; body-only edits pass.
- **Target move (#30).** `anchor portfolio move-target` (single, `--mapping` batch, `--dry-run`,
  `--resume`, `--rollback`) and `odibi_anchor.move_target`. Preflight checks the destination,
  quiescence (no open task window or non-terminal workflow) and exact hashes. A create-only
  journal under the artifact root records every step, so a crash at any point resumes or rolls
  back exactly. New codes: `target_migration_blocked` and `target_migration_incomplete`;
  `route_target_conflict` gains `migration_pending`, and a workflow bound to a prior target
  reports `workflow_bound_to_prior_target`. `set_target` refusals name the supported move.
- **Host discovery and reconcile (#30).** `anchor setup-host ... --portfolio <path> [--host <id>]`
  records a validated host binding that the launcher prefers over the default search.
  `setup-host --reconcile` classifies each managed guidance file (`current`, `released_version`,
  `unmanaged_edit`, `missing`), dry runs by default, and applies with backups; edited files need
  explicit approval. An apply refuses any file that changed after the plan read it. Drift at bootstrap reports every file and points to the reconcile plan.
  Released guidance bytes come from `_released_guidance_hashes.json`; regenerate it with
  `python scripts/generate_released_guidance_hashes.py --write` after each release tag.
- **Fresh-compute doctor.** `anchor doctor --fresh-compute --config --host --project` walks
  discovery, guidance, durable lineage, route comparison and launch inputs read-only, each with an
  exact next operation. `doctor()` now has a top-level `status` (`ready` or `attention`).
- **Result envelope.** Every dispatcher dict result carries one additive `envelope` key (v1) with
  `outcome`, `effects`, `retry_safety`, `obligations`, `next_operation` and `error`, identical
  across in-process, CLI and MCP calls; an approved `repair-descriptor` and a non-dry-run
  `move-target` report their writes and re-hash the files they wrote. MCP compact responses are budgeted; the boot banner goes
  to stderr; the mandatory contract shrank from about 26 KB to under 18 KB with every rule kept.
- **Snapshot descriptor integrity.** v2 manifests and `snapshot_state` report
  `descriptor_integrity` per project without blocking.

### Changed

- **Lifecycle friction (#39).** Planned workflow paths no longer consume the checkpoint cap;
  `task_rebind` restores touched files, known_bad, skills and spec links whose bytes still match;
  `continuation=True` works after the prior task closed; `test(request_id=...)` replays a result
  after a client timeout; `request_delivery_approval` accepts `timeout_minutes`; risk-downgrade,
  capture and checkpoint-cap refusals carry exact corrective calls; `task_rebind(abandon=True)`
  closes an unrestorable orphan without granting authority.
- **Visibility of defaulted descriptor fields.** The markdown project listing marks
  `(defaulted: …)`, and the startup packet reports `descriptor.integrity_status` and
  `descriptor.defaulted_fields`, so a boot that depends on a defaulted `id` is visible.
- **Restore timings.** A performed restore reports `status: "restored"`, and cold-start restore
  is timed per step (`restore_listing`, `restore_download`, `restore_verify_database`,
  `restore_extract_artifacts`, `restore_fill_artifacts`, `restore_publish`) inside
  `runtime_preparation`.
- Restore fill and cleanup act through held directory descriptors where the platform supports
  `dir_fd`.

### Fixed

- Deriving a missing `project_type` no longer raises when `target_root` is a symlink loop
  (Python 3.11 and 3.12); before, one such descriptor broke project listing for every project.
- Route lines inserted into a CRLF descriptor follow its line endings even when the closing
  `---` has no trailing newline.

## [0.3.25] - 2026-10-10

Hotfix for a 0.3.24 compatibility regression found by live Databricks validation.

### Fixed

- **Descriptor compatibility.** 0.3.24 required `id` and `project_type` in managed `PROJECT.md`
  frontmatter, so descriptors that 0.3.23 booted (for example hand-restored ones without an
  `id:` line) failed with `managed_descriptor_damaged`. Because the Databricks launcher requires
  the latest release, such projects could not start at all. `target_root` stays mandatory, so the
  #29 artifact-root fallback stays closed. A missing `id` defaults to the managed directory name,
  as in 0.3.23; a present but different `id` still fails closed. A missing `project_type` is
  derived: `managed` when the target is the artifact root, otherwise `referenced`. Project
  listings report `defaulted_fields`. `set_target` on a never-launched project inserts absent
  route lines.

## [0.3.24] - 2026-10-10

Hardening release: contain and diagnose the incidents reported in #24, #28, #29, #30 and #31.
The safe target-migration and descriptor-repair operations ship in 0.3.26.

### Fixed

- **Restore ownership (#24).** `restore_latest` claims its artifact destination exclusively
  (exclusive `mkdir`, inode identity and a per-invocation ownership marker). Cleanup removes only
  entries that invocation created, while its identity still holds. A directory created by a
  competing writer is never deleted; that case raises `restore_destination_conflict`.
- **Restore qualification.** Startup initializes empty local state only when restore classifies
  first use (`classification: no_lineage`: the durable root is reachable, with no snapshot
  manifests and no authority marker). An unreachable durable root raises
  `durable_root_unavailable`. A lineage whose authority marker exists but whose snapshots are gone
  raises `durable_lineage_missing`. A plain `FileNotFoundError` during restore is no longer read
  as "no snapshot". The startup packet reports `restore.classification`
  (`restored`, `no_lineage`, `local_present`, `not_configured`). A durable root writes
  `<durable_root>/<authority_id>/AUTHORITY.json` on its first publication, and existing lineages
  get it on their next publication. Older readers ignore the marker. Two cases still classify as
  first use: a local durable root that is an empty, unmounted mount point, and a lineage published
  before 0.3.24 (no marker yet) whose snapshots disappeared.
- **Recoverable partial restore.** A failure after the artifact copy leaves an owned record and
  raises `restore_incomplete`, with public, reversible `anchor state resume` and
  `anchor state abandon` operations (also `odibi_anchor.resume_restore` / `abandon_restore`).
  Abandon moves the owned tree to a preserved quarantine path and never deletes it.
- **Descriptor integrity (#29).** `PROJECT.md` route frontmatter is parsed strictly. A missing,
  malformed, unreadable or incomplete descriptor raises `managed_descriptor_damaged` with exact
  context. Indented content and `- item` lists under non-route fields stay accepted, as in 0.3.23.
  A referenced project no longer silently falls back to its artifact root as its target.
  Damaged descriptors are never rewritten during detection. A damaged project that is the
  remembered selector no longer breaks `project list` and `status` for other projects.
- **Structured route conflicts (#30).** The generic target-hint error is now
  `route_target_conflict`, with requested and descriptor targets, artifact root and a
  `probable_move` or `ambiguous` classification. The message keeps "conflicts with target hint"
  for existing callers.
- **Safe `set_target` (#30).** Writes are atomic, with optional `expected_sha256`
  (`managed_descriptor_changed` on a stale hash). `set_target` refuses with
  `project_retarget_requires_migration` when a continuity owner or authority records (accepted
  tasks, workflows, memory receipts) are bound to the old target. Before, it reported success and
  left the project unbootable (`ContinuityUnavailable: continuity owner mismatch`). Never-launched
  projects can still be retargeted.
- **MCP typed table resolution (#28).** The MCP gateway converts strings into DataFrames only for
  parameters that an action takes as tables. Paths such as `touched("…/manifest.json")` reach the
  action unchanged, with or without pandas. Dotted text is never sent to Spark. A missing table
  reader raises `table_input_dependency_unavailable`. The file cache is invalidated when a file's
  stat identity changes.
- **Truthful test measurement.** The pytest runner reports `xfailed` and `xpassed` separately from
  `skipped`. Workflow pytest criteria still require zero skipped and xpassed, and an `xfailed` count
  equal to the criterion's optional `expected_xfailed` (default 0).
- **Agent lifecycle fixes.** `anchor("memory", "status")` returns a clear error instead of a
  `TypeError`. `preflight` also looks for ruff and pyright beside the running interpreter and
  reports the resolved paths. `anchor("test")` accepts `timeout=` or `ANCHOR_TEST_TIMEOUT`
  (bounded to 600 s) and reports per-test timeouts explicitly. The review diffstat counts
  deletions. `touched` marks Git-untracked files as created, including files that were already
  untracked before the session. Workflow plan list fields are
  shape-checked at create. A gate before `implemented` warns with the exact next call.
  `task_rebind` returns its required next operations. `help("workflow")` documents the enforced
  order and the review findings schema.

### Added

- Bootstrap phase timings in the managed startup packet (`timings`), bounded Workspace API and
  package-index timeouts (`bootstrap_phase_timeout`), and `scripts/bootstrap_benchmark.py` (#31).
- Optional exact package pin: `ANCHOR_PACKAGE_VERSION` or `hosts.<id>.package_version`. A pinned
  launcher skips the package index. Pins must be 0.3.24 or newer (portfolio validation and the
  launcher both enforce this), and every host must run 0.3.24 or newer before the portfolio field
  is added.
- Portfolio-not-found errors list every searched path in precedence order (#30). The search
  precedence itself is unchanged.
- An incident replay harness (`tests/integration`) with a fake Databricks runtime and crash
  injection replays the reported incidents end to end.
- The owner Slack transport accepts the legacy `CW_SLACK_*` names, only when every
  `ANCHOR_SLACK_*` name is absent or empty. The two sets are never mixed.

### Changed

- Examples, packaged guidance and test fixtures use one neutral retail domain. A guard test blocks
  reintroduction; it is a regression guard, not a way to conceal the blocked terms. Memory project routing keeps only the `odibi_anchor` fragment; other roots
  resolve to their directory name, so exact roots are unchanged. The table profiler no longer
  treats `mw`, `kwh` and `mwh` name suffixes as measure-name hints (`capacity` still is), and such
  numeric columns remain MEASURE through the fallback.

### Unchanged boundaries

- Single-writer restore remains required. Ownership protects against a competing local writer but
  is not a distributed lock, and each ownership check is not atomic with the removal that follows
  it. On Databricks, the root check confirms the Unity Catalog Volume, so a
  mistyped subdirectory inside an existing Volume is treated as first use. All Databricks paths in
  this release are verified with fakes, not live workspaces.
- Relative `touched` paths still resolve against `target_root` in every mode.

## [0.3.23] - 2026-10-02

### Added

- Runtime artifact contract v1.2 exposes absolute managed artifact discovery paths in
  orientation, task context and project status. Symlinked or unavailable paths are not
  suggested as writable destinations; discovery grants no authority.
- Public help and distributed guidance explain absolute managed registration and the
  unchanged target-root-relative meaning of `touched`, including restart and blocked-task limits.

### Fixed

- Gate diagnostics distinguish registered/detected non-artifact paths from proven byte
  changes and point to canonical managed-path discovery. Correctly registered managed
  artifacts no longer receive misleading cross-project warnings.
- Cross-project warning containment uses path components instead of string prefixes.

### Unchanged boundaries

- Issue #26 was a relative-path registration mistake, not a classifier defect. Relative
  paths never change roots with execution mode. The historical Databricks task remains
  blocked; this release does not rewrite or retroactively qualify it.
- Single-writer/exclusive restore access remains mandatory; the pre-existing collision
  cleanup race tracked in #24 is not fixed by this release.

## [0.3.22] - 2026-10-02

### Added

- Durable Plan → Implement & Qualify → Deliver workflows for substantive work, with
  exact plan/candidate evidence, separate high-risk review tasks, explicit human delivery
  authority, and supported GitHub, PyPI, managed-artifact and workspace-file readback.
- Distinct implemented, qualified, approved-for-delivery, delivered and delivery-verified
  states. Only verified destination completion completes a workflow; local gates do not.

### Fixed

- Pre-plan artifact admission prevents draft-output laundering and preserves verified
  snapshot/restore provenance through implementation and exact task rebind qualification.
- Reconciliation remains a separate evidenced obligation from destination byte readback.
- Task-local drift baselines and the unsupported/unphased data-only compatibility status
  survive the relevant runtime and recovery boundaries.

### Operational restrictions

- Snapshot/restore remains single-writer: one active writer per durable authority, with
  exclusive access to restoration destinations for the entire operation. A pre-existing
  collision-cleanup race can delete a competing writer's directory and requires a separate
  repair; this release neither fixes it nor qualifies concurrent/shared-destination restore.
- Ordinary source edits in plain non-Git folders remain blocked. Historical evidence is
  never synthesized, and old runtimes cannot resume workflow-bound v2 task authority.
- High-risk review records a separate accepted read-only task with
  `reviewer_authentication=none`; it does not claim distinct authenticated principals.

## [0.3.21] - 2026-10-01

### Fixed

- Gate-time managed-artifact boundaries now use the accepted execution mode, including
  explicit artifact-only implementation tasks and documentation tasks.
- Pytest isolation preserves mandatory installed-distribution qualification and offline
  wheelhouse controls while removing Anchor routing variables.
- Dirty-worktree recovery requests missing semantic inputs through `prepare` rather than
  suggesting an incomplete task invocation.
- Memory-ID Markdown rendering uses the entry renderer, and help describes project-scoped lookup.
- Problem Record validation preserves reader defaults and exposes malformed records in Markdown.

### Changed

- Every accepted task projects explicit source-authority status, capabilities, and actionable
  guidance. Non-source modes do not probe Git or repository providers.
- Non-Git and rejected Git Folder diagnostics show the managed-artifact route and source-change
  prerequisites. Plain non-Git source editing remains blocked; no permissions are broadened.

## [0.3.20] - 2026-09-21

### Changed

- Databricks portfolios now keep a stable user-specific local-state base while managed preparation
  selects a physical runtime root isolated by effective UID and OS-account fingerprint.
- Startup packets expose the configured and physical local-state roots, compute UID, selection,
  and one-time legacy migration status for auditable recovery.

### Fixed

- Databricks compute identity recycling no longer blocks startup on a prior identity's protected
  local state or requires repeated portfolio rewrites; inaccessible legacy roots remain untouched
  while verified v2 snapshots restore database, artifacts, and continuity into the current root.
- Accessible pre-0.3.20 local state migrates atomically to the identity-isolated root, preventing a
  recycled UID from later reopening stale state at the configured base.

## [0.3.19] - 2026-09-20

### Changed

- Agent-facing Python, CLI, MCP, help, and packaged guidance surfaces now use only structured
  `learning capture/assess` and governed memory-promotion routes; obsolete `learn` and `confirm`
  actions are no longer public.
- Historical exact-owner learning markers migrate automatically to structured obligations during
  bootstrap, while unowned markers remain forensic evidence without granting or blocking authority.
- Databricks guidance now distinguishes package installation, Python restart, host setup,
  per-process bootstrap, and per-task `new_session` so healthy sessions avoid repeated setup.

### Fixed

- Checkpoints now accept and commit only structured learning payloads and require the resulting
  obligation to reach the `assessed` terminal state.
- Historical-marker migration fails closed when its structured obligation cannot be established.

## [0.3.18] - 2026-09-20

### Changed

- Dirty-worktree recovery now distinguishes exact interrupted-task ownership, exact completed-task
  delivery, and unowned or ambiguous changes before offering executable next operations.
- Ambiguous task-rebind diagnostics are bounded to ten verified open windows with exact copy-ready
  selection calls and an omitted-window count.

### Fixed

- Terminal task records now prevent completed windows from being rebound even when a process stops
  between terminal-record persistence and accepted-task closure.
- Completed artifact-only task windows remain terminal across fresh initialization instead of
  accumulating as stale rebind candidates.
- Historical terminal tasks claim dirty work only when branch, HEAD, and the complete changed-path
  set match their retained repository evidence.

## [0.3.17] - 2026-09-20

### Added

- Dirty-worktree source-task failures now carry bounded, copy-ready recovery metadata.
- Learning and task help now documents accepted schemas, field constraints, valid modes, and
  exact observation identifiers.
- Managed problem, work-item, spec, and decision directories now have a canonical schema
  registry with read enforcement or advisory reporting according to reader behavior.

### Changed

- Base-package help loads optional actions lazily, preserving help across minimal, MCP, and
  Databricks installations.
- Artifact-only tasks may deliver managed records while gates reject target-root drift that
  requires source-change authority.
- Pytest subprocesses and direct suite execution use isolated Anchor routing and unique state
  roots rather than inheriting operator projects or databases.

### Fixed

- Exact memory-ID lookup preserves project and lifecycle scope while still resolving eligible
  task selections and shared memories.
- Dirty-worktree recovery no longer recommends review before task authority can exist.
- Snapshot Markdown surfaces malformed managed records and unavailable validation; work-item
  listing isolates malformed files instead of hiding healthy records.
- Learning scope validation now enforces the documented `project_refs` cardinality for
  `workbench`, `project_local`, and `cross_project` observations.

## [0.3.16] - 2026-09-13

### Added

- Agent-facing memory, task, and learning results now expose state-valid operations, including
  copy-ready candidate rejection, governed owner approval, and completed-observation recovery.
- CLI and MCP v2 errors now preserve bounded, recursively redacted recovery metadata without
  changing direct Python exception classes or messages.
- One canonical public-action registry validates runtime contracts, help signatures, grouping,
  transport classification, and the startup action count against drift.

### Changed

- Databricks guidance drift checks read complete files with bounded concurrency while preserving
  deterministic hashing, ordering, and fail-closed behavior.
- Managed orientation advances past the status and audit checks it performs, avoiding redundant
  startup calls.
- Memory and learning help now documents unpromoted-candidate rejection, exact Databricks
  in-session approval handoff, and the evidence object schema.

### Fixed

- Compact MCP task responses retain `task_window_id` and bounded `memory_context` from Python
  results.

## [0.3.15] - 2026-09-13

### Added

- Databricks retention now maintains an immutable, authority-bound, checksummed snapshot index.
  Existing histories migrate on their first retention run; later runs fetch one index and only
  canonical manifests published since it instead of downloading the full history.
- Accepted task results expose `task_window_id` prominently, and ambiguous rebind failures include
  bounded task context plus copy-ready orphan-recovery guidance that distinguishes rebinding from
  authenticated dirty-task adoption.

### Changed

- Unchanged Databricks guidance setup reuses the file bytes already read for drift detection rather
  than downloading every managed file a second time. Mutation paths retain post-write verification.
- Startup and workflow guidance now treats managed orientation as the source of `status` and
  `audit_history`, avoiding ceremonial duplicate calls.
- Safe-stop prerequisite failures include the exact learning-assessment call needed to recover.

## [0.3.14] - 2026-09-13

### Changed

- Automatic intermediate checkpoints no longer run configured retention over the entire snapshot
  history; retention remains enforced at terminal learning closure and explicit checkpoint or
  snapshot actions.
- Irrelevant memory dispositions are recoverable bookkeeping deferred to the next durable
  lifecycle boundary, and managed guidance directs all-irrelevant selections through one
  `all_pending=True` call instead of a snapshot-producing loop.
- Databricks guidance now uses a re-raising traceback boundary for multi-step cells so generic
  execution failures remain diagnosable without unsafe retries or secret-bearing dumps.

### Fixed

- `task_adoption inspect` is now correctly classified as read-only rather than creating an
  unnecessary durable checkpoint.
- Ambiguous task rebinding now reports copy-ready recovery and accepts an exact
  `task_window_id`, while preserving full owner-identity validation.

## [0.3.13] - 2026-09-13

### Changed

- Databricks cold restore now downloads exactly the latest canonical manifest and its referenced
  database and artifact payloads instead of downloading every historical snapshot file.
- Complete warm local state skips remote snapshot enumeration, and cold startup no longer lists
  the full history before independently restoring it.
- High-frequency recoverable bookkeeping (`touched`, `skill_loaded`, `task_rebind`, and
  `new_session`) defers remote publication until the next substantive authority or lifecycle
  boundary, while task acceptance, memory/evidence changes, and terminal closure remain
  immediately durable.
- Remote snapshot listing validates canonical manifests plus referenced payload presence and size;
  payload hashes remain mandatory before restore, content reuse, or collision acceptance.

## [0.3.12] - 2026-09-12

### Changed

- Managed Databricks setup guidance now includes a copy-ready, optimistic-concurrency-safe
  portfolio retention recipe, including host validation, fresh-process re-bootstrap, and
  Anchor-managed pruning instead of manual TOML or snapshot-storage edits.
- The operating contract and quick reference now route approved portfolio-policy changes to the
  setup skill so agents discover that recipe before attempting configuration changes.

## [0.3.11] - 2026-09-12

### Fixed

- Canonical local Git baselines now resolve a configured target such as `main` or `master`
  through exactly one matching remote-tracking ref when no exact ref exists, supporting
  Databricks Git Folders that omit local default-branch refs while rejecting ambiguity.
- Missing, ambiguous, and unrelated-history target failures now identify the precise condition
  and remediation instead of reporting an apparently unresolved managed-project target.

## [0.3.10] - 2026-09-12

### Fixed

- Databricks source-change tasks now prefer canonical local Git evidence when a Git Folder exposes
  both a real local checkout and Repos API identity, while retaining the Databricks provider as the
  fallback when canonical local Git is unavailable.
- Repeating a durable task's `touched` registration after rebind is idempotent instead of failing
  while verifying the existing immutable record.

## [0.3.9] - 2026-09-12

### Added

- Learning assessment now explains each observation's semantic projection decision and returns
  the exact next operation for human triage or owner activation.
- Startup, task-memory, and help responses distinguish project-local memories from shared
  `project="all"` memories, which remain relevance-ranked rather than universally injected.

### Fixed

- Owner-governed activation, confirmation, and withdrawal now support shared memories under the
  boot-verified portfolio work authority without weakening managed-project trust boundaries.
- Host setup can cryptographically recognize and upgrade released legacy Anchor guidance when
  its ownership manifest is absent, while preserving customized operating instructions and
  continuing to reject unknown file bytes.

## [0.3.8] - 2026-09-12

### Added

- Portfolio-configurable durable snapshot retention keeps a time window plus a minimum
  number of restore points and safely reclaims only blobs unreferenced by retained manifests.

### Fixed

- Gates now report memory-verifier evidence as unavailable for Databricks Git Folder task
  baselines instead of accessing a local-Git-only target-ref field and raising `AttributeError`.

## [0.3.7] - 2026-09-12

### Added

- The managed launcher now needs only an explicit project ID for normal startup. It discovers the
  sibling portfolio and exact host, reconciles guidance, restores durable state, binds the route,
  orients once, and emits a compact startup packet.
- Explicitly authorized missing-project startup can optimistically guard the portfolio update,
  scaffold and register the exact existing target, and checkpoint the resulting durable state.
- Orientation advertises copy-ready managed actions for project, Problem, Spec, and work-item
  artifacts, while identifying artifact classes that still require a reported filesystem fallback.

### Changed

- On Databricks, the managed launcher resolves the newest non-yanked stable PyPI release and emits
  an exact pinned install-and-restart remediation only when the active distribution is stale or
  missing. Prompts and managed guidance no longer hard-code a package version.

## [0.3.6] - 2026-09-12

### Fixed

- Verified v2 restores can relocate managed continuity from a prior ephemeral local state root.
  Restore proves each original route fingerprint and canonical project path, rebases only local
  path fields plus derived fingerprints/checksums in unpublished staging, and leaves the immutable
  durable snapshot unchanged. Ambiguous or malformed continuity still fails before publication.
- Databricks setup guidance now recommends a stable user-specific local compute path to avoid
  cross-user ownership collisions on shared/serverless `/tmp`.

### Changed

- Quality CI runs the unchanged pytest coverage with two isolated workers per supported Python
  version, while the canonical local verifier remains serial unless parallelism is requested.

## [0.3.5] - 2026-09-12

### Fixed

- Managed host instructions and the setup skill now require an explicit, pinned installation
  check before the first Anchor import, including the Databricks Python restart and post-restart
  distribution, runtime-version, and module-origin verification. They also provide the exact
  direct-Python portfolio preparation sequence for notebook hosts and prohibit substituting the
  durable Volume for local `ANCHOR_HOME` or probing snapshots through FUSE.

## [0.3.4] - 2026-09-12

### Changed

- Unconfigured Databricks doctor now returns an actionable portfolio-preparation operation instead
  of requiring callers to invent `ANCHOR_HOME`.
- Startup guidance now prepares configured portfolios before doctor/bootstrap and identifies the
  process-bound source of the `anchor` callable.
- Adding a portfolio project now returns its exact preparation operation; targets may be non-Git
  directories when source-change evidence is not required.
- The Databricks extra constrains protobuf below version 6 for compatibility with Databricks
  runtime packages while retaining the qualified SDK floor.
- Durable snapshot v2 checkpoints both SQLite authority state and the complete managed-project
  artifact tree, restores both through verified no-overwrite staging, and remains able to read
  legacy v1 database-only snapshots.

## [0.3.3] - 2026-09-11

### Fixed

- Databricks Git Folder identity now recognizes the Workspace API's current
  `DIRECTORY` plus `directory_info.is_git_folder` shape while retaining legacy `REPO` support.
- Source-change evidence now records an omitted Repos provider label as unavailable instead of
  rejecting otherwise complete repository ID, path, branch, HEAD, and remote identity.

## [0.3.2] - 2026-09-11

### Fixed

- Databricks host setup now publishes and verifies Workspace guidance through the Workspace
  API, avoiding asynchronous FUSE writes, stale nodes, atomic renames, and staging residue.
- Databricks Git Folder tasks now treat a projected `.git` directory as part of the explicit
  provider-backed checkout when canonical local Git is unavailable, while still rejecting two
  genuinely usable repository authorities.

## [0.3.1] - 2026-09-11

### Fixed

- Databricks host setup now verifies every published guidance file and falls back to the
  Workspace API when FUSE cannot remove its staging directory.
- The preferred launcher now automatically supplies a Databricks Git Folder repository
  provider so implementation tasks can attest source identity without local Git commands.
- Startup guidance now states the required task-acceptance-before-skill-registration order.

## [0.3.0] - 2026-09-11

### Added

- A qualified Databricks installation extra and doctor diagnostics with exact remediation
  when the Workspace Files API SDK is missing or outdated.
- Launch-ready PortfolioV1 scaffolding for local state, instruction, and durable roots.
- A native startup skill and end-to-end getting-started guide for local, MCP, Claude,
  ChatGPT, and Databricks hosts.

### Changed

- Claude host setup now installs a managed native skill-discovery mirror while retaining
  `.assistant/skills` as the packaged authority.

## [0.2.4] - 2026-09-11

### Fixed

- Databricks bootstrap now accepts the exact prepared `ANCHOR_DURABLE_ROOT` environment
  without probing Unity Catalog Volume paths through FUSE, while preserving Files API
  qualification and fail-closed local validation on other hosts.

## [0.2.3] - 2026-09-11

### Fixed

- Host guidance setup now excludes generated Python bytecode for every adapter, preventing
  `__pycache__` staging failures on constrained filesystems such as Databricks Workspace Files.

## [0.2.2] - 2026-09-11

### Fixed

- Databricks durable snapshot, listing, startup restore, and manual restore now use the
  Workspace Files API instead of UC Volume FUSE access.
- Remote snapshot publication remains immutable and verifies uploaded content before
  publishing the manifest commit marker.

## [0.2.1] - 2026-09-11

### Fixed

- Databricks host setup now installs a Workspace Files-compatible guidance profile while
  retaining the deeply nested third-party reference snapshot cache in the installed package.

## [0.2.0] - 2026-09-11

### Added

- User-owned PortfolioV1 configuration for explicit multi-host projects and advisory personas.
- Idempotent `anchor setup-host` guidance installation with managed-file hashes and collision safety.
- Verified immutable SQLite snapshots, local restore, and automatic active-state persistence.
- Work-authority memory sharing for assessed evidence-backed candidates without crossing trust domains.

### Changed

- Project preparation now registers one exact immutable route and restores absent local state without relying on `.active_project`.
- Databricks guidance now keeps live SQLite on local compute and durable snapshots on approved storage.

## [0.1.0] - 2026-09-10

### Added

- First public release of the Odibi Anchor reliability, context, and evidence toolkit.
- `odibi_anchor` Python package, `anchor` CLI, optional MCP server, and portable agent instruction distribution.
- Local, MCP stdio, and Databricks wheel installation workflows.
- Explicit immutable project/root routing and fail-closed lifecycle checks.

[0.1.0]: https://github.com/henryodibi11/odibi-anchor/releases/tag/v0.1.0
[0.2.0]: https://github.com/henryodibi11/odibi-anchor/releases/tag/v0.2.0
[0.2.1]: https://github.com/henryodibi11/odibi-anchor/releases/tag/v0.2.1
[0.2.2]: https://github.com/henryodibi11/odibi-anchor/releases/tag/v0.2.2
[0.2.3]: https://github.com/henryodibi11/odibi-anchor/releases/tag/v0.2.3
[0.2.4]: https://github.com/henryodibi11/odibi-anchor/releases/tag/v0.2.4
[0.3.0]: https://github.com/henryodibi11/odibi-anchor/releases/tag/v0.3.0
[0.3.1]: https://github.com/henryodibi11/odibi-anchor/releases/tag/v0.3.1
[0.3.2]: https://github.com/henryodibi11/odibi-anchor/releases/tag/v0.3.2
[0.3.3]: https://github.com/henryodibi11/odibi-anchor/releases/tag/v0.3.3
[0.3.4]: https://github.com/henryodibi11/odibi-anchor/releases/tag/v0.3.4
[0.3.5]: https://github.com/henryodibi11/odibi-anchor/releases/tag/v0.3.5
[0.3.6]: https://github.com/henryodibi11/odibi-anchor/releases/tag/v0.3.6
[0.3.7]: https://github.com/henryodibi11/odibi-anchor/releases/tag/v0.3.7
[0.3.8]: https://github.com/henryodibi11/odibi-anchor/releases/tag/v0.3.8
[0.3.9]: https://github.com/henryodibi11/odibi-anchor/releases/tag/v0.3.9
[0.3.10]: https://github.com/henryodibi11/odibi-anchor/releases/tag/v0.3.10
[0.3.11]: https://github.com/henryodibi11/odibi-anchor/releases/tag/v0.3.11
[0.3.12]: https://github.com/henryodibi11/odibi-anchor/releases/tag/v0.3.12
[0.3.13]: https://github.com/henryodibi11/odibi-anchor/releases/tag/v0.3.13
[0.3.14]: https://github.com/henryodibi11/odibi-anchor/releases/tag/v0.3.14
[0.3.15]: https://github.com/henryodibi11/odibi-anchor/releases/tag/v0.3.15
[0.3.16]: https://github.com/henryodibi11/odibi-anchor/releases/tag/v0.3.16
[0.3.17]: https://github.com/henryodibi11/odibi-anchor/releases/tag/v0.3.17
[0.3.18]: https://github.com/henryodibi11/odibi-anchor/releases/tag/v0.3.18
[0.3.19]: https://github.com/henryodibi11/odibi-anchor/releases/tag/v0.3.19
[0.3.20]: https://github.com/henryodibi11/odibi-anchor/releases/tag/v0.3.20
