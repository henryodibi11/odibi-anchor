"""Shared test fixtures for odibi_anchor."""
import os
import sys
import tempfile
import warnings
from pathlib import Path

import pytest

# On Databricks, ANCHOR_HOME must be set to a durable external path. For the test
# session, default to a temp directory so tests never require explicit external
# state configuration and never write into the source checkout.
_cw_test_home = os.path.join(tempfile.gettempdir(), "anchor_test_home")
os.makedirs(_cw_test_home, exist_ok=True)
os.environ.setdefault("ANCHOR_HOME", _cw_test_home)

# Point the shared memory DB at a throwaway temp file for the whole test session,
# BEFORE any odibi_anchor import resolves _DEFAULT_DB_PATH. This guarantees
# tests never read or write the repo's committed .agent_memory.db by default,
# regardless of which environment profile is detected.
os.environ.setdefault(
    "ANCHOR_MEMORY_DB", os.path.join(tempfile.gettempdir(), "anchor_test_agent_memory.db")
)

# Ensure src/ is on path for test discovery
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Project root resolved from this file's location (tests/ -> project root)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True, scope="session")
def _guard_tracked_state_files():
    """Safety net: keep the repo's working tree clean during tests.

    The repository no longer tracks ``.agent_memory.db``; tests use isolated
    temporary databases via ``ANCHOR_MEMORY_DB`` and ``ANCHOR_HOME``.  This guard
    remains as a catch-all for any stateful file that might be accidentally
    written into the checkout.
    """
    targets = [PROJECT_ROOT / ".agent_memory.db"]
    snapshots = {p: p.read_bytes() for p in targets if p.exists()}
    yield
    for path, original in snapshots.items():
        if path.exists() and path.read_bytes() != original:
            path.write_bytes(original)
            warnings.warn(
                f"A test mutated the tracked state file {path.name!r}; restored it. "
                "Point the writer at a tmp path instead.",
                stacklevel=2,
            )


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "timeout(seconds): per-test timeout override")


@pytest.fixture
def project_root():
    """Portable project root — use instead of hardcoded paths."""
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def cached_project_ctx():
    """Session-scoped cached codebase map — avoids re-scanning per test.

    The full codebase_map_context call traverses all .py files with AST parsing.
    Caching this at session scope cuts ~30s of repeated work across slow test modules.
    """
    from odibi_anchor.codebase import codebase_map_context

    return codebase_map_context(PROJECT_ROOT, subject="odibi_anchor")


@pytest.fixture(scope="session")
def cached_src_ctx():
    """Session-scoped source-only scan (no test mapping)."""
    from odibi_anchor.codebase import codebase_map_context

    src_path = PROJECT_ROOT / "src" / "odibi_anchor"
    return codebase_map_context(src_path, subject="anchor_src", include_tests=False)


# Test modules that do full AST traversal or call all tools (>5s per test)
_SLOW_MODULES = {
    "test_change_impact_context",
    "test_codebase_map_context",
    "test_consistency_check_context",
    "test_session_snapshot_context",
    "test_output_schema",
    "test_focus_context",
    "test_workflow_gate_context",
    "test_framework_lookup_context",
    "test_render_functions",
}

# Fast modules that live under tests/codebase/ but don't do full traversal
_FAST_CODEBASE_MODULES = {
    "test_memory_db",
    "test_memory_context",
    "test_auto_confirm",
    "test_gate_timing_verification",
}


def pytest_collection_modifyitems(config, items):
    """Auto-mark tests as fast/slow.

    Slow: any test whose module name is in _SLOW_MODULES, or under tests/codebase/
          (unless explicitly listed in _FAST_CODEBASE_MODULES).
    Fast: everything else (validation, profiling, tables, planning, utils).
    """
    for item in items:
        module_name = item.module.__name__.rsplit(".", 1)[-1] if item.module else ""
        rel_path = str(item.fspath.relto(config.rootdir))
        is_slow = (
            module_name in _SLOW_MODULES
            or ("codebase" in rel_path and module_name not in _FAST_CODEBASE_MODULES)
        )
        if is_slow:
            item.add_marker(pytest.mark.slow)
        else:
            item.add_marker(pytest.mark.fast)
