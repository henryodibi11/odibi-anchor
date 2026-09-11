# Changelog

All notable changes to Odibi Anchor are documented here. This project follows [Semantic Versioning](https://semver.org/).

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
