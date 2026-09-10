# MCP Integration Troubleshooting for Databricks Apps

**Authority**: Reference document for debugging Model Context Protocol (MCP) integration issues in Databricks Apps when connecting to Genie Code.

**Scope**: CORS configuration, OPTIONS preflight handling, and FastMCP HTTP transport setup.

**Last Updated**: 2026-08-14

---

## Problem Signature

### Symptoms

* Genie Code settings show: "MCP server could not be added. {app-name} could not be added."
* App logs show: `INFO: "OPTIONS /mcp HTTP/1.1" 400 Bad Request` or `403 Forbidden`
* App deployment status is RUNNING and ACTIVE (compute is healthy)
* Simple working test apps (like mcp-test-simple) connect successfully
* The MCP endpoint exists and app is accessible

### Root Cause

MCP integration over HTTP requires proper CORS (Cross-Origin Resource Sharing) configuration to handle browser preflight OPTIONS requests. The failure occurs when:

1. **Missing OPTIONS method** in `allow_methods` for CORSMiddleware
2. **Custom security middleware** rejecting OPTIONS requests before CORS processing
3. **Overly restrictive origin validation** blocking legitimate Databricks workspace origins
4. **Middleware ordering** placing custom validation before CORS handlers

---

## Diagnostic Approach

### 1. Verify App Health

```bash
databricks apps get <app-name> --output JSON
```

Check:
* `app_status.state` = "RUNNING"
* `compute_status.state` = "ACTIVE"  
* `active_deployment.status.state` = "SUCCEEDED"
* No `pending_deployment`

If app is not RUNNING, resolve deployment/compute issues first before investigating MCP.

### 2. Inspect Logs for OPTIONS Requests

Use the `getAppsLogs` tool (NOT CLI, as app endpoints require OAuth):

```python
getAppsLogs(appName="your-app", tailLines=100, search="OPTIONS")
```

Look for:
* ✅ `"OPTIONS /mcp HTTP/1.1" 200 OK` = Working
* ❌ `"OPTIONS /mcp HTTP/1.1" 400 Bad Request` = CORS misconfiguration
* ❌ `"OPTIONS /mcp HTTP/1.1" 403 Forbidden` = Security middleware blocking

### 3. Compare with Working Pattern

Identify a known-working MCP app (e.g., mcp-test-simple) and compare:

```python
# Read both databricks_app.py or app entry point files
readAssetById(assetType="file", assetId="<working-app-file-id>")
readAssetById(assetType="file", assetId="<failing-app-file-id>")
```

Focus on:
* CORSMiddleware configuration
* Custom middleware presence and ordering
* `allow_methods`, `allow_headers`, `allow_origins` settings

---

## Working Solution Pattern

### Minimal Working Configuration

This pattern matches the successful mcp-test-simple app:

```python
import os
from fastmcp import FastMCP
from starlette.middleware.cors import CORSMiddleware

# Create MCP server
mcp = FastMCP("your-server-name")

# ... register tools with @mcp.tool() ...

# Create HTTP app
app = mcp.http_app(stateless_http=True)

# Configure CORS - simplified for MCP compatibility
cors_origin = os.environ.get("CORS_ORIGIN", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[cors_origin] if cors_origin != "*" else ["*"],
    allow_credentials=True,
    allow_methods=["POST", "OPTIONS"],  # ← OPTIONS is REQUIRED
    allow_headers=["*"],                 # ← Wildcard recommended
)
```

### app.yaml Configuration

```yaml
command:
  - uvicorn
  - your_module:app  # or databricks_app:http_app
  - --host
  - "0.0.0.0"
  - --port
  - "8000"

env:
  - name: CORS_ORIGIN
    value: "https://your-workspace.cloud.databricks.com"
```

---

## Anti-Patterns (What NOT To Do)

### ❌ Anti-Pattern 1: Missing OPTIONS in allow_methods

```python
# BROKEN - OPTIONS preflight will fail
app.add_middleware(
    CORSMiddleware,
    allow_methods=["POST"],  # ← Missing OPTIONS!
)
```

**Result**: Browser sends OPTIONS preflight → 400/403 → MCP connection fails

### ❌ Anti-Pattern 2: Custom Middleware Before CORS

```python
# BROKEN - Custom validation blocks OPTIONS before CORS handles it
app = mcp.http_app(stateless_http=True)
app.add_middleware(CORSMiddleware, ...)         # Added first
app.add_middleware(CustomOriginValidator, ...)  # Added second (executes FIRST!)
```

