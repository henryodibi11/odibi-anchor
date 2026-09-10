# Integration planning

> Preserved integration planning technique.

Evidence-gathering checklist for integration tasks — using external libraries, APIs, SDKs,
or services. Execute this checklist BEFORE calling `anchor("task")`. Every item you complete
becomes a `known_fact` or `constraint` in your plan.

Use this local reference when integration planning is material to the managed work item.

## Core Principle

The #1 cause of bad integration code is **using 10% of a library because you only read 10%
of its docs.** A library that has a built-in method for your exact use case will be ignored
if you never discovered it. Research FIRST, implement SECOND.

## Evidence Checklist — Execute In Order

### 1. Identify the Library/API

**Record as known_facts:**
- Exact package name and version installed (or to be installed)
- Import path (`from X import Y`)
- Is it already used in this codebase? Where? (`anchor("map")` or grep for imports)
- License (if adding a new dependency)

```python
# Check if already in use
anchor("map")  # look for existing imports of the library
# Or search directly
# grep for "import library_name" across the codebase
```

**If the library is NOT already in the codebase:**
- Record: "New dependency — needs `%pip install` or requirements.txt update"
- Check compatibility with current Python version and Databricks Runtime
- Load `skills/dependency-management/SKILL.md` if installation is non-trivial

### 2. Read the Official Documentation

This is the critical step most agents skip. Do NOT start coding until you've done this.

```python
# Search for official docs
web_search("library_name official documentation API reference")
web_search("library_name getting started tutorial")
web_search("library_name changelog latest version")
```

**Record as known_facts:**
- What the library does (one sentence)
- Core classes and their responsibilities
- Key methods you'll likely use (name, params, return type)
- Authentication/configuration requirements
- Rate limits, quotas, or usage constraints
- Known limitations or gotchas from docs

**Rule:** Read at minimum:
1. The **Getting Started / Quickstart** page
2. The **API Reference** for the classes you'll use
3. The **Examples** section (if it exists)
4. The **Changelog** for the current version (breaking changes, deprecations)

### 3. Explore the API Surface

After reading docs, explore programmatically to fill gaps:

```python
import library_name

# Top-level API
dir(library_name)

# Key class methods
help(library_name.MainClass)

# Or for specific methods
help(library_name.MainClass.method_name)
```

**Record as known_facts:**
- Full list of public classes/functions you might need
- Method signatures (params, types, defaults) for your target methods
- Return types and shapes (what does the method give back?)
- Exception types the library raises (for error handling)
- Configuration options and their defaults

### 4. Find Working Examples

```python
# Search for real usage examples
web_search("library_name example usage Python")
web_search("library_name library_name.MainClass example")
# For Databricks-specific libraries:
web_search("library_name Databricks notebook example")
```

**Record as known_facts:**
- Working code snippets from docs or reputable sources
- Common patterns (initialization → configure → execute → cleanup)
- Anti-patterns called out in docs ("don't do X")
- Thread safety, async considerations, connection pooling

### 5. Map Capabilities to Requirements

This is where research becomes planning. For each thing you need to accomplish:

| Requirement | Library feature | Method/class | Confidence |
|---|---|---|---|
| Read SharePoint files | `File.open_binary()` | `ctx.web.get_file_by_url()` | High — docs example |
| List folder contents | `Folder.files` property | `ctx.web.get_folder_by_url().files` | High — API ref |
| Handle auth | `ClientCredential` | `ClientContext(url, creds)` | Medium — need to verify |
| Retry on failure | (none built-in) | Need custom wrapper | Low — not in docs |

**Record as known_facts:**
- One row per requirement → library feature mapping
- Gaps: things you need that the library doesn't provide
- Things the library provides that you didn't know about (bonus capabilities)

**Record as constraints:**
- Library limitations that affect your approach
- Required configuration/setup before use
- Cleanup/teardown requirements (connections, sessions, temp files)

### 6. Check for Gotchas and Known Issues

```python
# Search for common problems
web_search("library_name common issues pitfalls")
web_search("library_name library_name.MainClass error")
```

**Record as known_facts:**
- Known bugs or limitations in current version
- Performance gotchas (memory leaks, slow methods, connection limits)
- Compatibility issues (Python version, OS, Databricks Runtime)
- Security considerations (credential handling, data exposure)

After task acceptance, review its bounded `memory_context` for advisory integration history.
Do not turn a pre-task evidence checklist into a standalone or full-store memory query.

**Record as risks:**
- Things that could go wrong based on docs/issues
- Mitigation strategies for each risk

### 7. Design the Integration Pattern

Before coding, decide on the integration architecture:

| Decision | Options | Choice | Why |
|---|---|---|---|
| Wrapper vs direct use | Wrap in helper function vs call directly | Wrap | Testable, swappable |
| Error handling | Let exceptions bubble vs catch and convert | Catch + convert | Library errors are cryptic |
| Configuration | Hardcode vs config dict vs environment vars | Config dict | Consistent with codebase |
| Connection lifecycle | Per-call vs session-scoped vs singleton | Session-scoped | Reuse across operations |

**Record as constraints:**
- Integration pattern chosen and why
- Error handling strategy
- Configuration approach
- How to make it testable (dependency injection, mock boundaries)

## Output — What You Feed to `anchor("task")`

After completing this checklist, you should have:

```python
known_facts = [
    "Library: office365-rest-python-client v2.5.1 (not yet in codebase)",
    "Auth: ClientCredential with client_id + client_secret from Azure Key Vault",
    "Key class: ClientContext — manages connection and auth lifecycle",
    "Read files: ctx.web.get_file_by_url(url).execute_query() returns binary",
    "List files: ctx.web.get_folder_by_url(url).files.get().execute_query()",
    "Pagination: library handles it internally via execute_query()",
    "Rate limit: SharePoint throttles at 600 req/min — need backoff",
    "Gotcha: execute_query() is lazy — must call it to trigger the request",
    "Gotcha: file paths are URL-encoded — spaces become %20",
    "Existing pattern: no SharePoint integration exists in codebase",
    "Test strategy: mock ClientContext, return fixture file bytes",
]

constraints = [
    "Wrap in SharePointClient helper class — isolate library dependency",
    "Credentials from Azure Key Vault, never hardcoded",
    "Add retry with exponential backoff for throttling (HTTP 429)",
    "Return bytes or DataFrame, never library-specific objects",
    "Must work on Databricks Runtime 17.3 LTS",
]

acceptance_criteria = [
    "Can list files in a SharePoint folder",
    "Can download a file and return as bytes",
    "Can read an Excel file into a DataFrame",
    "Retry logic handles HTTP 429 correctly",
    "All library calls wrapped — no direct library usage outside helper",
    "Unit tests with mocked SharePoint client pass",
]

in_scope = ["SharePoint file reader using office365-rest-python-client"]
out_of_scope = ["Writing to SharePoint", "SharePoint list operations"]

risks = [
    "Library may not be compatible with DBR 17.3 — test import first",
    "SharePoint throttling may require longer backoff than expected",
    "File paths with special characters may break URL encoding",
]
```

Continue the accepted work item using the gathered evidence.
## Formal Spec compliance
If accepted task policy requires a formal Spec, use `writing-specs`, include its
compliance requirements, and review the exact linked Spec to a rating ≥ good
before consequential effects. Do not infer a Spec requirement from mode alone.
