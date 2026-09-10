"""Table comparison, contract, and transform context generators."""

from odibi_anchor.tables.diff_ops import diff_tables_by_key, render_diff_report
from odibi_anchor.tables.compare_ops import cross_check, detect_deletes
from odibi_anchor.tables.schema_diff_context import schema_diff_context
from odibi_anchor.tables.table_contract_summary import table_contract_summary
from odibi_anchor.tables.transform_plan_context import (
    transform_plan_context,
    render_transform_plan_report,
    adapt_profile_to_transform_input,
)
from odibi_anchor.tables.apply_transform_context import (
    apply_sql,
    apply_transform_context,
    render_apply_transform_report,
    rollback,
    unpersist,
)
from odibi_anchor.tables.coercion_classifier import (
    coercion_check_context,
    render_coercion_report,
)

__all__ = [
    "diff_tables_by_key",
    "render_diff_report",
    "cross_check",
    "detect_deletes",
    "schema_diff_context",
    "table_contract_summary",
    "transform_plan_context",
    "render_transform_plan_report",
    "adapt_profile_to_transform_input",
    "apply_transform_context",
    "apply_sql",
    "render_apply_transform_report",
    "rollback",
    "unpersist",
    "coercion_check_context",
    "render_coercion_report",
]
