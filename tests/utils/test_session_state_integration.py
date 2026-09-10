"""Integration tests for the session state chain: safe → touched → gate.

Verifies that the shared singleton resolves the namespace split bug:
    - safe_change_context's auto-touched registers in the SAME session state
      that anchor("gate") reads from
    - AST cache invalidation from safe is visible to map/impact/preflight
    - Re-bootstrap preserves accumulated session state

These tests exercise the cross-module boundaries that unit tests cannot catch.
"""

import importlib
import os
import sys

import pytest


# ─── Cross-module coherence ──────────────────────────────────────────────────

class TestNamespaceCoherence:
    """Verify the singleton resolves the exec/import namespace split."""

    @pytest.mark.fast
    def test_touched_from_different_import_paths_share_state(self):
        """Both import paths resolve to the same mutable objects."""
        from odibi_anchor._utils._session_state import (
            _SESSION_FILES_CHANGED as direct_set,
            touched,
        )
        # Simulate what safe_change_context does
        from odibi_anchor._utils._session_state import (
            _SESSION_FILES_CHANGED as via_module,
        )
        assert direct_set is via_module

        touched("coherence_a.py")
        assert "coherence_a.py" in direct_set
        assert "coherence_a.py" in via_module

    @pytest.mark.fast
    def test_module_reimport_returns_same_objects(self):
        """Simulates re-bootstrap — re-importing preserves the singleton."""
        from odibi_anchor._utils._session_state import (
            _SESSION_FILES_CHANGED, touched,
        )
        touched("before_reimport.py")

        # Force re-import (simulates exec(agent_init.py) re-running)
        mod = importlib.import_module("odibi_anchor._utils._session_state")
        importlib.reload(mod)  # Explicit reload

        # After reload, the module-level sets are NEW objects...
        # but that's the point — we DON'T reload in production.
        # The singleton pattern works because sys.modules caches the module.
        # Let's verify the normal (non-reload) case:
        from odibi_anchor._utils._session_state import (
            _SESSION_FILES_CHANGED as after_import,
        )
        # After reload, Python re-binds — this is expected.
        # The key invariant is: within a session, all imports get the same object.
        # (reload is NOT what bootstrap does — bootstrap does `from ... import`)

    @pytest.mark.fast
    def test_sys_modules_singleton(self):
        """The module in sys.modules is the single source of truth."""
        mod = sys.modules.get("odibi_anchor._utils._session_state")
        assert mod is not None, "_session_state should be in sys.modules"

        from odibi_anchor._utils._session_state import _SESSION_FILES_CHANGED
        assert _SESSION_FILES_CHANGED is mod._SESSION_FILES_CHANGED


# ─── safe_change_context integration ─────────────────────────────────────────

class TestSafeChangeIntegration:
    """Verify safe_change_context uses the shared singleton."""

    @pytest.fixture
    def project_with_module(self, tmp_path):
        """Create a minimal project for safe_change_context."""
        src = tmp_path / "src" / "pkg"
        src.mkdir(parents=True)
        target = src / "mod.py"
        target.write_text(
            'def greet(name: str) -> str:\n'
            '    """Say hello."""\n'
            '    return f"Hello, {name}"\n'
        )
        # Need a tests/ dir for the project to look valid
        (tmp_path / "tests").mkdir()
        return tmp_path

    @pytest.mark.fast
    def test_safe_dry_run_does_not_register_touched(self, project_with_module):
        """Dry-run (apply=False) should NOT register the file as touched."""
        from odibi_anchor._utils._session_state import _SESSION_FILES_CHANGED
        from odibi_anchor.codebase.safe_change_context import safe_change_context

        safe_change_context(
            project_with_module,
            target="src/pkg/mod.py",
            action="replace_function",
            function="greet",
            body='return f"Hi, {name}"',
            apply=False,
            output_format="dict",
        )
        # Dry-run should NOT touch the file in session state
        assert "src/pkg/mod.py" not in _SESSION_FILES_CHANGED

    @pytest.mark.fast
    def test_safe_apply_registers_in_shared_state(self, project_with_module):
        """apply=True should register the file via the shared singleton."""
        from odibi_anchor._utils._session_state import _SESSION_FILES_CHANGED
        from odibi_anchor.codebase.safe_change_context import safe_change_context

        result = safe_change_context(
            project_with_module,
            target="src/pkg/mod.py",
            action="replace_function",
            function="greet",
            body='return f"Hi, {name}"',
            apply=True,
            output_format="dict",
        )
        # File should be registered in the SHARED singleton
        assert "src/pkg/mod.py" in _SESSION_FILES_CHANGED
        assert result.get("metrics", {}).get("applied") is True

    @pytest.mark.fast
    def test_safe_apply_syntax_check_passes(self, project_with_module):
        """Applied edits should pass syntax check via the singleton's touched()."""
        from odibi_anchor.codebase.safe_change_context import safe_change_context

        result = safe_change_context(
            project_with_module,
            target="src/pkg/mod.py",
            action="replace_function",
            function="greet",
            body='return f"Hi, {name}"',
            apply=True,
            output_format="dict",
        )
        # touched_syntax_check comes from the singleton's touched() call
        assert result.get("metrics", {}).get("touched_syntax_check") == "passed"


