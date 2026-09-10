"""Anchor Standard Output Contract for Table Profiler.

Converts TableProfile dataclass into the Anchor standard dict shape:
{
    "kind": "profile_table",
    "subject": "<table_name>",
    "summary": "<one-line operational summary>",
    "metrics": {<flat numeric/bool values>},
    "findings": [<flat list of strings>],
    "risks": [<flat list of strings>],
    "samples": {<bounded sample collections>},
    "suggested_next_actions": [<flat list of strings>]
}

Public API
----------
serialize_profile(profile: TableProfile, sample_limit: int = 10) -> dict
"""

from __future__ import annotations

import sys
from typing import Any

sys.dont_write_bytecode = True

from ._common import to_json_safe
from .models import TableProfile


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------




def _build_column_profiles(profile: TableProfile) -> dict:
    """Build column_profiles dict compatible with dataset_profile_context format.

    Maps TableProfile.columns (list[ColumnProfile]) to a dict keyed by column name,
    matching the format expected by downstream tools like suggest_rules.
    """
    _SPARK_TYPE_MAP = {
        "long": "numeric_integer", "int": "numeric_integer", "integer": "numeric_integer",
        "short": "numeric_integer", "byte": "numeric_integer", "bigint": "numeric_integer",
        "float": "numeric_float", "double": "numeric_float",
        "decimal": "numeric_float",
        "string": "text", "varchar": "text", "char": "text",
        "date": "datetime", "timestamp": "datetime", "timestamp_ntz": "datetime",
        "boolean": "boolean",
        "binary": "binary", "array": "complex", "map": "complex", "struct": "complex",
    }

    # Semantic types that imply a column is really numeric/typed despite being
    # stored as text (e.g. "$1,200.50", "45%", "00123"). Lets downstream tools
    # (transform_plan) propose TRY_CAST steps the spark_type alone wouldn't reveal.
    _SEMANTIC_TYPE_OVERRIDE = {
        "currency_amount": "numeric_float",
        "percentage": "numeric_float",
        "numeric_string": "numeric_float",
        "date_string": "datetime",
        "boolean_string": "boolean",
    }

    # Null-like sentinel values are computed during cleanliness auditing and
    # stored as format issues (not on the column). Index them by column so the
    # transform adapter can propose NULLIF/replace steps.
    null_like_by_col: dict[str, list] = {}
    for issue in getattr(profile, "format_issues", None) or []:
        if getattr(issue, "issue_type", None) == "null_like_strings" and getattr(issue, "column", None):
            vals = (issue.metadata or {}).get("null_like_values_found") or issue.examples or []
            if vals:
                null_like_by_col[issue.column] = list(vals)

    result = {}
    for col in profile.columns:
        # Map spark_type to inferred_type
        spark_base = col.spark_type.split("(")[0].lower() if col.spark_type else "unknown"
        inferred_type = _SPARK_TYPE_MAP.get(spark_base, "text")

        # When the storage type is generic text but the profiler inferred a
        # specific semantic type, surface the real type so cast steps are proposed.
        semantic_value = col.semantic_type.value if col.semantic_type else "unknown"
        if inferred_type == "text" and semantic_value in _SEMANTIC_TYPE_OVERRIDE:
            inferred_type = _SEMANTIC_TYPE_OVERRIDE[semantic_value]

        # Build stats dict
        stats = {}
        if col.min_value is not None:
            stats["min"] = col.min_value
        if col.max_value is not None:
            stats["max"] = col.max_value
        if col.mean_value is not None:
            stats["mean"] = col.mean_value
        if col.std_value is not None:
            stats["std"] = col.std_value

        result[col.name] = {
            "name": col.name,
            "dtype": col.spark_type or "unknown",
            "inferred_type": inferred_type,
            "total_count": col.row_count,
            "non_null_count": col.non_null_count,
            "null_count": col.null_count,
            "null_pct": col.null_pct,
            "distinct_count": col.distinct_count,
            "distinct_pct": col.distinct_pct,
            "is_unique": col.is_unique,
            "is_constant": col.is_constant,
            "top_values": col.top_values[:20] if col.top_values else [],
            "stats": stats,
            "quality_flags": col.quality_flags or [],
            "semantic_type": semantic_value,
            "null_like_values": null_like_by_col.get(col.name, []),
            # Cleanliness signals consumed by the transform generator to propose
            # trim / case-normalize / sentinel-map / unit-strip fixes.
            "has_leading_spaces": bool(col.has_leading_spaces),
            "has_trailing_spaces": bool(col.has_trailing_spaces),
            "has_mixed_case": bool(col.has_mixed_case),
            "has_embedded_units": bool(col.has_embedded_units),
            "sentinel_values": list(col.sentinel_values) if col.sentinel_values else [],
            "normalization_gap": _native(col.normalization_gap) or 0.0,
        }
    return result

