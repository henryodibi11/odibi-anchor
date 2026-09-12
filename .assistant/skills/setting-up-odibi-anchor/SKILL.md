---
name: setting-up-odibi-anchor
description: Installs, configures, diagnoses, upgrades, or recovers Odibi Anchor across local, Amp, Claude, ChatGPT, and Databricks hosts; do not use for ordinary work after a healthy Anchor runtime is already bound.
---

# Setting up Odibi Anchor

Establish one verified path from package installation to an exact managed-project runtime.

## When to load

Load when Anchor is missing, stale, unconfigured, bound to the wrong project, recovering
after compute replacement, or being installed for a new host or repository.

## When NOT to load

Do not load for ordinary implementation, review, data work, or memory use after doctor,
portfolio preparation, and bootstrap are already healthy.

## Workflow

1. Identify the host, intended project root, instruction root, local state root, durable
   root, authority ID, and portfolio path. Never infer a missing path from cwd or
   `.active_project`.
2. Install one exact released package. Use the host extra when applicable:
   - local/CLI: `python -m pip install "odibi-anchor==<version>"`
   - MCP: `python -m pip install "odibi-anchor[mcp]==<version>"`
   - Databricks: `%pip install "odibi-anchor[databricks]==<version>"`, then
     `dbutils.library.restartPython()`.
3. Verify `importlib.metadata.version("odibi-anchor")`, `odibi_anchor.__version__`, and
   module origin. Never continue through a source checkout when an installed release was
   requested.
4. If no portfolio exists, create a launch-ready PortfolioV1 without manual TOML surgery:

   ```bash
   anchor portfolio scaffold \
     --config <absolute-config-path> \
     --host <host-id> --adapter <adapter> \
     --target-root <absolute-project-root> --project <project-id> \
     --authority <authority-id> --local-state-root <absolute-local-state-root> \
     --instruction-root <absolute-instruction-root> \
     [--durable-root <absolute-durable-root>]
   ```

   If the portfolio exists but the explicitly approved project does not, run `anchor portfolio
   add-project` with its exact existing target. The target need not be Git unless source-change
   evidence is required. Follow the returned `portfolio.prepare` operation; never try to
   bootstrap a project ID before adding it to the portfolio.

5. Run `anchor portfolio validate`, then `anchor setup-host <adapter> --target
   <instruction-root>`. Setup is manifest-managed and must refuse modified managed files
   or incompatible user-owned collisions.
6. Run `anchor portfolio prepare --config <path> --host <id> --project <id>` before doctor
   whenever the portfolio exists. Do not set `ANCHOR_HOME` manually. Apply the
   returned environment exactly, including `ANCHOR_DURABLE_ROOT`, and use its packaged
   launcher or `odibi_anchor.startup.launch()` in one persistent Python process.
   The `anchor` callable comes from the launcher namespace or `launch()` return value; never
   attempt `from odibi_anchor import anchor`.
7. Run doctor after applying the prepared environment and follow any remediation. If doctor
   is run earlier on unconfigured Databricks, follow its `portfolio.prepare` operation rather
   than inventing route or state paths.
8. Verify the immutable route, orientation, and task lifecycle before substantive work.
   Accept the task before loading task-specific skills; then load every skill named in the
   accepted task result with `anchor("skill_loaded", "<name>")`. `skill_loaded` requires the
   active task by design, so calling it before task acceptance is an invocation error, not a
   circular dependency.

## Host boundaries

- **Databricks/Genie:** keep live SQLite and managed project artifacts under `/tmp`; use a UC
  Volume only for immutable database-and-artifact snapshots through the Workspace Files API.
  Use the Databricks extra. After compute replacement, rerun portfolio preparation to restore
  both before launch. Never run concurrent
  writers unless the actual filesystem/runtime has been qualified. The preferred launcher
  automatically attaches read-only Git Folder identity when the Workspace API can attest the
  target. For source changes, retain implementation mode, declare exact repository scope, and
  explicitly acknowledge unknown local Git state; do not downgrade source work to planning.
- **Amp:** install the MCP extra when using the stdio server. Keep the virtual environment
  and `ANCHOR_HOME` outside ephemeral build output.
- **Claude:** install guidance at the host's instruction root and preserve user-owned
  `CLAUDE.md` content when it already delegates to `.assistant_instructions.md`.
- **ChatGPT:** use only the execution and persistence capabilities actually available;
  do not claim durable or stateful operation from a guidance-only attachment.

## Recovery

For absent local Databricks state, rerun portfolio preparation before doctor or bootstrap rather
than copying SQLite manually. For modified managed guidance, inspect the collision and preserve
the user's file; never delete staging, legacy, state, or snapshot directories by pattern.
Cleanup requires an exact inventory and classification as active, evidence, rollback, or
disposable.

## Related skills

Use [[dependency-management]] when changing package dependency policy. Use
[[debugging]] only when a verified setup path fails because of a product defect.

## Enforcement

Self-enforced. Portfolio validation, immutable routing, host manifests, authority checks,
and lifecycle gates enforce the underlying runtime boundaries.
