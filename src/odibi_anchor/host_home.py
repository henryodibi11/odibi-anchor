"""Host installation layout and recoverable migration, independent of runtime state."""

from __future__ import annotations

import json
import os
import shlex
import time
import uuid
from pathlib import Path
from typing import Any

from odibi_anchor import host_setup as legacy
from odibi_anchor._recovery import attach_recovery

MARKER = "<!-- anchor-host-home: "
RECORD = "host.json"
MANIFEST = "guidance.json"
PENDING = "pending.json"
LAST = "last-migration.json"
# Temporary root-scoped locator: legacy launchers must discover a custom-home
# transaction even before the instruction pointer changes (or after rollback restores it).
TRANSACTION = ".anchor-host-migration.json"


def _absolute(value: str | os.PathLike[str]) -> Path:
    raw = os.fspath(value)
    if not raw or raw.startswith("~") or not Path(raw).is_absolute() or any(c in raw for c in "\n\r"):
        raise ValueError("host home must be an absolute, single-line, non-tilde path")
    path = Path(raw)
    if path != Path(os.path.normpath(raw)) or ".." in path.parts:
        raise ValueError("host paths must be normalized absolute paths")
    # Never follow a symlink into an unrelated owner's tree.
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise legacy.HostSetupError(f"host home path contains a symlink: {path}")
    return path


def _io(adapter: str):
    if adapter == "databricks":
        workspace, import_format = legacy._workspace_handles()

        def read(path: Path):
            if not path.as_posix().startswith("/Workspace/"):
                return _io("amp")[0](path)
            [value] = legacy._run_bounded(
                [(str(path), lambda: legacy._workspace_read(workspace, legacy._workspace_api_path(path)))],
                sub_phase="read",
            )
            return value

        def write(path: Path, content: bytes | None):
            if not path.as_posix().startswith("/Workspace/"):
                return _io("amp")[1](path, content)
            remote = legacy._workspace_api_path(path)

            def publish():
                if content is None:
                    legacy._workspace_delete(workspace, remote)
                else:
                    legacy._workspace_write(workspace, remote, content, import_format)

            try:
                legacy._run_bounded([(str(path), publish)], sub_phase="publish")
            except legacy.BootstrapPhaseTimeout as exc:
                attach_recovery(
                    exc,
                    error_code="bootstrap_phase_timeout",
                    context={**exc.context, "mutation_outcome": "unknown", "path": str(path)},
                    next_operations=[
                        {
                            "operation": "inspect_host_migration",
                            "requires_owner": True,
                            "retry_safety": "inspect_before_retry",
                            "reason": "Workspace mutation may still complete. Establish request completion before rollback or retry.",
                        }
                    ],
                )
                raise

        return read, write

    def read(path: Path):
        if not os.path.lexists(path):
            return None
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise legacy.HostSetupError(f"host path contains a symlink: {path}")
        return legacy._regular_bytes(path, "host file")

    def write(path: Path, content: bytes | None):
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

    return read, write


def discover_home(root: Path, adapter: str, selected=None) -> Path:
    read, _ = _io(adapter if root.as_posix().startswith("/Workspace/") else "amp")
    transaction = _transaction(root, read)
    pointer = read(root / ".assistant_instructions.md")
    recorded = None
    if pointer and pointer.startswith(MARKER.encode()):
        try:
            recorded = _absolute(json.loads(pointer.decode().splitlines()[0][len(MARKER) : -4]))
        except (ValueError, IndexError) as exc:
            raise legacy.HostSetupError("invalid host home instruction pointer") from exc
    explicit = selected if selected is not None else os.environ.get("ANCHOR_HOST_HOME")
    transaction_home = _absolute(transaction["home"]) if transaction else None
    home = _absolute(explicit if explicit is not None else recorded or transaction_home or root / "anchor")
    if transaction_home is not None and home != transaction_home:
        raise legacy.HostSetupError(f"pending host migration belongs to {transaction_home}, not {home}")
    if ".assistant" in home.parts:
        raise ValueError("host home must be outside .assistant")
    if recorded is not None and home != recorded:
        raise legacy.HostSetupError(f"host home conflict: pointer names {recorded}, requested {home}")
    if home == root or home in root.parents:
        raise ValueError("host home must not contain the instruction root")
    return home


