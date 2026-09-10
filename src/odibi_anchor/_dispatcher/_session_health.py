"""_session_health.py — Cross-session regression detection.

Captures session health snapshots at exit (learn/gate) and validates
codebase consistency on entry (boot/status). Detects drift between sessions.

Spec: specs/CROSS_SESSION_REGRESSION_SPEC.md
"""
import os as _os
import json as _json
import hashlib as _hashlib
from datetime import datetime as _datetime, timezone as _timezone

from odibi_anchor._utils.contract import build_base_context
from odibi_anchor._dispatcher._limits import (
    DEFAULT_CHECKPOINT_FILE_THRESHOLD,
    DEFAULT_MAX_UNGATED_EDITS,
)


_HEALTH_FILE = ".anchor_session_health.json"
_ENFORCEMENT_FILE = "_enforcement.py"


def _enforcement_hash() -> str | None:
    """SHA-256 of the enforcement module. Returns None if unreadable."""
    enforcement_path = _os.path.join(_os.path.dirname(__file__), _ENFORCEMENT_FILE)
    return _file_hash(enforcement_path)


def _file_hash(path: str) -> str | None:
    """SHA-256 hash of file contents. Returns None if file is unreadable."""
    try:
        with open(path, "rb") as f:
            return _hashlib.sha256(f.read()).hexdigest()
    except (OSError, IOError):
        return None


def _is_foreign_path(rel_path: str, root: str) -> bool:
    """True if rel_path is an absolute path outside the current project root.

    A health snapshot captured in another environment (e.g. a Databricks
    ``/Workspace/...`` run) carries absolute paths that don't exist under a local
    root. Those can never be hashed here, so they'd always read as "deleted
    outside session" — a false drift signal. Relative paths (resolved against
    root) and paths under root are NOT foreign.
    """
    if not _os.path.isabs(rel_path):
        return False
    try:
        common = _os.path.commonpath([_os.path.normcase(rel_path), _os.path.normcase(root)])
        return common != _os.path.normcase(root)
    except ValueError:
        # Different drives / mixed roots (Windows) — definitely foreign.
        return True


def capture_session_health(
    root: str,
    files_changed: set,
    timings: list,
    *,
    test_result: dict | None = None,
    preflight_result: dict | None = None,
    threshold_values: dict | None = None,
) -> dict:
    """Build and persist exit health snapshot.

    Called by the learn handler after a successful gate. Captures file hashes,
    test/preflight results, and threshold values. Writes to .anchor_session_health.json.

    Args:
        root: Project root path.
        files_changed: Set of relative paths modified this session.
        timings: Session timing records.
        test_result: Optional test outcome dict.
        preflight_result: Optional preflight outcome dict.
        threshold_values: Optional dict of enforcement thresholds.

    Returns:
        The snapshot dict that was written.
    """
    session_id = _datetime.now(_timezone.utc).isoformat(timespec="seconds")
    from odibi_anchor._utils._session_state import _SESSION_STATE

    # Build file hashes for session-changed files only (keeps snapshot small)
    file_hashes = {}
    for rel_path in sorted(files_changed):
        abs_path = _os.path.join(root, rel_path) if not _os.path.isabs(rel_path) else rel_path
        h = _file_hash(abs_path)
        if h is not None:
            file_hashes[rel_path] = h

    snapshot = {
        "session_id": session_id,
        "files_changed": sorted(files_changed),
        "file_hashes": file_hashes,
        "test_result": test_result,
        "preflight_result": preflight_result,
        "enforcement_functions_hash": _enforcement_hash(),
        "threshold_values": threshold_values or {
            "checkpoint_file_threshold": DEFAULT_CHECKPOINT_FILE_THRESHOLD,
            "max_ungated_edits": DEFAULT_MAX_UNGATED_EDITS,
        },
        "continuity": {
            "schema_version": 1,
            "status": _SESSION_STATE.continuity_status,
            "generation": _SESSION_STATE.continuity_generation,
            "record_sha256": _SESSION_STATE.continuity_record_sha256,
        },
    }

    # Persist to disk (never raise — caller wraps in try/except)
    health_path = _os.path.join(root, _HEALTH_FILE)
    try:
        with open(health_path, "w", encoding="utf-8") as f:
            _json.dump(snapshot, f, indent=2)
            f.write("\n")
    except (OSError, IOError):
        pass  # Best-effort persistence

    return snapshot