**Middleware execution order**: Last added = First executed. Custom validators must come AFTER CORS in the add_middleware call order, or they'll block preflight requests.

### ❌ Anti-Pattern 3: Overly Restrictive allow_headers

```python
# BRITTLE - Specific header lists can break MCP protocol evolution
app.add_middleware(
    CORSMiddleware,
    allow_headers=["Accept", "Content-Type", "Authorization"],  # Too restrictive
)
```

**Better**: Use `allow_headers=["*"]` for MCP endpoints unless you have specific security requirements.

---

## Testing the Fix

### Before Deployment

1. Review the CORS configuration code
2. Verify OPTIONS is in `allow_methods`
3. Confirm no custom middleware blocks preflight
4. Check `allow_headers` is `["*"]` or includes MCP headers

### After Deployment

1. Check logs immediately after deploy:
   ```python
   getAppsLogs(appName="your-app", tailLines=20, search="OPTIONS")
   ```

2. Attempt connection from Genie Code settings

3. Expected log signature:
   ```
   INFO: "OPTIONS /mcp HTTP/1.1" 200 OK
   INFO: "POST /mcp HTTP/1.1" 200 OK
   INFO: "POST /mcp HTTP/1.1" 202 Accepted
   ```

---

## Prevention Checklist

When creating or debugging MCP-enabled Databricks Apps:

- [ ] Use `FastMCP.http_app(stateless_http=True)` for Databricks Apps
- [ ] Add `CORSMiddleware` with `allow_methods=["POST", "OPTIONS"]`
- [ ] Set `allow_headers=["*"]` unless specific constraints required
- [ ] Configure `allow_origins` from environment variable
- [ ] Add CORS middleware LAST (so it executes first in request flow)
- [ ] Avoid custom origin validation middleware unless absolutely necessary
- [ ] Test with `getAppsLogs` tool to verify OPTIONS returns 200 OK
- [ ] Compare against known-working pattern (mcp-test-simple) when debugging

---

## Reference Implementation

See `/Workspace/Users/user@example.com/test-mcp-simple/test_server.py` for a complete working example.

**Key files to compare**:
* Working: `test-mcp-simple/test_server.py`
* Fixed: `Odibi/odibi_mcp/databricks_app.py` (after 2026-08-14 fix)

---

## Deployment Sequence

Always follow the mandatory Apps deployment sequence:

1. Edit source files (databricks_app.py, requirements.txt, etc.)
2. Check app status: `databricks apps get <name> --output JSON`
3. If status is RUNNING with no pending_deployment → proceed to deploy
4. If status is STOPPED → `databricks apps start <name> --timeout 20m`
5. If status is STARTING/STOPPING → poll with `apps get` (do not start or deploy)
6. Deploy: `databricks apps deploy <name> --source-code-path <path>`
7. Verify: Check logs for OPTIONS 200 OK responses

**Never** call `apps deploy` when status is not RUNNING or pending_deployment exists.

---

## Related Documentation

* FastMCP HTTP Transport: https://github.com/jlowin/fastmcp
* Starlette CORS Middleware: https://www.starlette.io/middleware/#corsmiddleware
* Databricks Apps V2: [Internal docs]
* MCP Protocol Specification: https://spec.modelcontextprotocol.io/

---

## Case Study: mcp-odibi Fix (2026-08-14)

**Problem**: mcp-odibi returned 400 Bad Request on OPTIONS, blocking Genie Code connection.

**Investigation**:
1. Verified app was RUNNING and ACTIVE
2. Compared logs: mcp-test-simple showed OPTIONS 200 OK, mcp-odibi showed OPTIONS 400
3. Compared source: mcp-odibi had custom `_OriginPolicyMiddleware` added AFTER CORS
4. Identified missing OPTIONS in `allow_methods=["POST"]`
5. Found custom middleware was rejecting requests before CORS could handle preflight

**Solution**:
1. Removed custom `_OriginPolicyMiddleware` entirely
2. Changed `allow_methods=["POST"]` → `allow_methods=["POST", "OPTIONS"]`
3. Changed `allow_headers=_CORS_REQUEST_HEADERS` → `allow_headers=["*"]`
4. Simplified to standard CORSMiddleware only (matching working pattern)

**Result**: OPTIONS requests returned 200 OK, MCP connection succeeded, all 43 Odibi actions available.

**Commits**: Deployment IDs:
* Initial fix attempt: `01f197f4fbc915088149830c64376ed3`
* Successful fix: `01f197f6843a171b939c42c1be69f40e`
