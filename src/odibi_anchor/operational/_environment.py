"""Portable local environment manifests and conservative comparisons."""

from __future__ import annotations

import os
import platform
import sys
from collections.abc import Iterable, Mapping
from importlib import metadata
from pathlib import Path
from typing import Any

from ._contract import CollectorResult


def capture_environment(*, packages: Iterable[str] = (), roots: Mapping[str, str | os.PathLike[str]] | None = None,
                        identity: str | None = None, actions: Iterable[str] = (),
                        execution_mode: str = "local") -> CollectorResult:
    """Capture a portable manifest. Environment variable values are never read."""
    package_facts: dict[str, Any] = {}
    for name in sorted(set(packages)):
        try:
            package_facts[name] = {"available": True, "version": metadata.version(name)}
        except metadata.PackageNotFoundError:
            package_facts[name] = {"available": False, "version": None}
    try:
        own_version = metadata.version("odibi-anchor")
    except metadata.PackageNotFoundError:
        own_version = None
    facts = {
        "runtime": {"implementation": platform.python_implementation(), "python_version": platform.python_version()},
        "platform": {"system": platform.system(), "machine": platform.machine()},
        "odibi_anchor_version": own_version,
        "packages": package_facts,
        "roots": {key: str(Path(value).resolve()) for key, value in sorted((roots or {}).items())},
        "execution_mode": execution_mode,
        "identity": identity,
        "actions": sorted(set(actions)),
        "environment_variable_names": sorted(os.environ),
    }
    return CollectorResult(collector="local_environment", status="collected", source={"provider": "local", "channel": "python"},
                           environment={"platform": sys.platform, "execution_mode": execution_mode,
                                        "evidence_channel": "process"}, facts=facts)


def compare_environments(left: CollectorResult | Mapping[str, Any],
                         right: CollectorResult | Mapping[str, Any]) -> dict[str, Any]:
    """Compare manifest facts, separating changed, missing, and unverifiable paths."""
    def parts(value: CollectorResult | Mapping[str, Any]) -> tuple[str | None, Mapping[str, Any], list[str]]:
        if isinstance(value, CollectorResult):
            return value.status, value.facts, list(value.limitations)
        status = value.get("status")
        raw = value.get("facts", value)
        facts = raw if isinstance(raw, Mapping) else {}
        limits = value.get("limitations", ())
        return status, facts, list(limits) if isinstance(limits, (list, tuple)) else []

    left_status, left_facts, left_limits = parts(left)
    right_status, right_facts, right_limits = parts(right)
    limitations = [*(f"left: {item}" for item in left_limits), *(f"right: {item}" for item in right_limits)]
    statuses = (left_status, right_status)
    if any(status != "collected" for status in statuses):
        limitations.extend(f"{side} environment status is {status}"
                           for side, status in zip(("left", "right"), statuses, strict=True)
                           if status != "collected")
        return {"schema_version": 1, "changed": [], "missing": [], "unverifiable": ["environment"],
                "equivalent": None, "limitations": limitations}

    changed: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    unverifiable: list[str] = []

    def walk(a: Any, b: Any, path: str) -> None:
        if isinstance(a, Mapping) and isinstance(b, Mapping):
            for key in sorted(set(a) | set(b)):
                child = f"{path}.{key}" if path else key
                if key not in a:
                    missing.append({"path": child, "side": "left"})
                elif key not in b:
                    missing.append({"path": child, "side": "right"})
                else:
                    walk(a[key], b[key], child)
        elif a is None or b is None:
            if a != b:
                unverifiable.append(path)
        elif a != b:
            changed.append({"path": path, "left": a, "right": b})

    walk(left_facts, right_facts, "")
    return {"schema_version": 1, "changed": changed, "missing": missing, "unverifiable": unverifiable,
            "equivalent": not changed and not missing and not unverifiable, "limitations": limitations}
