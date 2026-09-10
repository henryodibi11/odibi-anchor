"""Bounded Delta detail/history/CDF summary normalization."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from ._contract import CollectorResult, ContractError, normalize_json
from ._redaction import redact

_MAX_HISTORY = 100
_MAX_KEYS = 100


def _optional_bool(value: Any) -> bool | None:
    """Parse provider booleans without treating non-empty strings as true."""
    if type(value) is bool:
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    return None


def delta_changes(
    detail: Mapping[str, Any], *, history: Sequence[Mapping[str, Any]] = (),
    cdf_summary: Mapping[str, Any] | None = None, key_salt: str = "",
) -> CollectorResult:
    """Normalize metadata and aggregates; CDF rows themselves are not accepted/stored."""
    if not isinstance(detail, Mapping):
        raise ContractError("detail must be a mapping")
    status = detail.get("status", "collected")
    limitations = list(detail.get("limitations", []))
    cdf_summary = cdf_summary or {}
    cdf_enabled = _optional_bool(detail.get("cdf_enabled", detail.get("changeDataFeedEnabled")))
    retention = detail.get("retention", detail.get("retention_duration"))
    if cdf_enabled is False:
        limitations.append("change data feed is disabled; change rows are unavailable (consider delta_diff)")
    elif cdf_enabled is None:
        limitations.append("change data feed state is unknown; change rows may be unavailable")
    if retention is None:
        limitations.append("CDF/history retention is unknown")
    if cdf_summary.get("expired"):
        limitations.append("requested CDF range has expired and is not readable")
    counts_in = cdf_summary.get("change_counts", cdf_summary.get("counts", {}))
    counts: dict[str, int] = {}
    if isinstance(counts_in, Mapping):
        for key in ("insert", "update_preimage", "update_postimage", "delete"):
            value = counts_in.get(key, 0)
            if type(value) is int and value >= 0:
                counts[key] = value
    keys = cdf_summary.get("changed_keys", [])
    hashed = []
    if isinstance(keys, (list, tuple)):
        for key in keys[:_MAX_KEYS]:
            normalized = normalize_json(key)
            hashed.append(hashlib.sha256((key_salt + repr(normalized)).encode()).hexdigest())
        if len(keys) > _MAX_KEYS:
            limitations.append(f"changed keys truncated to {_MAX_KEYS}")
    hist = []
    for item in history[:_MAX_HISTORY]:
        hist.append({name: item.get(name) for name in
                     ("version", "timestamp", "operation", "operation_parameters", "operation_metrics", "user_name")
                     if name in item})
    if len(history) > _MAX_HISTORY:
        limitations.append(f"history truncated to {_MAX_HISTORY} entries")
    history_versions = [item.get("version") for item in hist if type(item.get("version")) is int]
    current_version = max(history_versions) if history_versions else None
    if current_version is None:
        limitations.append("current Delta version is unknown because bounded history contained no version")
    facts = {"current_version": current_version,
             "format": detail.get("format"), "table_type": detail.get("table_type"),
             "cdf": {"enabled": cdf_enabled, "readable_from": cdf_summary.get("readable_from"),
                     "readable_to": cdf_summary.get("readable_to"), "expired": bool(cdf_summary.get("expired")),
                     "change_counts": counts, "affected_columns": cdf_summary.get("affected_columns", []),
                     "hashed_changed_keys": hashed}, "retention": retention, "history": hist,
             "restore_prerequisites": {"target_version_available": cdf_summary.get("target_version_available"),
                                       "files_available": cdf_summary.get("files_available"),
                                       "permissions_verified": cdf_summary.get("restore_permissions_verified")}}
    limitations.append("restore prerequisites are observations, not a guarantee that restore will succeed")
    cleaned, summary = redact(facts)
    return CollectorResult(collector="delta_changes", status=status,
                           source=normalize_json(detail.get("source", {"provider": "databricks", "channel": "injected"})),
                           environment=normalize_json(detail.get("environment", {})), facts=cleaned,
                           findings=() if status != "collected" else ({"kind": "delta_versions", "statement": "Delta metadata and available change summaries were normalized.",
                                                                      "evidence": {"current_version": facts["current_version"], "history_entries": len(hist)}},),
                           limitations=tuple(limitations), error=detail.get("error"), redaction=summary)
