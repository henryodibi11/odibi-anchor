"""Narrow, read-only local measurements for readiness."""

from __future__ import annotations

import hashlib
import os
import stat
import struct
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import odibi_anchor

from ._canonical import dumps
from ._readiness_protocol import normalize_repository_id


@dataclass(frozen=True)
class LocalMeasurementInputs:
    project_root: Path
    plugin_path: Path
    routing: dict[str, Any]


@dataclass(frozen=True)
class LocalMeasurement:
    canonical_root: str
    plugin_digest: str
    anchor_version: str
    repository_evidence: dict[str, Any]


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    try:
        return subprocess.run(
            ["git", *args], cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            check=False, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("git evidence unavailable") from exc


def _git(root: Path, *args: str, check: bool = True) -> bytes:
    result = _run_git(root, *args)
    if check and result.returncode:
        raise ValueError("git evidence unavailable")
    return result.stdout


def _path(raw: bytes) -> str:
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("non-UTF-8 repository path") from exc
    if not value or value.startswith("/") or "\\" in value or any(
        not part or part in {".", ".."} for part in value.split("/")
    ) or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        raise ValueError("malformed repository path")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("repository path is not NFC")
    return value


def _frame(tag: str, value: bytes) -> bytes:
    encoded = tag.encode()
    return struct.pack(">I", len(encoded)) + encoded + struct.pack(">Q", len(value)) + value


def _one_git_line(raw: bytes, *, subject: str) -> bytes:
    if not raw.endswith(b"\n") or b"\n" in raw[:-1]:
        raise ValueError(f"malformed Git {subject} output")
    return raw[:-1]


def _git_evidence(root: Path, routing: dict[str, Any]) -> dict[str, Any]:
    configured = routing["repository_id"]
    try:
        remote_raw = _one_git_line(
            _git(root, "remote", "get-url", routing["remote_name"]), subject="remote URL"
        )
        remote = normalize_repository_id(remote_raw.decode())
    except UnicodeDecodeError as exc:
        raise ValueError("invalid remote URL") from exc
    if remote != configured:
        raise ValueError("repository identity mismatch")
    head_probe = _run_git(root, "rev-parse", "--verify", "HEAD^{commit}")
    if head_probe.returncode != 0:
        raise ValueError("Git readiness evidence requires a valid commit HEAD")
    head = _one_git_line(head_probe.stdout, subject="HEAD commit").decode("ascii")
    if len(head) not in {40, 64} or any(char not in "0123456789abcdef" for char in head):
        raise ValueError("malformed Git HEAD")
    records = _git(root, "ls-files", "--stage", "-z").split(b"\0")
    entries: list[dict[str, Any]] = []
    paths: set[str] = set()
    for record in records[:-1]:
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_id, stage_raw = metadata.decode("ascii").split(" ")
            stage = int(stage_raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("malformed Git index entry") from exc
        path = _path(raw_path)
        if (
            stage != 0
            or len(mode) != 6
            or any(c not in "01234567" for c in mode)
            or len(object_id) not in {40, 64}
            or any(c not in "0123456789abcdef" for c in object_id)
        ):
            raise ValueError("unmerged or malformed Git index")
        entries.append({"stage": stage, "mode": mode, "object_id": object_id, "path": path})
        paths.add(path)
    if records[-1] != b"":
        raise ValueError("unterminated Git index output")
    committed = _git(root, "ls-tree", "-r", "--name-only", "-z", "HEAD").split(b"\0")
    if committed[-1] != b"":
        raise ValueError("unterminated Git tree output")
    paths.update(_path(item) for item in committed[:-1])
    for args in (("ls-files", "--modified", "--deleted", "-z"), ("ls-files", "--others", "--exclude-standard", "-z")):
        output = _git(root, *args).split(b"\0")
        if output[-1] != b"":
            raise ValueError("unterminated Git path output")
        paths.update(_path(item) for item in output[:-1])
    entries.sort(key=lambda item: (item["path"].encode(), item["stage"]))
    ordered = sorted(paths, key=str.encode)
    content = bytearray()
    for relative in ordered:
        content += _frame("path", relative.encode())
        target = root / relative
        resolved = target.resolve(strict=False)
        if resolved != root and root not in resolved.parents:
            raise ValueError("repository path resolves outside the checkout")
        try:
            info = target.lstat()
        except FileNotFoundError:
            content += _frame("type", b"missing")
            continue
        content += _frame("type", struct.pack(">Q", stat.S_IFMT(info.st_mode)))
        content += _frame("mode", struct.pack(">I", info.st_mode & 0o7777))
        if stat.S_ISLNK(info.st_mode):
            content += _frame("symlink", os.readlink(os.fsencode(target)))
        elif stat.S_ISREG(info.st_mode):
            content += _frame("regular", target.read_bytes())
        elif stat.S_ISDIR(info.st_mode):
            content += _frame("directory", b"")
        else:
            content += _frame("special", b"")
    path_digest = hashlib.sha256("\0".join(ordered).encode()).digest()
    content_digest = hashlib.sha256(content).digest()
    worktree = hashlib.sha256(b"anchor-amp/worktree/v1\0" + path_digest + content_digest).hexdigest()
    branch_raw = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False) if head else b""
    return {"outcome": "available", "fingerprint": {
        "repository_id": remote, "canonical_root": os.fspath(root), "head_sha": head,
        "branch": branch_raw.decode().strip() or None,
        "index_fingerprint": hashlib.sha256(b"anchor-amp/index-entries/v1\0" + dumps(entries)).hexdigest(),
        "fingerprinted_paths": ordered, "worktree_fingerprint": worktree,
    }}


def measure_local(inputs: LocalMeasurementInputs) -> LocalMeasurement:
    root = inputs.project_root.resolve(strict=True)
    if inputs.plugin_path.is_symlink():
        raise ValueError("plugin path must not be a symlink")
    plugin = inputs.plugin_path.resolve(strict=True)
    if (
        not root.is_dir()
        or not inputs.plugin_path.is_absolute()
        or root not in plugin.parents
        or not stat.S_ISREG(plugin.stat().st_mode)
    ):
        raise ValueError("measurement paths must be explicit regular files under an absolute root")
    evidence = (
        _git_evidence(root, inputs.routing)
        if inputs.routing["kind"] == "git"
        else {"outcome": "unavailable", "reason": "non_git_read_only"}
    )
    anchor_version = odibi_anchor.__version__
    if anchor_version == "0+unknown":
        raise ValueError("Odibi Anchor version provenance is unavailable")
    return LocalMeasurement(
        os.fspath(root), hashlib.sha256(plugin.read_bytes()).hexdigest(),
        anchor_version, evidence,
    )
