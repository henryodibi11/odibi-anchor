# Dependency decision workflow

> Preserved technique source for the native owner.

Managing Python packages in the Databricks + local development environment. Covers
installation, version conflicts, import errors, and wheel builds.

## Applicable assurance overlay

When structured selection activates `supply-chain.software`, use
`.assistant/references/assurance/standards-overlays.md` for the applicable outcomes and
result-evidence contract. This workflow remains the procedure owner; the pointer creates no
obligation by itself.

## When to Use This Skill

| Request | Use this skill? |
|---|---|
| "Add library X to the project" | ✅ Yes |
| "Why is this import failing?" | ✅ Yes |
| "Resolve version conflict" | ✅ Yes |
| "Upgrade library from v1 to v2" | ✅ Partially — also use the accepted work item's migration technique when material |
| "Write code using library X" | ❌ No — use the accepted work item's integration technique |

## Databricks Package Management

### Installation Methods (ranked by preference)

| Method | When to use | Scope | Persistence |
|---|---|---|---|
| Cluster library (UI/API) | Shared team dependency | Cluster-wide | Persists across restarts |
| `%pip install` in notebook | Notebook-specific dependency | Notebook session | Lost on cluster restart |
| Workspace file (`--no-deps`) | Internal/custom packages | Notebook session | Wheel persists, install doesn't |
| Init script | Must be available at cluster start | Cluster-wide | Persists across restarts |

### Critical Rules for Databricks

```python
# ALWAYS use --no-deps for internal packages (avoids pulling in unwanted transitive deps)
%pip install --no-deps /path/to/current-project-approved-package

# ALWAYS restart Python after %pip install
dbutils.library.restartPython()

# NEVER %pip install at cluster scope from a notebook — affects all users
# Use cluster libraries (UI) for shared dependencies instead

# Check what's installed
%pip list | grep library_name
%pip show library_name  # Version, location, dependencies
```

### Common Databricks Issues

| Issue | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError` after install | Didn't restart Python | `dbutils.library.restartPython()` |
| `ModuleNotFoundError` on new cluster | `%pip install` doesn't persist | Add to cluster libraries or notebook init |
| Version conflict with DBR | DBR pre-installs packages (numpy, pandas) | Pin exact version: `%pip install lib==x.y.z` |
| `--no-deps` still pulls deps | Using `install` instead of `install --no-deps` | Verify flag spelling, check setup.py |
| Import works in notebook, fails in test | Different Python env for tests | Install in test env too, or use `conftest.py` fixture |
| `Py4JJavaError` on import | Java/JVM dependency missing | Check if library needs JARs — install via cluster config |

## Local Development

### Virtual Environment

```bash
# Create venv
python -m venv .venv

# Activate
# Windows:
.venv\Scripts\activate
# Linux/Mac:
source .venv/bin/activate

# Install project dependencies
pip install -r requirements.txt
pip install -e .  # Editable install for development
```

### Requirements Files

```
# requirements.txt — pinned production dependencies
pyspark==3.5.0
delta-spark==3.1.0
office365-rest-python-client==2.5.1

# requirements-dev.txt — development/test dependencies
-r requirements.txt
pytest==8.0.0
ruff==0.3.0
```

**Rules:**
- Pin exact versions in production (`==`), use ranges in libraries (`>=`, `~=`)
- Separate prod and dev requirements
- Include transitive deps only if you need to pin them
- Update `requirements.txt` whenever you add a dependency

## Troubleshooting Import Errors

### Decision Tree

```
ImportError or ModuleNotFoundError
├── Is the package installed?
│   ├── NO → pip install it
│   └── YES → Continue
├── Is the right version installed?
│   ├── NO → pip install package==correct_version
│   └── YES → Continue
├── Is the import path correct?
│   ├── NO → Check docs for correct import
│   └── YES → Continue
├── Is there a naming conflict?
│   ├── YES → Your file/folder shadows the package (rename it)
│   └── NO → Continue
├── Is __init__.py present? (for local packages)
│   ├── NO → Create it
│   └── YES → Continue
├── Circular import?
│   ├── YES → Restructure (extract shared types to third module)
│   └── NO → Continue
└── Environment mismatch?
    ├── Notebook vs test env → Install in both
    └── Wrong Python version → Check sys.version
```

### Circular Import Resolution

```python
# PROBLEM: a.py imports from b.py, b.py imports from a.py

# SOLUTION 1: Move shared types to c.py
# c.py — shared types (no imports from a or b)
# a.py — imports from c
# b.py — imports from c

# SOLUTION 2: Lazy import (import inside function)
def my_function():
    from b import helper  # Only imported when called
    return helper()

# SOLUTION 3: TYPE_CHECKING guard (for type hints only)
from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from b import SomeType  # Only imported during type checking
```

## Version Conflict Resolution

### Diagnostic

```bash
# What version is installed?
pip show package_name

# What depends on it?
pip show package_name  # Check "Required-by" field

# What versions are available?
pip index versions package_name

# What does the conflict look like?
pip check  # Shows broken dependencies
```

### Resolution Strategies

| Conflict type | Strategy |
|---|---|
| Two packages need different versions of same dep | Pin to version that satisfies both (find overlap) |
| DBR pre-installed version conflicts | Pin explicitly: `%pip install dep==compatible_version` |
| No compatible version exists | Use `--no-deps` and manually manage the dependency |
| Upgrading breaks other packages | Upgrade related packages together |

## Adding a New Dependency — Checklist

Before adding ANY new package:

1. **Is it necessary?** Can you do this with stdlib or existing dependencies?
2. **Is it maintained?** Check: last commit date, open issues, download count
3. **Is it compatible?** Check: Python version, Databricks Runtime, existing deps
4. **Is it licensed correctly?** Check: license type vs your project requirements
5. **How big is it?** Check: dependency tree size (`pip install --dry-run`)
6. **Is it secure?** Check: known vulnerabilities, trusted maintainers

```python
# After deciding to add:
# 1. Install
%pip install new-package==1.2.3

# 2. Verify import
import new_package
print(new_package.__version__)

# 3. Update requirements
# Add to requirements.txt with pinned version

# 4. Document WHY in the owning project decision/spec artifact.
# Capture learning only when current evidence supports a reusable observation.
```

## Integration with odibi-anchor

```python
# Plan dependency changes
anchor("task", "add SharePoint client library",
    goal="install and verify office365-rest-python-client for file access",
    mode="implementation")

# After changes
anchor("touched", "requirements.txt")
anchor("gate")
observation = anchor("learning", "capture", observation_type="reusable_practice", ...)
anchor("learning", "assess", outcome="observations_recorded",
   observation_ids=[observation["item"]["item_id"]])
```
