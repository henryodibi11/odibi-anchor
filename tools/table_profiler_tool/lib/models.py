"""Data models for Table Profiler.

Defines the serializable dataclass hierarchy used by the profiler foundation,
including inference metadata, column-level profiling, table-level profiling,
and future diff/decision-ready models.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


@dataclass
class CompetingHypothesis:
    """Describe a plausible alternative inference outcome.

    Stores the candidate value together with confidence, evidence, and a short
    explanation of why it did not become the primary inference.
    """

    value: Any
    confidence: float
    evidence: list[str] = field(default_factory=list)
    counter_signals: list[str] = field(default_factory=list)
    blocker_reason: str = ""
    verification_hint: str = ""

sys.dont_write_bytecode = True


class TableClassification(str, Enum):
    """What kind of table this dataset represents."""

    FACT = "fact"
    DIMENSION = "dimension"
    SNAPSHOT = "snapshot"
    EVENT_LOG = "event_log"
    SCD2 = "scd2"
    LOOKUP = "lookup"
    STAGING = "staging"
    AGGREGATE = "aggregate"
    BRIDGE = "bridge"
    UNKNOWN = "unknown"


class ColumnRole(str, Enum):
    """What role a column plays inside a table."""

    PRIMARY_KEY = "primary_key"
    NATURAL_KEY = "natural_key"
    SURROGATE_KEY = "surrogate_key"
    FOREIGN_KEY = "foreign_key"
    MEASURE = "measure"
    DIMENSION = "dimension"
    FLAG = "flag"
    TIMESTAMP = "timestamp"
    PARTITION = "partition"
    DERIVED = "derived"
    METADATA = "metadata"
    FREETEXT = "freetext"
    IDENTIFIER = "identifier"
    UNKNOWN = "unknown"


class SemanticType(str, Enum):
    """What values in a column semantically represent."""

    EMAIL = "email"
    PHONE = "phone"
    URL = "url"
    UUID = "uuid"
    IP_ADDRESS = "ip_address"
    ZIP_CODE = "zip_code"
    STATE_CODE = "state_code"
    COUNTRY_CODE = "country_code"
    CURRENCY_AMOUNT = "currency_amount"
    PERCENTAGE = "percentage"
    DATE_STRING = "date_string"
    BOOLEAN_STRING = "boolean_string"
    JSON_STRING = "json_string"
    DELIMITED_LIST = "delimited_list"
    FILE_PATH = "file_path"
    ENUM = "enum"
    CODE = "code"
    FREETEXT = "freetext"
    NUMERIC_STRING = "numeric_string"
    UNKNOWN = "unknown"


class ProfilingLevel(str, Enum):
    """Requested profiling depth for a profiling run."""

    QUICK = "quick"
    STANDARD = "standard"
    DEEP = "deep"


class IssueSeverity(str, Enum):
    """Severity assigned to a detected issue or risk."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class Inference:
    """Wrap an inferred value with confidence and supporting evidence."""

    value: Any
    confidence: float
    evidence: list[str] = field(default_factory=list)
    counter_signals: list[str] = field(default_factory=list)
    sample_size: int = 0
    method: str = "unknown"
    runner_ups: list[CompetingHypothesis] = field(default_factory=list)
    blocker_reason: str = ""
    verification_hint: str = ""


@dataclass
class UncertainResult:
    """Represent an explicit unknown or low-confidence profiler result."""

    attempted: str
    result: str
    confidence: float
    reason: str
    blocking_factor: str
    competing_hypotheses: list[dict[str, Any]] = field(default_factory=list)
    suggested_investigation: str = ""


@dataclass
class ActionableRisk:
    """Describe a risk together with safe and unsafe next actions."""

    description: str
    severity: str
    safe_action: str
    unsafe_action: str
    code_hint: str | None = None
    columns_affected: list[str] = field(default_factory=list)
    rows_affected_pct: float = 0.0


