"""Pre-join validation tool implementation.

Entry point: pre_join_context(left_df, right_df, keys=[...], ...)
Validates two DataFrames are safe to join before joining.
Returns a standard Anchor contract with findings, risks, and suggested_next_actions.
"""
from __future__ import annotations

import unicodedata
from typing import Any

from odibi_anchor._utils.contract import (
    build_base_context,
    finalize_context,
    guard_dataframe_type,
    validate_output_format,
)


# ============================================================================
# Engine detection (inlined from framework utils)
# ============================================================================

def _detect_engine(df: Any) -> str:
    """Detect Pandas vs Spark safely. Returns 'pandas' | 'spark' | 'unknown'."""
    try:
        import pandas as pd
        if isinstance(df, pd.DataFrame):
            return "pandas"
    except Exception:
        pass
    try:
        from pyspark.sql import DataFrame as SparkDF
        if isinstance(df, SparkDF):
            return "spark"
    except Exception:
        pass
    mod = getattr(type(df), "__module__", "") or ""
    if "pyspark.sql" in mod:
        return "spark"
    if "pandas" in mod:
        return "pandas"
    return "unknown"


# ============================================================================
# Internal helpers
# ============================================================================

_SEPARATOR = "|||"
_NULL_SENTINEL = "__NULL__"


def _normalize_key_series(s):
    """Stringify one key column so numeric values compare consistently across
    int/float dtypes and nulls map to the sentinel.

    A nullable numeric column is promoted to float by pandas (100 -> 100.0); the
    same key on the other side of a join may be a clean int (100). A plain
    ``.astype(str)`` would render these as "100.0" vs "100" and they would never
    match. Integer-valued floats are rendered without the trailing ".0" so they
    align with int keys; genuine fractional values keep a canonical repr.
    """
    import pandas as pd
    if pd.api.types.is_float_dtype(s):
        def _fmt(x):
            if pd.isna(x):
                return _NULL_SENTINEL
            f = float(x)
            return str(int(f)) if f.is_integer() else repr(f)
        return s.map(_fmt).astype(str)
    return s.fillna(_NULL_SENTINEL).astype(str)


def _build_composite_key_pandas(df, key_columns: list[str]):
    """Build a composite key Series from multiple columns (pandas)."""
    parts = [_normalize_key_series(df[c]) for c in key_columns]
    if len(parts) == 1:
        return parts[0]
    return parts[0].str.cat(parts[1:], sep=_SEPARATOR)


def _build_composite_key_spark(df, key_columns: list[str]):
    """Build a composite key column from multiple columns (Spark).

    Mirrors the pandas numeric normalization: integer-valued floating/decimal
    keys render without a trailing ".0" so they match the same key stored as an
    integer type on the other side of the join.
    """
    from pyspark.sql import functions as F
    dtypes = dict(df.dtypes)

    def _norm(c):
        col = F.col(c)
        t = dtypes.get(c, "")
        if t in ("float", "double") or t.startswith("decimal"):
            as_str = F.when(
                col == col.cast("long"), col.cast("long").cast("string")
            ).otherwise(col.cast("string"))
            return F.coalesce(as_str, F.lit(_NULL_SENTINEL))
        return F.coalesce(col.cast("string"), F.lit(_NULL_SENTINEL))

    exprs = [_norm(c) for c in key_columns]
    if len(exprs) == 1:
        return df.withColumn("_join_key", exprs[0])
    return df.withColumn("_join_key", F.concat_ws(_SEPARATOR, *exprs))


def _null_analysis_pandas(df, key_columns: list[str], side: str) -> dict:
    """Per-column null analysis for pandas."""
    result = {}
    total = len(df)
    for col in key_columns:
        null_count = int(df[col].isna().sum())
        result[col] = {
            "null_count": null_count,
            "null_pct": round(null_count / total, 6) if total > 0 else 0.0,
            "side": side,
        }
    return result


def _null_analysis_spark(df, key_columns: list[str], side: str) -> dict:
    """Per-column null analysis for Spark."""
    from pyspark.sql import functions as F
    total = df.count()
    result = {}
    # Compute all null counts in a single pass
    null_exprs = [F.sum(F.when(F.col(c).isNull(), 1).otherwise(0)).alias(c) for c in key_columns]
    row = df.select(*null_exprs).first()
    for col in key_columns:
        null_count = int(row[col]) if row[col] is not None else 0
        result[col] = {
            "null_count": null_count,
            "null_pct": round(null_count / total, 6) if total > 0 else 0.0,
            "side": side,
        }
    return result


def _determine_cardinality(
    left_key_distinct: int,
    right_key_distinct: int,
    left_count: int,
    right_count: int,
) -> str:
    """Determine join cardinality: 1:1, 1:many, many:1, many:many."""
    left_has_dups = left_count > left_key_distinct
    right_has_dups = right_count > right_key_distinct
    if not left_has_dups and not right_has_dups:
        return "1:1"
    elif not left_has_dups and right_has_dups:
        return "1:many"
    elif left_has_dups and not right_has_dups:
        return "many:1"
    else:
        return "many:many"


