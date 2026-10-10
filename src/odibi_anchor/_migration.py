"""Journaled, hash-verified managed-project target migration (``portfolio move-target``).

One migration moves a project's host target from ``from_target`` to ``to_target`` across
the authorities that bind it: the portfolio ``projects.<id>.targets.<host>`` entry, the
managed ``PROJECT.md`` route descriptor and the continuity owner epoch
(``continuity/v1``). ``artifact_root`` never changes and historical records are never
rewritten.

Every run is recorded in ``<artifact_root>/migrations/<migration-id>.json``. The journal
is created exclusively (hard link), then advanced only by compare-and-swap replacement
with an append-only ``history``. Before any route change the journal holds create-only
backups of the original portfolio and descriptor bytes. Each step is idempotent: the
store's current hash equal to the expected post-step hash means done, equal to the
pre-step hash means apply, and anything else stops with a recovery packet. Steps:

1. ``pre_snapshot``: a durable snapshot when a durable root is configured.
2. ``descriptor``: guarded route update; ``continuity``: rename ``continuity/v1`` to
   ``continuity/archive/<migration-id>`` so the next launch starts a new owner epoch.
3. Journal state ``portfolio_pending``, then ``pending_snapshot``.
4. ``portfolio``: compare-and-swap write of the host target.
5. ``receipt`` (create-only ``<migration-id>.receipt.json``), then ``post_snapshot``.

Rollback reverses steps 4 to 2 from the backups with expected hashes; it is refused
once a receipt exists. An epoch archived on another compute stays archived, because its
owner names that compute's home. Local state follows the same single-writer rule as
restore: the hash checks are optimistic concurrency, not a lock.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shlex
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from odibi_anchor._recovery import attach_recovery

FORMAT = "odibi-anchor-target-migration-v1"
RECEIPT_FORMAT = "odibi-anchor-target-migration-receipt-v1"
MIGRATIONS_DIRECTORY = "migrations"
STEPS = (
    "pre_snapshot", "descriptor", "continuity", "pending_snapshot", "portfolio", "receipt",
    "post_snapshot", "rollback_snapshot",
)
NON_TERMINAL_STATES = frozenset({"planned", "portfolio_pending", "portfolio_written", "rolling_back"})
TERMINAL_STATES = frozenset({"receipt_written", "completed", "rolled_back"})
_ID_PATTERN = re.compile(r"tm_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}\Z")
_MAX_BATCH = 50


class TargetMigrationError(RuntimeError):
    """A target migration was refused or stopped; see ``error_code`` and ``context``."""


@dataclass(frozen=True)
class _Context:
    config_path: Path
    host_id: str
    project_id: str
    adapter: str
    anchor_home: Path
    database: Path
    artifact_root: Path
    configured_state_root: str
    durable_root: str | None
    authority_id: str
    retention_days: int | None
    minimum_snapshots: int | None

    @property
    def databricks(self) -> bool:
        return self.adapter == "databricks"


# ── small primitives ─────────────────────────────────────────────────────────


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str | None:
    try:
        return _sha256(path.read_bytes())
    except FileNotFoundError:
        return None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _same_target(first: str, second: str) -> bool:
    from odibi_anchor._dispatcher._project import route_path_identity

    return route_path_identity(first) == route_path_identity(second)


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    with suppress(OSError):
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _create_only(path: Path, data: bytes) -> bool:
    """Publish complete bytes exclusively; return False when the path already exists."""
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
    _fsync_directory(path.parent)
    return True


# ── recovery packets ─────────────────────────────────────────────────────────


def _cli_operation(intent: Mapping[str, Any], mode: str | None, reason: str) -> dict[str, Any]:
    arguments = {
        "config_path": intent["config_path"], "host_id": intent["host_id"],
        "project_id": intent["project_id"], "from_target": intent["from_target"],
        "to_target": intent["to_target"],
    }
    flags = [
        "--config", arguments["config_path"], "--host", arguments["host_id"],
        "--project", arguments["project_id"], "--from", arguments["from_target"],
        "--to", arguments["to_target"],
    ]
    if mode is not None:
        arguments[mode] = True
        flags.append(f"--{mode.replace('_', '-')}")
    return {
        "operation": "portfolio.move_target",
        "arguments": arguments,
        "copy_ready": shlex.join(["anchor", "portfolio", "move-target", *flags]),
        "python": "odibi_anchor.move_target(**arguments)",
        "reason": reason,
        "requires_owner": True,
        "retry_safety": "every step re-verifies expected hashes and refuses on mismatch",
    }


def _recovery_operations(intent: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        _cli_operation(intent, "resume", "finish the journaled migration from its recorded step"),
        _cli_operation(intent, "rollback", "reverse the journaled migration to its recorded pre-state"),
    ]


def _blocked(
    classification: str, message: str, context: Mapping[str, Any],
    next_operations: Sequence[Mapping[str, Any]] = (),
) -> TargetMigrationError:
    return attach_recovery(
        TargetMigrationError(f"target migration refused (target_migration_blocked, {classification}): {message}"),
        error_code="target_migration_blocked",
        context={"classification": classification, **context},
        next_operations=next_operations,
    )


def _incomplete(journal: _Journal, message: str, **context: Any) -> TargetMigrationError:
    return attach_recovery(
        TargetMigrationError(
            f"target migration {journal.data['migration_id']} is incomplete "
            f"(target_migration_incomplete, state {journal.data['state']}): {message}. "
            "Resume or roll it back; do not edit the portfolio, PROJECT.md or continuity files manually."
        ),
        error_code="target_migration_incomplete",
        context={
            "classification": "target_migration_incomplete",
            "migration_id": journal.data["migration_id"],
            "journal_path": str(journal.path),
            "state": journal.data["state"],
            "steps": copy.deepcopy(journal.data["steps"]),
            **{key: journal.data["intent"][key] for key in ("project_id", "host_id", "from_target", "to_target", "config_path", "artifact_root")},
            **context,
        },
        next_operations=_recovery_operations(journal.data["intent"]),
    )


# ── journal ──────────────────────────────────────────────────────────────────


class _Journal:
    """One migration journal advanced only by compare-and-swap replacement."""

    def __init__(self, path: Path, data: dict[str, Any], sha256: str) -> None:
        self.path = path
        self.data = data
        self.sha256 = sha256

    @classmethod
    def load(cls, path: Path) -> _Journal:
        raw = path.read_bytes()
        try:
            data = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"migration journal is not JSON: {path}") from exc
        if (
            not isinstance(data, dict) or data.get("format") != FORMAT
            or data.get("migration_id") != path.name.removesuffix(".json")
            or raw != _canonical(data).encode("utf-8")
            or data.get("state") not in NON_TERMINAL_STATES | TERMINAL_STATES
        ):
            raise ValueError(f"migration journal is malformed or not canonical: {path}")
        return cls(path, data, _sha256(raw))

    @classmethod
    def create(cls, path: Path, data: dict[str, Any]) -> _Journal:
        payload = _canonical(data).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        if not _create_only(path, payload):
            raise FileExistsError(f"migration journal already exists: {path}")
        return cls(path, data, _sha256(payload))

    def update(self, *, state: str | None = None, steps: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        from odibi_anchor._dispatcher._descriptor import write_descriptor_atomic

        data = copy.deepcopy(self.data)
        if state is not None:
            data["state"] = state
        for name, value in (steps or {}).items():
            data["steps"][name] = dict(value)
        data["generation"] += 1
        data["history"].append({
            "generation": data["generation"], "state": data["state"],
            "steps": sorted(steps or {}), "at": _now(),
        })
        try:
            self.sha256 = write_descriptor_atomic(self.path, _canonical(data), expected_sha256=self.sha256)
        except FileExistsError as exc:
            raise _blocked(
                "concurrent_change", f"migration journal {self.path} changed during the migration",
                {"journal_path": str(self.path), "expected_sha256": self.sha256,
                 "actual_sha256": _file_sha256(self.path)},
            ) from exc
        self.data = data

    def step(self, name: str) -> dict[str, Any]:
        return self.data["steps"][name]

    def relative(self, value: str) -> Path:
        # The current artifact root, which a restore on another compute may have moved.
        return self.path.parent.parent / value


def _migrations_root(artifact_root: str | Path) -> Path:
    return Path(artifact_root) / MIGRATIONS_DIRECTORY


def _journal_paths(artifact_root: str | Path) -> list[Path]:
    root = _migrations_root(artifact_root)
    try:
        candidates = list(root.glob("tm_*.json")) if root.is_dir() else []
    except OSError:
        # Unreadable journals can never be matched; callers that act on them re-read and refuse.
        return []
    return sorted(
        path for path in candidates
        if not path.name.endswith(".receipt.json") and _ID_PATTERN.fullmatch(path.name.removesuffix(".json"))
    )


def _project_journals(ctx: _Context) -> list[_Journal]:
    journals = []
    for path in _journal_paths(ctx.artifact_root):
        try:
            journal = _Journal.load(path)
        except (OSError, ValueError) as exc:
            raise _blocked(
                "state_mismatch", f"migration journal {path} cannot be verified ({exc})",
                {"project_id": ctx.project_id, "journal_path": str(path),
                 "artifact_root": str(ctx.artifact_root), "owner_decision_required": True},
            ) from exc
        if journal.data["intent"]["project_id"] == ctx.project_id:
            journals.append(journal)
    return journals


def pending_migration(
    artifact_root: str | Path, project_id: str, first: str, second: str,
) -> dict[str, Any] | None:
    """Return the non-terminal journal whose from/to pair equals ``{first, second}``.

    Read-only and fail-soft: an unreadable or malformed journal never matches, so the
    caller falls back to its ordinary classification.
    """
    for path in _journal_paths(artifact_root):
        try:
            journal = _Journal.load(path)
            intent = journal.data["intent"]
            if journal.data["state"] not in NON_TERMINAL_STATES or intent["project_id"] != project_id:
                continue
            pair = (intent["from_target"], intent["to_target"])
            if not (
                (_same_target(pair[0], first) and _same_target(pair[1], second))
                or (_same_target(pair[0], second) and _same_target(pair[1], first))
            ):
                continue
        except (OSError, ValueError, KeyError, TypeError):
            continue
        return {
            "migration_id": journal.data["migration_id"],
            "journal_path": str(path),
            "state": journal.data["state"],
            "from_target": intent["from_target"],
            "to_target": intent["to_target"],
            "host_id": intent["host_id"],
            "config_path": intent["config_path"],
            "next_operations": _recovery_operations(intent),
        }
    return None


def migration_receipt_for(
    artifact_root: str | Path, project_id: str, prior_target: str, current_target: str | None = None,
) -> dict[str, Any] | None:
    """Return the receipt that moved ``project_id`` off ``prior_target``.

    With ``current_target``, the receipt chain starting there must reach the current
    target (at most 20 hops); otherwise nothing is reported.
    """
    root = _migrations_root(artifact_root)
    if not root.is_dir():
        return None
    receipts = []
    for path in sorted(root.glob("tm_*.receipt.json")):
        try:
            receipt = json.loads(path.read_bytes())
            if receipt.get("format") == RECEIPT_FORMAT and receipt.get("project_id") == project_id:
                receipts.append({"migration_id": receipt["migration_id"], "receipt_path": str(path),
                                 "from_target": receipt["from_target"], "to_target": receipt["to_target"],
                                 "completed_at": str(receipt.get("completed_at", ""))})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    receipts.sort(key=lambda item: (item["completed_at"], item["migration_id"]))
    first = None
    cursor = prior_target
    used: set[str] = set()
    for _hop in range(20):
        step = next((item for item in receipts if item["migration_id"] not in used
                     and _same_target(item["from_target"], cursor)), None)
        if step is None:
            return None
        used.add(step["migration_id"])
        # Name the most recent move off the prior target on the path to the current one.
        first = step if first is None or _same_target(step["from_target"], prior_target) else first
        if current_target is None or _same_target(step["to_target"], current_target):
            return first
        cursor = step["to_target"]
    return None


# ── context and observation ──────────────────────────────────────────────────


def _context(config_path: str | os.PathLike[str], host_id: str, project_id: str) -> tuple[_Context, dict[str, Any]]:
    from odibi_anchor._dispatcher._project import _managed_project_root
    from odibi_anchor.portfolio import load_portfolio_document, resolve_project
    from odibi_anchor.startup import _runtime_environment

    document = load_portfolio_document(config_path)
    portfolio = document["portfolio"]
    target = portfolio.get("projects", {}).get(project_id, {}).get("targets", {}).get(host_id)
    if host_id not in portfolio.get("hosts", {}) or target is None:
        raise _blocked(
            "state_mismatch",
            f"portfolio {document['path']} has no target for project {project_id!r} on host {host_id!r}",
            {"project_id": project_id, "host_id": host_id, "config_path": document["path"],
             "portfolio_sha256": document["sha256"]},
        )
    resolved = resolve_project(portfolio, host_id=host_id, project_id=project_id)
    host = portfolio["hosts"][host_id]
    environment, _local = _runtime_environment(resolved["environment"], adapter=host["adapter"])
    home = Path(environment["ANCHOR_HOME"]).resolve()
    retention = portfolio.get("durability", {}).get("retention")
    ctx = _Context(
        config_path=Path(document["path"]),
        host_id=host_id,
        project_id=project_id,
        adapter=host["adapter"],
        anchor_home=home,
        database=Path(environment["ANCHOR_MEMORY_DB"]),
        artifact_root=_managed_project_root(home, project_id),
        configured_state_root=host["local_state_root"],
        durable_root=environment.get("ANCHOR_DURABLE_ROOT"),
        authority_id=environment["ANCHOR_AUTHORITY_ID"],
        retention_days=retention["days"] if retention else None,
        minimum_snapshots=retention["minimum_snapshots"] if retention else None,
    )
    return ctx, document


def _base_context(ctx: _Context, document: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "project_id": ctx.project_id, "host_id": ctx.host_id, "config_path": str(ctx.config_path),
        "artifact_root": str(ctx.artifact_root), "anchor_home": str(ctx.anchor_home),
        "portfolio_sha256": document["sha256"],
    }


def _continuity_paths(artifact_root: Path, migration_id: str | None = None) -> tuple[Path, Path | None]:
    base = artifact_root / "continuity"
    return base / "v1", (base / "archive" / migration_id) if migration_id else None


def _observe(ctx: _Context, document: Mapping[str, Any]) -> dict[str, Any]:
    """Read every store the migration touches; never writes."""
    from odibi_anchor._dispatcher._descriptor import descriptor_damaged_error, read_descriptor
    from odibi_anchor._dispatcher._project import _descriptor_target

    base = _base_context(ctx, document)
    if not ctx.database.is_file() or not (ctx.artifact_root / "PROJECT.md").is_file():
        raise _blocked(
            "state_mismatch",
            f"local managed state for {ctx.project_id!r} is absent at {ctx.anchor_home}; prepare the "
            "runtime first (it restores the latest durable snapshot when one exists)",
            {**base, "database": str(ctx.database), "database_present": ctx.database.is_file(),
             "descriptor_present": (ctx.artifact_root / "PROJECT.md").is_file()},
            [{
                "operation": "portfolio.prepare",
                "arguments": {"config_path": str(ctx.config_path), "host_id": ctx.host_id, "project_id": ctx.project_id},
                "copy_ready": shlex.join(["anchor", "portfolio", "prepare", "--config", str(ctx.config_path),
                                          "--host", ctx.host_id, "--project", ctx.project_id]),
                "reason": "restore or register the exact local runtime before migrating",
                "requires_owner": False,
                "retry_safety": "idempotent; never retargets",
            }],
        )
    integrity = read_descriptor(ctx.artifact_root)
    if not integrity.intact:
        damage = descriptor_damaged_error(integrity, project_id=ctx.project_id, artifact_root=str(ctx.artifact_root))
        raise _blocked(
            "state_mismatch",
            f"the managed descriptor is damaged ({integrity.status}: {integrity.detail}); move-target never "
            "rewrites a damaged descriptor. Repair it with the supported descriptor repair operation first",
            {**base, "descriptor_sha256": integrity.sha256, "descriptor_damage": dict(damage.context),  # type: ignore[attr-defined]
             "owner_decision_required": True},
            getattr(damage, "next_operations", ()),
        )
    v1, _archive = _continuity_paths(ctx.artifact_root)
    owner = _read_owner(v1 / "OWNER.json")
    portfolio_target = document["portfolio"]["projects"][ctx.project_id]["targets"][ctx.host_id]
    return {
        "portfolio_sha256": document["sha256"],
        "portfolio_target": portfolio_target,
        "descriptor_sha256": integrity.sha256,
        "descriptor_target": _descriptor_target(ctx.artifact_root, integrity),
        "descriptor_integrity": integrity,
        "owner_sha256": _file_sha256(v1 / "OWNER.json"),
        "owner": owner,
        "owner_target": owner.get("target_root") if owner else None,
        "continuity_present": v1.exists(),
    }


def _read_owner(path: Path) -> dict[str, Any] | None:
    """Return a continuity owner sentinel's fields, or None when absent or unparseable."""
    try:
        owner = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    return owner if isinstance(owner, dict) else None