def serialize_profile(profile: TableProfile, sample_limit: int = 10) -> dict:
    """Convert a TableProfile to the Anchor standard output contract.

    Args:
        profile: A completed TableProfile from profile_table().
        sample_limit: Max items in each sample collection (default 10).

    Returns:
        Dict matching the Anchor standard contract shape.
        Guaranteed JSON-serializable with deterministic output.
    """
    return _deep_serialize({
        "kind": "profile_table",
        "subject": profile.subject,
        "summary": _build_summary(profile),
        "metrics": _build_metrics(profile),
        "column_profiles": _build_column_profiles(profile),
        "findings": _build_findings(profile),
        "risks": _build_risks(profile),
        "samples": _build_samples(profile, sample_limit),
        "suggested_next_actions": _build_suggested_actions(profile),
    })


# ---------------------------------------------------------------------------
# Private: summary
# ---------------------------------------------------------------------------


def _build_summary(profile: TableProfile) -> str:
    """One-line interpretive summary with key numbers."""
    row_count = _native(profile.row_count) or 0
    col_count = _native(profile.column_count) or 0
    quality = _native(profile.overall_quality_score) or 0.0

    parts = [f"{profile.subject}: {row_count:,} rows, {col_count} cols"]

    if profile.classification and profile.classification.value != "unknown":
        conf = _native(profile.classification_confidence) or 0.0
        if conf >= 0.8:
            qualifier = "confirmed"
        elif conf >= 0.6:
            qualifier = "likely"
        else:
            qualifier = "possible"
        parts.append(f"{profile.classification.value} table ({qualifier})")

    if profile.grain and profile.grain.best_grain:
        grain_str = "+".join(profile.grain.best_grain)
        if profile.grain.is_unique:
            parts.append(f"{grain_str} is unique grain — safe as join key")
        else:
            dup_rate = _native(profile.grain.duplicate_rate) or 0.0
            parts.append(f"grain [{grain_str}] not unique — {dup_rate:.0%} duplicate rate")

    parts.append(f"Quality {quality:.2f}")

    if profile.freshness and _native(profile.freshness.staleness_hours) and _native(profile.freshness.staleness_hours) > 24:
        hours = _native(profile.freshness.staleness_hours)
        parts.append(f"{hours:.0f}h stale on '{profile.freshness.freshness_column}'")

    if profile.degraded_features:
        parts.append(f"{len(profile.degraded_features)} degraded feature(s)")

    return ". ".join(parts)


# ---------------------------------------------------------------------------
# Private: metrics
# ---------------------------------------------------------------------------


def _build_metrics(profile: TableProfile) -> dict:
    """Flat dict of numeric/bool values only.

    Column-name aggregates (constant/all-null/key columns) are intentionally
    NOT placed here — the metrics contract is flat scalars. Downstream consumers
    derive those from column_profiles (is_constant / is_unique / null_pct).
    """
    metrics: dict[str, int | float | bool | None] = {
        "row_count": _native(profile.row_count) or 0,
        "column_count": _native(profile.column_count) or 0,
        "quality_score": _safe_round(profile.overall_quality_score, 4),
        "classification_confidence": _safe_round(profile.classification_confidence, 4),
        "profiling_duration_ms": _native(profile.profiling_duration_ms),
        "format_issue_count": len(profile.format_issues),
        "finding_count": len(profile.findings),
        "risk_count": len(profile.risks),
        "degraded_feature_count": len(profile.degraded_features),
    }

    # Grain metrics
    if profile.grain:
        metrics["grain_is_unique"] = bool(profile.grain.is_unique)
        metrics["grain_duplicate_rate"] = _safe_round(profile.grain.duplicate_rate, 4)
        metrics["grain_column_count"] = len(profile.grain.best_grain)

    # Freshness metrics
    if profile.freshness:
        metrics["staleness_hours"] = round(_native(profile.freshness.staleness_hours), 2)

    # Column-level summary metrics
    if profile.columns:
        null_rates = [_native(c.null_pct) for c in profile.columns]
        metrics["max_null_pct"] = round(max(null_rates), 4) if null_rates else 0.0
        metrics["columns_with_nulls"] = sum(1 for r in null_rates if r > 0)
        metrics["unique_columns"] = sum(1 for c in profile.columns if c.is_unique)

    return metrics