def _predict_output_size(
    left_count: int,
    right_count: int,
    matching_left_rows: int,
    cardinality: str,
    right_rows_per_key_avg: float,
) -> dict:
    """Predict join output size based on cardinality and actual matching rows.

    Uses matching_left_rows (actual count of left rows whose key exists in
    right) instead of overlap_pct * left_count. This avoids inaccuracy when
    left-side duplication is non-uniform across keys.
    """
    if cardinality in ("1:1", "many:1"):
        predicted = matching_left_rows
        fanout = round(matching_left_rows / left_count, 2) if left_count > 0 else 1.0
    elif cardinality == "1:many":
        predicted = int(matching_left_rows * right_rows_per_key_avg)
        fanout = right_rows_per_key_avg
    else:  # many:many
        predicted = int(matching_left_rows * right_rows_per_key_avg)
        fanout = right_rows_per_key_avg

    fanout = round(fanout, 2)
    if fanout <= 1.05:
        risk = "none"
    elif fanout <= 1.5:
        risk = "low"
    elif fanout <= 3.0:
        risk = "medium"
    elif fanout <= 10.0:
        risk = "high"
    else:
        risk = "extreme"

    return {
        "predicted_rows": predicted,
        "fanout_ratio": fanout,
        "fanout_risk": risk,
        "explanation": (
            f"{cardinality} join: {matching_left_rows:,} matching left rows "
            f"x {right_rows_per_key_avg:.1f} avg right dups "
            f"= ~{predicted:,} rows"
        ),
    }


def _check_format_compatibility(left_sample: list[str], right_sample: list[str]) -> dict:
    """Check for format mismatches: whitespace, case, unicode."""
    issues = []
    examples = []

    if not left_sample or not right_sample:
        return {"compatible": True, "issues": [], "examples": []}

    # Whitespace check
    left_has_ws = any(v != v.strip() for v in left_sample if v)
    right_has_ws = any(v != v.strip() for v in right_sample if v)
    if left_has_ws or right_has_ws:
        issues.append("whitespace")
        ws_examples = [v for v in (left_sample + right_sample) if v and v != v.strip()][:3]
        examples.extend([{"type": "whitespace", "value": repr(v)} for v in ws_examples])

    # Case check
    left_cases = {v.lower() for v in left_sample if v}
    right_cases = {v.lower() for v in right_sample if v}
    overlap_lower = left_cases & right_cases
    if overlap_lower:
        left_originals = {v.lower(): v for v in left_sample if v}
        right_originals = {v.lower(): v for v in right_sample if v}
        for key in list(overlap_lower)[:5]:
            lv = left_originals.get(key, "")
            rv = right_originals.get(key, "")
            if lv != rv:
                issues.append("case")
                examples.append({"type": "case", "left": lv, "right": rv})
                break

    # Unicode check (zero-width characters)
    def _has_zwc(val: str) -> bool:
        return any(unicodedata.category(ch) in ("Cf", "Mn") for ch in val)

    left_has_zwc = any(_has_zwc(v) for v in left_sample if v)
    right_has_zwc = any(_has_zwc(v) for v in right_sample if v)
    if left_has_zwc or right_has_zwc:
        issues.append("unicode")
        zwc_examples = [v for v in (left_sample + right_sample) if v and _has_zwc(v)][:2]
        examples.extend([{"type": "unicode", "value": repr(v)} for v in zwc_examples])

    return {
        "compatible": len(issues) == 0,
        "issues": list(set(issues)),
        "examples": examples,
    }


# ============================================================================
# Pandas engine
# ============================================================================