def _owner_sha_for(journal: _Journal, ctx: _Context) -> str | None:
    """Expected OWNER.json hash on this compute.

    Restoring a snapshot on another compute rewrites ``continuity/v1/OWNER.json`` to the
    new home (``_relocate_restored_continuity``), so the expected bytes are the recorded
    owner relocated to the current home, in the same canonical encoding.
    """
    pre = journal.data["pre"]
    owner = pre.get("owner")
    if owner is None or owner.get("anchor_home") == str(ctx.anchor_home):
        return pre["owner_sha256"]
    relocated = {**owner, "anchor_home": str(ctx.anchor_home), "artifact_root": str(ctx.artifact_root)}
    return _sha256(_canonical(relocated).encode("utf-8"))


def _absolute(value: str, label: str) -> str:
    from odibi_anchor.portfolio import _absolute as absolute

    return absolute(value, label)


def _destination_problems(ctx: _Context, document: Mapping[str, Any], to_target: str) -> list[str]:
    from odibi_anchor._dispatcher._project import _list_projects, resolve_active_project, route_path_identity

    problems = []
    destination = Path(to_target)
    if destination.is_symlink():
        problems.append("destination is a symlink")
    elif not destination.exists():
        problems.append("destination does not exist")
    elif not destination.is_dir():
        problems.append("destination is not a directory")
    elif os.path.normcase(str(destination.resolve())) != os.path.normcase(to_target):
        problems.append("destination path contains a symlink component or is not canonical")
    identity = route_path_identity(to_target)
    for project_id, project in sorted(document["portfolio"].get("projects", {}).items()):
        if project_id == ctx.project_id:
            continue
        for host_id, target in sorted(project.get("targets", {}).items()):
            if route_path_identity(target) == identity:
                problems.append(f"destination is the portfolio target of project {project_id!r} on host {host_id!r}")
    for item in _list_projects(ctx.anchor_home):
        if item["id"] == ctx.project_id or item["integrity_status"] != "intact":
            continue
        with suppress(ValueError, FileNotFoundError):
            other = resolve_active_project(ctx.anchor_home, item["id"])
            if other is not None and route_path_identity(other["target_root"]) == identity:
                problems.append(f"destination is the managed descriptor target of project {item['id']!r}")
    resolved = Path(os.path.normcase(str(destination.resolve())))
    forbidden = {
        "ANCHOR_HOME": str(ctx.anchor_home),
        "configured local_state_root": ctx.configured_state_root,
        "artifact_root": str(ctx.artifact_root),
    }
    if ctx.durable_root:
        forbidden["durable_root"] = ctx.durable_root
    for label, root in forbidden.items():
        normalized = Path(os.path.normcase(str(Path(root).resolve())))
        if resolved == normalized or normalized in resolved.parents:
            problems.append(f"destination is inside the {label} {root}")
        elif resolved in normalized.parents:
            problems.append(f"destination contains the {label} {root}")
    return problems


