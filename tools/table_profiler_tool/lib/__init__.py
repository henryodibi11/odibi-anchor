"""Table Profiler public package surface.

This package starts with the Thread 1 foundation:
* Core data models
* Shared sampling utilities
* Column statistics engine

The full public API will expand as later profiling features are implemented.
"""

from .freshness import detect_freshness
from .table_classifier import classify_table
from .format_checker import detect_format_issues
from .cleanliness import audit_cleanliness
from .case_file import case_file
from .contract import serialize_case_file, serialize_microscope, serialize_profile
from .microscope import microscope
from .profiler import profile_table
from .renderer import (
    render_case_file_md,
    render_microscope_md,
    render_table_ai_summary,
    render_table_profile_md,
)

__all__ = [
    "audit_cleanliness",
    "case_file",
    "classify_table",
    "detect_format_issues",
    "detect_freshness",
    "microscope",
    "profile_table",
    "render_case_file_md",
    "render_microscope_md",
    "render_table_ai_summary",
    "render_table_profile_md",
    "serialize_case_file",
    "serialize_microscope",
    "serialize_profile",
]
