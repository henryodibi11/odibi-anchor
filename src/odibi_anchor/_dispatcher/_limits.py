"""Centralized, validated lifecycle checkpoint limits."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_CHECKPOINT_FILE_THRESHOLD = 20
DEFAULT_MAX_UNGATED_EDITS = 20
_RISK_FACTORS = {"low": 1.0, "medium": 0.75, "high": 0.5, "critical": 0.4}


def lifecycle_limits(root: str, profile: Any | None = None) -> dict[str, int | str]:
    """Resolve project limits and apply the accepted task's risk factor."""
    base = DEFAULT_CHECKPOINT_FILE_THRESHOLD
    max_ungated = DEFAULT_MAX_UNGATED_EDITS
    path = Path(root) / ".anchor_config.json"
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid Odibi Anchor config: {exc}") from exc
        enforcement = raw.get("enforcement", {})
        if not isinstance(enforcement, dict):
            raise RuntimeError(".anchor_config.json enforcement must be an object")
        base = _positive_limit(
            enforcement.get("checkpoint_file_threshold", base),
            "checkpoint_file_threshold",
        )
        max_ungated = _positive_limit(
            enforcement.get("max_ungated_edits", max_ungated),
            "max_ungated_edits",
        )
    risk = str(getattr(profile, "risk", "medium") or "medium").lower()
    factor = _RISK_FACTORS.get(risk, _RISK_FACTORS["medium"])
    effective = max(1, int(base * factor))
    return {
        "checkpoint_file_threshold": effective,
        "checkpoint_configured_upper_bound": base,
        "checkpoint_risk": risk if risk in _RISK_FACTORS else "medium",
        "max_ungated_edits": max_ungated,
    }


def _positive_limit(value: Any, name: str) -> int:
    if type(value) is not int or not 1 <= value <= 1000:
        raise RuntimeError(f"enforcement.{name} must be an integer from 1 to 1000")
    return value
