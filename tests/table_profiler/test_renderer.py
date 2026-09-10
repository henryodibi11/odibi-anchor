"""Tests for lib/renderer.py.

Covers render_table_ai_summary and render_table_profile_md:
  - Happy-path smoke tests (returns non-empty str)
  - All major sections: TABLE/SHAPE/CLASS/QUALITY/GRAIN/FRESH/COLUMNS/ISSUES/FINDINGS
  - Conditional sections absent when data is empty
  - max_cols and column_importance_ranking behaviour
  - Freshness gap detection rendering
  - Purity — neither renderer may mutate the profile
  - GFM table structural validity
"""

import sys

sys.dont_write_bytecode = True
sys.path.insert(0, "/Workspace/Users/user@example.com/tools/table_profiler")

from tools.table_profiler_tool.lib.models import (
    ColumnProfile,
    ColumnRole,
    CompetingHypothesis,
    FormatIssue,
    FreshnessAnalysis,
    GrainAnalysis,
    Inference,
    SemanticType,
    TableClassification,
    TableProfile,
)
from tools.table_profiler_tool.lib.renderer import render_table_ai_summary, render_table_profile_md


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

def _col(
    name: str,
    position: int = 0,
    spark_type: str = "string",
    role: ColumnRole = ColumnRole.UNKNOWN,
    null_pct: float = 0.0,
    distinct_pct: float = 1.0,
    semantic_type: SemanticType | None = None,
    quality_flags: list[str] | None = None,
) -> ColumnProfile:
    """Build a minimal ColumnProfile with only the required fields set."""
    return ColumnProfile(
        name=name,
        position=position,
        spark_type=spark_type,
        role=role,
        null_pct=null_pct,
        distinct_pct=distinct_pct,
        semantic_type=semantic_type if semantic_type is not None else SemanticType.UNKNOWN,
        quality_flags=quality_flags or [],
    )


def _make_minimal_profile(**overrides) -> TableProfile:
    """Minimal TableProfile — no freshness, no issues, no findings."""
    profile = TableProfile(
        subject="test.schema.t",
        row_count=100,
        column_count=2,
        columns=[
            _col("id", position=0, spark_type="bigint", role=ColumnRole.IDENTIFIER),
            _col("name", position=1, spark_type="string", role=ColumnRole.DIMENSION),
        ],
        profiling_level="standard",
        profiling_duration_ms=500,
        profiled_at="2026-05-26T00:00:00+00:00",
        overall_quality_score=0.95,
        quality_summary="excellent",
    )
    for k, v in overrides.items():
        setattr(profile, k, v)
    return profile