def _analyze_pandas(
    left_df,
    right_df,
    left_keys: list[str],
    right_keys: list[str],
    sample_limit: int,
) -> dict:
    """Run all pre-join analyses on pandas DataFrames."""
    import pandas as pd

    left_count = len(left_df)
    right_count = len(right_df)

    # Build composite keys
    left_key_series = _build_composite_key_pandas(left_df, left_keys)
    right_key_series = _build_composite_key_pandas(right_df, right_keys)

    # Distinct keys
    left_key_set = set(left_key_series.unique())
    right_key_set = set(right_key_series.unique())
    left_key_distinct = len(left_key_set)
    right_key_distinct = len(right_key_set)

    # Overlap
    overlap_keys = left_key_set & right_key_set
    overlap_count = len(overlap_keys)
    overlap_pct = round(overlap_count / left_key_distinct, 6) if left_key_distinct > 0 else 0.0

    # Orphans
    left_orphan_keys = left_key_set - right_key_set
    right_orphan_keys = right_key_set - left_key_set
    left_orphan_count = len(left_orphan_keys)
    right_orphan_count = len(right_orphan_keys)
    left_orphan_pct = round(left_orphan_count / left_key_distinct, 6) if left_key_distinct > 0 else 0.0
    right_orphan_pct = round(right_orphan_count / right_key_distinct, 6) if right_key_distinct > 0 else 0.0

    # Cardinality
    cardinality = _determine_cardinality(left_key_distinct, right_key_distinct, left_count, right_count)

    # Right-side duplication stats
    right_dups = right_key_series.value_counts()
    right_rows_per_key_avg = round(float(right_dups.mean()), 2) if len(right_dups) > 0 else 1.0
    right_rows_per_key_max = int(right_dups.max()) if len(right_dups) > 0 else 1

    # Null analysis
    left_null_info = _null_analysis_pandas(left_df, left_keys, "left")
    right_null_info = _null_analysis_pandas(right_df, right_keys, "right")
    left_null_key_count = sum(v["null_count"] for v in left_null_info.values())
    right_null_key_count = sum(v["null_count"] for v in right_null_info.values())
    left_null_key_pct = round(left_null_key_count / (left_count * len(left_keys)), 6) if left_count > 0 else 0.0
    right_null_key_pct = round(right_null_key_count / (right_count * len(right_keys)), 6) if right_count > 0 else 0.0

    # Matching left rows (actual count, not estimate from overlap_pct)
    matching_left_rows = int(left_key_series.isin(overlap_keys).sum())

    # Right dups over overlap keys only (for accurate 1:many/many:many prediction)
    if overlap_keys and cardinality in ("1:many", "many:many"):
        right_overlap_dups = right_key_series[right_key_series.isin(overlap_keys)].value_counts()
        right_rows_per_key_avg_overlap = round(float(right_overlap_dups.mean()), 2) if len(right_overlap_dups) > 0 else 1.0
    else:
        right_rows_per_key_avg_overlap = right_rows_per_key_avg

    # Predicted output
    prediction = _predict_output_size(
        left_count, right_count, matching_left_rows, cardinality, right_rows_per_key_avg_overlap
    )

    # Format compatibility (sample key values)
    left_sample_vals = [str(v) for v in left_key_series.dropna().head(50).tolist() if v != _NULL_SENTINEL]
    right_sample_vals = [str(v) for v in right_key_series.dropna().head(50).tolist() if v != _NULL_SENTINEL]
    format_check = _check_format_compatibility(left_sample_vals, right_sample_vals)

    # Orphan samples
    left_orphan_samples = sorted(list(left_orphan_keys))[:sample_limit]
    right_orphan_samples = sorted(list(right_orphan_keys))[:sample_limit]

    # Max fanout keys
    top_fanout = right_dups.nlargest(min(sample_limit, 5))
    max_fanout_keys = [
        {"key": str(k), "right_matches": int(v)}
        for k, v in top_fanout.items()
        if v > 1
    ]

    # Null key column breakdown
    null_key_columns = {}
    for i, lk in enumerate(left_keys):
        rk = right_keys[i] if i < len(right_keys) else right_keys[-1]
        col_name = lk if lk == rk else f"{lk}/{rk}"
        null_key_columns[col_name] = {
            "left_nulls": left_null_info[lk]["null_count"],
            "right_nulls": right_null_info[rk]["null_count"],
        }

    return {
        "left_count": left_count,
        "right_count": right_count,
        "left_key_distinct": left_key_distinct,
        "right_key_distinct": right_key_distinct,
        "overlap_count": overlap_count,
        "overlap_pct": overlap_pct,
        "matching_left_rows": matching_left_rows,
        "matching_left_rows_pct": round(matching_left_rows / left_count, 6) if left_count > 0 else 0.0,
        "left_orphan_count": left_orphan_count,
        "left_orphan_pct": left_orphan_pct,
        "right_orphan_count": right_orphan_count,
        "right_orphan_pct": right_orphan_pct,
        "cardinality": cardinality,
        "right_rows_per_key_avg": right_rows_per_key_avg,
        "right_rows_per_key_max": right_rows_per_key_max,
        "left_null_key_count": left_null_key_count,
        "left_null_key_pct": left_null_key_pct,
        "right_null_key_count": right_null_key_count,
        "right_null_key_pct": right_null_key_pct,
        "null_key_columns": null_key_columns,
        "predicted_rows": prediction["predicted_rows"],
        "fanout_ratio": prediction["fanout_ratio"],
        "fanout_risk": prediction["fanout_risk"],
        "fanout_explanation": prediction["explanation"],
        "format_compatible": format_check["compatible"],
        "format_issues": format_check["issues"],
        "left_orphan_samples": left_orphan_samples,
        "right_orphan_samples": right_orphan_samples,
        "max_fanout_keys": max_fanout_keys,
        "format_mismatch_examples": format_check["examples"],
    }


