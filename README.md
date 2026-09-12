# Odibi Anchor

Odibi Anchor is a provider-neutral reliability and evidence layer for engineering agents. It gives an agent a bounded, auditable lifecycle for selecting context, authorizing work, checking changes, and retaining evidence without coupling the workflow to one host.

Odibi Anchor requires Python 3.11 or newer and is licensed under Apache-2.0.

New installation? Follow the [getting-started guide](docs/guides/getting-started.md) for
package selection, launch-ready PortfolioV1 scaffolding, host setup, preparation, and recovery.

## Install and run locally

```bash
python -m venv .venv
. .venv/bin/activate                 # Windows: .venv\Scripts\activate
python -m pip install "odibi-anchor==0.3.10"
anchor help
```

The `anchor` CLI emits deterministic JSON. One-shot execution does not preserve process state; use `anchor batch` or `anchor shell` for a sequence that shares a bootstrap:

```bash
printf '%s\n' '{"action":"status"}' '{"action":"audit_history"}' | anchor batch -
```

Check routing before bootstrap, or install the packaged agent contract into a repository:

```bash
anchor doctor
anchor setup-host amp --target /absolute/path/to/repository
```

For source development, clone the repository, create a virtual environment, and run `python -m pip install -e '.[dev,mcp]'`.

## MCP quickstart (stdio)

Install the MCP extra and configure one long-lived stdio server:

```bash
python -m pip install "odibi-anchor[mcp]==0.3.10"
export ANCHOR_HOME=/absolute/writable/odibi-anchor-state
export ANCHOR_PROJECT_ID=my-project
export ANCHOR_PROJECT_ROOT=/absolute/path/to/my-project
python -m odibi_anchor.mcp_server
```

Equivalent client configuration:

```json
{
  "command": "/absolute/path/to/python",
  "args": ["-m", "odibi_anchor.mcp_server"],
  "env": {
    "ANCHOR_HOME": "/absolute/writable/odibi-anchor-state",
    "ANCHOR_PROJECT_ID": "my-project",
    "ANCHOR_PROJECT_ROOT": "/absolute/path/to/my-project"
  }
}
```

The project must already be registered under `ANCHOR_HOME`. See [runtime rollout](docs/guides/runtime-rollout.md) and [MCP integration](docs/guides/amp-project-integration.md) for setup and operational details.

## Databricks quickstart

Install the pinned public release in a Databricks notebook:

```python
%pip install "odibi-anchor[databricks]==0.3.10"
dbutils.library.restartPython()
```

After one-time host setup, run the managed launcher with only the project ID. It selects local
state, restores durable artifacts, binds the exact route, and advertises managed artifact actions.
No source clone, host ID, portfolio path, or manually chosen `ANCHOR_HOME` is required:

```python
import runpy

namespace = runpy.run_path(
    "/Workspace/Users/<user>/.assistant/agent_bootstrap.py",
    init_globals={"ANCHOR_PROJECT_ID": "my-databricks-project"},
)
anchor = namespace["anchor"]
startup = namespace["STARTUP_PACKET"]
assert startup["status"] == "ready"
```

If the installed distribution is missing or stale, the launcher returns the exact pinned
latest-stable install and Python-restart remediation; run it and rerun the same launcher call.
The `anchor` callable is process-bound and comes from the launcher namespace (or the return
value of `odibi_anchor.launch()`); `from odibi_anchor import anchor` is intentionally unsupported.
Run doctor only when additional startup diagnostics are needed.

Run `anchor setup-host databricks --target /Workspace/Users/<user>` once to install and
subsequently reconcile the packaged instructions and launcher. Workspace targets are published
and byte-verified through the Databricks Workspace API rather than the FUSE mount. Create a
PortfolioV1 to keep host-specific project roots and state locations explicit instead of rediscovering them. See
[getting started](docs/guides/getting-started.md) and
[portfolio and durable state](docs/guides/portfolio-and-durable-state.md).

`ANCHOR_HOME` must be writable local filesystem storage outside the installed package/source
checkout. Let portfolio preparation set it. On Databricks, `/tmp` is session-scoped and must not be treated as durable. Configure
an approved durable snapshot root while keeping live state local; Anchor checkpoints successful
authority writes and restores the absent SQLite database and managed project artifacts through
verified immutable files. Do not run live
SQLite state from Workspace Files, DBFS, Volumes, or another network/distributed filesystem.
See [portfolio and durable state](docs/guides/portfolio-and-durable-state.md) and
[Databricks MCP guidance](docs/guides/mcp-databricks.md).

## Import legacy v0.11.0 state

Legacy Context Workbench state is never selected or mutated implicitly. Import one exact
v0.11.0 home into a new, non-overlapping Anchor home with the explicit two-step API:

```python
from odibi_anchor import apply_legacy_import, plan_legacy_import

plan = plan_legacy_import(
    "/absolute/path/to/legacy-home",
    anchor_home="/absolute/path/to/new-anchor-home",
)
# Inspect the source, destination, schema set, and plan_id before approving the write.
result = apply_legacy_import(plan)
```

The importer fails closed on destination collisions, incompatible/newer schemas, symlinks,
ambiguous managed-project targets, or open tasks. It copies project artifacts, rewrites only
exact managed-project self-targets, and leaves the source unchanged. Legacy database history
is preserved in a verified backup but is not activated: its branded task/memory payloads are
not an Anchor runtime contract. The first Anchor launch creates fresh runtime ownership state;
new tasks are established through the normal lifecycle.

## Routing and concurrent runtimes

Server routing is an immutable binding between `ANCHOR_PROJECT_ID` and the exact canonical `ANCHOR_PROJECT_ROOT`. Startup verifies the ID, registered target, and requested root agree. If the root matches exactly one managed project, `launch()` can derive its ID; ambiguous roots fail closed and require an explicit ID. Once bound, changing environment variables or `workspace/.active_project` cannot redirect that process. `.active_project` is an interactive preference only—**it is not routing authority**.

Concurrent runtimes may share an `ANCHOR_HOME` only when it is a **local filesystem** that provides the required locking and atomic filesystem semantics. Do not place a concurrently shared home on DBFS, object storage, an NFS-like mount, or another network/distributed filesystem. Bind every process explicitly, use distinct project IDs for distinct roots, and use a separate per-project `ANCHOR_HOME` when local-filesystem guarantees are uncertain. A shared home does not make same-project conflicting writers safe.

## Security and support

Odibi Anchor records operational state under `ANCHOR_HOME`; keep it outside source control and protect it as potentially sensitive. Never commit environment files, runtime databases, session records, or generated manifests.

For bugs and feature requests, use [GitHub Issues](https://github.com/henryodibi11/odibi-anchor/issues). Security-sensitive reports should not include credentials, private repository content, or runtime state in a public issue.

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [CHANGELOG.md](CHANGELOG.md), and [LICENSE](LICENSE).
