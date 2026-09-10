"""Tests for auto-touched filesystem drift detection.

Verifies:
- snapshot_file_mtimes captures governed file types
- check_filesystem_drift detects modified and new files
- Files already in session are NOT flagged as unregistered
- Excluded directories are never scanned
- Graceful degradation with no boot manifest
"""
import os
import time

import pytest

from odibi_anchor._utils._session_state import (
    _SESSION_FILES_CHANGED,
    _build_current_manifest,
    build_boot_manifest,
    check_filesystem_drift,
)


@pytest.fixture(autouse=True)
def _clean_session_state():
    """Reset session state between tests."""
    _SESSION_FILES_CHANGED.clear()
    yield
    _SESSION_FILES_CHANGED.clear()


def test_manifest_captures_py_and_md(tmp_path):
    """Manifest captures .py and .md files with mtimes."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "module.py").write_text("x = 1")
    (tmp_path / "src" / "readme.md").write_text("# docs")
    (tmp_path / "src" / "data.csv").write_text("a,b")  # not governed

    manifest = _build_current_manifest(str(tmp_path))

    assert "src/module.py" in manifest
    assert "src/readme.md" in manifest
    assert "src/data.csv" not in manifest  # .csv not in governed extensions


def test_detect_modified_file(tmp_path):
    """Modified file not in session is detected as unregistered."""
    (tmp_path / "module.py").write_text("original")

    # Build boot manifest
    build_boot_manifest(str(tmp_path))
    # Force lazy build
    check_filesystem_drift(str(tmp_path))  # triggers manifest build

    # Modify the file
    time.sleep(0.05)
    (tmp_path / "module.py").write_text("modified")

    result = check_filesystem_drift(str(tmp_path))

    assert result["has_drift"] is True
    assert "module.py" in result["unregistered"]


def test_touched_file_not_flagged(tmp_path):
    """File properly registered via touched is NOT flagged."""
    (tmp_path / "module.py").write_text("original")

    build_boot_manifest(str(tmp_path))
    check_filesystem_drift(str(tmp_path))  # triggers lazy build

    # Modify and register
    time.sleep(0.05)
    (tmp_path / "module.py").write_text("modified")
    _SESSION_FILES_CHANGED.add("module.py")

    result = check_filesystem_drift(str(tmp_path))

    # Should NOT be unregistered since we touched it
    assert "module.py" not in result.get("unregistered", [])


def test_new_file_detected(tmp_path):
    """File created after boot manifest is detected as drift."""
    import odibi_anchor._utils._session_state as _ss

    # Need at least one file at boot so manifest is non-empty
    (tmp_path / "existing.py").write_text("original")

    # Reset manifest state for isolation
    _ss._BOOT_MANIFEST_ROOT = str(tmp_path)
    _ss._BOOT_MANIFEST_BUILT = False
    _ss._SESSION_BOOT_MANIFEST = {}

    # Build boot manifest (has existing.py)
    build_boot_manifest(str(tmp_path))
    check_filesystem_drift(str(tmp_path))  # triggers lazy build

    # Create new file AFTER manifest was built
    (tmp_path / "new_module.py").write_text("new content")

    result = check_filesystem_drift(str(tmp_path))

    assert result["has_drift"] is True
    assert "new_module.py" in result["unregistered"]
    assert "new_module.py" in result["created"]


def test_no_manifest_returns_graceful(tmp_path):
    """Without boot manifest, returns no drift (graceful degradation)."""
    # Reset globals to simulate no manifest
    import odibi_anchor._utils._session_state as _ss
    old_built = _ss._BOOT_MANIFEST_BUILT
    old_root = _ss._BOOT_MANIFEST_ROOT
    old_manifest = _ss._SESSION_BOOT_MANIFEST

    _ss._BOOT_MANIFEST_BUILT = True  # Skip lazy build
    _ss._SESSION_BOOT_MANIFEST = {}  # Empty manifest

    try:
        result = check_filesystem_drift(str(tmp_path))
        assert result["has_drift"] is False
    finally:
        _ss._BOOT_MANIFEST_BUILT = old_built
        _ss._BOOT_MANIFEST_ROOT = old_root
        _ss._SESSION_BOOT_MANIFEST = old_manifest


def test_excludes_pycache_and_git(tmp_path):
    """Files in excluded directories are not in manifest."""
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "module.cpython-311.pyc").write_text("bytecode")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config.py").write_text("git stuff")
    (tmp_path / "good.py").write_text("real code")

    manifest = _build_current_manifest(str(tmp_path))

    assert "good.py" in manifest
    assert not any("__pycache__" in k for k in manifest)
    assert not any(".git" in k for k in manifest)
