"""Aggregate-only table observations and conservative longitudinal comparisons."""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ._artifacts import persist_artifact, read_artifact
from ._contract import ContractError, canonical_json, normalize_json, utc_now
from ._redaction import redact

_LIMITATION_ID = re.compile(r"[A-Za-z0-9_.:-]{1,100}\Z")


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if is_dataclass(value):
        return asdict(value)
    raise ContractError("profile_table output must be a mapping")


def _profile(value: Any, subject: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping) or is_dataclass(value):
        return _mapping(value)
    # Deliberately lazy: pandas remains optional and the existing public profiler is
    # the only code allowed to inspect rows.
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - only reached without pandas
        raise ContractError("a profile_table output mapping is required") from exc
    if not isinstance(value, pd.DataFrame):
        raise ContractError("input must be profile_table output or a pandas DataFrame")
    from tools.table_profiler_tool.lib.profiler import profile_table

    return _mapping(profile_table(value, subject=subject))


def _column_stats(profile: Mapping[str, Any], selected: Sequence[str] | None) -> list[dict[str, Any]]:
    wanted = set(selected) if selected is not None else None
    result = []
    for column in profile.get("columns", ()):
        if not isinstance(column, Mapping):
            continue
        name = column.get("name") or column.get("column")
        if not isinstance(name, str) or (wanted is not None and name not in wanted):
            continue
        item = {"name": name}
        for source, target in (("spark_type", "dtype"), ("dtype", "dtype"), ("null_count", "null_count"),
                               ("null_pct", "null_pct"), ("distinct_count", "distinct_count"),
                               ("distinct_pct", "distinct_pct")):
            if source in column and type(column[source]) in (str, int, float, type(None)):
                item[target] = column[source]
        result.append(item)
    return sorted(result, key=lambda item: item["name"])


def _aggregate_mapping(value: Any, allowed: set[str], list_fields: set[str] = frozenset()) -> dict[str, Any] | None:
    """Project profiler structures to explicitly aggregate scalar fields."""
    if not isinstance(value, Mapping):
        return None
    projected = {}
    for key in allowed:
        if key not in value:
            continue
        item = value.get(key)
        if type(item) in (str, int, float, bool, type(None)):
            projected[key] = item
        elif key in list_fields and isinstance(item, (list, tuple)) and all(isinstance(entry, str) for entry in item):
            projected[key] = list(item[:100])
    return projected


def _finite_number(value: Any) -> int | float | None:
    if type(value) is int:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    return None


def observe_table(profile_table_output: Any, *, subject: str, selected_columns: Sequence[str] | None = None,
                  delta_metadata: Mapping[str, Any] | None = None, collection_method: str | None = None,
                  persist: bool = False, artifact_root: str | Path | None = None) -> dict[str, Any]:
    """Create a stable observation from an existing profile; never retain rows."""
    profile = _profile(profile_table_output, subject)
    columns = _column_stats(profile, selected_columns)
    schema = [{"name": item["name"], "dtype": item.get("dtype")} for item in columns]
    degraded = profile.get("degraded_features", ())
    limitations = [
        item for item in degraded
        if isinstance(item, str) and _LIMITATION_ID.fullmatch(item)
    ] if isinstance(degraded, (list, tuple)) else []
    if selected_columns is not None:
        missing = sorted(set(selected_columns) - {item["name"] for item in columns})
        if missing:
            limitations.append("requested columns absent from profile: " + ", ".join(missing))
    facts = {
        "row_count": _finite_number(profile.get("row_count")),
        "column_count": _finite_number(profile.get("column_count", len(profile.get("columns", ())))),
        "schema_fingerprint": hashlib.sha256(canonical_json(schema)).hexdigest(),
        "columns": columns,
        "grain": _aggregate_mapping(
            profile.get("grain"), {"best_grain", "is_unique", "duplicate_rate", "candidate_count"}, {"best_grain"},
        ),
        "duplicates": _aggregate_mapping(
            profile.get("duplicate_forensics"),
            {"grain_columns", "duplicate_count", "duplicate_row_count", "duplicate_group_count", "duplicate_rate"},
            {"grain_columns"},
        ),
        "freshness": _aggregate_mapping(
            profile.get("freshness"),
            {"freshness_column", "staleness", "staleness_hours", "status"},
        ),
        "delta": _aggregate_mapping(
            delta_metadata if delta_metadata is not None else profile.get("delta_metadata"),
            {"format", "current_version", "version", "table_type", "num_files", "numFiles", "size_bytes", "sizeInBytes"},
        ),
        "duration_ms": _finite_number(profile.get("profiling_duration_ms")),
    }
    observation = normalize_json({
        "schema_version": 1, "kind": "table_observation", "subject": subject,
        "observed_at": profile.get("profiled_at") or utc_now(),
        "collection_method": collection_method or profile.get("profiling_level") or "profile_table",
        "facts": facts, "limitations": limitations,
    })
    evidence, redaction = redact(observation)
    evidence["redaction"] = redaction
    evidence = normalize_json(evidence)
    if persist:
        if artifact_root is None:
            raise ValueError("artifact_root is required when persist=True")
        artifact = persist_artifact(evidence, artifact_root, kind="tables", subject=evidence["subject"])
        return normalize_json({
            **evidence,
            "evidence": evidence,
            "managed_writes": [{
                "kind": "table_observation", "status": "written", "path": str(artifact.path),
                "relative_path": artifact.relative_path, "sha256": artifact.sha256,
            }],
            "write_performed": True,
        })
    return evidence


