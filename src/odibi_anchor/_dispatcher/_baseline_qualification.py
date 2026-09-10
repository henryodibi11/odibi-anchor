"""Truthful qualification bound to one immutable task repository baseline."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

BaselineOutcome = Literal[
    "smoke_passed",
    "existing_failure_reproduced",
    "unrelated_failure",
    "unavailable",
    "unsafe",
    "not_applicable",
]
OUTCOMES = frozenset({
    "smoke_passed", "existing_failure_reproduced", "unrelated_failure",
    "unavailable", "unsafe", "not_applicable",
})
_CHECKED_IN_AUTHORITY = ".odibi-anchor/baseline.json"


@dataclass(frozen=True)
class BaselineQualification:
    """One non-authoritative check result tied to exact task authority."""

    schema_version: str
    task_window_id: str
    baseline_sha256: str | None
    outcome: BaselineOutcome
    reason: str
    authority_source: str
    evidence_ref: str | None
    command_identity: tuple[str, ...]
    observed_at: str


def baseline_fingerprint(baseline: Any | None) -> str | None:
    """Hash the safe immutable baseline payload without live provider objects."""
    if baseline is None:
        return None
    if is_dataclass(baseline) and not isinstance(baseline, type):
        payload = {
            item.name: getattr(baseline, item.name)
            for item in fields(baseline)
            if item.name != "identity_provider"
        }
    elif isinstance(baseline, Mapping):
        payload = dict(baseline)
    else:
        raise TypeError("baseline qualification requires a supported immutable baseline")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def checked_in_baseline_authority(target_root: str | None) -> dict[str, Any] | None:
    """Read the sole bounded repository convention; never infer a command."""
    if not target_root:
        return None
    path = Path(target_root) / _CHECKED_IN_AUTHORITY
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("checked-in baseline authority is unreadable") from exc
    if not isinstance(value, dict):
        raise ValueError("checked-in baseline authority must be a JSON object")
    return value


def qualify_task_baseline(
    baseline: Any | None,
    *,
    task_window_id: str,
    execution_mode: str,
    target_root: str | None,
    request: Mapping[str, Any] | None = None,
) -> BaselineQualification:
    """Record one truthful result without running or mutating the environment."""
    if not isinstance(task_window_id, str) or not task_window_id.startswith("ltw_"):
        raise ValueError("baseline qualification requires an exact task window")
    configured = checked_in_baseline_authority(target_root)
    supplied = dict(request) if request is not None else configured
    source = "task.baseline_qualification" if request is not None else (
        f"repository:{_CHECKED_IN_AUTHORITY}" if configured is not None else "baseline_policy:v1"
    )

    if execution_mode != "source_change":
        outcome: str = "not_applicable"
        reason = "the accepted task does not authorize source changes"
        evidence_ref = None
        command: tuple[str, ...] = ()
    elif baseline is None:
        raise ValueError("source-change baseline qualification requires repository authority")
    elif supplied is None:
        outcome = "unavailable"
        reason = "no explicit or checked-in safe baseline qualification is available"
        evidence_ref = None
        command = ()
    else:
        allowed = {"outcome", "reason", "evidence_ref", "command_identity"}
        unknown = set(supplied) - allowed
        if unknown:
            raise ValueError("unknown baseline qualification fields: " + ", ".join(sorted(unknown)))
        outcome = str(supplied.get("outcome") or "")
        reason = str(supplied.get("reason") or "").strip()
        evidence_ref = supplied.get("evidence_ref")
        raw_command = supplied.get("command_identity") or []
        if not isinstance(raw_command, list) or any(
            not isinstance(item, str) or not item.strip() for item in raw_command
        ):
            raise TypeError("command_identity must be a list of non-empty strings")
        command = (
            Path(raw_command[0]).name,
            "sha256:" + hashlib.sha256(
                json.dumps(raw_command, separators=(",", ":")).encode()
            ).hexdigest(),
        ) if raw_command else ()
        if evidence_ref is not None and (
            not isinstance(evidence_ref, str) or not evidence_ref.strip()
        ):
            raise TypeError("evidence_ref must be a non-empty string or None")
        if outcome in {"smoke_passed", "existing_failure_reproduced", "unrelated_failure"}:
            if request is None:
                outcome = "unavailable"
                reason = (
                    "checked-in baseline configuration identifies a check method, not a "
                    "result for this exact task baseline; run it and supply retained evidence"
                )
                evidence_ref = None
            else:
                if not evidence_ref:
                    raise ValueError(f"{outcome} requires a retained evidence_ref")
                if not command:
                    raise ValueError(f"{outcome} requires a bounded command_identity")

    if outcome not in OUTCOMES:
        raise ValueError("baseline outcome must be one of: " + ", ".join(sorted(OUTCOMES)))
    if not reason:
        raise ValueError("baseline qualification requires a truthful reason")
    return BaselineQualification(
        "1.0", task_window_id, baseline_fingerprint(baseline),
        cast(BaselineOutcome, outcome), reason,
        source, str(evidence_ref) if evidence_ref else None, command,
        datetime.now(UTC).isoformat(),
    )


def project_baseline_qualification(
    qualification: BaselineQualification | Mapping[str, Any] | None,
    baseline: Any | None,
    *,
    task_window_id: str | None,
) -> dict[str, Any]:
    """Project current/stale qualification with provenance and no new claims."""
    if qualification is None:
        return {
            "status": "missing",
            "reason": "the accepted task has no baseline qualification",
            "provenance": {"source": "baseline_qualification:v1"},
        }
    value = asdict(qualification) if is_dataclass(qualification) else dict(qualification)
    expected = baseline_fingerprint(baseline)
    stale_reasons = []
    if value.get("task_window_id") != task_window_id:
        stale_reasons.append("task window changed")
    if value.get("baseline_sha256") != expected:
        stale_reasons.append("repository baseline changed")
    return {
        "status": "stale" if stale_reasons else "attested",
        "value": value,
        "reason": "; ".join(stale_reasons) if stale_reasons else None,
        "provenance": {"source": value.get("authority_source", "baseline_qualification:v1")},
    }