# ============================================================================
# Spark engine
# ============================================================================

def _analyze_spark(
    left_df,
    right_df,
    left_keys: list[str],
    right_keys: list[str],
    sample_limit: int,
) -> dict:
    """Run all pre-join analyses on Spark DataFrames."""
    from pyspark.sql import functions as F

    left_count = left_df.count()
    right_count = right_df.count()

    # Build composite keys
    left_keyed = _build_composite_key_spark(left_df, left_keys)
    right_keyed = _build_composite_key_spark(right_df, right_keys)

    # Distinct keys
    left_key_distinct = left_keyed.select("_join_key").distinct().count()
    right_key_distinct = right_keyed.select("_join_key").distinct().count()

    # Overlap via inner join on key
    left_keys_df = left_keyed.select("_join_key").distinct()
    right_keys_df = right_keyed.select("_join_key").distinct()
    overlap_df = left_keys_df.intersect(right_keys_df)
    overlap_count = overlap_df.count()
    overlap_pct = round(overlap_count / left_key_distinct, 6) if left_key_distinct > 0 else 0.0

    # Orphans
    left_orphan_df = left_keys_df.subtract(right_keys_df)
    right_orphan_df = right_keys_df.subtract(left_keys_df)
    left_orphan_count = left_orphan_df.count()
    right_orphan_count = right_orphan_df.count()
    left_orphan_pct = round(left_orphan_count / left_key_distinct, 6) if left_key_distinct > 0 else 0.0
    right_orphan_pct = round(right_orphan_count / right_key_distinct, 6) if right_key_distinct > 0 else 0.0

    # Cardinality
    cardinality = _determine_cardinality(left_key_distinct, right_key_distinct, left_count, right_count)

    # Right-side duplication stats
    right_dup_stats = right_keyed.groupBy("_join_key").count()
    dup_summary = right_dup_stats.agg(
        F.avg("count").alias("avg_dups"),
        F.max("count").alias("max_dups"),
    ).first()
    right_rows_per_key_avg = round(float(dup_summary["avg_dups"]), 2) if dup_summary["avg_dups"] else 1.0
    right_rows_per_key_max = int(dup_summary["max_dups"]) if dup_summary["max_dups"] else 1

    # Null analysis
    left_null_info = _null_analysis_spark(left_df, left_keys, "left")
    right_null_info = _null_analysis_spark(right_df, right_keys, "right")
    left_null_key_count = sum(v["null_count"] for v in left_null_info.values())
    right_null_key_count = sum(v["null_count"] for v in right_null_info.values())
    left_null_key_pct = round(left_null_key_count / (left_count * len(left_keys)), 6) if left_count > 0 else 0.0
    right_null_key_pct = round(right_null_key_count / (right_count * len(right_keys)), 6) if right_count > 0 else 0.0

    # Matching left rows (actual count via left semi-join on key)
    matching_left_rows = left_keyed.join(
        right_keys_df, left_keyed["_join_key"] == right_keys_df["_join_key"], "left_semi"
    ).count()

    # Right dups over overlap keys only (for accurate 1:many/many:many prediction)
    if overlap_count > 0 and cardinality in ("1:many", "many:many"):
        right_overlap_dups = right_keyed.join(
            overlap_df, right_keyed["_join_key"] == overlap_df["_join_key"], "left_semi"
        ).groupBy(right_keyed["_join_key"]).count()
        overlap_dup_summary = right_overlap_dups.agg(F.avg("count").alias("avg_dups")).first()
        right_rows_per_key_avg_overlap = round(float(overlap_dup_summary["avg_dups"]), 2) if overlap_dup_summary["avg_dups"] else 1.0
    else:
        right_rows_per_key_avg_overlap = right_rows_per_key_avg

    # Predicted output
    prediction = _predict_output_size(
        left_count, right_count, matching_left_rows, cardinality, right_rows_per_key_avg_overlap
    )

    # Format compatibility (sample key values)
    left_sample_rows = left_keyed.select("_join_key").limit(50).collect()
    right_sample_rows = right_keyed.select("_join_key").limit(50).collect()
    left_sample_vals = [str(r["_join_key"]) for r in left_sample_rows if r["_join_key"] != _NULL_SENTINEL]
    right_sample_vals = [str(r["_join_key"]) for r in right_sample_rows if r["_join_key"] != _NULL_SENTINEL]
    format_check = _check_format_compatibility(left_sample_vals, right_sample_vals)

    # Orphan samples
    left_orphan_rows = left_orphan_df.limit(sample_limit).collect()
    right_orphan_rows = right_orphan_df.limit(sample_limit).collect()
    left_orphan_samples = [str(r["_join_key"]) for r in left_orphan_rows]
    right_orphan_samples = [str(r["_join_key"]) for r in right_orphan_rows]

    # Max fanout keys
    top_fanout_rows = right_dup_stats.where(F.col("count") > 1).orderBy(
        F.col("count").desc()
    ).limit(min(sample_limit, 5)).collect()
    max_fanout_keys = [
        {"key": str(r["_join_key"]), "right_matches": int(r["count"])}
        for r in top_fanout_rows
    ]

    # Null key column breakdown
    null_key_columns = {}
    for i, lk in enumerate(left_keys):
        rk = right_keys[i] if i < len(right_keys) else right_keys[-1]
        col_name = lk if lk == rk else f"{lk}/{rk}"
        null_key_columns[col_name] = {
            "left_nulls": left_null_info[lk]["null_count"],
            "right_nulls": right_null_info[rk]["null_count"],
        }

    return {
        "left_count": left_count,
        "right_count": right_count,
        "left_key_distinct": left_key_distinct,
        "right_key_distinct": right_key_distinct,
        "overlap_count": overlap_count,
        "overlap_pct": overlap_pct,
        "matching_left_rows": matching_left_rows,
        "matching_left_rows_pct": round(matching_left_rows / left_count, 6) if left_count > 0 else 0.0,
        "left_orphan_count": left_orphan_count,
        "left_orphan_pct": left_orphan_pct,
        "right_orphan_count": right_orphan_count,
        "right_orphan_pct": right_orphan_pct,
        "cardinality": cardinality,
        "right_rows_per_key_avg": right_rows_per_key_avg,
        "right_rows_per_key_max": right_rows_per_key_max,
        "left_null_key_count": left_null_key_count,
        "left_null_key_pct": left_null_key_pct,
        "right_null_key_count": right_null_key_count,
        "right_null_key_pct": right_null_key_pct,
        "null_key_columns": null_key_columns,
        "predicted_rows": prediction["predicted_rows"],
        "fanout_ratio": prediction["fanout_ratio"],
        "fanout_risk": prediction["fanout_risk"],
        "fanout_explanation": prediction["explanation"],
        "format_compatible": format_check["compatible"],
        "format_issues": format_check["issues"],
        "left_orphan_samples": left_orphan_samples,
        "right_orphan_samples": right_orphan_samples,
        "max_fanout_keys": max_fanout_keys,
        "format_mismatch_examples": format_check["examples"],
    }