@dataclass
class FormatIssue:
    """Represent a formatting inconsistency or cleanliness problem."""

    issue_type: str
    column: str
    severity: str
    description: str
    affected_count: int
    affected_pct: float
    examples: list[str] = field(default_factory=list)
    fix_suggestion: str = ""
    safe_action: str = "Inspect and normalize before downstream use."
    unsafe_action: str = "Using raw values may break joins, casts, or aggregations."
    code_hint: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ColumnProfile:
    """Deep profile of a single column."""

    name: str
    position: int

    # Type information
    spark_type: str
    semantic_type: SemanticType = SemanticType.UNKNOWN
    semantic_type_inference: Inference | None = None
    role: ColumnRole = ColumnRole.UNKNOWN
    role_inference: Inference | None = None
    is_nullable: bool = True
    confidence: float = 0.0

    # Statistics
    row_count: int = 0
    non_null_count: int = 0
    null_count: int = 0
    null_pct: float = 0.0
    effective_null_pct: float = 0.0
    distinct_count: int = 0
    distinct_pct: float = 0.0
    distinct_normalized: int | None = None
    normalization_gap: int | None = None
    is_unique: bool = False
    is_constant: bool = False

    # Value analysis
    top_values: list[dict[str, Any]] = field(default_factory=list)
    sample_values: list[str] = field(default_factory=list)
    min_value: Any | None = None
    max_value: Any | None = None
    mean_value: float | None = None
    std_value: float | None = None
    median_value: float | None = None

    # String-specific
    min_length: int | None = None
    max_length: int | None = None
    avg_length: float | None = None
    pattern_fingerprint: str | None = None
    format_variants: list[dict[str, Any]] | None = None

    # Cleanliness
    has_leading_spaces: bool = False
    has_trailing_spaces: bool = False
    has_mixed_case: bool = False
    has_embedded_units: bool = False
    null_like_count: int = 0
    null_like_values: list[str] = field(default_factory=list)
    sentinel_values: list[dict[str, Any]] = field(default_factory=list)

    # Relationships
    correlated_nulls: list[str] = field(default_factory=list)
    suspected_fk_target: str | None = None

    # Quality flags
    quality_flags: list[str] = field(default_factory=list)
    format_issues: list[FormatIssue] = field(default_factory=list)
    uncertainty: UncertainResult | None = None


@dataclass
class GrainAnalysis:
    """Store inferred grain and duplicate concentration analysis."""

    best_grain: list[str] = field(default_factory=list)
    is_unique: bool = False
    duplicate_rate: float = 0.0
    candidates_tested: list[dict[str, Any]] = field(default_factory=list)
    runner_up_grains: list[dict[str, Any]] = field(default_factory=list)
    null_exclusion_rate: float = 0.0
    verification_hint: str = ""
    duplicate_concentration: str | None = None
    inference: Inference | None = None
    uncertainty: UncertainResult | None = None


@dataclass
class FreshnessAnalysis:
    """Store freshness and cadence detection results."""

    freshness_column: str
    latest_value: str
    earliest_value: str
    staleness: str
    staleness_hours: float
    cadence: str | None = None
    avg_rows_per_period: float | None = None
    last_load_row_count: int | None = None
    gap_detected: bool = False
    gap_description: str | None = None
    inference: Inference | None = None
    uncertainty: UncertainResult | None = None


@dataclass
class JoinProfile:
    """Describe a potential join relationship to another table."""

    source_column: str
    target_table: str
    target_column: str
    cardinality: str
    overlap_pct: float
    orphan_count: int
    orphan_pct: float
    format_compatible: bool
    format_mismatch_description: str | None = None
    safe_join_type: str = "LEFT"
    inference: Inference | None = None


@dataclass
class OutlierProfile:
    """Store outlier detection results for a numeric column."""

    column: str
    method: str
    lower_bound: float
    upper_bound: float
    outlier_count: int
    outlier_pct: float
    extreme_values: list[dict[str, Any]] = field(default_factory=list)
    context: str = ""