def _make_full_profile() -> TableProfile:
    """Fully populated TableProfile with all optional sections non-empty."""
    return TableProfile(
        subject="catalog.schema.orders",
        row_count=50_000,
        column_count=4,
        columns=[
            _col("order_id",    position=0, spark_type="bigint",  role=ColumnRole.PRIMARY_KEY),
            _col("customer_id", position=1, spark_type="bigint",  role=ColumnRole.FOREIGN_KEY, distinct_pct=0.6),
            _col("amount",      position=2, spark_type="double",  role=ColumnRole.MEASURE, null_pct=0.02,
                 semantic_type=SemanticType.CURRENCY_AMOUNT),
            ColumnProfile(
                name="email",
                position=3,
                spark_type="string",
                role=ColumnRole.DIMENSION,
                semantic_type=SemanticType.EMAIL,
                quality_flags=["mixed_case"],
                semantic_type_inference=Inference(
                    value=SemanticType.EMAIL,
                    confidence=0.93,
                    evidence=["pattern_match_ratio=1.00"],
                    runner_ups=[
                        CompetingHypothesis(
                            value=SemanticType.URL,
                            confidence=0.22,
                            evidence=["contains punctuation"],
                            verification_hint="spot-check representative values from email to confirm this semantic label",
                        )
                    ],
                    verification_hint="spot-check representative values from email to confirm this semantic label",
                ),
                role_inference=Inference(
                    value=ColumnRole.DIMENSION,
                    confidence=0.78,
                    evidence=["not unique"],
                    runner_ups=[
                        CompetingHypothesis(
                            value=ColumnRole.IDENTIFIER,
                            confidence=0.41,
                            evidence=["high distinctness"],
                            verification_hint="compare values to a trusted customer master before using as a key",
                        )
                    ],
                    verification_hint="compare values to a trusted customer master before using as a join key",
                ),
            ),
        ],
        profiling_level="standard",
        profiling_duration_ms=2_500,
        profiled_at="2026-05-26T12:00:00+00:00",
        classification=TableClassification.FACT,
        classification_confidence=0.82,
        classification_inference=Inference(
            value=TableClassification.FACT,
            confidence=0.82,
            evidence=["measure columns dominate"],
            runner_ups=[
                CompetingHypothesis(
                    value=TableClassification.SNAPSHOT,
                    confidence=0.58,
                    evidence=["freshness column present"],
                    verification_hint="verify whether each row is an event or a daily snapshot",
                )
            ],
            verification_hint="verify whether rows capture transactions or periodic snapshots",
        ),
        grain=GrainAnalysis(
            best_grain=["order_id"],
            is_unique=True,
            duplicate_rate=0.0,
            runner_up_grains=[
                {
                    "columns": ["customer_id", "order_id"],
                    "is_unique": True,
                    "duplicate_rate": 0.0,
                    "null_exclusion_rate": 0.0,
                    "score": 0.91,
                    "verification_hint": "confirm whether customer_id is needed to represent order line grain",
                }
            ],
            null_exclusion_rate=0.0,
            verification_hint="validate that order_id is a business key and not a load sequence",
        ),
        freshness=FreshnessAnalysis(
            freshness_column="_extracted_at",
            latest_value="2026-05-26T12:00:00",
            earliest_value="2025-01-01T00:00:00",
            staleness="0.5 days",
            staleness_hours=12.0,
            cadence="daily",
            gap_detected=False,
        ),
        format_issues=[
            FormatIssue(
                issue_type="mixed_case_enum",
                column="email",
                severity="warning",
                description="Mixed case values detected.",
                affected_count=50,
                affected_pct=0.001,
                examples=["ALICE@example.com"],
            )
        ],
        overall_quality_score=0.88,
        quality_summary="good",
        findings=["Classified as FACT with 82% confidence"],
        risks=["email column has mixed case — join-unsafe"],
        suggested_actions=["normalize email case before downstream use"],
        degraded_features=["outlier_detection"],
        degradation_reasons={"outlier_detection": "timeout after 10s"},
        column_importance_ranking=["order_id", "customer_id", "amount", "email"],
    )


# ---------------------------------------------------------------------------
# render_table_ai_summary
# ---------------------------------------------------------------------------