def _load_observation(value: Mapping[str, Any] | str | Path, root: Path | None) -> dict[str, Any]:
    if isinstance(value, Mapping):
        result = dict(value.get("evidence", value))
    else:
        path = Path(value)
        if not path.is_absolute():
            if root is None:
                raise ValueError("artifact_root is required for relative observation paths")
            path = root / path
        if root is None:
            raise ValueError("artifact_root is required for observation artifact paths")
        document = json.loads(read_artifact(path, root))
        result = document.get("payload", document)
    if result.get("schema_version") != 1 or result.get("kind") != "table_observation":
        raise ContractError("not a schema_version 1 table observation")
    return result


def table_trend(observations: Sequence[Mapping[str, Any] | str | Path], *,
                artifact_root: str | Path | None = None) -> dict[str, Any]:
    """Compare ordered observations without making an anomaly claim."""
    root = Path(artifact_root) if artifact_root is not None else None
    loaded = [_load_observation(item, root) for item in observations]
    timestamped = []
    timestamp_errors = []
    for index, item in enumerate(loaded):
        value = item.get("observed_at")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
        except ValueError:
            parsed = None
        if parsed is None or parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            timestamp_errors.append(f"observation {index} has an invalid or non-UTC observed_at")
        else:
            timestamped.append((parsed, item))
    ordered = [item for _, item in sorted(timestamped, key=lambda pair: pair[0])]
    result: dict[str, Any] = {"schema_version": 1, "kind": "table_trend", "observation_count": len(ordered),
                              "ordered_observed_at": [item["observed_at"] for item in ordered],
                              "status": "insufficient_history" if len(ordered) < 2 else "compared",
                              "point_changes": [], "limitations": [], "anomaly_claim": None}
    if timestamp_errors:
        result["observation_count"] = len(loaded)
        result["status"] = "incomparable"
        result["limitations"].extend(timestamp_errors)
        return normalize_json(result)
    if len(ordered) < 2:
        result["limitations"].append("at least two observations are required")
        return result
    subjects = {item.get("subject") for item in ordered}
    methods = {item.get("collection_method") for item in ordered}
    schemas = {item.get("facts", {}).get("schema_fingerprint") for item in ordered}
    if len(subjects) != 1 or None in subjects:
        result["limitations"].append("subjects differ or are missing; observations are not comparable")
    if len(methods) != 1 or None in methods:
        result["limitations"].append("collection methods differ or are missing; observations are not comparable")
    if len(schemas) != 1 or None in schemas:
        result["limitations"].append("schema dimensions differ or are missing; observations are not comparable")
    comparable = not result["limitations"]
    if not comparable:
        result["status"] = "incomparable"
        return normalize_json(result)
    left, right = ordered[-2:]
    for metric in ("row_count", "column_count"):
        a, b = left["facts"].get(metric), right["facts"].get(metric)
        if type(a) in (int, float) and type(b) in (int, float):
            result["point_changes"].append({"metric": metric, "from": a, "to": b, "change": b - a})
    if comparable:
        summaries = {}
        for metric in ("row_count", "column_count"):
            values = [item["facts"].get(metric) for item in ordered]
            if all(type(value) in (int, float) for value in values):
                summaries[metric] = {"median": statistics.median(values), "range": [min(values), max(values)]}
        result["summaries"] = summaries
    return normalize_json(result)


def render_table_observation_markdown(value: Mapping[str, Any]) -> str:
    return (f"# Table observation: {value['subject']}\n\n"
            f"- Observed: {value['observed_at']}\n- Rows: {value['facts'].get('row_count')}\n"
            f"- Schema fingerprint: `{value['facts'].get('schema_fingerprint')}`\n")


def render_table_trend_markdown(value: Mapping[str, Any]) -> str:
    return (f"# Table trend\n\n- Status: {value['status']}\n"
            f"- Observations: {value['observation_count']}\n- Anomaly claim: none\n")
