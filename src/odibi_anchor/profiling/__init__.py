"""Data profiling context generators.

Note: exploration_context and dataset_profile_context have been removed.
Use anchor("profile_table", ...) for all profiling needs.
"""

from odibi_anchor.profiling.dogfood_regression_context import dogfood_regression_context
from odibi_anchor.profiling.dogfood_regression_context import render_dogfood_regression_report

__all__ = [
    "dogfood_regression_context",
    "render_dogfood_regression_report",
]
