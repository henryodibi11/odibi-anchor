"""Quality gate context generator.

Pre-write safety checks for DataFrames. Answers "is this data
structurally safe to write downstream?" in one call.

Unlike ``validation_summary_context`` (business-rule validation),
this tool checks structural integrity: duplicate keys, null keys,
schema compatibility, row count sanity, column completeness, and
data freshness.

Typical usage:
    from odibi_anchor.validation.quality_gate_context import (
        quality_gate_context,
        render_quality_gate_report,
    )

    ctx = quality_gate_context(
        df,
        keys=["asset_id"],
        target_schema=target_df.schema,
        freshness_column="updated_at",
        df_name="silver_df",
    )

    if not ctx["metrics"]["is_write_safe"]:
        # fix_all_expr uses your df_name variable
        exec(ctx["fix_all_expr"])

    report = render_quality_gate_report(ctx)
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from odibi_anchor._utils.engine_utils import detect_engine
from odibi_anchor._utils.contract import validate_output_format

# Shared helpers (re-exported so existing internal imports keep working)
from odibi_anchor.validation._quality_gate_helpers import (  # noqa: F401
    _BOOLEAN_TYPES,
    _NUMERIC_TYPES,
    _STRING_TYPES,
    _TEMPORAL_TYPES,
    _build_actions,
    _build_context,
    _build_findings,
    _build_fix_all_expr,
    _build_fix_impact,
    _build_recommendation,
    _build_risks,
    _check_row_count,
    _extract_target_columns,
    _format_age,
    _make_check,
    _pandas_rows_to_dicts,
    _sanitize_dict,
    _sanitize_value,
    _spark_rows_to_dicts,
    _type_category,
)

# Rendering
from odibi_anchor.validation._quality_gate_render import (
    render_quality_gate_report as _render_quality_gate_report_impl,
)


def render_quality_gate_report(ctx: "dict[str, Any]", *, show_samples: bool = True) -> str:
    """Render a quality gate context dict as markdown."""
    return _render_quality_gate_report_impl(ctx, show_samples=show_samples)

# Pandas check functions
from odibi_anchor.validation._quality_gate_checks_pandas import (  # noqa: F401
    _check_completeness_pandas,
    _check_dup_keys_pandas,
    _check_freshness_pandas,
    _check_null_cols_pandas,
    _check_null_keys_pandas,
    _check_schema_pandas,
    _check_type_compat_pandas,
)

# Spark check functions
from odibi_anchor.validation._quality_gate_checks_spark import (  # noqa: F401
    _check_completeness_spark,
    _check_dup_keys_spark,
    _check_freshness_spark,
    _check_null_cols_spark,
    _check_null_keys_spark,
    _check_schema_spark,
    _check_type_compat_spark,
    _gate_spark,
)

logger = logging.getLogger(__name__)

Engine = Literal["auto", "pandas", "spark"]
OutputFormat = Literal["dict", "markdown"]

_ALL_CHECKS = (
    "row_count",
    "duplicate_keys",
    "null_keys",
    "schema_compat",
    "null_columns",
    "completeness",
    "freshness",
)

_DEFAULT_THRESHOLDS: dict[str, Any] = {
    "min_rows": 10,
    "max_staleness_hours": 24,
}


# -------------------------------------------------------------------
# Public API
# -------------------------------------------------------------------


def quality_gate_context(
    df: Any,
    *,
    keys: list[str] | None = None,
    target_schema: Any = None,
    checks: list[str] | None = None,
    subject: str = "quality_gate",
    engine: Engine = "auto",
    sample_limit: int = 10,
    output_format: OutputFormat = "dict",
    freshness_column: str | None = None,
    df_name: str = "df",
    thresholds: dict[str, Any] | None = None,
) -> dict[str, Any] | str:
    """Generate a pre-write quality gate context packet.

    Runs automatic structural checks on a DataFrame and produces
    a binary write-safety decision, per-check results, fix code,
    data loss estimates, and an actionable recommendation.

    Args:
        df: Pandas or Spark DataFrame to check.
        keys: Business key columns. Required for duplicate_keys
            and null_keys checks. If None, those checks are
            skipped.
        target_schema: Expected schema for compatibility check.
            Accepts Spark StructType or a dict mapping column
            names to type strings. If None, schema_compat is
            skipped.
        checks: Subset of check IDs to run. None runs all
            applicable checks. Valid IDs: ``row_count``,
            ``duplicate_keys``, ``null_keys``,
            ``schema_compat``, ``null_columns``,
            ``completeness``, ``freshness``.
        subject: Human-readable label for the dataset.
        engine: Execution engine: ``"auto"``, ``"pandas"``,
            or ``"spark"``.
        sample_limit: Max sample rows per check (default 10).
        output_format: ``"dict"`` or ``"markdown"``.
        freshness_column: Column name containing timestamps
            for freshness check. If None, freshness check is
            skipped.
        df_name: Variable name used in fix expressions.
            Defaults to ``"df"``. Set to your actual variable
            name so fix code is paste-and-run ready.
        thresholds: Override default thresholds. Supported
            keys: ``min_rows`` (int, default 10),
            ``max_staleness_hours`` (int, default 24).

    Returns:
        Dictionary or markdown string depending on
        ``output_format``.

    Raises:
        ValueError: If parameters are invalid.
        TypeError: If df type does not match engine.

    Example:
        >>> import pandas as pd
        >>> df = pd.DataFrame({"id": [1, 1, 3], "v": [10, 20, 30]})
        >>> ctx = quality_gate_context(df, keys=["id"])
        >>> ctx["metrics"]["is_write_safe"]
        False
    """
    _validate_params(
        engine, output_format, sample_limit, checks
    )

    resolved = _resolve_engine(df, engine)
    merged_thresholds = _merge_thresholds(thresholds)

    active_checks = _resolve_checks(
        checks, keys, target_schema, freshness_column
    )

    if resolved == "pandas":
        ctx = _gate_pandas(
            df,
            keys=keys,
            target_schema=target_schema,
            active_checks=active_checks,
            subject=subject,
            sample_limit=sample_limit,
            freshness_column=freshness_column,
            df_name=df_name,
            thresholds=merged_thresholds,
        )
    elif resolved == "spark":
        ctx = _gate_spark(
            df,
            keys=keys,
            target_schema=target_schema,
            active_checks=active_checks,
            subject=subject,
            sample_limit=sample_limit,
            freshness_column=freshness_column,
            df_name=df_name,
            thresholds=merged_thresholds,
        )
    else:
        raise TypeError(
            f"Unsupported engine: {resolved!r}"
        )

    _attach_write_safety_card(ctx)

    if output_format == "markdown":
        return render_quality_gate_report(ctx)
    return ctx


def _attach_write_safety_card(ctx: "dict[str, Any]") -> None:
    """Attach a compact write-safety verdict card (W-1) to a quality result."""
    if not isinstance(ctx, dict):
        return
    m = ctx.setdefault("metrics", {})
    safe = bool(m.get("is_write_safe"))
    failed = [
        c.get("check_id", "?")
        for c in ctx.get("checks", [])
        if isinstance(c, dict) and c.get("status") == "fail"
    ]
    signals = [
        f"checks: {m.get('checks_passed', 0)}/{m.get('checks_run', 0)} passed",
        f"rows: {m.get('total_rows', '?')}",
    ]
    if failed:
        signals.append("failed: " + ", ".join(failed))
    m["verdict"] = "SAFE" if safe else "BLOCKED"
    from odibi_anchor._utils._write_safety import render_write_safety_card
    ctx["write_safety_card"] = render_write_safety_card(
        safe=safe,
        headline="safe to write" if safe else "do NOT write",
        signals=signals,
        fix_hint=None if safe else (
            ctx.get("recommendation")
            or 'Diagnose with anchor("duplicate")/anchor("schema_diff"), fix, then re-run anchor("quality").'
        ),
    )


# -------------------------------------------------------------------
# Private — Parameter validation
# -------------------------------------------------------------------


def _validate_params(
    engine: str,
    output_format: str,
    sample_limit: int,
    checks: list[str] | None,
) -> None:
    """Validate public API parameters."""
    if engine not in {"auto", "pandas", "spark"}:
        raise ValueError(
            "engine must be 'auto', 'pandas', or 'spark'"
        )
    validate_output_format(output_format)
    if sample_limit < 0:
        raise ValueError("sample_limit must be >= 0")
    if checks is not None:
        invalid = set(checks) - set(_ALL_CHECKS)
        if invalid:
            raise ValueError(
                f"Invalid check IDs: {sorted(invalid)}"
            )


def _resolve_engine(df: Any, engine: str) -> str:
    """Resolve engine from df type or explicit param."""
    if engine != "auto":
        return engine
    detected = detect_engine(df)
    if detected == "unknown":
        raise TypeError(
            "Cannot detect engine. Pass engine= "
            "explicitly or use a Pandas/Spark "
            "DataFrame."
        )
    return detected


def _merge_thresholds(
    thresholds: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge user thresholds with defaults."""
    merged = dict(_DEFAULT_THRESHOLDS)
    if thresholds:
        merged.update(thresholds)
    return merged


