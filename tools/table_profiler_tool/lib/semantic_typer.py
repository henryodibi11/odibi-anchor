"""Semantic type inference for Table Profiler.

Classifies string column values into semantic types (email, phone, UUID, etc.)
using regex patterns and column name hints.  Returns an Inference wrapper with
confidence and supporting evidence.

Public API
----------
infer_semantic_type(values, column_name) -> Inference
"""

from __future__ import annotations

import re
import sys
from typing import Any

sys.dont_write_bytecode = True

from .models import CompetingHypothesis, Inference, SemanticType

# ---------------------------------------------------------------------------
# Pattern Registry — ordered by specificity (most specific first)
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")
_URL_RE = re.compile(r"^https?://[^\s]{4,}$", re.IGNORECASE)
_IP_RE = re.compile(
    r"^(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)$"
)
_PHONE_RE = re.compile(
    r"^[\+]?[\d\s\-\(\)\.]{7,18}$"
)
_ZIP_RE = re.compile(r"^\d{5}(?:-\d{4})?$")
_STATE_CODE_RE = re.compile(r"^[A-Z]{2}$")
_COUNTRY_CODE_RE = re.compile(r"^[A-Z]{2,3}$")
# Currency requires at least one indicator: a leading symbol OR a trailing code.
# Bare numbers (e.g. "60601") must NOT match.
_CURRENCY_RE = re.compile(
    r"^-?(?:[\$\u20ac\u00a3\u00a5\u20b9]\s*-?[\d,]+\.?\d*"
    r"|[\d,]+\.?\d*\s+(?:USD|EUR|GBP|CAD|AUD))$",
    re.IGNORECASE,
)
_PERCENTAGE_RE = re.compile(r"^-?\d+\.?\d*\s*%$")
_DATE_STRING_RE = re.compile(
    r"^(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})$"
)
_BOOLEAN_RE = re.compile(
    r"^(?:true|false|yes|no|y|n|t|f|0|1)$", re.IGNORECASE
)
_JSON_RE = re.compile(r"^\s*[\{\[].*[\}\]]\s*$", re.DOTALL)
_DELIMITED_RE = re.compile(r"^[^|,;]+(?:[|,;][^|,;]+){1,}$")
_FILE_PATH_RE = re.compile(r"^(?:[A-Za-z]:)?[/\\](?:[^/\\]+[/\\])*[^/\\]*$")
_NUMERIC_STRING_RE = re.compile(r"^-?[\d,]+\.?\d*$")
_CODE_RE = re.compile(r"^[A-Z0-9]{2,}[-_][A-Z0-9\-_]+$", re.IGNORECASE)

# Evaluation order — most specific patterns first to avoid false positives.
_PATTERN_REGISTRY: list[tuple[SemanticType, re.Pattern[str]]] = [
    (SemanticType.UUID, _UUID_RE),
    (SemanticType.EMAIL, _EMAIL_RE),
    (SemanticType.URL, _URL_RE),
    (SemanticType.IP_ADDRESS, _IP_RE),
    (SemanticType.JSON_STRING, _JSON_RE),
    (SemanticType.FILE_PATH, _FILE_PATH_RE),
    (SemanticType.CURRENCY_AMOUNT, _CURRENCY_RE),
    (SemanticType.PERCENTAGE, _PERCENTAGE_RE),
    (SemanticType.DATE_STRING, _DATE_STRING_RE),
    (SemanticType.BOOLEAN_STRING, _BOOLEAN_RE),
    (SemanticType.PHONE, _PHONE_RE),
    (SemanticType.ZIP_CODE, _ZIP_RE),
    (SemanticType.STATE_CODE, _STATE_CODE_RE),
    (SemanticType.COUNTRY_CODE, _COUNTRY_CODE_RE),
    (SemanticType.DELIMITED_LIST, _DELIMITED_RE),
    (SemanticType.CODE, _CODE_RE),
    (SemanticType.NUMERIC_STRING, _NUMERIC_STRING_RE),
]

# ---------------------------------------------------------------------------
# Name Hint Registry — column name substrings that hint at semantic type
# ---------------------------------------------------------------------------

