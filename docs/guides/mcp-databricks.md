# Historical Databricks HTTP deployment

The former Databricks App deployment is archived. It is not a supported production
runtime and this guide intentionally provides no deployment recipe.

Odibi Anchor's HTTP adapter is disabled by default, experimental, and deprecated.
FastMCP's stateless HTTP mode does not isolate Odibi Anchor's mutable process
state, so multiple clients can affect one another. Conversation context and Unity
Catalog persistence do not fix that isolation boundary.

The checked-in `app.yaml` does not opt into HTTP. Experimental evaluation requires an
external environment override with both `ANCHOR_EXPERIMENTAL_HTTP=1` and an explicit,
comma-separated `ANCHOR_HTTP_ALLOWED_ORIGINS` list. Wildcard origins are rejected. Do not
check those overrides into reusable configuration.

Use runtimes in this order:

1. Direct Python bootstrap for notebooks, Databricks, Spark, and local Python.
2. The standard-library `anchor` CLI where only a process interface is available.
3. Optional MCP stdio when an MCP client is required.

For direct Python in a Databricks notebook, prepare one portfolio route before launch; do not
invent `ANCHOR_HOME` or use notebook cwd, workspace visibility, authentication, or
`.active_project` as routing/task authority:

```python
import os
from odibi_anchor import launch, prepare_portfolio_runtime

prepared = prepare_portfolio_runtime(
    config_path="/Workspace/Users/<user>/.odibi-anchor/anchor.toml",
    host_id="databricks-work",
    project_id="exact-project-id",
)
os.environ.update(prepared["environment"])
anchor = launch(
    anchor_home=prepared["environment"]["ANCHOR_HOME"],
    project_id=prepared["project_id"],
    project_root=prepared["target_root"],
)
diagnostics = anchor("concurrency", command="inspect")
```

The paths are placeholders and must be supplied by the host; do not hard-code credentials or copy
private state into the product repository. `/tmp` is session-scoped; portfolio preparation restores
the database and managed project artifacts from the configured durable snapshot root. Workspace
Files, DBFS, and Volumes are not supported locations for live state. Direct-Python Databricks
concurrency is **not qualified** by orb/Linux results. Use one Anchor writer for the configured
authority. The archived HTTP App remains unqualified regardless of these settings.

See the [runtime rollout guide](runtime-rollout.md) for supported commands and
compatibility guarantees. A future HTTP design requires a separate approved spec for
authentication, authorization, per-client state isolation, concurrency, quotas,
timeouts, payload limits, redaction, and audit behavior.
