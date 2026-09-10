"""Qualification for the passive external-attestor host probe."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from odibi_anchor._governance_host_probe import __main__ as probe_cli
from odibi_anchor._governance_host_probe._capabilities import (
    _MAX_SYSTEM_FILE_BYTES,
    _collect_host_capabilities,
    _read_bounded,
)


def _host_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    proc = tmp_path / "proc"
    cgroup = tmp_path / "cgroup"
    current = cgroup / "system.slice" / "amp.service"
    (proc / "self").mkdir(parents=True)
    current.mkdir(parents=True)
    (proc / "self" / "mountinfo").write_text(
        f"29 23 0:26 / {cgroup} rw - cgroup2 cgroup rw\n", encoding="utf-8"
    )
    (proc / "self" / "cgroup").write_text("0::/system.slice/amp.service\n", encoding="utf-8")
    (current / "cgroup.controllers").write_text("memory cpu pids\n", encoding="utf-8")
    (current / "cgroup.subtree_control").write_text("", encoding="utf-8")
    (current / "cgroup.kill").write_text("", encoding="utf-8")
    return proc, cgroup, current


def test_passive_probe_reports_delegation_and_external_authority_blockers(tmp_path: Path) -> None:
    proc, cgroup, current = _host_fixture(tmp_path)

    def access(path: Path, mode: int) -> bool:
        assert mode == os.W_OK
        return path == current

    report = _collect_host_capabilities(
        proc_root=proc, cgroup_root=cgroup, system="Linux", access=access,
        pidfd_open=lambda _pid, _flags: os.open(os.devnull, os.O_RDONLY),
    )
    assert report["document_type"] == "governance_host_capability_probe"
    assert report["outcome"] == "unsupported"
    assert report["authorizes_readiness"] is False
    assert report["probe_mode"] == "passive_read_only"
    assert report["cgroup"] == {
        "version": 2,
        "current_relative_path": "/system.slice/amp.service",
        "controllers": ["cpu", "memory", "pids"],
        "subtree_control_writable": False,
        "current_cgroup_writable": True,
        "cgroup_kill_present": True,
        "active_child_creation_qualified": False,
    }
    assert report["process_identity"] == {
        "pidfd_open_available": True,
        "clone3_into_cgroup_qualified": False,
    }
    assert "delegated_cgroup_subtree_unavailable" in report["blockers"]
    assert "trusted_amp_inventory_unavailable" in report["blockers"]
    assert "gateway_revocation_unavailable" in report["blockers"]


def test_non_linux_and_malformed_unified_cgroup_fail_closed(tmp_path: Path) -> None:
    non_linux = _collect_host_capabilities(
        proc_root=tmp_path / "missing-proc", cgroup_root=tmp_path / "missing-cgroup",
        system="Darwin", access=os.access, pidfd_open=None,
    )
    assert non_linux["outcome"] == "unsupported"
    assert {"linux_required", "pidfd_open_unavailable"} <= set(non_linux["blockers"])

    proc, cgroup, _ = _host_fixture(tmp_path)
    (proc / "self" / "cgroup").write_text("1:name:/legacy\n", encoding="utf-8")
    malformed = _collect_host_capabilities(
        proc_root=proc, cgroup_root=cgroup, system="Linux", access=os.access, pidfd_open=None,
    )
    assert "current_cgroup_unavailable" in malformed["blockers"]
    assert malformed["cgroup"]["current_relative_path"] is None


def test_system_file_read_is_bounded_and_subtree_mount_is_not_misclassified(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized"
    oversized.write_bytes(b"x" * (_MAX_SYSTEM_FILE_BYTES + 1))
    try:
        _read_bounded(oversized)
    except ValueError as exc:
        assert "exceeds bound" in str(exc)
    else:
        raise AssertionError("oversized host file was accepted")

    proc, cgroup, _ = _host_fixture(tmp_path / "subtree")
    (proc / "self" / "mountinfo").write_text(
        f"29 23 0:26 /delegated {cgroup} rw - cgroup2 cgroup rw\n", encoding="utf-8"
    )
    report = _collect_host_capabilities(
        proc_root=proc, cgroup_root=cgroup, system="Linux", access=os.access, pidfd_open=None,
    )
    assert "cgroup_v2_unavailable" in report["blockers"]


def test_cli_emits_one_strict_non_authorizing_json_object() -> None:
    source = Path(__file__).resolve().parents[2] / "src"
    result = subprocess.run(
        [sys.executable, "-m", "odibi_anchor._governance_host_probe"],
        env={**os.environ, "PYTHONPATH": os.fspath(source)},
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 2
    assert result.stderr == ""
    assert result.stdout.endswith("\n") and result.stdout.count("\n") == 1
    report = json.loads(result.stdout)
    assert report["document_type"] == "governance_host_capability_probe"
    assert report["schema_version"] == 1
    assert report["outcome"] == "unsupported"
    assert report["authorizes_readiness"] is False
    assert "clone3_into_cgroup_unqualified" in report["blockers"]


def test_cli_collection_failure_emits_fixed_non_authorizing_report(
    monkeypatch, capsys,
) -> None:
    def fail_collection() -> dict:
        raise ValueError("synthetic collection failure")

    monkeypatch.setattr(probe_cli, "probe_host_capabilities", fail_collection)
    assert probe_cli.main() == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.endswith("\n") and captured.out.count("\n") == 1
    report = json.loads(captured.out)
    assert report["document_type"] == "governance_host_capability_probe"
    assert report["schema_version"] == 1
    assert report["outcome"] == "unsupported"
    assert report["authorizes_readiness"] is False
    assert report["platform"] == {"system": "unavailable"}
    assert "host_capability_collection_failed" in report["blockers"]