_NAME_HINTS: dict[SemanticType, list[str]] = {
    SemanticType.EMAIL: ["email", "e_mail", "email_address", "mail"],
    SemanticType.PHONE: ["phone", "tel", "mobile", "fax", "cell"],
    SemanticType.URL: ["url", "link", "href", "website", "endpoint"],
    SemanticType.UUID: ["uuid", "guid", "request_id", "trace_id", "correlation_id"],
    SemanticType.IP_ADDRESS: ["ip", "ip_address", "ipv4", "ipv6", "host_ip"],
    SemanticType.ZIP_CODE: ["zip", "zipcode", "zip_code", "postal", "postal_code"],
    SemanticType.STATE_CODE: ["state", "state_code", "province"],
    SemanticType.COUNTRY_CODE: ["country", "country_code", "nation"],
    SemanticType.CURRENCY_AMOUNT: ["amount", "price", "cost", "revenue", "fee", "salary"],
    SemanticType.PERCENTAGE: ["pct", "percent", "percentage", "rate"],
    SemanticType.DATE_STRING: ["date", "dt", "dob", "created", "updated", "expires"],
    SemanticType.BOOLEAN_STRING: ["flag", "is_", "has_", "active", "enabled", "deleted"],
    SemanticType.JSON_STRING: ["json", "payload", "metadata", "config", "settings"],
    SemanticType.DELIMITED_LIST: ["tags", "categories", "labels", "list"],
    SemanticType.FILE_PATH: ["path", "file", "filepath", "filename", "directory"],
    SemanticType.NUMERIC_STRING: ["num", "number", "count", "qty", "quantity"],
    SemanticType.CODE: ["code", "sku", "part_number", "project_code", "ref"],
}

# Types that are inherently ambiguous when matched by pattern alone (short
# numeric strings, 2-letter codes, etc.).  These require a name hint to reach
# high confidence.
_AMBIGUOUS_TYPES: set[SemanticType] = {
    SemanticType.ZIP_CODE,
    SemanticType.STATE_CODE,
    SemanticType.COUNTRY_CODE,
    SemanticType.PHONE,
    SemanticType.NUMERIC_STRING,
    SemanticType.BOOLEAN_STRING,
}

# Types that must NEVER be inferred from pattern alone — a column-name hint
# is required.  Their regex patterns are too generic (2-3 uppercase letters)
# and frequently false-positive on energy-market acronyms (DEC, SPP, PJM…),
# fuel codes, and other short-code domains.
_REQUIRE_NAME_HINT: set[SemanticType] = {
    SemanticType.COUNTRY_CODE,
    SemanticType.STATE_CODE,
}

# Confidence thresholds
_CONFIDENCE_CLEAR = 0.85
_CONFIDENCE_CLEAR_WITH_HINT = 0.95
_CONFIDENCE_AMBIGUOUS_NO_HINT = 0.55
_CONFIDENCE_AMBIGUOUS_WITH_HINT = 0.85

# Minimum match ratio to consider a type as candidate
_MIN_MATCH_RATIO = 0.60


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def infer_semantic_type(
    values: list[str],
    column_name: str = "",
) -> Inference:
    """Infer the semantic type from a sample of string values.

    Args:
        values: Non-null string values sampled from the column.
        column_name: Column name used for name-hint boosting.

    Returns:
        Inference with value=SemanticType, confidence, evidence, and method.
    """

    if not values:
        return Inference(
            value=SemanticType.UNKNOWN,
            confidence=0.0,
            evidence=["no_values"],
            counter_signals=[],
            sample_size=0,
            method="regex+name_hint",
        )

    col_lower = column_name.lower().replace(" ", "_")
    sample_size = len(values)
    ranked_candidates = _rank_candidates(
        _collect_candidate_records(values, column_name, col_lower)
    )
    viable_candidates = [
        candidate for candidate in ranked_candidates if candidate["confidence"] > 0
    ]

    if not viable_candidates:
        return Inference(
            value=SemanticType.UNKNOWN,
            confidence=0.0,
            evidence=["no_pattern_match"],
            counter_signals=[],
            sample_size=sample_size,
            method="regex+name_hint",
            runner_ups=_build_competing_hypotheses(ranked_candidates),
            blocker_reason="no semantic pattern cleared the confidence threshold",
            verification_hint=(
                "inspect representative values and header meaning before assigning "
                "a semantic type"
            ),
        )

    best_candidate = viable_candidates[0]
    dominance_gap = _candidate_confidence_gap(best_candidate, viable_candidates)
    blocker_reason = best_candidate["blocker_reason"]
    if (
        not blocker_reason
        and best_candidate["value"] in _AMBIGUOUS_TYPES
        and len(viable_candidates) > 1
        and dominance_gap < 0.20
    ):
        blocker_reason = (
            "top semantic candidates remain close, so the label is directionally "
            "useful but still ambiguous"
        )

    return Inference(
        value=best_candidate["value"],
        confidence=round(best_candidate["confidence"], 3),
        evidence=best_candidate["evidence"],
        counter_signals=best_candidate["counter_signals"],
        sample_size=sample_size,
        method=best_candidate["method"],
        runner_ups=_build_competing_hypotheses(ranked_candidates[1:]),
        blocker_reason=blocker_reason,
        verification_hint=_best_candidate_verification_hint(
            best_candidate,
            viable_candidates,
            column_name,
        ),
    )


