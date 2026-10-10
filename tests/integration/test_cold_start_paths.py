"""Real snapshot/restore and host setup with fake Databricks transport, not live compute.

The harness's documented /Workspace FUSE seam means managed bootstrap uses its local
instruction root; Workspace guidance is exercised separately via public setup_host.
"""
from __future__ import annotations

import hashlib
import importlib
from pathlib import Path
from types import SimpleNamespace


def test_cold_restore_warm_receipt_and_new_compute(replay, databricks, monkeypatch):
    workspace = databricks.workspace
    workspace_root = "/Users/alex@example.invalid/cold-start"
    workspace.add_directory(workspace_root)
    original_list = workspace.list

    def metadata_list(path):
        # Supply actual SDK ObjectInfo fields missing from the older shared fake.
        for entry in original_list(path):
            data = workspace.files.get(entry.path, b"")
            yield SimpleNamespace(
                path=entry.path, object_type=entry.object_type,
                object_id=int(hashlib.sha256(entry.path.encode()).hexdigest()[:12], 16),
                size=len(data), modified_at=int(hashlib.sha256(data).hexdigest()[:12], 16),
            )

    monkeypatch.setattr(workspace, "list", metadata_list)

    def guidance():
        return importlib.import_module("odibi_anchor.host_setup").setup_host(
            f"/Workspace{workspace_root}", adapter="databricks",
            receipt_root=databricks.local_root / "guidance",
        )

    created = replay.create_project("alpha")
    artifacts = Path(created["startup_packet"]["artifact_root"])
    records = artifacts / "evidence"
    records.mkdir()
    for index in range(600):
        (records / f"{index}.txt").write_text(f"record-{index}")
    replay.snapshot(created)
    assert guidance()["verification"] == "content"
    replay.new_compute()

    restored = replay.bootstrap("alpha")
    packet = restored["startup_packet"]
    assert packet["restore"]["action"] == "created"
    assert packet["guidance"]["verification"] == "content"
    restored_records = Path(packet["artifact_root"]) / "evidence"
    assert {path.name: path.read_text() for path in restored_records.iterdir()} == {
        f"{index}.txt": f"record-{index}" for index in range(600)
    }
    databricks.calls.clear()
    assert guidance()["verification"] == "content"
    assert not any(method == "list" for _api, method, _path in databricks.calls)
    replay.new_process()
    databricks.calls.clear()
    assert guidance()["verification"] == "content"
    assert sum(method == "list" for _api, method, _path in databricks.calls) == 90
    replay.new_process()
    databricks.calls.clear()
    warm = guidance()
    assert warm["verification"] == "metadata_receipt"
    assert not any(api == "workspace" and method == "download"
                   for api, method, _path in databricks.calls)
    assert sum(method == "list" for _api, method, _path in databricks.calls) == 45
    assert not any("receipt" in path for path in workspace.files)
    replay.new_compute()
    databricks.calls.clear()
    assert guidance()["verification"] == "content"
    assert not any(method == "list" for _api, method, _path in databricks.calls)
