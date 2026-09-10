"""Domain-Specific Extension Registry for Table Profiler (F22).

Allows users to register custom semantic patterns that the profiler
will apply during semantic type inference. This is the extension point
for domain-specific knowledge like:
- Interconnection numbers (energy industry)
- Internal project codes
- Custom ID formats

Public API
----------
register_semantic_pattern(name, regex, description="", examples=None)
get_registered_patterns() -> dict[str, PatternSpec]
clear_registered_patterns()
match_registered_patterns(value: str) -> list[str]
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import Any

sys.dont_write_bytecode = True


# ---------------------------------------------------------------------------
# Data Model
# ---------------------------------------------------------------------------


@dataclass
class PatternSpec:
    """Specification for a registered semantic pattern."""

    name: str
    regex: str
    compiled: Any = field(repr=False, default=None)
    description: str = ""
    examples: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Registry (module-level singleton)
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, PatternSpec] = {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def register_semantic_pattern(
    name: str,
    regex: str,
    description: str = "",
    examples: list[str] | None = None,
) -> PatternSpec:
    """Register a custom semantic pattern for the profiler.

    Args:
        name: Unique name for this pattern (e.g., "interconnection_number").
        regex: Regular expression that matches values of this type.
        description: Human-readable description.
        examples: Example values that match the pattern.

    Returns:
        The registered PatternSpec.

    Raises:
        ValueError: If name is empty or regex is invalid.
    """
    if not name or not name.strip():
        raise ValueError("Pattern name must not be empty")

    if not regex or not regex.strip():
        raise ValueError("Pattern regex must not be empty")

    # Validate regex compiles
    try:
        compiled = re.compile(regex)
    except re.error as e:
        raise ValueError(f"Invalid regex '{regex}': {e}") from e

    spec = PatternSpec(
        name=name.strip(),
        regex=regex,
        compiled=compiled,
        description=description,
        examples=examples or [],
    )

    _REGISTRY[spec.name] = spec
    return spec


def get_registered_patterns() -> dict[str, PatternSpec]:
    """Return all registered patterns."""
    return dict(_REGISTRY)


def clear_registered_patterns() -> None:
    """Remove all registered patterns."""
    _REGISTRY.clear()


def match_registered_patterns(value: str) -> list[str]:
    """Match a value against all registered patterns.

    Args:
        value: String value to test.

    Returns:
        List of pattern names that match.
    """
    if not value or not isinstance(value, str):
        return []

    matches: list[str] = []
    for name, spec in _REGISTRY.items():
        if spec.compiled and spec.compiled.fullmatch(value):
            matches.append(name)

    return matches


def match_column_values(
    sample_values: list[str],
    threshold: float = 0.80,
) -> str | None:
    """Check if a column's sample values predominantly match a registered pattern.

    Args:
        sample_values: Sample of string values from the column.
        threshold: Fraction of values that must match for a positive detection.

    Returns:
        Pattern name if threshold is met, None otherwise.
    """
    if not sample_values or not _REGISTRY:
        return None

    non_empty = [v for v in sample_values if v and isinstance(v, str)]
    if not non_empty:
        return None

    for name, spec in _REGISTRY.items():
        if spec.compiled is None:
            continue
        match_count = sum(1 for v in non_empty if spec.compiled.fullmatch(v))
        if match_count / len(non_empty) >= threshold:
            return name

    return None
