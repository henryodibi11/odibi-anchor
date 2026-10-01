# Changelog

All notable changes to Odibi Anchor are documented here. This project follows [Semantic Versioning](https://semver.org/).

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