def _load_health(root: str) -> dict | None:
    """Load the most recent health snapshot, or None if absent/corrupt."""
    health_path = _os.path.join(root, _HEALTH_FILE)
    try:
        if _os.path.exists(health_path):
            with open(health_path, "r", encoding="utf-8") as f:
                return _json.load(f)
    except (OSError, IOError, _json.JSONDecodeError):
        pass
    return None


def check_cross_session_drift(root: str) -> list[str]:
    """Compare current state against last session's exit snapshot.

    Returns list of warning strings (empty = clean). Called by
    _build_status_suggestions() to surface drift in status output.

    Never raises — returns empty list on any error.
    """
    warnings = []
    prev = _load_health(root)
    if prev is None:
        return warnings  # First session or no snapshot — clean

    # 1. File hash drift — files changed outside a session
    for rel_path, old_hash in prev.get("file_hashes", {}).items():
        if _is_foreign_path(rel_path, root):
            continue  # snapshot from another environment — not comparable here
        abs_path = _os.path.join(root, rel_path) if not _os.path.isabs(rel_path) else rel_path
        current = _file_hash(abs_path)
        if current is None:
            warnings.append(f"DRIFT: {rel_path} was deleted outside session")
        elif current != old_hash:
            warnings.append(f"DRIFT: {rel_path} modified outside session")

    # 2. Threshold drift — enforcement config changed
    current_thresholds = {
        "checkpoint_file_threshold": DEFAULT_CHECKPOINT_FILE_THRESHOLD,
        "max_ungated_edits": DEFAULT_MAX_UNGATED_EDITS,
    }
    for key, old_val in prev.get("threshold_values", {}).items():
        cur_val = current_thresholds.get(key)
        if cur_val is not None and cur_val != old_val:
            warnings.append(f"CONFIG: {key} changed {old_val} → {cur_val}")

    # 3. Enforcement hash drift — enforcement code changed without session
    prev_enforcement = prev.get("enforcement_functions_hash")
    if prev_enforcement is not None:
        cur_enforcement = _enforcement_hash()
        if cur_enforcement is not None and cur_enforcement != prev_enforcement:
            warnings.append("ENFORCEMENT: enforcement logic changed since last session")

    return warnings