def _transaction(root: Path, read):
    raw = read(root / TRANSACTION)
    if raw is None:
        return None
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "instruction_root", "home", "journal_sha256"}
        or value["version"] != 1
        or value["instruction_root"] != str(root)
        or not isinstance(value["journal_sha256"], str)
        or len(value["journal_sha256"]) != 64
        or legacy._canonical_json(value) != raw
    ):
        raise legacy.HostSetupError("invalid host migration locator; preserve it for recovery")
    _absolute(value["home"])
    return value


def host_launcher(root: Path, adapter: str) -> Path:
    """Return the installed canonical launcher, or the compatible legacy entry point."""
    home = discover_home(root, adapter)
    read, _ = _io(adapter if root.as_posix().startswith("/Workspace/") else "amp")
    if _transaction(root, read) is not None or read(home / PENDING) is not None:
        raise legacy.HostSetupError(f"host migration pending at {home}; recover before bootstrap")
    if read(home / RECORD) is not None:
        return home / "agent_bootstrap.py"
    # Bootstrap may verify an existing portfolio before guidance was installed.
    # Only explicit setup may relocate it; retain its legacy path on that first boot.
    return root / ".assistant/agent_bootstrap.py"


def rollback_home(root: Path, *, adapter: str, home=None) -> dict[str, Any]:
    root = _absolute(root)
    home = discover_home(root, adapter, home)
    read, write = _io(adapter if root.as_posix().startswith("/Workspace/") else "amp")
    locator = _transaction(root, read)
    raw = read(home / PENDING) or read(home / LAST)
    if locator and (raw is None or legacy._sha256(raw) != locator["journal_sha256"]):
        if read(home / PENDING) is not None:
            raise legacy.HostSetupError("host migration journal does not match its locator")
        # Locator was published, but its journal was not. No destination publication
        # is allowed before a verified journal; only the preparation needs cancelling.
        write(root / TRANSACTION, None)
        return {"status": "rolled_back", "host_home": str(home), "backup_path": None}
    if raw is None:
        raise legacy.HostSetupError(f"no pending host migration at {home}")
    journal = json.loads(raw)
    if journal["instruction_root"] != str(root) or journal["home"] != str(home):
        raise legacy.HostSetupError("host migration journal ownership mismatch")
    # Check every path before undoing any; preserve concurrent edits.
    restores = []
    for entry in journal["files"]:
        path = _absolute(entry["path"])
        if not (path.is_relative_to(root) or path.is_relative_to(home)):
            raise legacy.HostSetupError("migration journal path outside host ownership")
        if entry["backup"] and not _absolute(entry["backup"]).is_relative_to(home / "backups"):
            raise legacy.HostSetupError("migration journal backup outside host home")
        before = read(Path(entry["backup"])) if entry["backup"] else None
        if (None if before is None else legacy._sha256(before)) != entry["before"]:
            raise legacy.HostSetupError(f"migration backup damaged: {path}")
        current = read(path)
        digest = None if current is None else legacy._sha256(current)
        if digest not in (entry["before"], entry["after"]):
            raise legacy.HostSetupError(f"rollback conflict at {path}; preserve edits and backups")
        restores.append((path, before, current))
    # A crash during an explicit rollback must remain diagnosable on the next launch.
    write(
        root / TRANSACTION,
        legacy._canonical_json(
            {"version": 1, "instruction_root": str(root), "home": str(home), "journal_sha256": legacy._sha256(raw)}
        ),
    )
    write(home / PENDING, raw)
    for path, before, expected in reversed(restores):
        if read(path) != expected:
            raise legacy.HostSetupError(f"rollback conflict at {path}; preserve edits and backups")
        write(path, before)
        if read(path) != before:
            raise legacy.HostSetupError(f"rollback readback failed: {path}")
    write(home / LAST, None)
    write(home / PENDING, None)
    write(root / TRANSACTION, None)
    return {"status": "rolled_back", "host_home": str(home), "backup_path": journal["backup_path"]}