# ============================================================================
# Contract builder
# ============================================================================

def _build_contract(
    analysis: dict,
    left_subject: str,
    right_subject: str,
    left_keys: list[str],
    right_keys: list[str],
) -> dict:
    """Build the Anchor standard contract from raw analysis results."""
    findings = []
    risks = []

    # --- Findings ---
    # Orphans
    if analysis["left_orphan_count"] > 0:
        findings.append(
            f"{analysis['left_orphan_count']:,} left keys ({analysis['left_orphan_pct']:.1%}) "
            f"have no match in right -- use LEFT JOIN to preserve"
        )
    if analysis["right_orphan_count"] > 0:
        findings.append(
            f"{analysis['right_orphan_count']:,} right keys ({analysis['right_orphan_pct']:.1%}) "
            f"have no match in left -- will be excluded in LEFT JOIN"
        )

    # Cardinality
    card = analysis["cardinality"]
    if card != "1:1":
        findings.append(
            f"Cardinality is {card} -- each left row may match multiple right rows "
            f"(avg {analysis['right_rows_per_key_avg']}, max {analysis['right_rows_per_key_max']})"
        )
    else:
        findings.append("Cardinality is 1:1 -- clean join expected")

    # Null keys
    total_null = analysis["left_null_key_count"] + analysis["right_null_key_count"]
    if total_null > 0:
        for col_name, null_info in analysis["null_key_columns"].items():
            if null_info["left_nulls"] > 0:
                findings.append(
                    f"left.{col_name} has {null_info['left_nulls']:,} NULLs -- "
                    f"these rows will silently drop in any join"
                )
            if null_info["right_nulls"] > 0:
                findings.append(
                    f"right.{col_name} has {null_info['right_nulls']:,} NULLs -- "
                    f"these rows will silently drop in any join"
                )

    # Format
    if not analysis["format_compatible"]:
        findings.append(
            f"Format mismatch detected: {', '.join(analysis['format_issues'])} -- "
            f"keys may fail to match despite being semantically equal"
        )

    # --- Risks ---
    if analysis["left_null_key_count"] > 0:
        risks.append(
            f"NULL keys: {analysis['left_null_key_count']:,} left rows "
            f"({analysis['left_null_key_pct']:.1%}) will drop silently -- "
            f"filter or COALESCE before joining"
        )

    if analysis["fanout_risk"] in ("high", "extreme"):
        top_key = analysis["max_fanout_keys"][0] if analysis["max_fanout_keys"] else None
        risk_msg = f"Fanout risk is {analysis['fanout_risk'].upper()}: predicted {analysis['predicted_rows']:,} output rows"
        if top_key:
            risk_msg += f" (max {top_key['right_matches']}x on key '{top_key['key']}')"
        risks.append(risk_msg)
    elif analysis["fanout_risk"] == "medium":
        risks.append(
            f"Moderate fanout: {analysis['fanout_ratio']}x expansion expected "
            f"({analysis['predicted_rows']:,} predicted rows)"
        )

    if not analysis["format_compatible"]:
        risks.append(
            f"Format incompatibility ({', '.join(analysis['format_issues'])}) will cause "
            f"false non-matches -- clean keys before joining"
        )

    # --- Summary line ---
    if not risks:
        join_rec = "INNER JOIN safe" if analysis["overlap_pct"] >= 0.999 else "LEFT JOIN recommended"
        summary = f"OK: {join_rec}, {card} cardinality, {analysis['overlap_pct']:.1%} overlap"
    else:
        top_risk = risks[0].split(" -- ")[0] if " -- " in risks[0] else risks[0][:60]
        join_rec = "LEFT JOIN recommended" if analysis["left_orphan_count"] > 0 else "INNER JOIN safe"
        summary = f"WARNING: {join_rec} -- {top_risk}"

    # --- Suggested next actions ---
    suggested = []
    key_expr = ", ".join(f"'{k}'" for k in left_keys)
    if analysis["left_orphan_count"] > 0:
        join_col = left_keys[0] if left_keys == right_keys else f"{left_keys[0]}"
        suggested.append(
            f"Use LEFT JOIN to preserve {analysis['left_orphan_count']:,} orphan rows: "
            f"left_df.join(right_df, '{join_col}', 'left')"
        )
    if analysis["left_null_key_count"] > 0:
        null_col = [c for c, v in analysis["null_key_columns"].items() if v["left_nulls"] > 0][0]
        suggested.append(
            f"Filter NULL keys before join: left_df.where(F.col('{null_col}').isNotNull())"
        )
    if analysis["left_orphan_count"] > 0:
        suggested.append(
            f"Investigate orphans: anchor('case_file', left_df, column='{left_keys[0]}', "
            f"filter='where:{left_keys[0]} NOT IN (top_matches)')"
        )
    if analysis["fanout_risk"] in ("medium", "high", "extreme"):
        suggested.append(
            f"Investigate fanout: anchor('microscope', right_df, '{right_keys[0]}') "
            f"to check for duplicates"
        )
    if not analysis["format_compatible"]:
        suggested.append(
            f"Fix format mismatch: TRIM and normalize keys before joining"
        )
    if not suggested:
        suggested.append("Join looks safe -- proceed with INNER JOIN")

    # --- Build contract ---
    return build_base_context(
        kind="pre_join",
        subject=f"{left_subject} -> {right_subject}",
        summary=summary,
        metrics={
            "left_count": analysis["left_count"],
            "right_count": analysis["right_count"],
            "left_key_distinct": analysis["left_key_distinct"],
            "right_key_distinct": analysis["right_key_distinct"],
            "overlap_count": analysis["overlap_count"],
            "overlap_pct": analysis["overlap_pct"],
            "left_orphan_count": analysis["left_orphan_count"],
            "left_orphan_pct": analysis["left_orphan_pct"],
            "right_orphan_count": analysis["right_orphan_count"],
            "right_orphan_pct": analysis["right_orphan_pct"],
            "cardinality": analysis["cardinality"],
            "right_rows_per_key_avg": analysis["right_rows_per_key_avg"],
            "right_rows_per_key_max": analysis["right_rows_per_key_max"],
            "left_null_key_count": analysis["left_null_key_count"],
            "left_null_key_pct": analysis["left_null_key_pct"],
            "right_null_key_count": analysis["right_null_key_count"],
            "right_null_key_pct": analysis["right_null_key_pct"],
            "null_key_columns": analysis["null_key_columns"],
            "matching_left_rows": analysis["matching_left_rows"],
            "matching_left_rows_pct": analysis["matching_left_rows_pct"],
            "predicted_rows": analysis["predicted_rows"],
            "fanout_ratio": analysis["fanout_ratio"],
            "fanout_risk": analysis["fanout_risk"],
            "format_compatible": analysis["format_compatible"],
            "format_mismatch_description": ", ".join(analysis["format_issues"]) if analysis["format_issues"] else None,
        },
        findings=findings,
        risks=risks,
        samples={
            "left_orphan_keys": analysis["left_orphan_samples"],
            "right_orphan_keys": analysis["right_orphan_samples"],
            "max_fanout_keys": analysis["max_fanout_keys"],
            "format_mismatch_examples": analysis["format_mismatch_examples"],
        },
        suggested_next_actions=suggested,
    )


