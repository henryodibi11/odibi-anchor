"""Table Profiler orchestrator.

Wires all profiling sub-modules into a single public entry point.

Public API
----------
profile_table(df, subject, level='standard', **opts) -> TableProfile
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from typing import Any

import pandas as pd

sys.dont_write_bytecode = True

from ._sampling import detect_engine
from .cleanliness import audit_cleanliness
from .cross_column import detect_cross_column_dependencies
from .duplicate_forensics import analyze_duplicates
from .findings_extra import detect_extra_findings
from .value_stability import analyze_value_stability
from .join_profiler import profile_joins
from .string_length import detect_string_length_issues
from .format_checker import detect_format_issues
from .freshness import detect_freshness
from .grain_detector import detect_grain
from .models import (
    ColumnProfile,
    DuplicateForensics,
    FreshnessAnalysis,
    FormatIssue,
    GrainAnalysis,
    Inference,
    JoinProfile,
    OutlierProfile,
    ProfilingLevel,
    SemanticType,
    TableClassification,
    TableProfile,
)

try:
    from pyspark.sql import functions as F
except ImportError:  # pragma: no cover
    F = None
from .role_classifier import infer_column_role
from .semantic_typer import infer_semantic_type
from .stats_engine import compute_column_stats
from .table_classifier import classify_table


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_STRING_TYPE_MARKERS = ("string", "object", "varchar", "str", "char")

_DEDUCT_ERROR = 0.05
_DEDUCT_WARNING = 0.02
_DEDUCT_HIGH_NULLS = 0.10
_DEDUCT_NO_GRAIN = 0.05

# Maximum rows for early pandas materialization (performance optimization).
# Tables below this threshold are converted to pandas up-front, eliminating
# all per-column Spark action overhead. 500K rows × 100 cols ≈ 200-400MB
# which fits comfortably in standard driver memory.
_PANDAS_MATERIALIZE_THRESHOLD = 500_000
_PANDAS_MATERIALIZE_MAX_COLUMNS = 100

# For pattern-detection steps (cross-column, duplicate forensics), sample the
# pandas DF when it exceeds this row count. Statistical patterns (co-null at
# 90%+, functional dependencies) are equally detectable in a 20K-row sample.
_PATTERN_DETECTION_SAMPLE = 20_000

_OUTLIER_MIN_NON_NULL = 20
_OUTLIER_MIN_PCT = 0.01
_OUTLIER_IQR_MULTIPLIER = 1.5
_OUTLIER_EXTREME_LIMIT = 5


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def profile_table(
    df: Any,
    subject: str,
    level: str = ProfilingLevel.STANDARD.value,
    **opts: Any,
) -> TableProfile:
    """Profile a DataFrame and return a fully populated TableProfile.

    Orchestrates all profiling sub-modules in order:
    1. Column statistics (all levels)
    2. Semantic type inference on string columns (all levels)
    3. Role classification (all levels)
    4. Grain detection (standard+)
    5. Freshness detection (standard+)
    6. Table classification (standard+)
    7. Format issue detection (standard+)
    8. Cleanliness audit (standard+)

    Degradation: each step is wrapped in try/except; failures add the step
    name to degraded_features without crashing the profile.

    Args:
        df: Input pandas or Spark DataFrame.
        subject: Identifying label (e.g. catalog.schema.table).
        level: Profiling depth â 'quick', 'standard', or 'deep'.
        **opts: Reserved for future extension.

    Returns:
        Fully populated TableProfile.
    """
    started_at = time.monotonic()
    profiled_at = datetime.now(timezone.utc).isoformat()
    degraded_features: list[str] = []
    degradation_reasons: dict[str, str] = {}
    step_timings: dict[str, int] = {}

    valid_levels = {ProfilingLevel.QUICK.value, ProfilingLevel.STANDARD.value, ProfilingLevel.DEEP.value}
    if level not in valid_levels:
        raise ValueError(
            f"Invalid profiling level '{level}'. Must be one of: {sorted(valid_levels)}"
        )

    is_standard_plus = level in (
        ProfilingLevel.STANDARD.value,
        ProfilingLevel.DEEP.value,
    )

    # Shape
    engine = detect_engine(df)
    row_count, column_count = _get_shape(df, engine)

    # ------------------------------------------------------------------
    # Performance optimization: early materialization
    # ------------------------------------------------------------------
    # For tables that fit in driver memory, convert to pandas ONCE up-front.
    # This eliminates all per-column Spark round-trips (the dominant cost).
    # All downstream functions call detect_engine() and use fast local paths.
    _spark_schema: dict[str, str] = {}
    _t = time.monotonic()
    _safe_to_materialize = (
        row_count <= _PANDAS_MATERIALIZE_THRESHOLD
        and column_count <= _PANDAS_MATERIALIZE_MAX_COLUMNS
    )
    if engine == "spark" and _safe_to_materialize:
        _spark_schema = {
            f.name: f.dataType.simpleString() for f in df.schema.fields
        }
        df = df.toPandas()
        engine = "pandas"
    step_timings["materialize"] = int((time.monotonic() - _t) * 1000)

    # ------------------------------------------------------------------
    # Step 1: Column statistics
    # ------------------------------------------------------------------
    profiles: list[ColumnProfile] = []
    _t = time.monotonic()
    try:
        profiles = compute_column_stats(df)
    except Exception as exc:  # noqa: BLE001
        degraded_features.append("column_stats")
        degradation_reasons["column_stats"] = str(exc)
    step_timings["column_stats"] = int((time.monotonic() - _t) * 1000)

    # Restore original Spark type names when we materialized early
    if _spark_schema:
        for profile in profiles:
            if profile.name in _spark_schema:
                profile.spark_type = _spark_schema[profile.name]

    # ------------------------------------------------------------------
    # Step 2: Semantic type enrichment (string cols only)
    # ------------------------------------------------------------------
    _t = time.monotonic()
    for profile in profiles:
        if not _is_string_col(profile):
            continue
        try:
            sem_inf = infer_semantic_type(profile.sample_values, profile.name)
            profile.semantic_type = sem_inf.value
            profile.semantic_type_inference = sem_inf

            # Fallback enum detection: if semantic typer returned UNKNOWN but
            # the column has very low cardinality from full-table stats, mark
            # as ENUM. This handles skewed distributions where a tiny sample
            # may not represent all distinct values.
            if (profile.semantic_type == SemanticType.UNKNOWN
                    and profile.distinct_count is not None
                    and 2 <= profile.distinct_count <= 20
                    and profile.distinct_pct < 0.05):
                profile.semantic_type = SemanticType.ENUM
                profile.semantic_type_inference = Inference(
                    value=SemanticType.ENUM,
                    confidence=0.80,
                    evidence=[
                        f"distinct_count={profile.distinct_count}",
                        f"distinct_pct={profile.distinct_pct:.3f}",
                        "stats_based_enum_fallback",
                    ],
                    counter_signals=["sample_too_small_for_pattern_detection"],
                    sample_size=profile.row_count,
                    method="cardinality_heuristic",
                )
        except Exception as exc:  # noqa: BLE001
            degraded_features.append(f"semantic_type:{profile.name}")
            degradation_reasons[f"semantic_type:{profile.name}"] = str(exc)
    step_timings["semantic_type"] = int((time.monotonic() - _t) * 1000)

    # ------------------------------------------------------------------
    # Step 3: Role classification
    # ------------------------------------------------------------------
    _t = time.monotonic()
    for profile in profiles:
        try:
            role_inf = infer_column_role(profile)
            profile.role = role_inf.value
            profile.role_inference = role_inf
        except Exception as exc:  # noqa: BLE001
            degraded_features.append(f"role:{profile.name}")
            degradation_reasons[f"role:{profile.name}"] = str(exc)
    step_timings["role_classification"] = int((time.monotonic() - _t) * 1000)

    # ------------------------------------------------------------------
    # Steps 4â8: Standard+ only
    # ------------------------------------------------------------------
    grain = GrainAnalysis()
    freshness: FreshnessAnalysis | None = None
    classification_inference: Inference | None = None
    all_format_issues: list[FormatIssue] = []
    duplicate_forensics_result: DuplicateForensics | None = None
    cross_col_result = None
    stability_result = None
    outlier_profiles: list[OutlierProfile] = []

    if is_standard_plus:
        # Step 4: Grain
        _t = time.monotonic()
        try:
            grain = detect_grain(df, profiles)
        except Exception as exc:  # noqa: BLE001
            degraded_features.append("grain")
            degradation_reasons["grain"] = str(exc)
        step_timings["grain"] = int((time.monotonic() - _t) * 1000)

        # Step 5: Freshness
        _t = time.monotonic()
        try:
            freshness = detect_freshness(df, profiles)
        except Exception as exc:  # noqa: BLE001
            degraded_features.append("freshness")
            degradation_reasons["freshness"] = str(exc)
        step_timings["freshness"] = int((time.monotonic() - _t) * 1000)

        # Step 6: Classification
        _t = time.monotonic()
        try:
            classification_inference = classify_table(
                profiles, grain=grain, freshness=freshness
            )
        except Exception as exc:  # noqa: BLE001
            degraded_features.append("classification")
            degradation_reasons["classification"] = str(exc)
        step_timings["classification"] = int((time.monotonic() - _t) * 1000)

        # Step 7: Format issues
        _t = time.monotonic()
        try:
            all_format_issues.extend(detect_format_issues(df, profiles))
        except Exception as exc:  # noqa: BLE001
            degraded_features.append("format_issues")
            degradation_reasons["format_issues"] = str(exc)
        step_timings["format_issues"] = int((time.monotonic() - _t) * 1000)

        # Step 8: Cleanliness
        _t = time.monotonic()
        try:
            all_format_issues.extend(audit_cleanliness(df, profiles))
        except Exception as exc:  # noqa: BLE001
            degraded_features.append("cleanliness")
            degradation_reasons["cleanliness"] = str(exc)
        step_timings["cleanliness"] = int((time.monotonic() - _t) * 1000)

        # Step 9: Duplicate forensics
        _t = time.monotonic()
        try:
            duplicate_forensics_result = analyze_duplicates(
                df, grain, profiles, freshness
            )
        except Exception as exc:  # noqa: BLE001
            duplicate_forensics_result = None
            degraded_features.append("duplicate_forensics")
            degradation_reasons["duplicate_forensics"] = str(exc)
        step_timings["duplicate_forensics"] = int((time.monotonic() - _t) * 1000)

        # Step 10: String length analysis
        _t = time.monotonic()
        try:
            all_format_issues.extend(detect_string_length_issues(profiles))
        except Exception as exc:  # noqa: BLE001
            degraded_features.append("string_length")
            degradation_reasons["string_length"] = str(exc)
        step_timings["string_length"] = int((time.monotonic() - _t) * 1000)

        # Step 11: Numeric outlier context
        _t = time.monotonic()
        try:
            outlier_profiles = _detect_numeric_outliers(df, profiles, engine)
        except Exception as exc:  # noqa: BLE001
            outlier_profiles = []
            degraded_features.append("outlier_detection")
            degradation_reasons["outlier_detection"] = str(exc)
        step_timings["outlier_detection"] = int((time.monotonic() - _t) * 1000)

        # Step 12: Cross-column dependencies
        # Use sampled DF for pattern detection on large tables (preserves
        # statistical patterns while avoiding O(n*pairs) groupBy cost).
        _t = time.monotonic()
        _df_patterns = df
        if engine == "pandas" and hasattr(df, "__len__") and len(df) > _PATTERN_DETECTION_SAMPLE:
            _df_patterns = df.sample(n=_PATTERN_DETECTION_SAMPLE, random_state=42)
        try:
            cross_col_result = detect_cross_column_dependencies(_df_patterns, profiles)
            # Populate ColumnProfile.correlated_nulls
            if cross_col_result and cross_col_result.co_null_patterns:
                for pattern in cross_col_result.co_null_patterns:
                    for p in profiles:
                        if p.name == pattern.target_column:
                            if pattern.driver_column not in p.correlated_nulls:
                                p.correlated_nulls.append(pattern.driver_column)
        except Exception as exc:  # noqa: BLE001
            cross_col_result = None
            degraded_features.append("cross_column")
            degradation_reasons["cross_column"] = str(exc)
        step_timings["cross_column"] = int((time.monotonic() - _t) * 1000)

        # Step 13: Value stability analysis
        _t = time.monotonic()
        try:
            _classification = (
                classification_inference.value
                if classification_inference is not None
                else TableClassification.UNKNOWN
            )
            stability_result = analyze_value_stability(
                df, grain, profiles, _classification
            )
        except Exception as exc:  # noqa: BLE001
            stability_result = None
            degraded_features.append("value_stability")
            degradation_reasons["value_stability"] = str(exc)
        step_timings["value_stability"] = int((time.monotonic() - _t) * 1000)

    if not is_standard_plus:
        grain = None  # type: ignore  — not evaluated, not 'no grain found'

    # ------------------------------------------------------------------
    # Attach format issues to column profiles
    # ------------------------------------------------------------------
    col_issues: dict[str, list[FormatIssue]] = {}
    for issue in all_format_issues:
        col_issues.setdefault(issue.column, []).append(issue)

    for profile in profiles:
        profile.format_issues = col_issues.get(profile.name, [])
        issue_types = {i.issue_type for i in profile.format_issues}
        if "leading_spaces" in issue_types:
            profile.has_leading_spaces = True
        if "trailing_spaces" in issue_types:
            profile.has_trailing_spaces = True
        if "mixed_case_enum" in issue_types:
            profile.has_mixed_case = True

    # ------------------------------------------------------------------
    # Optional: Join profiling (when reference_tables provided)
    # ------------------------------------------------------------------
    join_profiles: list[JoinProfile] = []
    reference_tables = opts.get("reference_tables")
    if reference_tables:
        try:
            join_profiles = profile_joins(df, reference_tables, profiles)
        except Exception as exc:  # noqa: BLE001
            degraded_features.append("join_profiling")
            degradation_reasons["join_profiling"] = str(exc)

    # ------------------------------------------------------------------
    # Quality score
    # ------------------------------------------------------------------
    quality_score = _compute_quality_score(
        profiles=profiles,
        grain=grain,
        all_issues=all_format_issues,
    )

    # ------------------------------------------------------------------
    # Findings
    # ------------------------------------------------------------------
    findings = _build_findings(
        classification_inference=classification_inference,
        grain=grain,
        duplicate_forensics=duplicate_forensics_result,
        freshness=freshness,
        all_issues=all_format_issues,
        degraded_features=degraded_features,
        profiles=profiles,
        cross_column=cross_col_result,
        value_stability=stability_result,
        outliers=outlier_profiles,
        join_profiles=join_profiles,
    )

    # ------------------------------------------------------------------
    # Unpack classification inference
    # ------------------------------------------------------------------
    classification = TableClassification.UNKNOWN
    classification_confidence = 0.0
    classification_reasoning = ""
    classification_evidence: list[str] = []
    classification_counter_signals: list[str] = []
    if classification_inference is not None:
        classification = classification_inference.value
        classification_confidence = classification_inference.confidence
        classification_reasoning = (
            classification_inference.evidence[0]
            if classification_inference.evidence else ""
        )
        classification_evidence = classification_inference.evidence
        classification_counter_signals = classification_inference.counter_signals

    duration_ms = int((time.monotonic() - started_at) * 1000)

    # Synthesize risks directly on the profile (not deferred to serialization)
    risks = _build_profile_risks(all_format_issues, degraded_features, join_profiles=join_profiles, outliers=outlier_profiles, duplicate_forensics=duplicate_forensics_result, freshness=freshness)
    suggested_actions = _build_profile_actions(
        grain, all_format_issues, degraded_features, join_profiles=join_profiles, outliers=outlier_profiles, duplicate_forensics=duplicate_forensics_result, freshness=freshness
    )

    return TableProfile(
        subject=subject,
        classification=classification,
        classification_confidence=classification_confidence,
        classification_reasoning=classification_reasoning,
        classification_evidence=classification_evidence,
        classification_counter_signals=classification_counter_signals,
        classification_inference=classification_inference,
        row_count=row_count,
        column_count=column_count,
        grain=grain,
        duplicate_forensics=duplicate_forensics_result,
        freshness=freshness,
        columns=profiles,
        joins=join_profiles,
        format_issues=all_format_issues,
        outliers=outlier_profiles,
        overall_quality_score=quality_score,
        quality_summary=_quality_label(quality_score),
        profiling_level=level,
        profiling_duration_ms=duration_ms,
        step_timings=step_timings,
        profiled_at=profiled_at,
        findings=findings,
        risks=risks,
        suggested_actions=suggested_actions,
        degraded_features=degraded_features,
        degradation_reasons=degradation_reasons,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _build_profile_risks(
    format_issues: list[FormatIssue],
    degraded_features: list[str],
    join_profiles: list[JoinProfile] | None = None,
    outliers: list[OutlierProfile] | None = None,
    duplicate_forensics: DuplicateForensics | None = None,
    freshness: FreshnessAnalysis | None = None,
) -> list[str]:
    """Synthesize risk strings from profiling results."""
    risks: list[str] = []
    for issue in format_issues:
        if issue.severity == "error":
            risks.append(f"[{issue.column}] {issue.description}")
    if degraded_features:
        risks.append(
            f"Profiling incomplete: {len(degraded_features)} feature(s) "
            f"degraded ({', '.join(degraded_features)})"
        )
    risks.extend(_build_join_risks(join_profiles or []))
    risks.extend(_build_outlier_risks(outliers or []))
    risks.extend(_build_dedup_risks(duplicate_forensics))
    risks.extend(_build_freshness_risks(freshness))
    return risks


def _build_profile_actions(
    grain: GrainAnalysis | None,
    format_issues: list[FormatIssue],
    degraded_features: list[str],
    join_profiles: list[JoinProfile] | None = None,
    outliers: list[OutlierProfile] | None = None,
    duplicate_forensics: DuplicateForensics | None = None,
    freshness: FreshnessAnalysis | None = None,
) -> list[str]:
    """Synthesize suggested action strings from profiling results."""
    actions: list[str] = []
    if grain and not grain.is_unique:
        actions.append(
            f"Investigate duplicates: grain [{'+'.join(grain.best_grain)}] "
            f"has {grain.duplicate_rate:.0%} duplicate rate"
        )
    error_count = sum(1 for i in format_issues if i.severity == "error")
    if error_count:
        actions.append(
            f"Fix {error_count} format error(s) before downstream consumption"
        )
    if degraded_features:
        actions.append(
            "Re-run with level='deep' to attempt recovery of degraded features"
        )
    actions.extend(_build_join_actions(join_profiles or []))
    actions.extend(_build_outlier_actions(outliers or []))
    actions.extend(_build_dedup_actions(duplicate_forensics))
    actions.extend(_build_freshness_actions(freshness))
    if not actions:
        actions.append("Profile looks clean \u2014 safe to proceed with downstream usage")
    return actions


def _get_shape(df: Any, engine: str) -> tuple[int, int]:
    """Return (row_count, column_count)."""
    try:
        if engine == "pandas":
            return len(df), len(df.columns)
        return df.count(), len(df.columns)
    except Exception:  # noqa: BLE001
        return 0, 0


def _is_string_col(profile: ColumnProfile) -> bool:
    """Return True if the column dtype is string-like."""
    t = profile.spark_type.lower()
    return any(marker in t for marker in _STRING_TYPE_MARKERS)


def _is_numeric_profile(profile: ColumnProfile) -> bool:
    """Return True when a column profile represents a numeric dtype."""
    t = profile.spark_type.lower()
    numeric_markers = (
        "int",
        "long",
        "double",
        "float",
        "decimal",
        "numeric",
        "number",
        "short",
        "byte",
    )
    return any(marker in t for marker in numeric_markers) and "bool" not in t


def _compute_quality_score(
    profiles: list[ColumnProfile],
    grain: GrainAnalysis | None,
    all_issues: list[FormatIssue],
) -> float:
    """Compute overall quality score [0.0, 1.0]."""
    score = 1.0

    for issue in all_issues:
        if issue.severity == "error":
            score -= _DEDUCT_ERROR * min(issue.affected_pct, 1.0)
        elif issue.severity == "warning":
            score -= _DEDUCT_WARNING * min(issue.affected_pct, 1.0)

    if profiles:
        avg_null_pct = sum(p.null_pct for p in profiles) / len(profiles)
        if avg_null_pct > 0.20:
            score -= _DEDUCT_HIGH_NULLS

    if grain is not None and not grain.is_unique:
        score -= _DEDUCT_NO_GRAIN

    return round(max(0.0, min(1.0, score)), 4)


def _quality_label(score: float) -> str:
    """Human-readable quality label."""
    if score >= 0.90:
        return "excellent"
    if score >= 0.75:
        return "good"
    if score >= 0.50:
        return "fair"
    return "poor"


def _build_findings(
    classification_inference: Inference | None,
    grain: GrainAnalysis | None,
    freshness: FreshnessAnalysis | None,
    all_issues: list[FormatIssue],
    degraded_features: list[str],
    profiles: list[Any] | None = None,
    duplicate_forensics: Any | None = None,
    cross_column: Any | None = None,
    value_stability: Any | None = None,
    outliers: list[OutlierProfile] | None = None,
    join_profiles: list[JoinProfile] | None = None,
) -> list[str]:
    """Build human-readable findings from profiling results."""
    findings: list[str] = []

    if classification_inference is not None:
        pct = int(classification_inference.confidence * 100)
        findings.append(
            f"Classified as {classification_inference.value.value} "
            f"(confidence: {pct}%)"
        )
        if classification_inference.runner_ups:
            findings.append(
                "Classification alternatives: "
                + _format_inference_runner_ups(classification_inference.runner_ups)
            )
        if classification_inference.verification_hint:
            findings.append(
                f"Classification verification: {classification_inference.verification_hint}"
            )

    if grain is not None:
        if grain.best_grain:
            if grain.is_unique:
                cols = ", ".join(grain.best_grain)
                findings.append(f"Grain: [{cols}] (unique key confirmed)")
            else:
                findings.append(
                    f"No unique grain found ({grain.duplicate_rate:.1%} duplicate rate)"
                )
        else:
            findings.append("Grain: no candidate key identified")

    if freshness is not None:
        cadence = freshness.cadence or "unknown cadence"
        findings.append(f"Freshness: {freshness.staleness} ({cadence})")

    if all_issues:
        n_errors = sum(1 for i in all_issues if i.severity == "error")
        n_warnings = sum(1 for i in all_issues if i.severity == "warning")
        cols_affected = len({i.column for i in all_issues})
        parts = []
        if n_errors:
            parts.append(f"{n_errors} error(s)")
        if n_warnings:
            parts.append(f"{n_warnings} warning(s)")
        findings.append(
            f"Quality issues: {', '.join(parts)} across {cols_affected} column(s)"
        )

    if degraded_features:
        top = degraded_features[:3]
        findings.append(f"Degraded features: {', '.join(top)}")

    findings.extend(_build_null_forensics_findings(cross_column))
    findings.extend(_build_outlier_findings(outliers or []))

    # Cross-column dependency findings
    if cross_column is not None and hasattr(cross_column, "findings"):
        for finding in cross_column.findings[:2]:
            if not str(finding).startswith("Co-null pattern:"):
                findings.append(finding)

    # Value stability findings
    if value_stability is not None and hasattr(value_stability, "findings"):
        for finding in value_stability.findings[:2]:
            findings.append(finding)

    # Duplicate forensics verdict
    if duplicate_forensics is not None:
        verdict = duplicate_forensics.verdict
        if verdict == "snapshot_design":
            findings.append(
                f"Duplicates ({duplicate_forensics.duplicate_rate:.1%}): "
                f"snapshot design — expected duplication"
            )
        elif verdict == "dedup_needed":
            conc = (
                duplicate_forensics.time_concentration_description
                or duplicate_forensics.source_concentration_description
                or ""
            )
            findings.append(
                f"Duplicates ({duplicate_forensics.duplicate_rate:.1%}): "
                f"dedup needed — {conc}"
            )
        elif verdict == "partial_overlap":
            findings.append(
                f"Duplicates ({duplicate_forensics.duplicate_rate:.1%}): "
                f"partial overlap — high dup rate, investigate"
            )

    ambiguity_findings = _collect_ambiguity_findings(
        classification_inference,
        grain,
        profiles or [],
    )
    findings.extend(ambiguity_findings)

    # F19-F24: Extra findings (monotonicity, concentration, empty cols, ordering, sparsity)
    if profiles:
        extra = detect_extra_findings(profiles, profiles[0].row_count if profiles else 0)
        findings.extend(extra)

    # Periodic partition signal: value-range evidence for year+month/quarter columns
    periodic = _detect_periodic_partition(profiles or [])
    if periodic:
        findings.append(periodic)

    # Surface join-readiness signals (Phase 3: decision-ready output)
    findings.extend(_build_join_findings(join_profiles or []))

    return findings


def _build_null_forensics_findings(cross_column: Any | None) -> list[str]:
    """Build explicit null-forensics findings from co-null patterns."""
    if cross_column is None or not getattr(cross_column, "co_null_patterns", None):
        return []

    findings: list[str] = []
    for pattern in cross_column.co_null_patterns[:2]:
        findings.append(
            f"Null forensics: {pattern.target_column} is "
            f"{pattern.null_rate_when_driven:.0%} null when "
            f"{pattern.driver_column}='{pattern.driver_value}' "
            f"({pattern.affected_rows:,} rows)"
        )
    return findings


def _build_outlier_findings(outliers: list[OutlierProfile]) -> list[str]:
    """Build concise outlier findings for the profile summary."""
    findings: list[str] = []
    for outlier in outliers[:2]:
        findings.append(
            f"Outlier context: {outlier.column} has {outlier.outlier_count} "
            f"value(s) outside [{_format_numeric(outlier.lower_bound)}, "
            f"{_format_numeric(outlier.upper_bound)}] ({outlier.outlier_pct:.1%})"
        )
    return findings


# Staleness threshold: tables not updated in >48h are flagged as stale.
_STALE_THRESHOLD_HOURS = 48.0


def _build_freshness_risks(freshness: FreshnessAnalysis | None) -> list[str]:
    """Build a staleness risk when data has not been refreshed recently.

    Triggers when staleness_hours exceeds _STALE_THRESHOLD_HOURS (48h).
    Surfaces the already-computed FreshnessAnalysis.staleness string so
    downstream agents see data-currency hazards in the standard risk field.

    Args:
        freshness: FreshnessAnalysis from detect_freshness, or None if no
                   temporal column was found.

    Returns:
        Zero or one risk string.
    """
    if freshness is None:
        return []
    if freshness.staleness_hours < _STALE_THRESHOLD_HOURS:
        return []
    col  = freshness.freshness_column
    hrs  = freshness.staleness_hours
    desc = freshness.staleness
    return [
        f"[{col}] stale — last load {desc} ({hrs:.0f}h ago)"
    ]


def _build_freshness_actions(freshness: FreshnessAnalysis | None) -> list[str]:
    """Build a freshness-investigation action for stale tables.

    Triggers on the same _STALE_THRESHOLD_HOURS threshold as
    _build_freshness_risks, so risk and action always appear together.

    Args:
        freshness: FreshnessAnalysis from detect_freshness, or None.

    Returns:
        Zero or one action string.
    """
    if freshness is None:
        return []
    if freshness.staleness_hours < _STALE_THRESHOLD_HOURS:
        return []
    col  = freshness.freshness_column
    desc = freshness.staleness
    return [
        f"Investigate data freshness: '{col}' last updated {desc} — check if pipeline is broken"
    ]


def _build_dedup_risks(duplicate_forensics: DuplicateForensics | None) -> list[str]:
    """Build risk strings when duplicate forensics detect actionable duplication.

    Emits a risk for `dedup_needed` and `partial_overlap` verdicts only.
    `snapshot_design` is expected duplication — no risk emitted.
    `unknown` is ambiguous — skipped to avoid noise.

    Args:
        duplicate_forensics: DuplicateForensics result from analyze_duplicates.

    Returns:
        Zero or one risk string.
    """
    if duplicate_forensics is None:
        return []
    verdict = duplicate_forensics.verdict
    if verdict not in ("dedup_needed", "partial_overlap"):
        return []
    grain = "+".join(duplicate_forensics.grain_columns)
    pct = duplicate_forensics.duplicate_rate
    label = "dedup needed" if verdict == "dedup_needed" else "cause unclear"
    return [
        f"[{grain}] {pct:.0%} duplicate rate — {label}"
    ]


def _build_dedup_actions(duplicate_forensics: DuplicateForensics | None) -> list[str]:
    """Build a deduplication suggested action for actionable duplicate verdicts.

    Complements the grain-level 'Investigate duplicates' action with a forensics-driven
    deduplication directive. Emits only for `dedup_needed` and `partial_overlap` verdicts.

    Args:
        duplicate_forensics: DuplicateForensics result from analyze_duplicates.

    Returns:
        Zero or one action string.
    """
    if duplicate_forensics is None:
        return []
    verdict = duplicate_forensics.verdict
    if verdict not in ("dedup_needed", "partial_overlap"):
        return []
    grain = "+".join(duplicate_forensics.grain_columns)
    pct = duplicate_forensics.duplicate_rate
    return [
        f"Deduplicate [{grain}] before downstream consumption ({pct:.0%} rate)"
    ]


def _build_outlier_risks(outliers: list[OutlierProfile]) -> list[str]:
    """Build risk strings for columns with IQR-detected outliers.

    Surfaces already-computed OutlierProfile results into TableProfile.risks
    so downstream agents see outlier hazards in the standard output fields.

    Args:
        outliers: List of OutlierProfile objects computed by _detect_numeric_outliers.

    Returns:
        One risk string per outlier profile.
    """
    risks: list[str] = []
    for op in outliers:
        risks.append(
            f"[{op.column}] {op.outlier_count} outlier(s) ({op.outlier_pct:.1%}) "
            f"outside IQR bounds \u2014 may skew aggregations"
        )
    return risks


def _build_outlier_actions(outliers: list[OutlierProfile]) -> list[str]:
    """Build remediation actions for columns with IQR-detected outliers.

    Args:
        outliers: List of OutlierProfile objects from _detect_numeric_outliers.

    Returns:
        One suggested action string per outlier profile.
    """
    actions: list[str] = []
    for op in outliers:
        actions.append(
            f"Investigate or cap outliers in '{op.column}' before aggregating "
            f"({op.outlier_count} value(s), {op.outlier_pct:.1%})"
        )
    return actions


def _build_join_findings(join_profiles: list[JoinProfile]) -> list[str]:
    """Build join-readiness findings, one entry per join profile.

    Surfaces already-computed JoinProfile results into the standard
    TableProfile.findings list for downstream decision-ready consumption.

    Args:
        join_profiles: List of JoinProfile objects computed by join_profiler.

    Returns:
        One finding string per JoinProfile summarising overlap and join type.
    """
    findings: list[str] = []
    for jp in join_profiles:
        join_label = f"{jp.source_column} -> {jp.target_table}.{jp.target_column}"
        overlap_str = f"{jp.overlap_pct:.0%}"
        base = (
            f"Join ready: [{join_label}] overlap {overlap_str}, "
            f"safe join type: {jp.safe_join_type}"
        )
        if not jp.format_compatible and jp.format_mismatch_description:
            base += f" (format warning: {jp.format_mismatch_description})"
        findings.append(base)
    return findings


def _build_join_risks(join_profiles: list[JoinProfile]) -> list[str]:
    """Build risk strings for orphan rows, format incompatibility, and fan-out cardinality.

    Args:
        join_profiles: List of JoinProfile objects from join_profiler.

    Returns:
        Risk strings for any join hazards detected across all join profiles.
    """
    risks: list[str] = []
    for jp in join_profiles:
        join_label = f"{jp.source_column} -> {jp.target_table}"
        # Orphan risk: source values with no match in target cause silent row loss on INNER JOIN
        if jp.orphan_count > 0:
            risks.append(
                f"[{join_label}] {jp.orphan_count} orphan row(s) "
                f"({jp.orphan_pct:.0%}) \u2014 INNER JOIN will silently drop rows"
            )
        # Format risk: case/whitespace/dash mismatch prevents key matching
        if not jp.format_compatible:
            desc = jp.format_mismatch_description or "format mismatch detected"
            risks.append(f"[{join_label}] format incompatibility: {desc}")
        # Fan-out risk: non-unique target side multiplies source rows
        if jp.cardinality in ("many:many", "1:many"):
            risks.append(
                f"[{join_label}] cardinality={jp.cardinality} "
                f"\u2014 fan-out risk: join may multiply rows"
            )
    return risks


def _build_join_actions(join_profiles: list[JoinProfile]) -> list[str]:
    """Build remediation actions for join issues.

    Covers: LEFT JOIN recommendation (orphans), format normalisation,
    and uniqueness validation to prevent row fan-out.

    Args:
        join_profiles: List of JoinProfile objects from join_profiler.

    Returns:
        Suggested action strings for any join hazards detected.
    """
    actions: list[str] = []
    for jp in join_profiles:
        join_label = f"{jp.source_column} -> {jp.target_table}.{jp.target_column}"
        # Orphan remedy: use LEFT JOIN to preserve all source rows
        if jp.orphan_count > 0:
            actions.append(
                f"Use LEFT JOIN on [{join_label}] to preserve "
                f"{jp.orphan_count} orphan row(s)"
            )
        # Format remedy: normalise keys before joining
        if not jp.format_compatible:
            desc = jp.format_mismatch_description or "format mismatch"
            actions.append(
                f"Normalise formats before joining [{join_label}]: {desc}"
            )
        # Fan-out remedy: validate target-side uniqueness
        if jp.cardinality in ("many:many", "1:many"):
            actions.append(
                f"Validate uniqueness of {jp.target_table}.{jp.target_column} "
                f"before joining to avoid row fan-out ({jp.cardinality})"
            )
    return actions

def _detect_numeric_outliers(
    df: Any,
    profiles: list[ColumnProfile],
    engine: str,
) -> list[OutlierProfile]:
    """Detect IQR-based outliers for numeric columns."""
    outliers: list[OutlierProfile] = []
    for profile in profiles:
        if not _is_numeric_profile(profile):
            continue
        result = _detect_outlier_profile_pandas(df, profile)
        if engine == "spark":
            result = _detect_outlier_profile_spark(df, profile)
        if result is not None:
            outliers.append(result)
    return outliers


def _detect_outlier_profile_pandas(
    df: Any,
    profile: ColumnProfile,
) -> OutlierProfile | None:
    """Return an outlier profile for a pandas-backed numeric column."""
    numeric_series = pd.Series(
        pd.to_numeric(df[profile.name], errors="coerce"),
        dtype="float64",
    ).dropna()
    if len(numeric_series) < _OUTLIER_MIN_NON_NULL:
        return None

    q1 = float(numeric_series.quantile(0.25))
    q3 = float(numeric_series.quantile(0.75))
    iqr = q3 - q1
    if iqr <= 0:
        return None

    lower = q1 - (_OUTLIER_IQR_MULTIPLIER * iqr)
    upper = q3 + (_OUTLIER_IQR_MULTIPLIER * iqr)
    mask = (numeric_series < lower) | (numeric_series > upper)
    outlier_values = numeric_series[mask]
    outlier_count = int(outlier_values.shape[0])
    if outlier_count == 0:
        return None

    outlier_pct = outlier_count / float(numeric_series.shape[0])
    if outlier_pct < _OUTLIER_MIN_PCT:
        return None

    counts = outlier_values.value_counts().head(_OUTLIER_EXTREME_LIMIT)
    extreme_values = [
        {"value": _normalize_numeric_value(value), "count": int(count)}
        for value, count in counts.items()
    ]
    context = _build_outlier_context(q1, q3, lower, upper, outlier_count)
    return OutlierProfile(
        column=profile.name,
        method="iqr",
        lower_bound=round(lower, 4),
        upper_bound=round(upper, 4),
        outlier_count=outlier_count,
        outlier_pct=round(outlier_pct, 4),
        extreme_values=extreme_values,
        context=context,
    )


def _detect_outlier_profile_spark(
    df: Any,
    profile: ColumnProfile,
) -> OutlierProfile | None:
    """Return an outlier profile for a Spark-backed numeric column."""
    if F is None:
        return None

    numeric_df = (
        df.select(F.col(profile.name).cast("double").alias(profile.name))
        .where(F.col(profile.name).isNotNull())
    )
    non_null_count = int(numeric_df.count())
    if non_null_count < _OUTLIER_MIN_NON_NULL:
        return None

    q1, q3 = numeric_df.approxQuantile(profile.name, [0.25, 0.75], 0.01)
    iqr = q3 - q1
    if iqr <= 0:
        return None

    lower = q1 - (_OUTLIER_IQR_MULTIPLIER * iqr)
    upper = q3 + (_OUTLIER_IQR_MULTIPLIER * iqr)
    outlier_df = numeric_df.where(
        (F.col(profile.name) < lower) | (F.col(profile.name) > upper)
    )
    outlier_count = int(outlier_df.count())
    if outlier_count == 0:
        return None

    outlier_pct = outlier_count / float(non_null_count)
    if outlier_pct < _OUTLIER_MIN_PCT:
        return None

    extreme_rows = (
        outlier_df.groupBy(profile.name)
        .count()
        .orderBy(F.desc("count"), F.desc(profile.name))
        .limit(_OUTLIER_EXTREME_LIMIT)
        .collect()
    )
    extreme_values = [
        {
            "value": _normalize_numeric_value(row[profile.name]),
            "count": int(row["count"]),
        }
        for row in extreme_rows
    ]
    context = _build_outlier_context(q1, q3, lower, upper, outlier_count)
    return OutlierProfile(
        column=profile.name,
        method="iqr",
        lower_bound=round(lower, 4),
        upper_bound=round(upper, 4),
        outlier_count=outlier_count,
        outlier_pct=round(outlier_pct, 4),
        extreme_values=extreme_values,
        context=context,
    )


def _build_outlier_context(
    q1: float,
    q3: float,
    lower: float,
    upper: float,
    outlier_count: int,
) -> str:
    """Describe the typical numeric range and outlier bounds."""
    return (
        f"Typical values cluster between {_format_numeric(q1)} and "
        f"{_format_numeric(q3)}, but {outlier_count} value(s) fall outside "
        f"the IQR bounds [{_format_numeric(lower)}, {_format_numeric(upper)}]."
    )


def _normalize_numeric_value(value: Any) -> int | float:
    """Normalize numeric values for serialization and testing."""
    numeric_value = float(value)
    if numeric_value.is_integer():
        return int(numeric_value)
    return round(numeric_value, 4)


def _format_numeric(value: float) -> str:
    """Format a numeric value compactly for findings."""
    formatted = f"{value:,.2f}"
    return formatted.rstrip("0").rstrip(".")


def _format_runner_up_value(value: Any) -> str:
    """Render enum-backed runner-up values compactly for findings."""

    if hasattr(value, "value"):
        return str(value.value)
    return str(value)



def _format_inference_runner_ups(items: list[Any], limit: int = 2) -> str:
    """Format runner-up hypotheses for concise ambiguity findings."""

    parts: list[str] = []
    for item in items[:limit]:
        parts.append(
            f"{_format_runner_up_value(getattr(item, 'value', item))} "
            f"({float(getattr(item, 'confidence', 0.0)):.0%})"
        )
    return "; ".join(parts)



def _format_grain_runner_ups(items: list[dict[str, Any]], limit: int = 2) -> str:
    """Format alternate grain candidates for concise findings."""

    parts: list[str] = []
    for item in items[:limit]:
        columns = "+".join(str(v) for v in item.get("columns", [])) or "unknown"
        parts.append(
            f"{columns} (dup={float(item.get('duplicate_rate', 0.0)):.1%})"
        )
    return "; ".join(parts)



def _collect_ambiguity_findings(
    classification_inference: Inference | None,
    grain: GrainAnalysis | None,
    profiles: list[Any],
    limit: int = 3,
) -> list[str]:
    """Collect bounded ambiguity findings for non-renderer consumers."""

    findings: list[str] = []

    if grain is not None:
        if grain.runner_up_grains:
            findings.append(
                f"Grain alternatives: {_format_grain_runner_ups(grain.runner_up_grains)}"
            )
        if grain.verification_hint:
            findings.append(f"Grain verification: {grain.verification_hint}")

    column_findings: list[str] = []
    for profile in profiles:
        if len(column_findings) >= limit:
            break

        semantic = getattr(profile, "semantic_type_inference", None)
        if semantic and (
            semantic.runner_ups or semantic.verification_hint or semantic.blocker_reason
        ):
            details: list[str] = []
            if semantic.runner_ups:
                details.append(
                    f"semantic alternatives: {_format_inference_runner_ups(semantic.runner_ups)}"
                )
            if semantic.verification_hint:
                details.append(f"verify {semantic.verification_hint}")
            if semantic.blocker_reason:
                details.append(semantic.blocker_reason)
            column_findings.append(f"Column {profile.name} — " + " | ".join(details[:3]))
            continue

        role = getattr(profile, "role_inference", None)
        if role and (role.runner_ups or role.verification_hint or role.blocker_reason):
            details = []
            if role.runner_ups:
                details.append(
                    f"role alternatives: {_format_inference_runner_ups(role.runner_ups)}"
                )
            if role.verification_hint:
                details.append(f"verify {role.verification_hint}")
            if role.blocker_reason:
                details.append(role.blocker_reason)
            column_findings.append(f"Column {profile.name} — " + " | ".join(details[:3]))

    findings.extend(column_findings)
    return findings



def _detect_periodic_partition(profiles: list[Any]) -> str | None:
    """Return a finding string if value-range evidence suggests periodic snapshots.

    Scans for integer columns whose value distribution matches a year range
    (2000-2040) paired with a month (1-12) or quarter (1-4) range.  This is
    project-agnostic: it works regardless of column naming convention.

    When multiple candidates exist for a category, prefer the one whose name
    contains a temporal hint (e.g. "year", "month", "quarter") over arbitrary
    numeric columns that happen to fall in the same value range.

    Args:
        profiles: List of ColumnProfile objects.

    Returns:
        A human-readable finding string, or None if no evidence found.
    """
    _YEAR_MIN  = 2000
    _YEAR_MAX  = 2040
    _YEAR_DISTINCT = 30
    _INT_TYPES = ("long", "integer", "int", "bigint", "smallint", "short")

    _YEAR_HINTS    = ("year", "yr", "yyyy", "fy", "annum")
    _MONTH_HINTS   = ("month", "mth", "mnth", "period")
    _QUARTER_HINTS = ("quarter", "qtr", "fq")

    year_candidates: list[str] = []
    month_candidates: list[str] = []
    quarter_candidates: list[str] = []

    for p in profiles:
        base_type = getattr(p, "spark_type", "").lower().split("[")[0].strip()
        if not any(t in base_type for t in _INT_TYPES):
            continue

        mn = getattr(p, "min_value", None)
        mx = getattr(p, "max_value", None)
        dc = getattr(p, "distinct_count", None)
        if mn is None or mx is None:
            continue

        try:
            mn_f, mx_f = float(mn), float(mx)
        except (TypeError, ValueError):
            continue

        # Year candidate: 2000-2040, at most 30 distinct values
        if (_YEAR_MIN <= mn_f and mx_f <= _YEAR_MAX
                and (dc is None or dc <= _YEAR_DISTINCT)):
            year_candidates.append(p.name)
            continue

        # Quarter candidate: 1-4, at most 4 distinct values (check BEFORE month — tighter range)
        if mn_f >= 1 and mx_f <= 4 and (dc is None or dc <= 4):
            quarter_candidates.append(p.name)
            continue

        # Month candidate: 1-12, at most 12 distinct values
        if mn_f >= 1 and mx_f <= 12 and (dc is None or dc <= 12):
            month_candidates.append(p.name)

    # --- Name-preference tiebreaker: prefer temporal-hinted names ---
    def _pick_best(candidates: list[str], hints: tuple[str, ...]) -> str | None:
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        # Prefer candidate whose lowered name contains any hint
        for c in candidates:
            name_lower = c.lower()
            if any(h in name_lower for h in hints):
                return c
        return candidates[0]  # fallback: first found

    year_col = _pick_best(year_candidates, _YEAR_HINTS)
    month_col = _pick_best(month_candidates, _MONTH_HINTS)
    quarter_col = _pick_best(quarter_candidates, _QUARTER_HINTS)

    if year_col is None:
        return None

    period_col = month_col or quarter_col
    if period_col is None:
        return None

    period_label = "month" if month_col else "quarter"
    return (
        f"Possible periodic snapshot: year_col={year_col} + "
        f"{period_label}_col={period_col} detected "
        f"\u2014 consider whether rows represent incremental snapshots"
    )