def _legacy_backups(root: Path, *, remote: bool):
    """Enumerate only receipt-proven backup files; leave unknown content untouched."""
    parent = root / legacy._BACKUPS
    if remote:
        workspace, _ = legacy._workspace_handles()
        try:
            [children] = legacy._run_bounded(
                [(str(parent), lambda: list(workspace.list(legacy._workspace_api_path(parent))))], sub_phase="read"
            )
        except Exception as exc:
            if legacy._workspace_missing(exc):
                return {}
            raise
        directories = [
            parent / Path(child.path).name
            for child in children
            if str(getattr(child.object_type, "value", child.object_type)) == "DIRECTORY"
        ]
    else:
        if not parent.exists():
            return {}
        if parent.is_symlink():
            raise legacy.HostSetupError("legacy backup directory is a symlink")
        directories = [path for path in parent.iterdir() if path.is_dir() and not path.is_symlink()]
    read, _ = _io("databricks" if remote else "amp")
    result = {}
    for directory in sorted(directories):
        receipt_bytes = read(directory / legacy._BACKUP_RECEIPT)
        if receipt_bytes is None:
            continue
        receipt = json.loads(receipt_bytes)
        if receipt.get("format") != "odibi-anchor-host-guidance-backup-v1" or receipt.get("target_root") != str(root):
            raise legacy.HostSetupError(f"legacy backup ownership mismatch: {directory}")
        claims = {entry["path"]: entry["actual_sha256"] for entry in receipt["files"]}
        if receipt.get("manifest_sha256") is not None:
            claims[legacy._MANIFEST] = receipt["manifest_sha256"]
        files = {directory / legacy._BACKUP_RECEIPT: receipt_bytes}
        for relative, digest in claims.items():
            path = directory / legacy._safe_relative(relative)
            data = read(path)
            if data is None or legacy._sha256(data) != digest:
                raise legacy.HostSetupError(f"legacy backup is damaged: {path}")
            files[path] = data
        result.update(files)
    return result


