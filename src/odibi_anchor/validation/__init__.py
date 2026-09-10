"""Validation context generators.

Imports are eager since function names match submodule names
(lazy __getattr__ would resolve to modules instead of functions).
"""

from odibi_anchor.validation.quality_gate_context import (
    quality_gate_context,
    render_quality_gate_report,
)
from odibi_anchor.validation.validation_summary_context import (
    validation_summary_context,
    render_validation_report,
)
from odibi_anchor.validation.duplicate_key_context import (
    duplicate_key_context,
    render_duplicate_key_report,
)

__all__ = [
    "quality_gate_context",
    "render_quality_gate_report",
    "validation_summary_context",
    "render_validation_report",
    "duplicate_key_context",
    "render_duplicate_key_report",
]