# ---------------------------------------------------------------------------
# Private: findings
# ---------------------------------------------------------------------------


def _build_findings(profile: TableProfile) -> list[str]:
    """Flat list of string findings with interpretive context."""
    findings: list[str] = list(profile.findings) if profile.findings else []

    _CLASSIFICATION_HINTS = {
        "fact": "Classified as fact table — expect unique transactional grain and append-only loading pattern.",
        "dimension": "Classified as dimension table — expect slowly changing attributes keyed by a surrogate or natural key.",
        "snapshot": "Classified as snapshot table — expect periodic full-state captures with a timestamp partition.",
        "event_log": "Classified as event log — expect append-only inserts with an event timestamp grain.",
        "scd2": "Classified as SCD2 table — expect effective-dated rows with start/end timestamps per entity.",
        "lookup": "Classified as lookup table — expect small, mostly static reference data.",
        "staging": "Classified as staging table — expect raw ingestion data that may need cleansing before downstream use.",
        "aggregate": "Classified as aggregate table — expect pre-computed rollups; verify grain matches intended aggregation level.",
        "bridge": "Classified as bridge table — expect many-to-many relationship mapping between two entities.",
    }

    if profile.classification and profile.classification.value != "unknown":
        hint = _CLASSIFICATION_HINTS.get(profile.classification.value)
        if hint:
            findings.append(hint)

    if profile.grain and profile.grain.best_grain and not profile.grain.is_unique:
        grain_str = "+".join(profile.grain.best_grain)
        dup_rate = _native(profile.grain.duplicate_rate) or 0.0
        cls_label = profile.classification.value if profile.classification else "this type"
        findings.append(
            f"Grain [{grain_str}] has {dup_rate:.0%} duplicate rate — if this table is meant to be "
            f"{cls_label}, duplicate keys suggest the grain definition may need revision."
        )

    if profile.columns:
        for col in profile.columns:
            null_pct = _native(col.null_pct) or 0.0
            role_val = col.role.value if col.role else "unknown"
            if null_pct > 0.5 and role_val in ("measure", "primary_key"):
                findings.append(
                    f"Column '{col.name}' ({role_val}) has {null_pct:.0%} nulls — if this is a "
                    f"{role_val}, high nulls likely indicates upstream data quality issues."
                )
            if role_val == "foreign_key" and null_pct > 0:
                findings.append(
                    f"Column '{col.name}' appears to be a foreign key — if nulls are present "
                    f"({null_pct:.0%}), joins on this column will silently drop rows."
                )

    return findings


# ---------------------------------------------------------------------------
# Private: risks
# ---------------------------------------------------------------------------


def _build_risks(profile: TableProfile) -> list[str]:
    """Flat list of actionable risk strings with interpretive context.

    Starts with profile.risks pass-through, then appends interpretive risks.
    Falls back to format_issues and degraded_features synthesis.
    """
    risks: list[str] = list(profile.risks) if profile.risks else []

    if not risks:
        for issue in profile.format_issues:
            if issue.severity == "error":
                risks.append(f"[{issue.column}] {issue.description}")
        if profile.degraded_features:
            risks.append(
                f"Profiling incomplete: {len(profile.degraded_features)} feature(s) "
                f"degraded ({', '.join(profile.degraded_features)})"
            )

    if profile.grain and profile.grain.best_grain and not profile.grain.is_unique:
        grain_str = "+".join(profile.grain.best_grain)
        cls_val = profile.classification.value if profile.classification else "unknown"
        if cls_val == "fact":
            risks.append(
                f"If this fact table is used as a join target, the non-unique grain "
                f"[{grain_str}] will cause row multiplication in downstream queries."
            )

    if profile.columns:
        for col in profile.columns:
            role_val = col.role.value if col.role else "unknown"
            null_pct = _native(col.null_pct) or 0.0
            if role_val == "primary_key" and null_pct > 0:
                risks.append(
                    f"If '{col.name}' is the primary key, {null_pct:.0%} null rate "
                    f"suggests upstream identity resolution issues."
                )

    if profile.freshness and _native(profile.freshness.staleness_hours) and _native(profile.freshness.staleness_hours) > 24:
        hours = _native(profile.freshness.staleness_hours)
        freshness_col = profile.freshness.freshness_column
        risks.append(
            f"If downstream consumers expect near-real-time data, {hours:.0f}h staleness "
            f"on '{freshness_col}' suggests a stale or broken pipeline."
        )

    conf = _native(profile.classification_confidence) or 0.0
    if profile.classification and profile.classification.value != "unknown" and conf < 0.6:
        risks.append(
            f"Table classification is uncertain ({conf:.0%}) — the table structure is ambiguous. "
            f"Verify intended use before building downstream dependencies."
        )

    return risks


