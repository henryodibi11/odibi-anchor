"""Portable analysis of bounded Spark plans and already-normalized metrics."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

from ._contract import CollectorResult, ContractError, normalize_json
from ._redaction import redact

_MAX_PLAN = 64_000
_MAX_CITATIONS = 50


def _number(mapping: Mapping[str, Any], *names: str) -> float | int | None:
    for name in names:
        value = mapping.get(name)
        if type(value) in (int, float) and not isinstance(value, bool):
            return value
    return None


def spark_diagnose(
    plan_text: str,
    *,
    query_profile: Mapping[str, Any] | None = None,
    stage_metrics: Sequence[Mapping[str, Any]] | None = None,
    source: Mapping[str, Any] | None = None,
    environment: Mapping[str, Any] | None = None,
) -> CollectorResult:
    """Analyze static plan text and optional normalized profile/stage metrics.

    Inputs are adapter products, not SDK responses. Metric records may use common
    snake-case spill and partition-size spellings; records are summarized and are
    never returned verbatim.
    """
    if not isinstance(plan_text, str):
        raise ContractError("plan_text must be a string")
    limitations: list[str] = []
    if len(plan_text) > _MAX_PLAN:
        plan_text = plan_text[:_MAX_PLAN]
        limitations.append(f"plan text truncated to {_MAX_PLAN} characters")
    cleaned, redaction = redact(plan_text)
    lines = [line.strip()[:500] for line in cleaned.splitlines() if line.strip()]
    findings: list[dict[str, Any]] = []

    def cite(kind: str, message: str, pattern: str) -> None:
        for index, line in enumerate(lines):
            if re.search(pattern, line, re.I):
                findings.append({"kind": kind, "statement": message,
                                 "evidence": {"channel": "static_plan", "line": index + 1,
                                              "node": line}})
                return

    cite("exchange", "The plan contains a data exchange.", r"\b(exchange|shuffle)\b")
    cite("join_strategy", "The plan specifies a join strategy.",
         r"\b(?:broadcast|sortmerge|shuffledhash|broadcastnestedloop).*join\b")
    cite("cartesian_product", "The plan contains a Cartesian product or cross join.",
         r"\b(cartesianproduct|cross\s*join)\b")
    cite("python_udf", "The plan contains a Python UDF execution barrier.",
         r"\b(batch)?evalpython\b|pythonudf")
    cite("partition_pruning", "The plan reports partition-pruning filters.",
         r"partitionfilters\s*:\s*\[[^]]+")
    cite("adaptive_execution", "Adaptive query execution is represented in the plan.",
         r"adaptive(sparkplan)?")

    scans: dict[str, list[int]] = {}
    for index, line in enumerate(lines):
        match = re.search(r"(?:scan|filescan)\s+[^\s]*\s*([`\w.]+)", line, re.I)
        if match:
            scans.setdefault(match.group(1).lower(), []).append(index + 1)
    for name, occurrences in scans.items():
        if len(occurrences) > 1 and len(findings) < _MAX_CITATIONS:
            findings.append({"kind": "repeated_scan", "statement": f"{name} is scanned repeatedly.",
                             "evidence": {"channel": "static_plan", "lines": occurrences[:10]}})

    metric_records: list[Mapping[str, Any]] = []
    if query_profile is not None:
        profile_metrics = query_profile.get("metrics", query_profile.get("stages", []))
        if isinstance(profile_metrics, Mapping):
            metric_records.append(profile_metrics)
        elif isinstance(profile_metrics, (list, tuple)):
            metric_records.extend(x for x in profile_metrics if isinstance(x, Mapping))
    if stage_metrics:
        metric_records.extend(stage_metrics)
    metric_summary = {"record_count": len(metric_records), "spill_bytes": 0,
                      "max_partition_bytes": None, "median_partition_bytes": None}
    for index, metric in enumerate(metric_records[:500]):
        spill = _number(metric, "spill_bytes", "memory_bytes_spilled", "disk_bytes_spilled")
        if spill is not None and spill > 0:
            metric_summary["spill_bytes"] += spill
            findings.append({"kind": "spill", "statement": "Execution metrics report spilled data.",
                             "evidence": {"channel": "metrics", "record": index,
                                          "spill_bytes": spill}})
        maximum = _number(metric, "max_partition_bytes", "max_task_input_bytes")
        median = _number(metric, "median_partition_bytes", "median_task_input_bytes")
        if maximum is not None:
            metric_summary["max_partition_bytes"] = max(maximum, metric_summary["max_partition_bytes"] or 0)
        if median is not None:
            metric_summary["median_partition_bytes"] = median
        ratio = _number(metric, "skew_ratio")
        if ratio is None and maximum is not None and median not in (None, 0):
            ratio = maximum / median
        if ratio is not None and ratio >= 3:
            findings.append({"kind": "skew", "statement": "Partition metrics report skew.",
                             "evidence": {"channel": "metrics", "record": index,
                                          "max_to_median_ratio": round(ratio, 3)}})
    if len(metric_records) > 500:
        limitations.append("metrics truncated to 500 records")
    if not metric_records:
        limitations.append("skew and spill cannot be assessed without metrics")
    facts = {"plan_sha256": hashlib.sha256(cleaned.encode()).hexdigest(),
             "plan_line_count": len(lines), "metrics": metric_summary}
    return CollectorResult(collector="spark_diagnose", status="collected",
                           source=normalize_json(source or {"provider": "portable", "channel": "injected"}),
                           environment=normalize_json(environment or {}), facts=facts,
                           findings=tuple(findings[:_MAX_CITATIONS]), limitations=tuple(limitations),
                           redaction=redaction)