def session_delta_context(root: str, *args, **kwargs) -> dict | str:
    """Between-session diff — shows what changed since the last session's exit.

    Usage:
        anchor("session_delta")

    Returns dict with files_modified_outside_session, config_changes, last_session.
    """
    output_format = kwargs.pop("output_format", "dict")
    prev = _load_health(root)

    if prev is None:
        ctx = build_base_context(
            kind="session_delta",
            subject="cross-session",
            summary="No previous session health snapshot found",
            metrics={"has_previous_snapshot": False},
        )
        ctx["findings"] = ["No .anchor_session_health.json found — this is the first tracked session."]
        ctx["suggested_next_actions"] = [
            "Run plan → edit → gate → structured assessment to create a health snapshot.",
        ]
        if output_format == "markdown":
            return _render_delta_markdown(ctx)
        return ctx

    # Compute file drift
    files_modified = []
    files_deleted = []
    for rel_path, old_hash in prev.get("file_hashes", {}).items():
        if _is_foreign_path(rel_path, root):
            continue  # snapshot from another environment — not comparable here
        abs_path = _os.path.join(root, rel_path) if not _os.path.isabs(rel_path) else rel_path
        current = _file_hash(abs_path)
        if current is None:
            files_deleted.append(rel_path)
        elif current != old_hash:
            files_modified.append(rel_path)

    # Compute config changes
    config_changes = []
    current_thresholds = {
        "checkpoint_file_threshold": DEFAULT_CHECKPOINT_FILE_THRESHOLD,
        "max_ungated_edits": DEFAULT_MAX_UNGATED_EDITS,
    }
    for key, old_val in prev.get("threshold_values", {}).items():
        cur_val = current_thresholds.get(key)
        if cur_val is not None and cur_val != old_val:
            config_changes.append({"key": key, "was": old_val, "now": cur_val})

    # Check enforcement drift
    enforcement_changed = False
    prev_enforcement = prev.get("enforcement_functions_hash")
    if prev_enforcement is not None:
        cur_enforcement = _enforcement_hash()
        if cur_enforcement is not None and cur_enforcement != prev_enforcement:
            enforcement_changed = True

    has_drift = bool(files_modified or files_deleted or config_changes or enforcement_changed)

    ctx = build_base_context(
        kind="session_delta",
        subject="cross-session",
        summary=f"{'Drift detected' if has_drift else 'No drift'} since {prev.get('session_id', 'unknown')}",
        metrics={
            "has_previous_snapshot": True,
            "last_session": prev.get("session_id", "unknown"),
            "files_modified_outside_session": len(files_modified),
            "files_deleted_outside_session": len(files_deleted),
            "config_changes": len(config_changes),
            "has_drift": has_drift,
            "enforcement_changed": enforcement_changed,
        },
    )
    ctx["files_modified_outside_session"] = files_modified
    ctx["files_deleted_outside_session"] = files_deleted
    ctx["config_changes"] = config_changes
    ctx["last_session_snapshot"] = prev

    findings = []
    if files_modified:
        findings.append(f"{len(files_modified)} file(s) modified outside session: {', '.join(files_modified[:5])}")
    if files_deleted:
        findings.append(f"{len(files_deleted)} file(s) deleted outside session: {', '.join(files_deleted[:5])}")
    if config_changes:
        for ch in config_changes:
            findings.append(f"Config '{ch['key']}' changed: {ch['was']} → {ch['now']}")
    if enforcement_changed:
        findings.append("Enforcement logic (_enforcement.py) changed since last session")
    if not findings:
        findings.append("Codebase matches last session's exit snapshot — no drift detected.")
    ctx["findings"] = findings

    ctx["suggested_next_actions"] = []
    if has_drift:
        ctx["suggested_next_actions"].append("SHOULD: Investigate drifted files before editing them.")
        ctx["suggested_next_actions"].append("SHOULD: Run anchor('map') to verify codebase consistency.")
    else:
        ctx["suggested_next_actions"].append("Session is clean — proceed with anchor('task').")

    if output_format == "markdown":
        return _render_delta_markdown(ctx)
    return ctx


def _render_delta_markdown(ctx: dict) -> str:
    """Render session_delta context as markdown."""
    lines = [f"# Session Delta: {ctx.get('subject', 'cross-session')}\n"]
    lines.append(f"**{ctx.get('summary', '')}**\n")

    lines.append("## Metrics\n")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    for k, v in ctx.get("metrics", {}).items():
        lines.append(f"| {k} | {v} |")

    if ctx.get("findings"):
        lines.append("\n## Findings\n")
        for f in ctx["findings"]:
            lines.append(f"- {f}")

    if ctx.get("files_modified_outside_session"):
        lines.append("\n## Modified Files\n")
        for f in ctx["files_modified_outside_session"]:
            lines.append(f"- {f}")

    if ctx.get("files_deleted_outside_session"):
        lines.append("\n## Deleted Files\n")
        for f in ctx["files_deleted_outside_session"]:
            lines.append(f"- {f}")

    if ctx.get("config_changes"):
        lines.append("\n## Config Changes\n")
        lines.append("| Key | Was | Now |")
        lines.append("| --- | --- | --- |")
        for ch in ctx["config_changes"]:
            lines.append(f"| {ch['key']} | {ch['was']} | {ch['now']} |")

    if ctx.get("suggested_next_actions"):
        lines.append("\n## Next Actions\n")
        for a in ctx["suggested_next_actions"]:
            lines.append(f"- {a}")

    return "\n".join(lines)