class TestRenderTableAiSummary:
    """Tests for render_table_ai_summary."""

    def test_returns_non_empty_str(self):
        result = render_table_ai_summary(_make_minimal_profile())
        assert isinstance(result, str)
        assert len(result) > 0

    def test_contains_table_name(self):
        assert "test.schema.t" in render_table_ai_summary(_make_minimal_profile())

    def test_contains_shape(self):
        result = render_table_ai_summary(_make_minimal_profile())
        assert "100" in result   # row count
        assert "2" in result     # col count

    def test_contains_classification(self):
        result = render_table_ai_summary(_make_minimal_profile())
        assert "unknown" in result   # default TableClassification.UNKNOWN

    def test_contains_quality_score_and_label(self):
        result = render_table_ai_summary(_make_minimal_profile())
        assert "0.9500" in result
        assert "excellent" in result

    def test_quality_excellent_emoji(self):
        result = render_table_ai_summary(_make_minimal_profile(overall_quality_score=0.95))
        assert "✅" in result

    def test_quality_warning_emoji(self):
        result = render_table_ai_summary(_make_minimal_profile(overall_quality_score=0.75))
        assert "⚠️" in result

    def test_quality_error_emoji(self):
        result = render_table_ai_summary(_make_minimal_profile(overall_quality_score=0.50))
        assert "❌" in result

    def test_no_freshness_shows_placeholder(self):
        result = render_table_ai_summary(_make_minimal_profile())  # freshness=None
        assert "no temporal column" in result

    def test_with_freshness_column_and_cadence(self):
        result = render_table_ai_summary(_make_full_profile())
        assert "_extracted_at" in result
        assert "daily" in result

    def test_freshness_gap_shown(self):
        fv = FreshnessAnalysis(
            freshness_column="loaded_at",
            latest_value="2026-05-01",
            earliest_value="2025-01-01",
            staleness="25 days",
            staleness_hours=600.0,
            cadence="weekly",
            gap_detected=True,
            gap_description="30-day gap in 2025-03",
        )
        result = render_table_ai_summary(_make_minimal_profile(freshness=fv))
        assert "GAP" in result
        assert "30-day gap" in result

    def test_grain_section_present(self):
        result = render_table_ai_summary(_make_full_profile())
        assert "GRAIN" in result
        assert "order_id" in result
        assert "unique=True" in result

    def test_columns_section_present(self):
        result = render_table_ai_summary(_make_minimal_profile())
        assert "COLUMNS" in result
        assert "id" in result
        assert "name" in result

    def test_max_cols_limits_column_listing(self):
        profile = _make_full_profile()   # ranking: order_id, customer_id, amount, email
        result = render_table_ai_summary(profile, max_cols=2)
        assert "top 2 of 4" in result
        # Column rows always contain 'null=' -- use that to distinguish them from
        # ISSUES lines ('  [WARNING] ...') and FINDINGS lines ('  * ...').
        lines = result.split("\n")
        col_block = [l for l in lines if l.startswith("  ") and "null=" in l]
        assert len(col_block) == 2, f"Expected 2 column rows, got {len(col_block)}: {col_block}"
        assert any("order_id" in l for l in col_block)
        assert any("customer_id" in l for l in col_block)
        assert not any("email" in l for l in col_block), "email should be cut off at max_cols=2"
    def test_column_importance_ranking_respected(self):
        profile = _make_full_profile()
        profile.column_importance_ranking = ["email", "amount", "order_id", "customer_id"]
        result = render_table_ai_summary(profile, max_cols=1)
        # Column rows always contain 'null=' -- use that to identify them precisely
        lines = result.split("\n")
        col_block = [l for l in lines if l.startswith("  ") and "null=" in l]
        assert len(col_block) == 1, f"Expected 1 column row, got {len(col_block)}: {col_block}"
        assert "email" in col_block[0]
    def test_no_issues_section_when_empty(self):
        result = render_table_ai_summary(_make_minimal_profile())
        assert "ISSUES" not in result

    def test_issues_section_present_and_accurate(self):
        result = render_table_ai_summary(_make_full_profile())
        assert "ISSUES" in result
        assert "mixed_case_enum" in result
        assert "WARNING" in result

    def test_no_findings_section_when_empty(self):
        result = render_table_ai_summary(_make_minimal_profile())
        assert "FINDINGS" not in result

    def test_findings_section_present(self):
        result = render_table_ai_summary(_make_full_profile())
        assert "FINDINGS" in result
        assert "FACT" in result

    def test_no_degraded_section_when_empty(self):
        result = render_table_ai_summary(_make_minimal_profile())
        assert "DEGRADED" not in result

    def test_degraded_section_present(self):
        result = render_table_ai_summary(_make_full_profile())
        assert "DEGRADED" in result
        assert "outlier_detection" in result

    def test_profiled_at_none_uses_fallback(self):
        result = render_table_ai_summary(_make_minimal_profile(profiled_at=None))
        assert "unknown" in result

    def test_pure_no_mutation(self):
        profile = _make_full_profile()
        subject_before = profile.subject
        score_before = profile.overall_quality_score
        render_table_ai_summary(profile)
        assert profile.subject == subject_before
        assert profile.overall_quality_score == score_before


# ---------------------------------------------------------------------------
# render_table_profile_md
# ---------------------------------------------------------------------------