@dataclass
class DuplicateForensics:
    """Results of duplicate concentration forensics.

    Answers WHERE duplicates are concentrated and whether the pattern
    looks like intentional snapshot design or a dedup bug.
    """

    grain_columns: list[str] = field(default_factory=list)
    duplicate_count: int = 0
    duplicate_rate: float = 0.0

    # Concentration analysis
    concentration_column: str | None = None
    concentration_values: list[dict[str, Any]] = field(default_factory=list)
    # Each entry: {"value": "2026-06", "dup_count": 500, "dup_pct": 0.45}

    # Temporal concentration
    is_time_concentrated: bool = False
    time_concentration_description: str | None = None
    # e.g. "80% of duplicates in last 3 days"

    # Source/category concentration
    is_source_concentrated: bool = False
    source_concentration_description: str | None = None
    # e.g. "92% of duplicates where source='legacy_import'"

    # Verdict
    is_snapshot_pattern: bool = False
    verdict: str = "unknown"
    # "snapshot_design" | "dedup_needed" | "partial_overlap" | "unknown"
    explanation: str = ""

    inference: Inference | None = None



@dataclass
class ProfileDiff:
    """Compare two profiles of the same table."""

    subject: str
    old_row_count: int
    new_row_count: int
    row_count_change_pct: float
    columns_added: list[str] = field(default_factory=list)
    columns_removed: list[str] = field(default_factory=list)
    type_changes: list[dict[str, Any]] = field(default_factory=list)
    new_issues: list[FormatIssue] = field(default_factory=list)
    resolved_issues: list[FormatIssue] = field(default_factory=list)
    null_rate_changes: list[dict[str, Any]] = field(default_factory=list)
    new_enum_values: list[dict[str, Any]] = field(default_factory=list)
    removed_enum_values: list[dict[str, Any]] = field(default_factory=list)
    grain_changed: bool = False
    classification_changed: bool = False
    drift_severity: str = "none"
    summary: str = ""


@dataclass
class TableProfile:
    """Complete profile of a DataFrame or table."""

    # Identity
    subject: str
    classification: TableClassification = TableClassification.UNKNOWN
    classification_confidence: float = 0.0
    classification_reasoning: str = ""
    classification_evidence: list[str] = field(default_factory=list)
    classification_counter_signals: list[str] = field(default_factory=list)
    classification_inference: Inference | None = None

    # Shape
    row_count: int = 0
    column_count: int = 0

    # Grain and freshness
    grain: GrainAnalysis = field(default_factory=GrainAnalysis)
    duplicate_forensics: "DuplicateForensics | None" = None
    freshness: FreshnessAnalysis | None = None

    # Column profiles
    columns: list[ColumnProfile] = field(default_factory=list)

    # Cross-table relationships
    joins: list[JoinProfile] = field(default_factory=list)

    # Quality summary
    format_issues: list[FormatIssue] = field(default_factory=list)
    outliers: list[OutlierProfile] = field(default_factory=list)
    overall_quality_score: float = 1.0
    quality_summary: str = ""

    # Metadata
    delta_metadata: dict[str, Any] | None = None
    profiling_level: str = ProfilingLevel.STANDARD.value
    profiling_duration_ms: int = 0
    step_timings: dict[str, int] = field(default_factory=dict)
    profiled_at: str | None = None
    degraded_features: list[str] = field(default_factory=list)
    degradation_reasons: dict[str, str] = field(default_factory=dict)

    # Decision-ready output
    summary: str = ""
    findings: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    actionable_risks: list[ActionableRisk] = field(default_factory=list)
    suggested_actions: list[str] = field(default_factory=list)
    suggested_sql: list[str] = field(default_factory=list)

    # Safety and sentinel handling
    sentinel_columns: list[str] = field(default_factory=list)
    effective_null_summary: str = ""
    pii_flagged_columns: list[str] = field(default_factory=list)
    redaction_applied: bool = False

    # Rendering / token budget
    column_importance_ranking: list[str] = field(default_factory=list)
    estimated_token_count: int = 0

__all__ = [
    "ActionableRisk",
    "ColumnProfile",
    "ColumnRole",
    "CompetingHypothesis",
    "DuplicateForensics",
    "FormatIssue",
    "FreshnessAnalysis",
    "GrainAnalysis",
    "Inference",
    "IssueSeverity",
    "JoinProfile",
    "OutlierProfile",
    "ProfileDiff",
    "ProfilingLevel",
    "SemanticType",
    "TableClassification",
    "TableProfile",
    "UncertainResult",
]
