# Odibi Anchor

Odibi Anchor is a provider-neutral reliability and evidence layer for engineering agents. It gives an agent a bounded, auditable lifecycle for selecting context, authorizing work, checking changes, and retaining evidence without coupling the workflow to one host.

> **First public release:** `0.1.0`. Odibi Anchor requires Python 3.11 or newer and is licensed under Apache-2.0.

## Install and run locally

```bash
python -m venv .venv
. .venv/bin/activate                 # Windows: .venv\Scripts\activate
python -m pip install "odibi-anchor==0.1.0"
anchor help
```

The `anchor` CLI emits deterministic JSON. One-shot execution does not preserve process state; use `anchor batch` or `anchor shell` for a sequence that shares a bootstrap:

```bash
printf '%s\n' '{"action":"status"}' '{"action":"audit_history"}' | anchor batch -
```

For source development, clone the repository, create a virtual environment, and run `python -m pip install -e '.[dev,mcp]'`.

## MCP quickstart (stdio)

Install the MCP extra and configure one long-lived stdio server:

```bash
python -m pip install "odibi-anchor[mcp]==0.1.0"
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

## Databricks wheel quickstart

Build a wheel on a trusted workstation or in CI, then upload `dist/odibi_anchor-0.1.0-py3-none-any.whl` to a Unity Catalog volume or Workspace Files. In a Databricks notebook:

```python
%pip install /Volumes/<catalog>/<schema>/<volume>/odibi_anchor-0.1.0-py3-none-any.whl
dbutils.library.restartPython()
```

Then bind external durable state and the exact Git folder checkout **before importing** Odibi Anchor:

```python
import os

os.environ["ANCHOR_HOME"] = "/Volumes/<catalog>/<schema>/<volume>/anchor-state"
os.environ["ANCHOR_PROJECT_ID"] = "my-databricks-project"
os.environ["ANCHOR_PROJECT_ROOT"] = "/Workspace/Users/<user>/<git-folder>"

import runpy
namespace = runpy.run_path("/Workspace/Users/<user>/<git-folder>/.assistant/agent_bootstrap.py")
anchor = namespace["anchor"]
```

`ANCHOR_HOME` must be durable, writable, and outside the installed package/source checkout. The managed project must already register the exact target root. See [Databricks MCP guidance](docs/guides/mcp-databricks.md) for host limitations.

## Routing and concurrent runtimes

Server routing is an immutable binding between `ANCHOR_PROJECT_ID` and the exact canonical `ANCHOR_PROJECT_ROOT`. Startup verifies the ID, registered target, and requested root agree. Once bound, changing environment variables or `workspace/.active_project` cannot redirect that process. `.active_project` is an interactive preference only—**it is not routing authority**.

Concurrent runtimes may share an `ANCHOR_HOME` only when it is a **local filesystem** that provides the required locking and atomic filesystem semantics. Do not place a concurrently shared home on DBFS, object storage, an NFS-like mount, or another network/distributed filesystem. Bind every process explicitly, use distinct project IDs for distinct roots, and use a separate per-project `ANCHOR_HOME` when local-filesystem guarantees are uncertain. A shared home does not make same-project conflicting writers safe.

## Security and support

Odibi Anchor records operational state under `ANCHOR_HOME`; keep it outside source control and protect it as potentially sensitive. Never commit environment files, runtime databases, session records, or generated manifests.

For bugs and feature requests, use [GitHub Issues](https://github.com/henryodibi11/odibi-anchor/issues). Security-sensitive reports should not include credentials, private repository content, or runtime state in a public issue.

See [CONTRIBUTING.md](CONTRIBUTING.md), [CHANGELOG.md](CHANGELOG.md), and [LICENSE](LICENSE).