# ---------------------------------------------------------------------------
# Private: samples
# ---------------------------------------------------------------------------


def _build_samples(profile: TableProfile, sample_limit: int) -> dict:
    """Dict of bounded sample collections."""
    samples: dict[str, Any] = {}
    ambiguity_limit = max(1, min(sample_limit, 3))

    # Grain sample
    if profile.grain and profile.grain.best_grain:
        samples["grain"] = {
            "columns": profile.grain.best_grain,
            "is_unique": bool(profile.grain.is_unique),
            "duplicate_rate": round(_native(profile.grain.duplicate_rate), 4),
        }

    # Freshness sample
    if profile.freshness:
        samples["freshness"] = {
            "column": profile.freshness.freshness_column,
            "latest_value": str(profile.freshness.latest_value),
            "staleness": profile.freshness.staleness,
            "staleness_hours": round(_native(profile.freshness.staleness_hours), 2),
        }

    # Column summaries (bounded)
    if profile.columns:
        samples["columns"] = [
            _serialize_column_summary(col, ambiguity_limit=ambiguity_limit)
            for col in profile.columns[:sample_limit]
        ]

    # Format issues (bounded)
    if profile.format_issues:
        samples["format_issues"] = [
            {
                "column": issue.column,
                "issue_type": issue.issue_type,
                "severity": issue.severity,
                "description": issue.description,
            }
            for issue in profile.format_issues[:sample_limit]
        ]

    # Classification evidence
    if profile.classification and profile.classification.value != "unknown":
        samples["classification"] = {
            "type": profile.classification.value,
            "confidence": round(_native(profile.classification_confidence), 4),
            "reasoning": profile.classification_reasoning,
        }
        if profile.classification_inference:
            if profile.classification_inference.runner_ups:
                samples["classification"]["runner_ups"] = [
                    _serialize_competing_hypothesis(item)
                    for item in profile.classification_inference.runner_ups[:ambiguity_limit]
                ]
            if profile.classification_inference.verification_hint:
                samples["classification"]["verification_hint"] = (
                    profile.classification_inference.verification_hint
                )
            if profile.classification_inference.blocker_reason:
                samples["classification"]["blocker_reason"] = (
                    profile.classification_inference.blocker_reason
                )

    # Joins (bounded)
    if profile.joins:
        samples["joins"] = [
            {
                "source_column": j.source_column,
                "target_table": j.target_table,
                "target_column": j.target_column,
                "overlap_pct": round(_native(j.overlap_pct), 4),
                "cardinality": j.cardinality,
                "safe_join_type": j.safe_join_type,
            }
            for j in profile.joins[:sample_limit]
        ]

    # Duplicate forensics
    if profile.duplicate_forensics:
        df_ = profile.duplicate_forensics
        samples["duplicate_forensics"] = {
            "verdict": df_.verdict,
            "duplicate_rate": round(_native(df_.duplicate_rate), 4),
            "duplicate_count": _native(df_.duplicate_count),
            "explanation": df_.explanation,
        }

    if profile.grain and (
        profile.grain.runner_up_grains or profile.grain.verification_hint
    ):
        samples.setdefault("grain", {
            "columns": profile.grain.best_grain,
            "is_unique": bool(profile.grain.is_unique),
            "duplicate_rate": round(_native(profile.grain.duplicate_rate), 4),
        })
        if profile.grain.runner_up_grains:
            samples["grain"]["runner_up_grains"] = [
                {
                    "columns": list(item.get("columns", [])),
                    "duplicate_rate": round(float(item.get("duplicate_rate", 0.0)), 4),
                    "null_exclusion_rate": round(float(item.get("null_exclusion_rate", 0.0)), 4),
                    "is_unique": bool(item.get("is_unique", False)),
                }
                for item in profile.grain.runner_up_grains[:ambiguity_limit]
            ]
        if profile.grain.verification_hint:
            samples["grain"]["verification_hint"] = profile.grain.verification_hint

    ambiguity_columns = [
        column
        for column in profile.columns
        if (
            (column.semantic_type_inference and (
                column.semantic_type_inference.runner_ups
                or column.semantic_type_inference.verification_hint
                or column.semantic_type_inference.blocker_reason
            ))
            or (column.role_inference and (
                column.role_inference.runner_ups
                or column.role_inference.verification_hint
                or column.role_inference.blocker_reason
            ))
        )
    ]
    if ambiguity_columns:
        samples["ambiguity"] = [
            _serialize_column_ambiguity(column, ambiguity_limit)
            for column in ambiguity_columns[:ambiguity_limit]
        ]

    return samples