class TestRenderTableProfileMd:
    """Tests for render_table_profile_md."""

    def test_returns_non_empty_str(self):
        result = render_table_profile_md(_make_minimal_profile())
        assert isinstance(result, str)
        assert len(result) > 0

    def test_title_header_present(self):
        result = render_table_profile_md(_make_minimal_profile())
        assert "# 📊 Table Profile: `test.schema.t`" in result

    def test_overview_section_present(self):
        result = render_table_profile_md(_make_minimal_profile())
        assert "## Overview" in result
        assert "100" in result   # row count in table

    def test_grain_section_present(self):
        result = render_table_profile_md(_make_minimal_profile())
        assert "## Grain" in result

    def test_freshness_placeholder_when_none(self):
        result = render_table_profile_md(_make_minimal_profile())
        assert "## Freshness" in result
        assert "No temporal column detected" in result

    def test_freshness_section_populated(self):
        result = render_table_profile_md(_make_full_profile())
        assert "_extracted_at" in result
        assert "daily" in result

    def test_freshness_gap_warning_shown(self):
        fv = FreshnessAnalysis(
            freshness_column="loaded_at",
            latest_value="2026-05-01",
            earliest_value="2025-01-01",
            staleness="25 days",
            staleness_hours=600.0,
            cadence="weekly",
            gap_detected=True,
            gap_description="30-day gap starting 2025-03",
        )
        result = render_table_profile_md(_make_minimal_profile(freshness=fv))
        assert "⚠️" in result
        assert "30-day gap" in result

    def test_freshness_no_gap_shows_ok(self):
        fv = FreshnessAnalysis(
            freshness_column="_extracted_at",
            latest_value="2026-05-26",
            earliest_value="2025-01-01",
            staleness="0 days",
            staleness_hours=0.0,
            cadence="daily",
            gap_detected=False,
        )
        result = render_table_profile_md(_make_minimal_profile(freshness=fv))
        assert "✅ none" in result

    def test_columns_section_present(self):
        result = render_table_profile_md(_make_minimal_profile())
        assert "## Columns" in result
        assert "`id`" in result
        assert "`name`" in result

    def test_semantic_type_in_columns_table(self):
        result = render_table_profile_md(_make_full_profile())
        assert "currency_amount" in result
        assert "email" in result

    def test_no_issues_section_when_empty(self):
        result = render_table_profile_md(_make_minimal_profile())
        assert "Format & Cleanliness Issues" not in result

    def test_issues_section_present_when_non_empty(self):
        result = render_table_profile_md(_make_full_profile())
        assert "Format & Cleanliness Issues" in result
        assert "mixed_case_enum" in result
        assert "🟡 warning" in result

    def test_error_issues_get_red_badge(self):
        iss = FormatIssue(
            issue_type="null_like_values",
            column="status",
            severity="error",
            description="Null-like strings found.",
            affected_count=200,
            affected_pct=0.02,
            examples=["N/A", "NULL"],
        )
        result = render_table_profile_md(_make_minimal_profile(format_issues=[iss]))
        assert "🔴 error" in result

    def test_info_issues_get_blue_badge(self):
        iss = FormatIssue(
            issue_type="uniform_length",
            column="date_col",
            severity="info",
            description="All values are exactly 10 characters.",
            affected_count=100,
            affected_pct=1.0,
            examples=["length=10"],
        )
        result = render_table_profile_md(_make_minimal_profile(format_issues=[iss]))
        assert "🔵 info" in result
        assert "🟡 warning" not in result
        assert "🔴 error" not in result

    def test_no_findings_section_when_all_empty(self):
        result = render_table_profile_md(_make_minimal_profile())
        assert "Findings & Recommendations" not in result

    def test_findings_section_present(self):
        result = render_table_profile_md(_make_full_profile())
        assert "Findings & Recommendations" in result
        assert "⚠️" in result   # risk bullet
        assert "💡" in result   # action bullet

    def test_no_degraded_section_when_empty(self):
        result = render_table_profile_md(_make_minimal_profile())
        assert "Degraded Features" not in result

    def test_degraded_section_present(self):
        result = render_table_profile_md(_make_full_profile())
        assert "Degraded Features" in result
        assert "outlier_detection" in result
        assert "timeout" in result

    def test_gfm_table_rows_column_count_consistent(self):
        """Every pipe-table row must have the same cell count as its header."""
        profile = _make_full_profile()
        result = render_table_profile_md(profile)
        lines = result.split("\n")
        # Walk through the lines, tracking expected column count per table block
        expected_cols: int | None = None
        for line in lines:
            if not line.startswith("|"):
                expected_cols = None
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if expected_cols is None:
                expected_cols = len(cells)
            elif "---" in line:
                # Separator row — verify count matches header
                assert len(cells) == expected_cols, (
                    f"Separator has {len(cells)} cols, expected {expected_cols}: {line!r}"
                )
            else:
                assert len(cells) == expected_cols, (
                    f"Data row has {len(cells)} cols, expected {expected_cols}: {line!r}"
                )

    def test_pure_no_mutation(self):
        """Both renderers must be pure — no profile field must be mutated."""
        profile = _make_full_profile()
        subject_before = profile.subject
        score_before = profile.overall_quality_score
        render_table_ai_summary(profile)
        render_table_profile_md(profile)
        assert profile.subject == subject_before
        assert profile.overall_quality_score == score_before


    def test_ai_summary_surfaces_ambiguity_hints(self):
        result = render_table_ai_summary(_make_full_profile())
        assert "CLASS_ALT" in result
        assert "alternates=customer_id+order_id" in result
        assert "VERIFY:" in result
        assert "email:" in result

    def test_markdown_surfaces_runner_up_tables(self):
        result = render_table_profile_md(_make_full_profile())
        assert "Runner-up grain candidates" in result
        assert "Ambiguity & Verification" in result
        assert "classification" in result
        assert "semantic" in result
