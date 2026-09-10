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

For direct Python in a Databricks notebook, configure an external writable `ANCHOR_HOME` and bind one
existing managed project explicitly; do not use notebook cwd, workspace visibility,
authentication, or `.active_project` as routing/task authority:

```python
import os
from odibi_anchor.bootstrap import init

os.environ["ANCHOR_HOME"] = "/absolute/writable/odibi-anchor-state"
anchor, ROOT, MANIFEST = init(
    root="/absolute/checked-out-target",
    project="existing-managed-project",
)
diagnostics = anchor("concurrency", command="inspect")
```

The paths are placeholders and must be supplied by the host; do not hard-code credentials or copy
private state into the product repository. Direct-Python Databricks concurrency is **not
qualified** by orb/Linux results. Until the full two-process, crash/restart, migration, recovery,
and packaging matrix runs on the exact Databricks filesystem/runtime profile, use one writer and a
per-project `ANCHOR_HOME`. The archived HTTP App remains unqualified regardless of these settings.

See the [runtime rollout guide](runtime-rollout.md) for supported commands and
compatibility guarantees. A future HTTP design requires a separate approved spec for
authentication, authorization, per-client state isolation, concurrency, quotas,
timeouts, payload limits, redaction, and audit behavior.