def _serialize_column_summary(col, ambiguity_limit: int = 2) -> dict:
    """Serialize a ColumnProfile to a compact summary dict."""
    summary = {
        "name": col.name,
        "spark_type": col.spark_type,
        "null_pct": round(_native(col.null_pct), 4),
        "distinct_count": _native(col.distinct_count),
        "is_unique": bool(col.is_unique),
    }

    if col.semantic_type and col.semantic_type.value != "unknown":
        summary["semantic_type"] = col.semantic_type.value

    if col.role and col.role.value != "unknown":
        summary["role"] = col.role.value

    if col.top_values:
        summary["top_values"] = col.top_values[:3]

    if col.semantic_type_inference and (
        col.semantic_type_inference.runner_ups
        or col.semantic_type_inference.verification_hint
        or col.semantic_type_inference.blocker_reason
    ):
        summary["semantic_inference"] = _serialize_inference_ambiguity(
            col.semantic_type_inference,
            ambiguity_limit,
        )

    if col.role_inference and (
        col.role_inference.runner_ups
        or col.role_inference.verification_hint
        or col.role_inference.blocker_reason
    ):
        summary["role_inference"] = _serialize_inference_ambiguity(
            col.role_inference,
            ambiguity_limit,
        )

    return summary


# ---------------------------------------------------------------------------
# Private: suggested_next_actions
# ---------------------------------------------------------------------------


def _serialize_inference_ambiguity(inference: Any, ambiguity_limit: int) -> dict[str, Any]:
    """Serialize ambiguity metadata from an inference object."""

    payload: dict[str, Any] = {}
    if getattr(inference, 'runner_ups', None):
        payload['runner_ups'] = [
            _serialize_competing_hypothesis(item)
            for item in inference.runner_ups[:ambiguity_limit]
        ]
    if getattr(inference, 'verification_hint', ''):
        payload['verification_hint'] = inference.verification_hint
    if getattr(inference, 'blocker_reason', ''):
        payload['blocker_reason'] = inference.blocker_reason
    return payload



def _serialize_competing_hypothesis(item: Any) -> dict[str, Any]:
    """Serialize a CompetingHypothesis or hypothesis-like object."""

    value = getattr(item, 'value', None)
    enum_value = getattr(value, 'value', None)
    if enum_value is not None:
        value = enum_value
    return {
        'value': value,
        'confidence': round(float(getattr(item, 'confidence', 0.0)), 4),
        'evidence': list(getattr(item, 'evidence', [])),
        'counter_signals': list(getattr(item, 'counter_signals', [])),
        'blocker_reason': getattr(item, 'blocker_reason', ''),
        'verification_hint': getattr(item, 'verification_hint', ''),
    }



def _serialize_column_ambiguity(col: Any, ambiguity_limit: int) -> dict[str, Any]:
    """Serialize the ambiguity surface for a column."""

    payload: dict[str, Any] = {'name': col.name}
    if col.semantic_type and getattr(col.semantic_type, 'value', None) != 'unknown':
        payload['semantic_type'] = col.semantic_type.value
    if col.role and getattr(col.role, 'value', None) != 'unknown':
        payload['role'] = col.role.value
    if col.semantic_type_inference and (
        col.semantic_type_inference.runner_ups
        or col.semantic_type_inference.verification_hint
        or col.semantic_type_inference.blocker_reason
    ):
        payload['semantic_inference'] = _serialize_inference_ambiguity(
            col.semantic_type_inference,
            ambiguity_limit,
        )
    if col.role_inference and (
        col.role_inference.runner_ups
        or col.role_inference.verification_hint
        or col.role_inference.blocker_reason
    ):
        payload['role_inference'] = _serialize_inference_ambiguity(
            col.role_inference,
            ambiguity_limit,
        )
    return payload