def _quiescence(ctx: _Context) -> dict[str, list[Any]]:
    """Open accepted task windows and non-terminal workflows bound to the project."""
    from odibi_anchor.codebase._task_authority import _terminal_task_ids

    result: dict[str, list[Any]] = {"open_task_windows": [], "non_terminal_workflows": [], "unverifiable": []}
    try:
        connection = sqlite3.connect(ctx.database.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as exc:
        result["unverifiable"].append({"database": str(ctx.database), "error_type": type(exc).__name__})
        return result
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "accepted_task_records" in tables:
            closed = (
                " AND NOT EXISTS (SELECT 1 FROM accepted_task_events e WHERE "
                "e.task_window_id=r.task_window_id AND e.event_type='closed')"
                if "accepted_task_events" in tables else ""
            )
            candidates = [
                row[0] for row in connection.execute(
                    f"SELECT task_window_id FROM accepted_task_records r WHERE project_id=?{closed} "
                    "ORDER BY task_window_id", (ctx.project_id,),
                )
            ]
            if candidates:
                terminal = _terminal_task_ids(ctx.database)
                result["open_task_windows"] = [item for item in candidates if item not in terminal]
        if "workflow_events" in tables:
            latest: dict[str, str] = {}
            for workflow_id, event_json in connection.execute(
                "SELECT workflow_id, event_json FROM workflow_events WHERE "
                "json_extract(event_json,'$.state.owner.project_id')=? ORDER BY workflow_id, generation",
                (ctx.project_id,),
            ):
                latest[workflow_id] = json.loads(event_json)["state"]["status"]
            result["non_terminal_workflows"] = sorted(
                workflow_id for workflow_id, status in latest.items() if status not in {"completed", "cancelled"}
            )
    except (sqlite3.Error, ValueError, KeyError, TypeError, RuntimeError) as exc:
        result["unverifiable"].append({"database": str(ctx.database), "error_type": type(exc).__name__})
    finally:
        connection.close()
    return result


def _require_quiescent(
    ctx: _Context, document: Mapping[str, Any], extra: Mapping[str, Any] | None = None,
    next_operations: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    quiescence = _quiescence(ctx)
    if any(quiescence.values()):
        blocking = [*quiescence["open_task_windows"], *quiescence["non_terminal_workflows"]]
        raise _blocked(
            "not_quiescent",
            "records bound to the project are still open: "
            + (", ".join(blocking) if blocking else "task and workflow authority could not be verified")
            + ". Close or cancel them first; a migration never orphans live authority",
            {**_base_context(ctx, document), **quiescence, **(extra or {})},
            next_operations,
        )
    return quiescence


def _expected_descriptor(integrity: Any, ctx: _Context, to_target: str) -> str:
    from odibi_anchor._dispatcher._descriptor import render_route_update, require_route_roundtrip

    updates = {"target_root": to_target}
    if integrity.fields.get("project_type") != "referenced":
        updates["project_type"] = "referenced"
    text = render_route_update(integrity, updates)
    require_route_roundtrip(text, path=integrity.path, project_id=ctx.project_id, target_root=to_target)
    return text


def _expected_portfolio(portfolio: Mapping[str, Any], ctx: _Context, to_target: str) -> dict[str, Any]:
    from odibi_anchor.portfolio import validate_portfolio

    updated = copy.deepcopy(dict(portfolio))
    updated["projects"][ctx.project_id]["targets"][ctx.host_id] = to_target
    validate_portfolio(updated)
    return updated


def _portfolio_bytes(portfolio: Mapping[str, Any]) -> bytes:
    from odibi_anchor.portfolio import _render

    return _render(dict(portfolio))


# ── preflight ────────────────────────────────────────────────────────────────


def _preflight(
    ctx: _Context, document: Mapping[str, Any], from_target: str, to_target: str,
) -> dict[str, Any]:
    """Read-only classification of the start state with every hash; raises when blocked."""
    observed = _observe(ctx, document)
    base = _base_context(ctx, document)
    hashes = {key: observed[key] for key in ("portfolio_sha256", "descriptor_sha256", "owner_sha256")}
    targets = {
        "portfolio_target": observed["portfolio_target"],
        "descriptor_target": observed["descriptor_target"],
        "owner_target": observed["owner_target"],
    }
    portfolio_from = _same_target(observed["portfolio_target"], from_target)
    portfolio_to = _same_target(observed["portfolio_target"], to_target)
    descriptor_from = _same_target(observed["descriptor_target"], from_target)
    descriptor_to = _same_target(observed["descriptor_target"], to_target)
    owner_ok_after = observed["owner_target"] is None or _same_target(observed["owner_target"], to_target)
    report = {
        "kind": "target_migration_preflight", **base, "from_target": from_target, "to_target": to_target,
        "hashes": hashes, "targets": targets, "continuity_present": observed["continuity_present"],
        "owner": observed["owner"], "durable_root": ctx.durable_root,
    }
    if portfolio_to and descriptor_to and owner_ok_after:
        receipt = migration_receipt_for(ctx.artifact_root, ctx.project_id, from_target)
        return {**report, "start_state": "already_migrated", "receipt": receipt}
    if portfolio_from and descriptor_from:
        start_state = "aligned"
    elif portfolio_to and descriptor_from:
        # The #30 incident: the portfolio was moved by hand; only local authorities follow it.
        start_state = "portfolio_already_moved"
    else:
        raise _blocked(
            "state_mismatch",
            f"--from {from_target!r} and --to {to_target!r} do not describe the observed route "
            f"(portfolio {observed['portfolio_target']!r}, descriptor {observed['descriptor_target']!r}"
            + (f", continuity owner {observed['owner_target']!r}" if observed["owner_target"] else "") + ")",
            {**base, "from_target": from_target, "to_target": to_target, "hashes": hashes, **targets,
             "owner_decision_required": True},
        )
    problems = _destination_problems(ctx, document, to_target)
    if problems:
        raise _blocked(
            "destination_unsuitable", "; ".join(problems),
            {**base, "from_target": from_target, "to_target": to_target, "problems": problems, "hashes": hashes},
        )
    quiescence = _require_quiescent(ctx, document, {"from_target": from_target, "to_target": to_target, "hashes": hashes})
    descriptor_text = _expected_descriptor(observed["descriptor_integrity"], ctx, to_target)
    expected_portfolio_sha = (
        document["sha256"] if start_state == "portfolio_already_moved"
        else _sha256(_portfolio_bytes(_expected_portfolio(document["portfolio"], ctx, to_target)))
    )
    return {
        **report,
        "start_state": start_state,
        "expected": {
            "portfolio_sha256": expected_portfolio_sha,
            "descriptor_sha256": _sha256(descriptor_text.encode("utf-8")),
        },
        "quiescence": quiescence,
        "planned_steps": [
            "pre_snapshot" if ctx.durable_root else "pre_snapshot (not configured)",
            "descriptor",
            "continuity" if observed["continuity_present"] else "continuity (not present)",
            "pending_snapshot" if ctx.durable_root else "pending_snapshot (not configured)",
            "portfolio" if start_state == "aligned" else "portfolio (already applied)",
            "receipt",
            "post_snapshot" if ctx.durable_root else "post_snapshot (not configured)",
        ],
    }


# ── steps ────────────────────────────────────────────────────────────────────


def _snapshot(ctx: _Context) -> dict[str, Any]:
    if ctx.durable_root is None:
        return {"status": "not_applicable", "reason": "no durable root is configured"}
    from odibi_anchor.durability import snapshot_state

    result = snapshot_state(
        source_db=ctx.database,
        source_artifacts=ctx.anchor_home / "workspace" / "projects",
        durable_root=ctx.durable_root,
        authority_id=ctx.authority_id,
        databricks=ctx.databricks,
        retention_days=ctx.retention_days,
        minimum_snapshots=ctx.minimum_snapshots,
    )
    return {"status": "done", "action": result["action"], "snapshot_id": result["manifest"]["snapshot_id"],
            "sha256": result["manifest"]["sha256"]}


def _mismatch(journal: _Journal, store: str, expected: Mapping[str, Any], actual: Any) -> TargetMigrationError:
    return _blocked(
        "concurrent_change" if store in {"portfolio", "descriptor"} else "state_mismatch",
        f"{store} matches neither its recorded pre-state nor its expected post-state; stopping "
        f"migration {journal.data['migration_id']} without guessing",
        {"migration_id": journal.data["migration_id"], "journal_path": str(journal.path),
         "state": journal.data["state"], "store": store, "expected": dict(expected), "actual": actual,
         **{key: journal.data["intent"][key] for key in ("project_id", "host_id", "from_target", "to_target", "config_path", "artifact_root")},
         "owner_decision_required": True},
        _recovery_operations(journal.data["intent"])[::-1],
    )


def _ensure_backup(journal: _Journal, name: str, source: Path, pre_sha256: str) -> bytes:
    """Return verified pre-migration bytes, publishing the create-only backup if needed."""
    backup = journal.relative(journal.data["backups"][name])
    if backup.is_file():
        data = backup.read_bytes()
        if _sha256(data) != pre_sha256:
            raise _mismatch(journal, f"{name} backup", {"sha256": pre_sha256}, {"sha256": _sha256(data)})
        return data
    current = source.read_bytes()
    if _sha256(current) != pre_sha256:
        raise _mismatch(journal, name, {"pre_sha256": pre_sha256, "backup": "absent"}, {"sha256": _sha256(current)})
    backup.parent.mkdir(parents=True, exist_ok=True)
    _create_only(backup, current)
    if _file_sha256(backup) != pre_sha256:
        raise _mismatch(journal, f"{name} backup", {"sha256": pre_sha256}, {"sha256": _file_sha256(backup)})
    return current


def _descriptor_after(ctx: _Context, journal: _Journal, pre_bytes: bytes) -> str:
    from odibi_anchor._dispatcher._descriptor import parse_descriptor_text

    integrity = parse_descriptor_text(
        pre_bytes.decode("utf-8"), path=str(ctx.artifact_root / "PROJECT.md"),
        sha256=_sha256(pre_bytes), expected_id=ctx.project_id,
    )
    text = _expected_descriptor(integrity, ctx, journal.data["intent"]["to_target"])
    if _sha256(text.encode("utf-8")) != journal.data["expected"]["descriptor_sha256"]:
        raise _mismatch(journal, "descriptor", journal.data["expected"], {"rendered_sha256": _sha256(text.encode("utf-8"))})
    return text


def _local_order(journal: _Journal) -> tuple[str, str]:
    """Order the two local steps so every split state fails route resolution.

    An aligned start updates the descriptor first: until the portfolio write, the
    portfolio still names ``from`` and bootstrap stops at ``migration_pending``. When the
    portfolio already names ``to``, a descriptor written first would let a launch reach
    the old continuity owner, so continuity is archived first instead.
    """
    if journal.data["intent"]["start_state"] == "portfolio_already_moved":
        return ("continuity", "descriptor")
    return ("descriptor", "continuity")


def _owner_names(path: Path, ctx: _Context, target: str) -> bool:
    """Whether a continuity owner sentinel is bound to ``target`` in this artifact root."""
    owner = _read_owner(path)
    return bool(
        owner and isinstance(owner.get("target_root"), str) and _same_target(owner["target_root"], target)
        and owner.get("artifact_root") == str(ctx.artifact_root)
    )


def _apply_descriptor(ctx: _Context, journal: _Journal, *, last: bool) -> None:
    from odibi_anchor._dispatcher._descriptor import write_descriptor_atomic

    path = ctx.artifact_root / "PROJECT.md"
    pre, after = journal.data["pre"]["descriptor_sha256"], journal.data["expected"]["descriptor_sha256"]
    current = _file_sha256(path)
    if current == pre:
        pre_bytes = journal.relative(journal.data["backups"]["descriptor"]).read_bytes()
        try:
            write_descriptor_atomic(path, _descriptor_after(ctx, journal, pre_bytes), expected_sha256=pre)
        except FileExistsError as exc:
            raise _mismatch(journal, "descriptor", {"pre_sha256": pre, "after_sha256": after}, {"sha256": _file_sha256(path)}) from exc
        current = _file_sha256(path)
    if current != after:
        raise _mismatch(journal, "descriptor", {"pre_sha256": pre, "after_sha256": after}, {"sha256": current})
    journal.update(state="portfolio_pending" if last else None,
                   steps={"descriptor": {"status": "done", "sha256": after}})


def _apply_continuity(ctx: _Context, journal: _Journal, *, last: bool) -> None:
    pre = journal.data["pre"]
    to_target = journal.data["intent"]["to_target"]
    v1, archive = _continuity_paths(ctx.artifact_root, journal.data["migration_id"])
    assert archive is not None
    state = "portfolio_pending" if last else None
    owner_sha = _owner_sha_for(journal, ctx)
    expected = {"owner_sha256": owner_sha, "continuity_present": pre["continuity_present"],
                "archive": str(archive)}
    # A launch on the finished route may already have started the new epoch in v1.
    new_epoch = v1.exists() and _owner_names(v1 / "OWNER.json", ctx, to_target)
    if not pre["continuity_present"]:
        if v1.exists() and not new_epoch:
            raise _mismatch(journal, "continuity", expected, {"v1_present": True})
        journal.update(state=state, steps={"continuity": {"status": "not_applicable", "new_epoch_started": new_epoch}})
        return
    if v1.exists() and not new_epoch and not archive.exists():
        if _file_sha256(v1 / "OWNER.json") != owner_sha:
            raise _mismatch(journal, "continuity", expected, {"owner_sha256": _file_sha256(v1 / "OWNER.json")})
        archive.parent.mkdir(parents=True, exist_ok=True)
        os.rename(v1, archive)
        _fsync_directory(archive.parent)
        new_epoch = False
    if (v1.exists() and not new_epoch) or not archive.is_dir() or _file_sha256(archive / "OWNER.json") != owner_sha:
        raise _mismatch(journal, "continuity", expected, {
            "v1_present": v1.exists(), "archive_present": archive.exists(),
            "archived_owner_sha256": _file_sha256(archive / "OWNER.json"),
        })
    journal.update(state=state, steps={"continuity": {
        "status": "done", "archive": str(archive.relative_to(ctx.artifact_root)), "owner_sha256": owner_sha,
        "anchor_home": str(ctx.anchor_home), "new_epoch_started": new_epoch,
    }})


def _apply_portfolio(ctx: _Context, journal: _Journal) -> None:
    from odibi_anchor.portfolio import load_portfolio_document, write_portfolio

    if journal.step("portfolio")["status"] == "already_applied":
        document = load_portfolio_document(ctx.config_path)
        target = document["portfolio"]["projects"].get(ctx.project_id, {}).get("targets", {}).get(ctx.host_id)
        if target is None or not _same_target(target, journal.data["intent"]["to_target"]):
            raise _mismatch(journal, "portfolio", {"target": journal.data["intent"]["to_target"]}, {"target": target, "sha256": document["sha256"]})
        journal.update(state="portfolio_written", steps={"portfolio": {"status": "already_applied", "sha256": document["sha256"]}})
        return
    pre, after = journal.data["pre"]["portfolio_sha256"], journal.data["expected"]["portfolio_sha256"]
    current = _file_sha256(ctx.config_path)
    if current == pre:
        document = load_portfolio_document(ctx.config_path)
        updated = _expected_portfolio(document["portfolio"], ctx, journal.data["intent"]["to_target"])
        if _sha256(_portfolio_bytes(updated)) != after:
            raise _mismatch(journal, "portfolio", {"after_sha256": after}, {"rendered_sha256": _sha256(_portfolio_bytes(updated))})
        try:
            write_portfolio(ctx.config_path, updated, expected_sha256=pre)
        except ValueError as exc:
            if "base digest changed" not in str(exc):
                raise
            raise _mismatch(journal, "portfolio", {"pre_sha256": pre, "after_sha256": after}, {"sha256": _file_sha256(ctx.config_path)}) from exc
        current = _file_sha256(ctx.config_path)
    if current != after:
        raise _mismatch(journal, "portfolio", {"pre_sha256": pre, "after_sha256": after}, {"sha256": current})
    journal.update(state="portfolio_written", steps={"portfolio": {"status": "done", "sha256": after}})


def _receipt_path(journal: _Journal) -> Path:
    return journal.path.with_name(f"{journal.data['migration_id']}.receipt.json")


def _apply_receipt(ctx: _Context, journal: _Journal) -> None:
    path = _receipt_path(journal)
    data = journal.data
    receipt = {
        "format": RECEIPT_FORMAT,
        "migration_id": data["migration_id"],
        **{key: data["intent"][key] for key in ("project_id", "host_id", "config_path", "from_target", "to_target", "artifact_root", "start_state")},
        "pre": data["pre"],
        "post": {"portfolio_sha256": data["steps"]["portfolio"]["sha256"],
                 "descriptor_sha256": data["expected"]["descriptor_sha256"]},
        "continuity": data["steps"]["continuity"],
        "snapshots": {name: data["steps"][name] for name in ("pre_snapshot", "pending_snapshot")},
        "completed_at": _now(),
    }
    if not _create_only(path, _canonical(receipt).encode("utf-8")):
        try:
            existing = json.loads(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise _mismatch(journal, "receipt", {"migration_id": data["migration_id"]}, {"readable": False}) from exc
        stable = {key: value for key, value in receipt.items() if key != "completed_at"}
        if {key: existing.get(key) for key in stable} != stable:
            raise _mismatch(journal, "receipt", {"migration_id": data["migration_id"]}, {"receipt_path": str(path)})
    journal.update(state="receipt_written", steps={"receipt": {"status": "done", "path": str(path.relative_to(ctx.artifact_root)),
                                                              "sha256": _file_sha256(path)}})


def _route_mutations_remaining(ctx: _Context, journal: _Journal) -> bool:
    data = journal.data
    v1, archive = _continuity_paths(ctx.artifact_root, data["migration_id"])
    assert archive is not None
    descriptor_pending = _file_sha256(ctx.artifact_root / "PROJECT.md") != data["expected"]["descriptor_sha256"]
    archived = archive.is_dir() and (not v1.exists() or _owner_names(v1 / "OWNER.json", ctx, data["intent"]["to_target"]))
    continuity_pending = data["pre"]["continuity_present"] and not archived
    portfolio_pending = (
        data["steps"]["portfolio"]["status"] != "already_applied"
        and _file_sha256(ctx.config_path) != data["expected"]["portfolio_sha256"]
    )
    return descriptor_pending or continuity_pending or portfolio_pending


def _route_mutations_applied(ctx: _Context, journal: _Journal) -> bool:
    data = journal.data
    _v1, archive = _continuity_paths(ctx.artifact_root, data["migration_id"])
    assert archive is not None
    return (
        _file_sha256(ctx.artifact_root / "PROJECT.md") != data["pre"]["descriptor_sha256"]
        or archive.exists()
        or (data["steps"]["portfolio"]["status"] != "already_applied"
            and _file_sha256(ctx.config_path) != data["pre"]["portfolio_sha256"])
    )


def _journal_context(ctx: _Context, journal: _Journal) -> dict[str, Any]:
    return {"migration_id": journal.data["migration_id"], "journal_path": str(journal.path),
            "state": journal.data["state"]}


def _run_forward(ctx: _Context, journal: _Journal) -> None:
    _ensure_backup(journal, "portfolio", ctx.config_path, journal.data["pre"]["portfolio_sha256"])
    _ensure_backup(journal, "descriptor", ctx.artifact_root / "PROJECT.md", journal.data["pre"]["descriptor_sha256"])
    if journal.step("pre_snapshot")["status"] == "pending":
        journal.update(steps={"pre_snapshot": _snapshot(ctx)})
    order = _local_order(journal)
    if any(journal.step(name)["status"] == "pending" for name in order) and _route_mutations_remaining(ctx, journal):
        # Re-check right before the first route change; the snapshot may have taken a while.
        _require_quiescent(ctx, {"sha256": _file_sha256(ctx.config_path)}, _journal_context(ctx, journal),
                           _recovery_operations(journal.data["intent"])[::-1])
    for name in order:
        if journal.step(name)["status"] == "pending":
            apply = _apply_descriptor if name == "descriptor" else _apply_continuity
            apply(ctx, journal, last=name == order[-1])
    if journal.step("pending_snapshot")["status"] == "pending":
        journal.update(steps={"pending_snapshot": _snapshot(ctx)})
    if journal.data["state"] == "portfolio_pending":
        _apply_portfolio(ctx, journal)
    if journal.step("receipt")["status"] == "pending":
        _apply_receipt(ctx, journal)
    if journal.step("post_snapshot")["status"] == "pending":
        journal.update(state="completed", steps={"post_snapshot": _snapshot(ctx)})


def _refuse_rollback(ctx: _Context, journal: _Journal, reason: str, **context: Any) -> TargetMigrationError:
    intent = journal.data["intent"]
    return _blocked(
        "state_mismatch",
        f"migration {journal.data['migration_id']} cannot be rolled back: {reason}. Nothing was changed; "
        "finish it with --resume, then move back with a new move-target if needed",
        {**_journal_context(ctx, journal), **{key: intent[key] for key in ("project_id", "host_id", "from_target", "to_target", "config_path")},
         "artifact_root": str(ctx.artifact_root), **context},
        [_recovery_operations(intent)[0],
         _cli_operation({**intent, "from_target": intent["to_target"], "to_target": intent["from_target"]},
                        "dry_run", "preview the reverse move after completion")],
    )


def _rollback_actions(ctx: _Context, journal: _Journal) -> dict[str, str | None]:
    """Decide every compensation before any write; raise when one cannot be made exactly."""
    from odibi_anchor.portfolio import load_portfolio_document

    data = journal.data
    intent = data["intent"]
    receipt = _receipt_path(journal)
    if receipt.exists():
        raise _refuse_rollback(ctx, journal, "its receipt already exists", receipt_path=str(receipt))
    actions: dict[str, str | None] = {"portfolio": None, "continuity": None, "descriptor": None}
    if data["steps"]["portfolio"]["status"] != "already_applied":
        pre, after = data["pre"]["portfolio_sha256"], data["expected"]["portfolio_sha256"]
        current = _file_sha256(ctx.config_path)
        if current == after and pre != after:
            if _file_sha256(journal.relative(data["backups"]["portfolio"])) != pre:
                raise _mismatch(journal, "portfolio backup", {"sha256": pre}, {"sha256": _file_sha256(journal.relative(data["backups"]["portfolio"]))})
            actions["portfolio"] = "restore"
        elif current != pre:
            # A foreign write after a refused compare-and-swap: safe only if it still names --from.
            document = load_portfolio_document(ctx.config_path)
            target = document["portfolio"]["projects"].get(ctx.project_id, {}).get("targets", {}).get(ctx.host_id)
            if data["steps"]["portfolio"]["status"] == "done" or target is None or not _same_target(target, intent["from_target"]):
                raise _mismatch(journal, "portfolio", {"pre_sha256": pre, "after_sha256": after}, {"sha256": current, "target": target})
    v1, archive = _continuity_paths(ctx.artifact_root, data["migration_id"])
    assert archive is not None
    observed = {"v1_present": v1.exists(), "archive_present": archive.exists(),
                "owner_sha256": _file_sha256(v1 / "OWNER.json"),
                "archived_owner_sha256": _file_sha256(archive / "OWNER.json")}
    if v1.exists() and _owner_names(v1 / "OWNER.json", ctx, intent["to_target"]):
        raise _refuse_rollback(ctx, journal, "the project was already launched on the new target, which started a "
                               "new continuity epoch that a rollback would orphan", continuity=observed)
    if data["pre"]["continuity_present"]:
        owner_sha = _owner_sha_for(journal, ctx)
        archived = data["steps"]["continuity"]
        if archive.exists() and not v1.exists():
            if observed["archived_owner_sha256"] == owner_sha:
                actions["continuity"] = "rename_back"
            elif (
                archived.get("status") == "done" and archived.get("anchor_home") != str(ctx.anchor_home)
                and observed["archived_owner_sha256"] == archived.get("owner_sha256")
            ):
                # Archived on another compute, so its owner names that compute's home and
                # would not bind here. Keep it preserved; the next launch starts a new epoch.
                actions["continuity"] = "leave_archived"
            else:
                raise _mismatch(journal, "continuity", {"owner_sha256": owner_sha}, observed)
        elif archive.exists() or not v1.exists() or observed["owner_sha256"] != owner_sha:
            raise _mismatch(journal, "continuity", {"owner_sha256": owner_sha}, observed)
    elif v1.exists():
        raise _mismatch(journal, "continuity", {"continuity_present": False}, observed)
    pre, after = data["pre"]["descriptor_sha256"], data["expected"]["descriptor_sha256"]
    current = _file_sha256(ctx.artifact_root / "PROJECT.md")
    if current == after and pre != after:
        if _file_sha256(journal.relative(data["backups"]["descriptor"])) != pre:
            raise _mismatch(journal, "descriptor backup", {"sha256": pre}, {"sha256": _file_sha256(journal.relative(data["backups"]["descriptor"]))})
        actions["descriptor"] = "restore"
    elif current != pre:
        raise _mismatch(journal, "descriptor", {"pre_sha256": pre, "after_sha256": after}, {"sha256": current})
    return actions


def _run_rollback(ctx: _Context, journal: _Journal) -> None:
    from odibi_anchor._dispatcher._descriptor import write_descriptor_atomic
    from odibi_anchor.portfolio import _atomic_write

    actions = _rollback_actions(ctx, journal)
    journal.update(state="rolling_back")
    data = journal.data
    if actions["portfolio"] == "restore":
        try:
            _atomic_write(ctx.config_path, journal.relative(data["backups"]["portfolio"]).read_bytes(),
                          data["expected"]["portfolio_sha256"])
        except ValueError as exc:
            raise _mismatch(journal, "portfolio", {"after_sha256": data["expected"]["portfolio_sha256"]}, {"sha256": _file_sha256(ctx.config_path)}) from exc
    v1, archive = _continuity_paths(ctx.artifact_root, data["migration_id"])
    assert archive is not None
    # Reverse the forward order so a crash here still leaves a route that fails resolution.
    for name in reversed(_local_order(journal)):
        if name == "continuity" and actions["continuity"] == "rename_back":
            os.rename(archive, v1)
            _fsync_directory(v1.parent)
        elif name == "descriptor" and actions["descriptor"] == "restore":
            path = ctx.artifact_root / "PROJECT.md"
            try:
                write_descriptor_atomic(path, journal.relative(data["backups"]["descriptor"]).read_bytes().decode("utf-8"),
                                        expected_sha256=data["expected"]["descriptor_sha256"])
            except FileExistsError as exc:
                raise _mismatch(journal, "descriptor", {"after_sha256": data["expected"]["descriptor_sha256"]}, {"sha256": _file_sha256(path)}) from exc
    journal.update(state="rolled_back", steps={
        **{name: {**data["steps"][name], "status": "reverted"}
           for name in ("descriptor", "continuity", "portfolio") if data["steps"][name]["status"] == "done"},
        **({"continuity": {**data["steps"]["continuity"], "status": "left_archived"}}
           if actions["continuity"] == "leave_archived" else {}),
        "rollback_snapshot": {"status": "pending"},
    })
    _finish_rollback_snapshot(ctx, journal)


def _finish_rollback_snapshot(ctx: _Context, journal: _Journal) -> None:
    """Publish the rolled-back state so a fresh compute never restores the pending one."""
    if journal.step("rollback_snapshot")["status"] == "pending":
        journal.update(steps={"rollback_snapshot": _snapshot(ctx)})


# ── public API ───────────────────────────────────────────────────────────────


def _result(ctx: _Context, journal: _Journal, status: str) -> dict[str, Any]:
    data = journal.data
    receipt = _receipt_path(journal)
    return {
        "kind": "target_migration",
        "status": status,
        "migration_id": data["migration_id"],
        **{key: data["intent"][key] for key in ("project_id", "host_id", "config_path", "from_target", "to_target", "artifact_root", "anchor_home", "start_state")},
        "state": data["state"],
        "hashes": {
            "before": data["pre"],
            "after": {
                "portfolio_sha256": _file_sha256(ctx.config_path),
                "descriptor_sha256": _file_sha256(ctx.artifact_root / "PROJECT.md"),
                "owner_sha256": _file_sha256(ctx.artifact_root / "continuity" / "v1" / "OWNER.json"),
            },
            "expected": data["expected"],
        },
        "steps": copy.deepcopy(data["steps"]),
        "journal_path": str(journal.path),
        "journal_sha256": journal.sha256,
        "receipt_path": str(receipt) if receipt.is_file() else None,
        "next_operation": {
            "operation": "portfolio.prepare",
            "arguments": {"config_path": str(ctx.config_path), "host_id": ctx.host_id, "project_id": ctx.project_id},
            "copy_ready": shlex.join(["anchor", "portfolio", "prepare", "--config", str(ctx.config_path),
                                      "--host", ctx.host_id, "--project", ctx.project_id]),
            "reason": "bootstrap the project on its current target; a moved project starts a new continuity epoch",
        },
    }


def _new_journal(ctx: _Context, preflight: Mapping[str, Any]) -> _Journal:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    migration_id = f"tm_{stamp}_{uuid.uuid4().hex[:8]}"
    steps: dict[str, dict[str, Any]] = {name: {"status": "pending"} for name in STEPS}
    steps["rollback_snapshot"] = {"status": "not_started"}
    if preflight["start_state"] == "portfolio_already_moved":
        steps["portfolio"] = {"status": "already_applied"}
    data = {
        "format": FORMAT,
        "migration_id": migration_id,
        "created_at": _now(),
        "intent": {
            "project_id": ctx.project_id, "host_id": ctx.host_id, "config_path": str(ctx.config_path),
            "from_target": preflight["from_target"], "to_target": preflight["to_target"],
            "artifact_root": str(ctx.artifact_root), "anchor_home": str(ctx.anchor_home),
            "durable_root": ctx.durable_root, "authority_id": ctx.authority_id,
            "start_state": preflight["start_state"],
        },
        "pre": {**preflight["hashes"], "continuity_present": preflight["continuity_present"],
                "owner": preflight["owner"]},
        "expected": dict(preflight["expected"]),
        "backups": {
            "portfolio": f"{MIGRATIONS_DIRECTORY}/{migration_id}/portfolio.toml.pre",
            "descriptor": f"{MIGRATIONS_DIRECTORY}/{migration_id}/PROJECT.md.pre",
        },
        "state": "planned",
        "steps": steps,
        "generation": 0,
        "history": [{"generation": 0, "state": "planned", "steps": [], "at": _now()}],
    }
    return _Journal.create(_migrations_root(ctx.artifact_root) / f"{migration_id}.json", data)


def _find_journal(ctx: _Context, document: Mapping[str, Any], from_target: str, to_target: str) -> _Journal | None:
    matches = [
        journal for journal in _project_journals(ctx)
        if journal.data["intent"]["host_id"] == ctx.host_id
        and _same_target(journal.data["intent"]["from_target"], from_target)
        and _same_target(journal.data["intent"]["to_target"], to_target)
    ]
    active = [journal for journal in matches if journal.data["state"] not in {"completed", "rolled_back"}]
    if len(active) > 1:
        raise _blocked(
            "state_mismatch", "more than one unfinished journal names this migration",
            {**_base_context(ctx, document), "journal_paths": [str(item.path) for item in active]},
        )
    return active[0] if active else None


def _run(ctx: _Context, journal: _Journal, action: Any) -> None:
    try:
        action(ctx, journal)
    except TargetMigrationError:
        raise
    except Exception as exc:
        raise _incomplete(journal, f"{type(exc).__name__}: {exc}", error_type=type(exc).__name__) from exc


def move_target(
    *,
    config_path: str | os.PathLike[str],
    host_id: str,
    project_id: str,
    from_target: str,
    to_target: str,
    dry_run: bool = False,
    resume: bool = False,
    rollback: bool = False,
) -> dict[str, Any]:
    """Move one portfolio-managed project's host target with a journaled migration.

    ``dry_run`` returns the read-only preflight. ``resume`` finishes and ``rollback``
    reverses the unfinished journal for exactly this project, host, ``from_target`` and
    ``to_target``. Refusals raise ``TargetMigrationError`` with ``error_code``
    ``target_migration_blocked`` or ``target_migration_incomplete``.
    """
    if sum(bool(flag) for flag in (dry_run, resume, rollback)) > 1:
        raise ValueError("choose at most one of dry_run, resume and rollback")
    from_value, to_value = _absolute(from_target, "from_target"), _absolute(to_target, "to_target")
    if _same_target(from_value, to_value):
        raise ValueError("from_target and to_target must differ")
    ctx, document = _context(config_path, host_id, project_id)
    if resume or rollback:
        journal = _find_journal(ctx, document, from_value, to_value)
        if journal is None:
            completed = [item for item in _project_journals(ctx) if item.data["state"] in TERMINAL_STATES
                         and item.data["intent"]["host_id"] == ctx.host_id
                         and _same_target(item.data["intent"]["from_target"], from_value)
                         and _same_target(item.data["intent"]["to_target"], to_value)]
            if resume and completed and completed[-1].data["state"] == "completed":
                return _result(ctx, completed[-1], "completed")
            if rollback and completed and completed[-1].data["state"] == "rolled_back":
                _run(ctx, completed[-1], _finish_rollback_snapshot)
                return _result(ctx, completed[-1], "rolled_back")
            raise _blocked(
                "state_mismatch", "no unfinished migration journal matches this project, host and targets",
                {**_base_context(ctx, document), "from_target": from_value, "to_target": to_value},
            )
        if rollback:
            if journal.data["state"] in {"receipt_written", "completed"}:
                raise _blocked(
                    "state_mismatch",
                    f"migration {journal.data['migration_id']} already has a receipt; reverse it with a new "
                    "move-target from the new target back to the old one",
                    {**_base_context(ctx, document), "migration_id": journal.data["migration_id"]},
                    [_cli_operation({**journal.data["intent"], "from_target": to_value, "to_target": from_value},
                                    "dry_run", "preview the reverse move")],
                )
            if _route_mutations_applied(ctx, journal):
                _require_quiescent(ctx, document, {"migration_id": journal.data["migration_id"]})
            _run(ctx, journal, _run_rollback)
            return _result(ctx, journal, "rolled_back")
        if journal.data["state"] == "rolling_back":
            raise _incomplete(journal, "a rollback is in progress; finish it with --rollback")
        if _route_mutations_remaining(ctx, journal):
            _require_quiescent(ctx, document, {"migration_id": journal.data["migration_id"]})
        _run(ctx, journal, _run_forward)
        return _result(ctx, journal, "completed")
    unfinished = [item for item in _project_journals(ctx) if item.data["state"] in NON_TERMINAL_STATES]
    if unfinished:
        raise _incomplete(unfinished[0], "another migration of this project is unfinished")
    preflight = _preflight(ctx, document, from_value, to_value)
    if preflight["start_state"] == "already_migrated":
        return {**preflight, "kind": "target_migration", "status": "already_migrated", "dry_run": dry_run}
    if dry_run:
        return {**preflight, "status": "ready", "dry_run": True,
                "next_operation": _cli_operation({**_base_context(ctx, document), "from_target": from_value, "to_target": to_value},
                                                 None, "apply this exact migration")}
    journal = _new_journal(ctx, preflight)
    _run(ctx, journal, _run_forward)
    return _result(ctx, journal, "completed")


def load_mapping(path: str | os.PathLike[str]) -> list[dict[str, str]]:
    """Read an explicit batch mapping: ``{"moves": [{"project", "from", "to"}, ...]}``."""
    raw = Path(path).read_bytes()
    if len(raw) > 1024 * 1024:
        raise ValueError("mapping file exceeds 1 MiB")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate mapping key: {key}")
            result[key] = value
        return result

    document = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    if not isinstance(document, dict) or set(document) != {"moves"} or not isinstance(document["moves"], list):
        raise ValueError('mapping must be a JSON object {"moves": [...]}')
    moves = document["moves"]
    if not 1 <= len(moves) <= _MAX_BATCH:
        raise ValueError(f"mapping must list 1 to {_MAX_BATCH} moves")
    for index, move in enumerate(moves):
        if not isinstance(move, dict) or set(move) != {"project", "from", "to"} or not all(
            isinstance(value, str) and value for value in move.values()
        ):
            raise ValueError(f"moves[{index}] must have exactly non-empty string project, from and to")
    return moves


def move_targets(
    *,
    config_path: str | os.PathLike[str],
    host_id: str,
    moves: Sequence[Mapping[str, str]],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Preflight every move, then apply them one journaled migration at a time.

    The batch is bounded and explicit. Any preflight refusal stops the batch before
    any write. An apply failure stops at that project; earlier moves stay completed and
    later ones are untouched (each has its own journal, so there is no cross-project
    atomicity).
    """
    projects = [move["project"] for move in moves]
    destinations = [_absolute(move["to"], "to") for move in moves]
    sources = [_absolute(move["from"], "from") for move in moves]
    if len(set(projects)) != len(projects):
        raise ValueError("a batch may move each project at most once")
    from odibi_anchor._dispatcher._project import route_path_identity

    identities = [route_path_identity(value) for value in destinations]
    if len(set(identities)) != len(identities) or set(identities) & {route_path_identity(value) for value in sources}:
        raise _blocked(
            "destination_unsuitable", "batch destinations must be distinct and must not be another move's source",
            {"host_id": host_id, "config_path": str(config_path), "projects": projects},
        )
    previews: list[dict[str, Any]] = []
    for move in moves:
        try:
            previews.append(move_target(config_path=config_path, host_id=host_id, project_id=move["project"],
                                        from_target=move["from"], to_target=move["to"], dry_run=True))
        except TargetMigrationError as exc:
            exc.context.update(batch_failed_project=move["project"], batch_previews=[  # type: ignore[attr-defined]
                {"project_id": item["project_id"], "status": item["status"]} for item in previews
            ])
            raise
    if dry_run:
        return {"kind": "target_migration_batch", "status": "ready", "dry_run": True, "moves": previews}
    results: list[dict[str, Any]] = []
    for move in moves:
        try:
            results.append(move_target(config_path=config_path, host_id=host_id, project_id=move["project"],
                                       from_target=move["from"], to_target=move["to"]))
        except TargetMigrationError as exc:
            exc.context.update(  # type: ignore[attr-defined]
                batch_failed_project=move["project"],
                batch_completed=[item["project_id"] for item in results],
                batch_not_started=projects[len(results) + 1:],
            )
            raise
    return {"kind": "target_migration_batch", "status": "completed", "dry_run": False, "moves": results}
