import errno
import json
from pathlib import Path

import pytest

from odibi_anchor.operational import persist_artifact
from odibi_anchor.operational import _artifacts


def test_artifact_is_contained_canonical_redacted_and_leaves_no_temp(tmp_path):
    artifact = persist_artifact({"safe": 1, "token": "do-not-write"}, tmp_path)
    assert artifact.path.parent == tmp_path / ".odibi-anchor/evidence/incidents"
    document = json.loads(artifact.path.read_text(encoding="utf-8"))
    assert "do-not-write" not in artifact.path.read_text(encoding="utf-8")
    assert document["payload_sha256"] == artifact.sha256
    assert not list(artifact.path.parent.glob("*.tmp"))


def test_artifact_rejects_symlink_parent(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    managed = tmp_path / ".odibi-anchor"
    try:
        managed.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not permitted")
    with pytest.raises(ValueError, match=r"link|reparse"):
        persist_artifact({"safe": True}, tmp_path)


def test_table_subject_cannot_control_artifact_path(tmp_path):
    artifact = persist_artifact({"safe": True}, tmp_path, kind="tables", subject="../../secret")
    artifact.path.resolve().relative_to(Path(tmp_path).resolve())


@pytest.mark.parametrize("unsupported_errno", [errno.ENOSYS, errno.EPERM])
def test_artifact_falls_back_when_hard_links_are_unsupported(
    tmp_path, monkeypatch, unsupported_errno,
):
    def unsupported_link(*_args, **_kwargs):
        raise OSError(unsupported_errno, "hard links are unsupported")

    monkeypatch.setattr(_artifacts.os, "link", unsupported_link)

    artifact = persist_artifact({"safe": True}, tmp_path)

    assert artifact.path.is_file()
    assert not list(artifact.path.parent.glob("*.tmp"))


def test_artifact_propagates_unexpected_hard_link_error(tmp_path, monkeypatch):
    def failed_link(*_args, **_kwargs):
        raise OSError(errno.EIO, "storage failure")

    monkeypatch.setattr(_artifacts.os, "link", failed_link)

    with pytest.raises(OSError, match="storage failure"):
        persist_artifact({"safe": True}, tmp_path)

    directory = tmp_path / ".odibi-anchor/evidence/incidents"
    assert not list(directory.glob("*.tmp"))


def test_artifact_propagates_containment_revalidation_error(tmp_path, monkeypatch):
    real_safe_directory = _artifacts._safe_directory
    calls = 0

    def failed_revalidation(root, relative):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.EPERM, "containment revalidation denied")
        return real_safe_directory(root, relative)

    monkeypatch.setattr(_artifacts, "_USE_DIRECTORY_FD", False)
    monkeypatch.setattr(_artifacts, "_safe_directory", failed_revalidation)

    with pytest.raises(OSError, match="containment revalidation denied"):
        persist_artifact({"safe": True}, tmp_path)

    directory = tmp_path / ".odibi-anchor/evidence/incidents"
    assert not list(directory.glob("*.json"))
    assert not list(directory.glob("*.tmp"))


@pytest.mark.parametrize("unsupported_errno", [errno.ENOSYS, errno.EPERM])
def test_artifact_fallback_preserves_dangling_destination_symlink(
    tmp_path, monkeypatch, unsupported_errno,
):
    directory = tmp_path / ".odibi-anchor/evidence/incidents"
    directory.mkdir(parents=True)
    target = directory / "INC-20260807T000000Z-aaaaaaaaaaaa.json"
    try:
        target.symlink_to("missing.json")
    except OSError:
        pytest.skip("symlink creation is not permitted")
    tokens = iter(["a" * 12, "b" * 16])

    def unsupported_link(*_args, **_kwargs):
        raise OSError(unsupported_errno, "hard links are unsupported")

    monkeypatch.setattr(_artifacts, "utc_now", lambda: "2026-08-07T00:00:00Z")
    monkeypatch.setattr(_artifacts.secrets, "token_hex", lambda _size: next(tokens))
    monkeypatch.setattr(_artifacts.os, "link", unsupported_link)

    with pytest.raises(FileExistsError, match="artifact already exists"):
        persist_artifact({"safe": True}, tmp_path)

    assert target.is_symlink()
    assert target.readlink() == Path("missing.json")
    assert not list(directory.glob("*.tmp"))