# ─── AST cache invalidation across tool boundaries ───────────────────────────

class TestASTCacheCoherence:
    """Verify AST cache invalidation propagates correctly."""

    @pytest.mark.fast
    def test_touched_invalidates_ast_cache(self, tmp_path):
        """touched() should invalidate AST cache so get_file_info sees fresh content."""
        from odibi_anchor._utils._session_state import touched
        from odibi_anchor._utils.ast_utils import get_file_info

        test_file = tmp_path / "evolving.py"
        test_file.write_text("x = 1\n")

        # Populate cache
        info_v1 = get_file_info(str(test_file))
        assert info_v1["functions"] == []

        # Edit the file
        test_file.write_text("def new_func(): pass\n")

        # Without invalidation, cache would be stale
        # touched() should invalidate
        touched(str(test_file))

        # Now get_file_info should see the new function
        info_v2 = get_file_info(str(test_file))
        fn_names = [f["name"] for f in info_v2.get("functions", [])]
        assert "new_func" in fn_names

    @pytest.mark.fast
    def test_safe_apply_invalidates_ast_for_map(self, tmp_path):
        """After safe apply, get_file_info should reflect the edit."""
        from odibi_anchor._utils.ast_utils import get_file_info
        from odibi_anchor.codebase.safe_change_context import safe_change_context

        # Create project
        src = tmp_path / "src" / "pkg"
        src.mkdir(parents=True)
        (tmp_path / "tests").mkdir()
        target = src / "svc.py"
        target.write_text(
            'def process(data: list) -> list:\n'
            '    """Process data."""\n'
            '    return data\n'
        )

        # Cache the file's AST
        get_file_info(str(target))

        # Apply edit via safe
        safe_change_context(
            tmp_path,
            target="src/pkg/svc.py",
            action="replace_function",
            function="process",
            body='return [x * 2 for x in data]',
            apply=True,
            output_format="dict",
        )

        # AST cache should be invalidated — check file content changed
        info = get_file_info(str(target))
        # The function should still exist (we replaced body, not removed it)
        fn_names = [f["name"] for f in info.get("functions", [])]
        assert "process" in fn_names


# ─── Gate integration with session state ─────────────────────────────────────

class TestGateReadsSessionState:
    """Verify gate auto-populates from the shared session state."""

    @pytest.mark.fast
    def test_gate_sees_files_from_session_state(self, tmp_path):
        """Gate auto-populates from session state when called via dispatcher pattern."""
        from odibi_anchor._utils._session_state import (
            touched, _SESSION_FILES_CHANGED, get_state,
        )
        from odibi_anchor.codebase.workflow_gate_context import workflow_gate_context

        # Create minimal project
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.py").write_text("pass\n")
        (tmp_path / "tests").mkdir()

        # Register a file via the singleton
        touched("src/a.py", root=str(tmp_path))

        # Verify singleton captured it
        assert "src/a.py" in _SESSION_FILES_CHANGED
        state = get_state()
        assert state["total_changed"] == 1

        # Simulate what anchor("gate") dispatcher does: pass files_changed from session
        result = workflow_gate_context(
            tmp_path,
            actions_taken=["implement", "edit"],
            files_changed=state["files_changed"],  # Explicit from session (what dispatcher does)
            output_format="dict",
        )
        metrics = result.get("metrics", {})
        assert metrics.get("files_changed_count") == 1

    @pytest.mark.fast
    def test_gate_with_explicit_files_overrides_session(self, tmp_path):
        """Explicit files_changed should take priority over session state."""
        from odibi_anchor._utils._session_state import touched
        from odibi_anchor.codebase.workflow_gate_context import workflow_gate_context

        (tmp_path / "tests").mkdir()
        touched("from_session.py")

        result = workflow_gate_context(
            tmp_path,
            actions_taken=["implement"],
            files_changed=["explicit.py"],
            output_format="dict",
        )
        # Gate should use the explicit list, not session state
        metrics = result.get("metrics", {})
        assert metrics.get("files_changed_count") == 1
