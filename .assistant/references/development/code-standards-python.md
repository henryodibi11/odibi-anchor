# Anchor House Engineering Standard — Python Profile

> Profile: `anchor-house-engineering/v1` (Python). Apply only when Python is selected by
> canonical task metadata, repository configuration, or a changed `.py`/`.pyi` path.

This profile supplements the house hub. It does not choose a Python version or tool
configuration for a repository.

## Repository-derived contract

- Use the Python versions declared by project metadata, CI, packaging, and runtime
  configuration. Do not infer a version from personal defaults.
- Run the repository's formatter, linter, type checker, test runner, and build commands
  with their checked-in settings. If a tool is absent, report it as unavailable rather
  than inventing a substitute policy.
- Preserve the repository's import, naming, documentation, and package-layout
  conventions unless an approved change intentionally updates them.

## Python guidance

- Keep imports explicit and easy to trace. Avoid wildcard imports; place a local import
  at the narrow boundary that needs it when import cycles, startup cost, or optional
  availability justify the choice.
- Add type information where it clarifies a maintained boundary, catches meaningful
  misuse, or matches local policy. Do not add annotations that the supported runtime or
  configured checker cannot interpret.
- Document public contracts and non-obvious constraints at the level the repository
  expects. Prefer names and structure over comments that repeat mechanics.
- Catch exceptions narrowly enough to preserve failure meaning. Add actionable context
  at system boundaries and chain a replacement exception with `raise ... from error`.
  Do not swallow an exception unless the contract explicitly defines that outcome.
- Isolate optional dependencies behind the feature or adapter that owns them. Missing
  extras should fail with a clear capability message, not break unrelated imports.
- Keep resource ownership visible. Use context managers or an equivalent owning
  abstraction for files, connections, locks, and other releasable resources.
- Treat public signature changes as compatibility decisions. Prefer keyword-only options
  with behavior-preserving defaults when adding independent behavior, and update callers
  deliberately when a breaking change is approved.
- Test public outcomes, meaningful error paths, and supported-version compatibility.
  Choose fixtures and mocks that expose the boundary without coupling tests to private
  implementation steps.

## Review prompts

- Do syntax and APIs match the repository's supported Python versions?
- Are imports, optional dependencies, exceptions, and resource lifetimes explicit?
- Does typing improve a maintained contract rather than add unsupported ceremony?
- Do tests prove observable behavior and the intended compatibility decision?
