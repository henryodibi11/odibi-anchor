# Changelog

All notable changes to Odibi Anchor are documented here. This project follows [Semantic Versioning](https://semver.org/).

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