def setup_home(
    root: Path,
    *,
    adapter: str,
    home=None,
    portfolio_config=None,
    host_id=None,
    reconcile=False,
    dry_run=None,
    approve_replace_edited=False,
    receipt_root=None,
) -> dict[str, Any]:
    """Publish one canonical launcher; journal all changed bytes before publication.

    Runtime/project/durable roots in the portfolio are never rewritten or moved.
    Native skill and instruction entry points stay where their hosts discover them.
    """
    if adapter not in legacy._ADAPTER_FILES:
        raise ValueError("unknown host adapter")
    root = _absolute(root)
    home = discover_home(root, adapter, home)
    remote = adapter == "databricks" and root.as_posix().startswith("/Workspace/")
    if not remote:
        legacy._target_root(root)
    elif not home.as_posix().startswith("/Workspace/"):
        raise ValueError("Databricks host home must use durable /Workspace storage, not live compute")
    if remote:
        workspace, _ = legacy._workspace_handles()
        [status] = legacy._run_bounded(
            [(str(root), lambda: workspace.get_status(legacy._workspace_api_path(root)))], sub_phase="read"
        )
        if str(getattr(status.object_type, "value", status.object_type)) not in {"DIRECTORY", "REPO"}:
            raise legacy.HostSetupError("Databricks instruction root must be an existing directory")
    read, write = _io(adapter if remote else "amp")
    if _transaction(root, read) is not None or read(home / PENDING) is not None:
        error = legacy.HostSetupError(f"host migration pending at {home}; run setup-host --rollback --home {home}")
        error.error_code = "host_migration_pending"
        error.next_operations = [
            {
                "operation": "setup_host.rollback",
                "copy_ready": f"anchor setup-host {adapter} --target {root} --home {home} --rollback",
            }
        ]
        raise error
    old_manifest_bytes = read(root / legacy._MANIFEST)
    old_manifest = legacy._parse_manifest(old_manifest_bytes) if old_manifest_bytes else None
    current_bytes = read(home / MANIFEST)
    current = json.loads(current_bytes) if current_bytes else None
    if current and (current.get("instruction_root") != str(root) or current.get("adapter") != adapter):
        raise legacy.HostSetupError("host home manifest ownership mismatch")
    if current and (current.get("version") != 2 or not isinstance(current.get("files"), dict)):
        raise legacy.HostSetupError("unsupported host home manifest schema")
    previous = dict(current["files"]) if current else {}
    for name, digest in previous.items():
        path = _absolute(name)
        if (
            not (path.is_relative_to(root) or path.is_relative_to(home))
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise legacy.HostSetupError("invalid host home manifest path or digest")
    if old_manifest:
        if old_manifest["adapter"] != adapter:
            raise legacy.HostSetupError("legacy host adapter mismatch")
        previous.update({str(root / key): value for key, value in old_manifest["files"].items()})
    with legacy.phase("package_resources", layer="filesystem"):
        packaged = legacy._desired_files(adapter)
    launcher = packaged.pop(".assistant/agent_bootstrap.py")
    packaged.pop("agent_bootstrap.py", None)
    desired = {root / relative: value for relative, value in packaged.items()}
    desired[home / "agent_bootstrap.py"] = launcher
    desired[home / RECORD] = legacy._canonical_json({"version": 1, "instruction_root": str(root)})
    pointer = (
        MARKER + json.dumps(str(home)) + " -->\n"
        f"Read and follow `{home}/instructions.md` before substantive work.\n"
        f"Canonical bootstrap: `{home}/agent_bootstrap.py`.\n"
    ).encode()
    instructions = desired.pop(root / ".assistant_instructions.md")
    desired[home / "instructions.md"] = instructions.replace(
        b"<instruction-root>/.assistant/agent_bootstrap.py", str(home / "agent_bootstrap.py").encode()
    ).replace(b"](.assistant/", f"]({root}/.assistant/".encode())
    desired[root / ".assistant_instructions.md"] = pointer
    config_path = home / "anchor.toml"
    legacy_config = root / ".odibi-anchor" / "anchor.toml"
    config = read(config_path)
    old_config = read(legacy_config)
    old_binding = read(root / legacy._BINDING)
    canonical_binding = read(home / legacy._BINDING)
    bound = None
    binding_bytes = canonical_binding if config is not None else old_binding
    if binding_bytes is not None:
        bound = json.loads(binding_bytes)
        if (
            not isinstance(bound, dict)
            or set(bound) != {"version", "instruction_root", "host_id", "config_path"}
            or bound["version"] != 1
            or bound["instruction_root"] != str(root)
            or not isinstance(bound["host_id"], str)
            or not bound["host_id"]
            or legacy._canonical_json(bound) != binding_bytes
        ):
            raise legacy.HostSetupError("host binding ownership/schema mismatch")
        _absolute(bound["config_path"])
        if config is not None and bound["config_path"] != str(config_path):
            raise legacy.HostSetupError("canonical binding portfolio mismatch")
    if config is not None and old_config is not None and config != old_config:
        raise legacy.HostSetupError("host home and legacy portfolios conflict; owner must choose")
    if portfolio_config is not None:
        source_config = _absolute(portfolio_config)
        selected_config = read(source_config)
        if selected_config is None:
            raise legacy.HostSetupError(f"explicit portfolio does not exist: {source_config}")
    elif bound is not None:
        source_config = _absolute(bound["config_path"])
        if config is None and old_config is not None and source_config != legacy_config:
            raise legacy.HostSetupError(
                "legacy binding and default portfolio disagree; owner must select one explicitly"
            )
        selected_config = read(source_config)
        if selected_config is None:
            raise legacy.HostSetupError(f"bound portfolio does not exist: {source_config}")
        if host_id is None:
            host_id = bound["host_id"]
    else:
        source_config = config_path if config is not None else legacy_config
        selected_config = config if config is not None else old_config
    if selected_config is not None:
        if config is not None and config != selected_config:
            raise legacy.HostSetupError("selected portfolio conflicts with host home portfolio")
        desired[config_path] = selected_config
        # Validate against the original path before copying, without changing logical roots.
        binding = legacy._resolve_binding(
            root,
            adapter=adapter,
            portfolio_config=source_config,
            host_id=host_id,
        )
        binding["config_path"] = str(config_path)
        desired[home / legacy._BINDING] = legacy._canonical_json(binding)
    elif host_id is not None:
        raise ValueError("host_id requires a portfolio")
    obsolete = {
        root / ".assistant/agent_bootstrap.py",
        root / "agent_bootstrap.py",
        root / legacy._MANIFEST,
        root / legacy._BINDING,
    }
    obsolete.update(Path(name) for name in previous if Path(name) not in desired)
    if old_config is not None:
        obsolete.add(legacy_config)
        previous[str(legacy_config)] = legacy._sha256(old_config)
    if old_manifest_bytes:
        previous[str(root / legacy._MANIFEST)] = legacy._sha256(old_manifest_bytes)
    if old_binding is not None:
        previous[str(root / legacy._BINDING)] = legacy._sha256(old_binding)
    desired = dict(sorted(desired.items(), key=lambda item: str(item[0])))
    changes = {}
    classifications = []
    compatible = []
    for name in ("AGENTS.md", "CLAUDE.md"):
        path = root / name
        if path in desired and str(path) not in previous:
            content = read(path)
            if content is not None and b".assistant_instructions.md" in content:
                desired.pop(path)
                compatible.append(str(path))
    expected_manifest = legacy._canonical_json(
        {
            "version": 2,
            "instruction_root": str(root),
            "adapter": adapter,
            "files": {str(path): legacy._sha256(value) for path, value in desired.items()},
        }
    )
    receipt_path = legacy._guidance_receipt_path(receipt_root, root) if remote else None
    from odibi_anchor import __version__

    receipt_binding = {
        "layout": 2,
        "home": str(home),
        "package_version": __version__,
        "manifest_sha256": legacy._sha256(expected_manifest),
    }
    common = Path(os.path.commonpath([root, home]))
    metadata_contents = {str(path.relative_to(common)): data for path, data in desired.items()}
    metadata_contents[str((home / MANIFEST).relative_to(common))] = expected_manifest
    metadata_before = None
    workspace = legacy._workspace_handles()[0] if remote else None
    if receipt_path is not None and receipt_path.parent.is_dir():
        metadata_before = legacy._workspace_metadata(
            workspace, common, metadata_contents, legacy.workspace_timeout_policy()
        )
    receipt = legacy._read_guidance_receipt(receipt_path)
    cached = (
        metadata_before is not None
        and receipt is not None
        and receipt.get("binding") == receipt_binding
        and receipt.get("metadata") == metadata_before
        and current_bytes == expected_manifest
        and old_manifest is None
    )
    names = sorted(set(desired) | obsolete, key=str)
    with legacy.phase("read", layer="workspace_api" if remote else "filesystem"):
        if cached:
            existing = {path: desired[path] if path in desired else read(path) for path in names}
        elif remote:
            values = legacy._run_bounded(
                [(str(path), lambda path=path: read(path)) for path in names], sub_phase="read"
            )
            existing = dict(zip(names, values, strict=True))
        else:
            existing = {path: read(path) for path in names}
    verify_started = time.perf_counter()
    edits = []
    for path in names:
        before = existing[path]
        after = desired.get(path)
        relative = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
        entry = legacy._classify(
            relative, before, {relative: after} if after is not None else {}, previous.get(str(path))
        )
        entry["path"] = str(path)
        classifications.append(entry)
        if before == after:
            continue
        digest = legacy._sha256(before) if before is not None else None
        owned = digest is not None and (
            previous.get(str(path)) == digest or legacy._released_versions(relative, digest) is not None
        )
        # A copied legacy portfolio is explicitly selected, not an arbitrary collision.
        owned = owned or path == legacy_config or (path == config_path and before == selected_config)
        if before is not None and not owned:
            edits.append(str(path))
            if not reconcile or (dry_run is False and not approve_replace_edited):
                raise legacy.HostSetupError(
                    f"host migration conflict at {path}; preserve or explicitly reconcile edited bytes"
                )
        changes[path] = (before, after)
    manifest = legacy._canonical_json(
        {
            "version": 2,
            "instruction_root": str(root),
            "adapter": adapter,
            "files": {str(path): legacy._sha256(value) for path, value in desired.items()},
        }
    )
    if current_bytes != manifest:
        changes[home / MANIFEST] = (current_bytes, manifest)
    legacy_backups = _legacy_backups(root, remote=remote) if current is None or old_manifest is not None else {}
    for source, data in legacy_backups.items():
        destination = home / "backups" / "legacy" / source.relative_to(root / legacy._BACKUPS)
        existing = read(destination)
        if existing is not None and existing != data:
            raise legacy.HostSetupError(f"legacy backup destination conflict: {destination}")
        if existing is None:
            changes[destination] = (None, data)
        changes[source] = (data, None)
    legacy.record_phase("verify", elapsed_ms=legacy.elapsed_ms(verify_started), outcome="ok")
    report = {
        "kind": "host_guidance_setup",
        "adapter": adapter,
        "resource_profile": "databricks_workspace_compact" if adapter == "databricks" else "complete",
        "omitted_packaged_prefixes": list(legacy._ADAPTER_OMITTED_PREFIXES.get(adapter, ())),
        "status": (
            "planned"
            if reconcile and dry_run is not False and changes
            else "upgraded"
            if changes and (current or old_manifest)
            else "installed"
            if changes
            else "unchanged"
        ),
        "host_home": str(home),
        "target_root": str(root),
        "launcher": str(home / "agent_bootstrap.py"),
        "manifest_path": str(home / MANIFEST),
        "managed_files": [{"path": str(path), "sha256": legacy._sha256(data)} for path, data in desired.items()],
        "verified_file_count": len(desired),
        "verification": "metadata_receipt" if cached else "content",
        "changed_files": [str(p) for p in changes],
        "files": classifications,
        "classification_counts": {
            name: sum(e["classification"] == name for e in classifications) for name in legacy._CLASSIFICATIONS
        },
        "approval_required": bool(edits),
        "compatible_unmanaged_files": compatible,
        "verified_skill_count": sum(path.name == "SKILL.md" and ".assistant" in path.parts for path in desired),
        "next_operation": (
            {
                "operation": "setup_host.reconcile",
                "requires_owner": bool(edits),
                "copy_ready": f"anchor setup-host {adapter} --target {shlex.quote(str(root))} "
                f"--home {shlex.quote(str(home))} --reconcile --apply" + (" --approve-replace-edited" if edits else ""),
            }
            if changes
            else None
        ),
    }
    if reconcile and dry_run is not False:
        return report
    if not changes:
        legacy.record_phase("publish", outcome="not_required")
        if remote and not cached:
            if metadata_before is None:
                # A cold compute needs only a marker, not a metadata scan. Seed a
                # receipt on a later boot after observing both sides of the read.
                legacy._write_guidance_receipt(receipt_path, receipt_binding, None)
            else:
                metadata_after = legacy._workspace_metadata(
                    workspace, common, metadata_contents, legacy.workspace_timeout_policy()
                )
                if metadata_after == metadata_before:
                    legacy._write_guidance_receipt(receipt_path, receipt_binding, metadata_after)
        return report
    backup = home / "backups" / uuid.uuid4().hex
    entries = []
    for index, (path, (before, after)) in enumerate(changes.items()):
        saved = backup / str(index) if before is not None else None
        if saved is not None:
            write(saved, before)
            if read(saved) != before:
                raise legacy.HostSetupError(f"backup readback failed: {saved}")
        entries.append(
            {
                "path": str(path),
                "backup": str(saved) if saved else None,
                "before": legacy._sha256(before) if before is not None else None,
                "after": legacy._sha256(after) if after is not None else None,
            }
        )
    journal = legacy._canonical_json(
        {"instruction_root": str(root), "home": str(home), "backup_path": str(backup), "files": entries}
    )
    locator = legacy._canonical_json(
        {"version": 1, "instruction_root": str(root), "home": str(home), "journal_sha256": legacy._sha256(journal)}
    )
    write(root / TRANSACTION, locator)
    if read(root / TRANSACTION) != locator:
        raise legacy.HostSetupError("migration locator readback failed; no publication attempted")
    write(home / PENDING, journal)
    if read(home / PENDING) != journal:
        raise legacy.HostSetupError("migration journal readback failed; no publication attempted")
    try:
        for path, (before, after) in changes.items():
            if read(path) != before:
                raise legacy.HostSetupError(f"host changed during migration: {path}")
            write(path, after)
            if read(path) != after:
                raise legacy.HostSetupError(f"host publication readback failed: {path}")
    except legacy.BootstrapPhaseTimeout:
        # The SDK operation may still complete. Do not race it with rollback writes.
        raise
    except Exception:
        rollback_home(root, adapter=adapter, home=home)
        raise
    write(backup / "receipt.json", journal)
    write(home / LAST, journal)
    write(home / PENDING, None)
    write(root / TRANSACTION, None)
    legacy.record_phase("publish", outcome="ok")
    if remote:
        legacy._write_guidance_receipt(receipt_path, receipt_binding, None)
    report["backup_path"] = str(backup)
    return report
