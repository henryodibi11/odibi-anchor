"""Tests for boot manifest, filesystem drift detection, and write guard."""

import builtins
import os
from pathlib import Path

import pytest

import odibi_anchor._utils._session_state as _ss
from odibi_anchor._utils._session_state import (
    _SESSION_DIFF_BASELINES,
    _SESSION_FILES_CHANGED,
    _SESSION_FILES_CREATED,
    _SESSION_PROVEN_BASELINES,
    _SESSION_PROVEN_CREATED,
    _ensure_boot_manifest_built,
    build_boot_manifest,
    check_filesystem_drift,
    get_diff,
    install_write_guard,
    reconcile_session_file_ledger,
    touched,
    uninstall_write_guard,
)


@pytest.fixture
def project(tmp_path):
    """Create a minimal project with governed files."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("def hello(): pass\n")
    (tmp_path / "src" / "utils.py").write_text("import os\n")
    (tmp_path / "config.yaml").write_text("key: value\n")
    (tmp_path / "README.md").write_text("# Project\n")
    # Non-governed file
    (tmp_path / "data.bin").write_bytes(b"\x00\x01\x02")
    # Anchor-generated file (should be excluded)
    (tmp_path / ".patch_log.jsonl").write_text("{}\n")
    return tmp_path


@pytest.fixture(autouse=True)
def clean_session_state():
    """Reset session state between tests."""
    uninstall_write_guard()
    _ss._SESSION_BOOT_MANIFEST = {}
    _ss._BOOT_MANIFEST_BUILT = False
    _ss._BOOT_MANIFEST_ROOT = None
    _SESSION_FILES_CHANGED.clear()
    _SESSION_FILES_CREATED.clear()
    _SESSION_DIFF_BASELINES.clear()
    _SESSION_PROVEN_CREATED.clear()
    _SESSION_PROVEN_BASELINES.clear()
    yield
    uninstall_write_guard()
    _ss._SESSION_BOOT_MANIFEST = {}
    _ss._BOOT_MANIFEST_BUILT = False
    _ss._BOOT_MANIFEST_ROOT = None
    _SESSION_FILES_CHANGED.clear()
    _SESSION_FILES_CREATED.clear()
    _SESSION_DIFF_BASELINES.clear()
    _SESSION_PROVEN_CREATED.clear()
    _SESSION_PROVEN_BASELINES.clear()


class TestBuildBootManifest:
    def test_captures_governed_files(self, project):
        build_boot_manifest(str(project))
        _ensure_boot_manifest_built()
        manifest = _ss._SESSION_BOOT_MANIFEST
        assert "src/main.py" in manifest
        assert "src/utils.py" in manifest
        assert "config.yaml" in manifest
        assert "README.md" in manifest

    def test_excludes_non_governed_extensions(self, project):
        build_boot_manifest(str(project))
        _ensure_boot_manifest_built()
        manifest = _ss._SESSION_BOOT_MANIFEST
        assert "data.bin" not in manifest

    def test_excludes_cw_generated_files(self, project):
        build_boot_manifest(str(project))
        _ensure_boot_manifest_built()
        manifest = _ss._SESSION_BOOT_MANIFEST
        assert ".patch_log.jsonl" not in manifest

    def test_excludes_skip_dirs(self, project):
        pycache = project / "__pycache__"
        pycache.mkdir()
        (pycache / "cached.py").write_text("# cached\n")
        build_boot_manifest(str(project))
        _ensure_boot_manifest_built()
        manifest = _ss._SESSION_BOOT_MANIFEST
        assert "__pycache__/cached.py" not in manifest

    def test_manifest_has_mtime_and_size(self, project):
        build_boot_manifest(str(project))
        _ensure_boot_manifest_built()
        manifest = _ss._SESSION_BOOT_MANIFEST
        entry = manifest["src/main.py"]
        assert "mtime" in entry
        assert "size" in entry
        assert isinstance(entry["mtime"], float)
        assert entry["size"] > 0

    def test_stores_in_global(self, project):
        build_boot_manifest(str(project))
        _ensure_boot_manifest_built()
        assert len(_ss._SESSION_BOOT_MANIFEST) > 0

    def test_uses_forward_slashes(self, project):
        build_boot_manifest(str(project))
        _ensure_boot_manifest_built()
        manifest = _ss._SESSION_BOOT_MANIFEST
        for path in manifest:
            assert "\\" not in path


class TestCheckFilesystemDrift:
    def test_no_drift_when_unchanged(self, project):
        build_boot_manifest(str(project))
        result = check_filesystem_drift(str(project))
        assert result["has_drift"] is False
        assert result["unregistered"] == []
        assert result["modified"] == []
        assert result["created"] == []
        assert result["deleted"] == []

    def test_detects_unregistered_modification(self, project):
        build_boot_manifest(str(project))
        # Force manifest build before modifying file
        _ensure_boot_manifest_built()
        (project / "src" / "main.py").write_text("def hello(): return 42\n")
        result = check_filesystem_drift(str(project))
        assert result["has_drift"] is True
        assert "src/main.py" in result["unregistered"]
        assert "src/main.py" in result["modified"]

    def test_registered_modification_no_drift(self, project):
        build_boot_manifest(str(project))
        _ensure_boot_manifest_built()
        (project / "src" / "main.py").write_text("def hello(): return 42\n")
        _SESSION_FILES_CHANGED.add("src/main.py")
        result = check_filesystem_drift(str(project))
        assert result["has_drift"] is False
        assert "src/main.py" in result["modified"]
        assert result["unregistered"] == []

    def test_detects_unregistered_creation(self, project):
        build_boot_manifest(str(project))
        _ensure_boot_manifest_built()
        (project / "new_file.py").write_text("# new\n")
        result = check_filesystem_drift(str(project))
        assert result["has_drift"] is True
        assert "new_file.py" in result["unregistered"]
        assert "new_file.py" in result["created"]

    def test_detects_deletion(self, project):
        build_boot_manifest(str(project))
        _ensure_boot_manifest_built()
        (project / "src" / "utils.py").unlink()
        result = check_filesystem_drift(str(project))
        assert "src/utils.py" in result["deleted"]

    def test_graceful_when_no_manifest(self, project):
        result = check_filesystem_drift(str(project))
        assert result["has_drift"] is False
        assert "error" in result


class TestSessionLedgerReconciliation:
    def test_created_then_deleted_is_removed_from_ledger(self, project):
        build_boot_manifest(str(project))
        install_write_guard(str(project))
        path = project / "temporary.py"
        path.write_text("print('temporary')\n", encoding="utf-8")
        path.unlink()

        result = reconcile_session_file_ledger(str(project))

        assert result["removed_created"] == ["temporary.py"]
        assert "temporary.py" not in _SESSION_FILES_CHANGED
        assert "temporary.py" not in _SESSION_FILES_CREATED

    def test_modified_then_restored_is_removed_from_ledger(self, project):
        path = project / "src" / "main.py"
        original = path.read_text(encoding="utf-8")
        build_boot_manifest(str(project))
        install_write_guard(str(project))
        path.write_text("def hello(): return 42\n", encoding="utf-8")
        path.write_text(original, encoding="utf-8")

        result = reconcile_session_file_ledger(str(project))

        assert result["removed_restored"] == ["src/main.py"]
        assert "src/main.py" not in _SESSION_FILES_CHANGED
        assert check_filesystem_drift(str(project))["has_drift"] is False

    def test_preexisting_deleted_file_remains_a_change(self, project):
        path = project / "src" / "main.py"
        build_boot_manifest(str(project))  # Deliberately leave the scan lazy.
        install_write_guard(str(project))
        path.write_text("def hello(): return 42\n", encoding="utf-8")
        path.unlink()

        reconcile_session_file_ledger(str(project))

        assert "src/main.py" in _SESSION_FILES_CHANGED

    def test_guard_records_created_file_before_lazy_manifest_scan(self, project):
        build_boot_manifest(str(project))
        install_write_guard(str(project))
        path = project / "created_late.py"
        path.write_text("value = 1\n", encoding="utf-8")

        assert "created_late.py" in _SESSION_FILES_CREATED
        path.unlink()
        reconcile_session_file_ledger(str(project))
        assert "created_late.py" not in _SESSION_FILES_CHANGED

    def test_recreated_preexisting_then_deleted_remains_visible(self, project):
        build_boot_manifest(str(project))
        install_write_guard(str(project))
        path = project / "src" / "main.py"
        path.write_text("def hello(): return 42\n", encoding="utf-8")
        path.unlink()
        path.write_text("def hello(): return 43\n", encoding="utf-8")
        path.unlink()

        reconcile_session_file_ledger(str(project))

        assert "src/main.py" in _SESSION_FILES_CHANGED

    def test_public_created_claim_cannot_hide_preexisting_deletion(self, project):
        path = project / "src" / "main.py"
        touched("src/main.py", created=True, root=str(project))
        path.unlink()

        reconcile_session_file_ledger(str(project))

        assert "src/main.py" in _SESSION_FILES_CHANGED

    def test_guarded_restore_does_not_hide_prior_out_of_band_drift(self, project):
        path = project / "src" / "main.py"
        build_boot_manifest(str(project))
        _ensure_boot_manifest_built()
        path.write_text("out of band content with a different size\n", encoding="utf-8")
        install_write_guard(str(project))
        drifted = path.read_text(encoding="utf-8")
        path.write_text("guarded intermediate\n", encoding="utf-8")
        path.write_text(drifted, encoding="utf-8")

        reconcile_session_file_ledger(str(project))

        assert "src/main.py" in _SESSION_FILES_CHANGED
        assert "src/main.py" in check_filesystem_drift(str(project))["modified"]

    def test_preseeded_backslash_baseline_reconciles_one_canonical_key(self, project):
        path = project / "src" / "main.py"
        original = path.read_text(encoding="utf-8")
        build_boot_manifest(str(project))
        touched(r"src\main.py", root=str(project))  # Mirrors safe() pre-seeding.
        install_write_guard(str(project))
        path.write_text("def hello(): return 42\n", encoding="utf-8")
        path.write_text(original, encoding="utf-8")

        result = reconcile_session_file_ledger(str(project))

        assert result["removed_restored"] == ["src/main.py"]
        assert r"src\main.py" not in _SESSION_FILES_CHANGED

    def test_backslash_created_registration_does_not_leave_alias_after_delete(self, project):
        build_boot_manifest(str(project))
        install_write_guard(str(project))
        (project / "nested").mkdir()
        path = project / "nested" / "new_alias.py"
        path.write_text("value = 1\n", encoding="utf-8")
        touched(r"nested\new_alias.py", created=True, root=str(project))
        path.unlink()

        reconcile_session_file_ledger(str(project))

        assert "nested/new_alias.py" not in _SESSION_FILES_CHANGED

    def test_created_file_diff_reports_full_file_as_additions(self, project):
        path = project / "new_module.py"
        path.write_text("first = 1\nsecond = 2\n", encoding="utf-8")
        touched("new_module.py", created=True, root=str(project))

        result = get_diff(root=str(project))
        file_diff = result["samples"]["per_file"]["new_module.py"]

        assert file_diff["status"] == "new"
        assert file_diff["additions"] > 0

    def test_guarded_created_file_reviews_as_full_addition(self, project):
        install_write_guard(str(project))
        path = project / "guarded_new.py"
        path.write_text("first = 1\nsecond = 2\n", encoding="utf-8")

        file_diff = get_diff(root=str(project))["samples"]["per_file"]["guarded_new.py"]

        assert file_diff["status"] == "new"
        assert file_diff["additions"] > 0


class TestWriteGuard:
    def test_reconfigures_without_stacking_process_wrappers(self, tmp_path):
        first_root = tmp_path / "first"
        second_root = tmp_path / "second"
        first_root.mkdir()
        second_root.mkdir()
        original_open = builtins.open
        original_write_text = Path.write_text

        install_write_guard(str(first_root))
        first_open = builtins.open
        install_write_guard(str(second_root))

        assert builtins.open is not first_open
        assert builtins.open._cw_original is original_open
        assert Path.write_text._cw_original is original_write_text
        assert str(second_root.resolve()) == _ss._GUARDED_ROOT

    def test_auto_registers_open_write(self, project):
        build_boot_manifest(str(project))
        install_write_guard(str(project))
        target = os.path.join(str(project), "src", "new_module.py")
        with open(target, "w") as f:
            f.write("# written via raw open\n")
        assert any("new_module.py" in p for p in _SESSION_FILES_CHANGED)

    def test_does_not_register_reads(self, project):
        build_boot_manifest(str(project))
        install_write_guard(str(project))
        _SESSION_FILES_CHANGED.clear()
        target = os.path.join(str(project), "src", "main.py")
        with open(target) as f:
            f.read()
        assert not any("main.py" in p for p in _SESSION_FILES_CHANGED)

    def test_does_not_register_odibi_anchor_generated_files(self, project):
        build_boot_manifest(str(project))
        install_write_guard(str(project))
        _SESSION_FILES_CHANGED.clear()

        (project / ".anchor_session_state.json").write_text("{}", encoding="utf-8")

        assert ".anchor_session_state.json" not in _SESSION_FILES_CHANGED