def _resolve_checks(
    checks: list[str] | None,
    keys: list[str] | None,
    target_schema: Any,
    freshness_column: str | None,
) -> list[str]:
    """Determine which checks to run based on inputs."""
    if checks is not None:
        active = list(checks)
    else:
        active = list(_ALL_CHECKS)

    # Remove checks that require missing inputs
    if keys is None:
        active = [
            c for c in active
            if c not in ("duplicate_keys", "null_keys")
        ]
    if target_schema is None:
        active = [
            c for c in active if c != "schema_compat"
        ]
    if freshness_column is None:
        active = [
            c for c in active if c != "freshness"
        ]
    return active


# -------------------------------------------------------------------
# Private — Pandas engine orchestrator
# -------------------------------------------------------------------


def _gate_pandas(
    df: Any,
    *,
    keys: list[str] | None,
    target_schema: Any,
    active_checks: list[str],
    subject: str,
    sample_limit: int,
    freshness_column: str | None,
    df_name: str,
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    """Run quality gate checks on a pandas DataFrame."""
    total_rows = len(df)
    check_results: list[dict[str, Any]] = []

    for check_id in active_checks:
        if check_id == "row_count":
            result = _check_row_count(
                total_rows, thresholds
            )
        elif check_id == "duplicate_keys":
            result = _check_dup_keys_pandas(
                df, keys, sample_limit, df_name
            )
        elif check_id == "null_keys":
            result = _check_null_keys_pandas(
                df, keys, sample_limit, df_name
            )
        elif check_id == "schema_compat":
            result = _check_schema_pandas(
                df, target_schema, df_name
            )
        elif check_id == "null_columns":
            result = _check_null_cols_pandas(
                df, total_rows, df_name
            )
        elif check_id == "completeness":
            result = _check_completeness_pandas(
                df, total_rows
            )
        elif check_id == "freshness":
            result = _check_freshness_pandas(
                df, freshness_column, thresholds
            )
        else:
            continue
        check_results.append(result)

    return _build_context(
        check_results=check_results,
        total_rows=total_rows,
        subject=subject,
        engine_name="pandas",
        df_name=df_name,
    )