# ---------------------------------------------------------------------------
# Candidate collection and ranking
# ---------------------------------------------------------------------------


def _collect_candidate_records(
    values: list[str],
    column_name: str,
    col_lower: str,
) -> list[dict[str, Any]]:
    """Collect semantic-type candidates before final ranking."""

    sample_size = len(values)
    candidate_records: list[dict[str, Any]] = []

    for sem_type, pattern in _PATTERN_REGISTRY:
        match_count = sum(1 for value in values if pattern.match(value.strip()))
        match_ratio = match_count / sample_size
        if match_ratio < _MIN_MATCH_RATIO:
            continue

        has_hint = _has_name_hint(col_lower, sem_type)
        is_ambiguous = sem_type in _AMBIGUOUS_TYPES
        evidence = [f"pattern_match_ratio={match_ratio:.2f}"]
        if has_hint:
            evidence.append(f"name_hint='{column_name}'")
        evidence.append(f"sample_size={sample_size}")

        counter_signals: list[str] = []
        non_match_ratio = 1.0 - match_ratio
        if non_match_ratio > 0.10:
            counter_signals.append(f"non_matching_values={non_match_ratio:.0%}")

        if sem_type in _REQUIRE_NAME_HINT and not has_hint:
            candidate_records.append(
                {
                    "value": sem_type,
                    "confidence": 0.0,
                    "evidence": evidence,
                    "counter_signals": [
                        *counter_signals,
                        "missing_required_name_hint",
                    ],
                    "blocker_reason": (
                        "pattern matched but column name lacks the required semantic hint"
                    ),
                    "verification_hint": _verification_hint_for_semantic_type(
                        sem_type,
                        column_name,
                    ),
                    "method": "regex+name_hint",
                    "match_ratio": match_ratio,
                    "has_hint": has_hint,
                }
            )
            continue

        confidence = _base_confidence_for_candidate(is_ambiguous, has_hint)
        confidence *= min(match_ratio / 0.80, 1.0)
        candidate_records.append(
            {
                "value": sem_type,
                "confidence": confidence,
                "evidence": evidence,
                "counter_signals": counter_signals,
                "blocker_reason": "",
                "verification_hint": _verification_hint_for_semantic_type(
                    sem_type,
                    column_name,
                ),
                "method": "regex+name_hint",
                "match_ratio": match_ratio,
                "has_hint": has_hint,
            }
        )

    enum_result = _check_enum(values, col_lower, sample_size)
    if enum_result is not None:
        candidate_records.append(
            {
                "value": enum_result.value,
                "confidence": enum_result.confidence,
                "evidence": list(enum_result.evidence),
                "counter_signals": list(enum_result.counter_signals),
                "blocker_reason": "",
                "verification_hint": _verification_hint_for_semantic_type(
                    enum_result.value,
                    column_name,
                ),
                "method": enum_result.method,
                "match_ratio": 1.0,
                "has_hint": _has_name_hint(col_lower, enum_result.value),
            }
        )

    if _is_spreadsheet_artifact(col_lower):
        candidate_records = [
            _penalize_spreadsheet_artifact(candidate)
            for candidate in candidate_records
        ]

    return candidate_records


def _base_confidence_for_candidate(is_ambiguous: bool, has_hint: bool) -> float:
    """Return the base confidence before match-ratio scaling."""

    if is_ambiguous:
        return (
            _CONFIDENCE_AMBIGUOUS_WITH_HINT
            if has_hint
            else _CONFIDENCE_AMBIGUOUS_NO_HINT
        )
    return _CONFIDENCE_CLEAR_WITH_HINT if has_hint else _CONFIDENCE_CLEAR


def _penalize_spreadsheet_artifact(candidate: dict[str, Any]) -> dict[str, Any]:
    """Lower confidence when the header looks auto-generated by import tooling."""

    updated = {
        **candidate,
        "evidence": list(candidate["evidence"]),
        "counter_signals": [
            *candidate["counter_signals"],
            "spreadsheet_artifact_column_name",
        ],
    }
    if updated["confidence"] > 0:
        updated["confidence"] *= 0.60
    if not updated["blocker_reason"]:
        updated["blocker_reason"] = (
            "column header looks auto-generated and may not describe the values reliably"
        )
    return updated