# ============================================================================
# Markdown renderer
# ============================================================================

def _render_markdown(contract: dict) -> str:
    """Render pre-join report structured around 4 decision questions.

    Sections:
        1. Verdict — Will I lose rows?
        2. Fanout — Will it explode?
        3. Overlap — Why is overlap low? (conditional)
        4. Key Quality — Are keys dirty? (conditional)
        + Actions at the bottom
    """
    lines = []
    m = contract["metrics"]
    samples = contract["samples"]

    # ── Header ──
    lines.append(f"# Pre-Join: {contract['subject']}")
    lines.append("")
    lines.append(f"**{contract['summary']}**")
    lines.append("")

    # ── Section 1: Will I lose rows? ──
    lines.append("## 1. Row Survival")
    lines.append("")
    survival_pct = m["matching_left_rows_pct"]
    loss_pct = 1.0 - survival_pct
    lines.append(f"| | Count | % of left |")
    lines.append(f"| --- | ---: | ---: |")
    lines.append(f"| Left rows total | {m['left_count']:,} | 100% |")
    lines.append(f"| **Will join (INNER)** | **{m['matching_left_rows']:,}** | **{survival_pct:.1%}** |")
    lines.append(f"| Will drop | {m['left_count'] - m['matching_left_rows']:,} | {loss_pct:.1%} |")
    lines.append(f"| Predicted output rows | {m['predicted_rows']:,} | — |")
    lines.append("")
    if m["overlap_pct"] < 1.0 and m["matching_left_rows"] > 0:
        lines.append(f"> Key overlap: {m['overlap_count']:,} of {m['left_key_distinct']:,} distinct keys ({m['overlap_pct']:.1%}) match,")
        lines.append(f"> but those keys carry {survival_pct:.1%} of actual rows (non-uniform duplication).")
        lines.append("")

    # ── Section 2: Will it explode? ──
    lines.append("## 2. Fanout Risk")
    lines.append("")
    risk = m["fanout_risk"]
    risk_icon = {"none": "✅", "low": "🟡", "medium": "🟠", "high": "🔴", "extreme": "💥"}.get(risk, "❓")
    lines.append(f"**{risk_icon} {risk.upper()}** — {m['cardinality']} join, {m['fanout_ratio']}x expansion")
    lines.append("")
    if risk not in ("none", "low"):
        lines.append(f"| Key | Right Matches |")
        lines.append(f"| --- | ---: |")
        for item in (samples.get("max_fanout_keys") or [])[:5]:
            lines.append(f"| `{item['key']}` | {item['right_matches']} |")
        lines.append("")

    # ── Section 3: Why is overlap low? (only if <90% overlap) ──
    if m["overlap_pct"] < 0.9:
        lines.append("## 3. Overlap Gap")
        lines.append("")
        lines.append(f"Only **{m['overlap_pct']:.1%}** of left keys found in right.")
        lines.append("")
        left_orphans = samples.get("left_orphan_keys") or []
        right_orphans = samples.get("right_orphan_keys") or []
        if left_orphans or right_orphans:
            # Side-by-side comparison
            max_show = min(5, max(len(left_orphans), len(right_orphans)))
            lines.append("| Left orphans (no match) | Right orphans (unused) |")
            lines.append("| --- | --- |")
            for i in range(max_show):
                l_val = f"`{left_orphans[i]}`" if i < len(left_orphans) else ""
                r_val = f"`{right_orphans[i]}`" if i < len(right_orphans) else ""
                lines.append(f"| {l_val} | {r_val} |")
            if len(left_orphans) > max_show or len(right_orphans) > max_show:
                lines.append(f"| +{max(0, len(left_orphans) - max_show)} more | +{max(0, len(right_orphans) - max_show)} more |")
            lines.append("")
        # Format mismatch hint
        if not m["format_compatible"]:
            desc = m.get("format_mismatch_description") or ""
            lines.append(f"⚠️ **Format mismatch**: {desc}")
            examples = samples.get("format_mismatch_examples") or []
            for ex in examples[:3]:
                lines.append(f"  - {ex}")
            lines.append("")

    # ── Section 4: Key Quality (only if issues exist) ──
    has_nulls = m["left_null_key_count"] > 0 or m["right_null_key_count"] > 0
    has_format_issues = not m["format_compatible"] and m["overlap_pct"] >= 0.9  # only here if not shown above
    if has_nulls or has_format_issues:
        lines.append("## 4. Key Quality")
        lines.append("")
        if has_nulls:
            for col_name, null_info in m["null_key_columns"].items():
                if null_info["left_nulls"] > 0:
                    lines.append(f"- ⚠️ `left.{col_name}`: {null_info['left_nulls']:,} NULLs → silent drops")
                if null_info["right_nulls"] > 0:
                    lines.append(f"- ⚠️ `right.{col_name}`: {null_info['right_nulls']:,} NULLs → silent drops")
        if has_format_issues:
            desc = m.get("format_mismatch_description") or ""
            lines.append(f"- ⚠️ Format: {desc}")
        lines.append("")

    # ── Actions ──
    if contract["suggested_next_actions"]:
        lines.append("## Next Steps")
        lines.append("")
        for a in contract["suggested_next_actions"]:
            lines.append(f"- {a}")
        lines.append("")

    return "\n".join(lines)


