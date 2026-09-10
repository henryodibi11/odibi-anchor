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

For direct Python in a Databricks notebook, configure local compute storage and bind one
existing managed project explicitly; do not use notebook cwd, workspace visibility,
authentication, or `.active_project` as routing/task authority:

```python
import os
from odibi_anchor import launch, register_project

os.environ["ANCHOR_HOME"] = "/tmp/odibi-anchor-state"
registration = register_project(
    anchor_home=os.environ["ANCHOR_HOME"],
    project_id="exact-project-id",
    project_root="/absolute/checked-out-target",
)
anchor = launch(**registration["next_operation"]["arguments"])
diagnostics = anchor("concurrency", command="inspect")
```

The paths are placeholders and must be supplied by the host; do not hard-code credentials or copy
private state into the product repository. `/tmp` is session-scoped. Stop every Anchor process
before copying a closed backup to durable storage, and restore it to local storage before reuse.
Workspace Files, DBFS, and Volumes are not supported locations for the live SQLite database.
Direct-Python Databricks concurrency is **not qualified** by orb/Linux results. Use one writer and
a per-project `ANCHOR_HOME`. The archived HTTP App remains unqualified regardless of these settings.

See the [runtime rollout guide](runtime-rollout.md) for supported commands and
compatibility guarantees. A future HTTP design requires a separate approved spec for
authentication, authorization, per-client state isolation, concurrency, quotas,
timeouts, payload limits, redaction, and audit behavior.