def _rank_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank semantic candidates deterministically and cap output noise."""

    ranked = sorted(
        candidates,
        key=lambda candidate: (
            candidate["confidence"],
            candidate.get("has_hint", False),
            candidate.get("match_ratio", 0.0),
            candidate["value"] != SemanticType.ENUM,
        ),
        reverse=True,
    )
    return ranked[:5]


def _candidate_confidence_gap(
    best_candidate: dict[str, Any],
    viable_candidates: list[dict[str, Any]],
) -> float:
    """Return the confidence gap between the top two viable candidates."""

    if len(viable_candidates) < 2:
        return best_candidate["confidence"]
    return max(best_candidate["confidence"] - viable_candidates[1]["confidence"], 0.0)


def _best_candidate_verification_hint(
    best_candidate: dict[str, Any],
    viable_candidates: list[dict[str, Any]],
    column_name: str,
) -> str:
    """Build a concise verification hint for the winning semantic candidate."""

    base_hint = best_candidate["verification_hint"]
    dominance_gap = _candidate_confidence_gap(best_candidate, viable_candidates)
    if (
        best_candidate["value"] in _AMBIGUOUS_TYPES
        and len(viable_candidates) > 1
        and dominance_gap < 0.20
    ):
        alternate = viable_candidates[1]["value"].value
        return (
            f"{base_hint}; contrast representative values with {alternate} before "
            f"treating '{column_name or 'the column'}' as canonical"
        )
    return base_hint


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_name_hint(col_lower: str, sem_type: SemanticType) -> bool:
    """Check whether column name contains a hint for the given type."""

    hints = _NAME_HINTS.get(sem_type, [])
    return any(hint in col_lower for hint in hints)


def _check_enum(
    values: list[str],
    col_lower: str,
    sample_size: int,
) -> Inference | None:
    """Detect low-cardinality enum columns.

    Returns an Inference if the column looks like an enum, else None.
    """

    distinct_values = set(values)
    distinct_count = len(distinct_values)

    # Heuristic: <= 20 distinct values in the sample and ratio < 5%.
    # Floor of 10 ensures very low-cardinality columns (e.g. 3-8 distinct)
    # are always considered as enum candidates regardless of sample size.
    max_enum_distinct = min(20, max(10, int(sample_size * 0.05)))
    if distinct_count < 2 or distinct_count > max_enum_distinct:
        return None

    confidence = _CONFIDENCE_CLEAR
    evidence = [
        f"distinct_count={distinct_count}",
        f"sample_size={sample_size}",
        f"distinct_ratio={distinct_count / sample_size:.3f}",
    ]

    return Inference(
        value=SemanticType.ENUM,
        confidence=round(confidence, 3),
        evidence=evidence,
        counter_signals=[],
        sample_size=sample_size,
        method="cardinality_heuristic",
    )


# Regex for spreadsheet artifact column names (Excel auto-generated headers)
_SPREADSHEET_ARTIFACT_RE = re.compile(
    r"^(?:unnamed|column|field|col)(?:__|_)?\d*$", re.IGNORECASE
)


def _is_spreadsheet_artifact(col_lower: str) -> bool:
    """Detect column names that are likely auto-generated by spreadsheet import.

    Examples: unnamed__0, unnamed__12, column_3, field0, col1
    These columns have unreliable headers and data content may not match
    the apparent pattern — semantic confidence should be penalized.
    """

    return bool(_SPREADSHEET_ARTIFACT_RE.match(col_lower))


def _build_competing_hypotheses(
    candidates: list[dict[str, Any]],
    limit: int = 3,
) -> list[CompetingHypothesis]:
    """Convert candidate metadata into serialized runner-up hypotheses."""

    runner_ups: list[CompetingHypothesis] = []
    for candidate in candidates[:limit]:
        if candidate["value"] == SemanticType.UNKNOWN:
            continue
        runner_ups.append(
            CompetingHypothesis(
                value=candidate["value"],
                confidence=round(candidate["confidence"], 3),
                evidence=list(candidate["evidence"]),
                counter_signals=list(candidate["counter_signals"]),
                blocker_reason=candidate["blocker_reason"],
                verification_hint=candidate["verification_hint"],
            )
        )
    return runner_ups


def _verification_hint_for_semantic_type(
    sem_type: SemanticType,
    column_name: str,
) -> str:
    """Return a concise verification step for a semantic-type candidate."""

    if sem_type == SemanticType.ENUM:
        return "review distinct values from a broader sample to confirm the set stays small and stable"
    if sem_type in {SemanticType.STATE_CODE, SemanticType.COUNTRY_CODE}:
        return "check whether the header meaning and value set reflect geography instead of business abbreviations"
    if sem_type == SemanticType.DATE_STRING:
        return "parse a wider sample to confirm the date format is consistent"
    if sem_type == SemanticType.CODE:
        return "compare values to a reference code list or adjacent descriptive columns"
    if sem_type == SemanticType.NUMERIC_STRING:
        return "verify whether leading zeros or formatting make this categorical rather than numeric"
    return f"spot-check representative values from '{column_name or 'the column'}' to confirm this semantic label"