# ============================================================================
# Public entry point
# ============================================================================

def pre_join_context(
    *args,
    left_df=None,
    right_df=None,
    keys: list[str] | None = None,
    left_keys: list[str] | None = None,
    right_keys: list[str] | None = None,
    left_subject: str = "left",
    right_subject: str = "right",
    output_format: str = "dict",
    sample_limit: int = 10,
    **kwargs,
) -> dict | str:
    """Validate two DataFrames are safe to join before joining.

    Checks key overlap, cardinality, null rates, format compatibility,
    and predicts output size. Returns Anchor standard contract with findings,
    risks, and suggested_next_actions.

    Args:
        left_df: Left-side DataFrame (Spark or Pandas).
        right_df: Right-side DataFrame (Spark or Pandas).
        keys: Join key column(s) -- same name in both DataFrames.
        left_keys: Left-side key column(s) -- when names differ.
        right_keys: Right-side key column(s) -- when names differ.
        left_subject: Name for the left DataFrame in output.
        right_subject: Name for the right DataFrame in output.
        output_format: "dict" or "markdown".
        sample_limit: Max orphan/mismatch samples to include.

    Returns:
        Anchor standard contract dict (or markdown string).

    Raises:
        ValueError: If keys/left_keys/right_keys not provided, or columns missing.

    Example:
        >>> anchor("pre_join", orders_df, customers_df, keys=["customer_id"])
        >>> anchor("pre_join", fact_df, dim_df,
        ...    left_keys=["proj_id"], right_keys=["project_id"],
        ...    left_subject="fact_queue", right_subject="dim_project")
    """
    validate_output_format(output_format)
    # --- Resolve positional args ---
    if args:
        if len(args) >= 2 and left_df is None and right_df is None:
            left_df = args[0]
            right_df = args[1]
        elif len(args) == 1 and left_df is None:
            left_df = args[0]

    # --- Validate inputs ---
    if left_df is None or right_df is None:
        raise ValueError(
            "pre_join requires both left_df and right_df. "
            "Usage: anchor('pre_join', left_df, right_df, keys=['col'])"
        )

    guard_dataframe_type(left_df, "left_df")
    guard_dataframe_type(right_df, "right_df")

    # Resolve keys
    if keys is not None:
        if isinstance(keys, str):
            keys = [keys]
        left_keys = keys
        right_keys = keys
    elif left_keys is not None and right_keys is not None:
        if isinstance(left_keys, str):
            left_keys = [left_keys]
        if isinstance(right_keys, str):
            right_keys = [right_keys]
    else:
        raise ValueError(
            "pre_join requires either 'keys' (same name both sides) or "
            "both 'left_keys' and 'right_keys'. "
            "Example: anchor('pre_join', df1, df2, keys=['customer_id'])"
        )

    if len(left_keys) != len(right_keys):
        raise ValueError(
            f"left_keys and right_keys must have same length. "
            f"Got {len(left_keys)} vs {len(right_keys)}. "
            "Use equal-length lists or keys= if column names match "
            "— see anchor('help', 'pre_join')."
        )

    # Detect engine
    engine = _detect_engine(left_df)
    if engine == "unknown":
        raise ValueError(
            f"Cannot detect engine for left_df (type: {type(left_df).__name__}). "
            f"Expected Pandas DataFrame or Spark DataFrame."
        )

    # Validate columns exist
    if engine == "pandas":
        left_cols = set(left_df.columns)
        right_cols = set(right_df.columns)
    else:
        left_cols = set(left_df.columns)
        right_cols = set(right_df.columns)

    missing_left = [k for k in left_keys if k not in left_cols]
    missing_right = [k for k in right_keys if k not in right_cols]
    if missing_left:
        raise ValueError(
            f"Left DataFrame missing key columns: {missing_left}. "
            f"Available columns: {sorted(left_cols)}. "
            "Check the keys= argument — see anchor('help', 'pre_join')."
        )
    if missing_right:
        raise ValueError(
            f"Right DataFrame missing key columns: {missing_right}. "
            f"Available columns: {sorted(right_cols)}. "
            "Check the keys= argument — see anchor('help', 'pre_join')."
        )

    # --- Run analysis ---
    if engine == "pandas":
        analysis = _analyze_pandas(left_df, right_df, left_keys, right_keys, sample_limit)
    else:
        analysis = _analyze_spark(left_df, right_df, left_keys, right_keys, sample_limit)

    # --- Build contract ---
    contract = _build_contract(analysis, left_subject, right_subject, left_keys, right_keys)

    # --- Render ---
    return finalize_context(contract, output_format, _render_markdown)