def _build_suggested_actions(profile: TableProfile) -> list[str]:
    """Flat list of concrete next-step recommendations with tool references."""
    actions: list[str] = []

    if profile.suggested_actions:
        actions.extend(profile.suggested_actions)

    if not actions:
        if profile.grain and not profile.grain.is_unique and profile.grain.best_grain:
            grain_cols = profile.grain.best_grain
            grain_str = "+".join(grain_cols)
            dup_rate = _native(profile.grain.duplicate_rate) or 0.0
            keys_arg = ", ".join(f"'{c}'" for c in grain_cols)
            actions.append(
                f"Run anchor('duplicate', df, keys=[{keys_arg}]) to diagnose the "
                f"{dup_rate:.0%} duplicate rate on [{grain_str}]."
            )

        if profile.format_issues:
            error_issues = [i for i in profile.format_issues if i.severity == "error"]
            if error_issues:
                first_col = error_issues[0].column
                actions.append(
                    f"Run anchor('profile_table', df, subject='{profile.subject}', level='deep') "
                    f"to inspect {len(error_issues)} format error(s) — if concentrated in few "
                    f"columns, consider anchor('explore', df['{first_col}']) for targeted cleanup."
                )

        if profile.freshness and _native(profile.freshness.staleness_hours) and _native(profile.freshness.staleness_hours) > 24:
            hours = _native(profile.freshness.staleness_hours)
            freshness_col = profile.freshness.freshness_column
            actions.append(
                f"Run anchor('profile_table', df, subject='{profile.subject}') to check if "
                f"staleness on '{freshness_col}' ({hours:.0f}h) is expected or indicates "
                f"a broken upstream refresh."
            )

        conf = _native(profile.classification_confidence) or 0.0
        if profile.classification and profile.classification.value != "unknown" and conf < 0.6:
            actions.append(
                f"Verify table type — classification confidence is only {conf:.0%}. "
                f"Run anchor('profile_table', df, level='deep') for more evidence."
            )

        if profile.degraded_features:
            actions.append(
                "Re-run with level='deep' to attempt recovery of degraded features."
            )

        if not actions:
            actions.append("Profile looks clean — safe to proceed with downstream usage.")

    actions.append("If clean: Run anchor('quality', df, subject='...', keys=[...]) for write-readiness gate.")
    actions.append("If issues found: Run anchor('explore', df) to understand data shape before fixing.")

    return actions


# ---------------------------------------------------------------------------
# Private: helpers
# ---------------------------------------------------------------------------


def _native(value: Any) -> Any:
    """Convert numpy/pandas types to Python native for JSON serialization.

    Returns None for None values — callers must handle None explicitly
    where a default is semantically appropriate.
    """
    return to_json_safe(value)


def _safe_round(value: Any, digits: int) -> float | None:
    """Round a native value, returning None if value is None."""
    v = _native(value)
    return round(v, digits) if v is not None else None


# ---------------------------------------------------------------------------
# Public: Microscope & Case File serialization
# ---------------------------------------------------------------------------


def serialize_microscope(result: dict) -> dict:
    """Ensure all values in microscope output are JSON-serializable.

    Walks the dict and applies _native() to convert numpy/pandas scalar types.

    Args:
        result: The dict returned by microscope().

    Returns:
        JSON-serializable copy of the dict.
    """
    return _deep_serialize(result)


def serialize_case_file(result: dict) -> dict:
    """Ensure all values in case_file output are JSON-serializable.

    Walks the dict and applies _native() to convert numpy/pandas scalar types.

    Args:
        result: The dict returned by case_file().

    Returns:
        JSON-serializable copy of the dict.
    """
    return _deep_serialize(result)


def _deep_serialize(obj: Any) -> Any:
    """Recursively serialize a nested structure for JSON safety."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return {k: _deep_serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deep_serialize(item) for item in obj]
    return _native(obj)
