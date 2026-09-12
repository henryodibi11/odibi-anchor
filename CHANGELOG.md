# Changelog

All notable changes to Odibi Anchor are documented here. This project follows [Semantic Versioning](https://semver.org/).

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
