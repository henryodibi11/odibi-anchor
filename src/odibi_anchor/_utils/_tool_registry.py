"""_tool_registry.py — Dynamic tool discovery and registry for odibi_anchor.

Provides:
    - ToolSpec: frozen dataclass representing a tool declaration (from tool.json)
    - ToolLoadError: raised when a tool's entry_point cannot be resolved
    - Registry: discovers, validates, stores, and resolves tools

Usage (internal to agent_init.py):
    from odibi_anchor._utils._tool_registry import Registry, ToolSpec, ToolLoadError

    registry = Registry()
    count = registry.discover_tools([Path("tools/")])
    spec = registry.get_tool("microscope")
    fn = registry.resolve_callable(spec)
"""
from __future__ import annotations

import importlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from odibi_anchor._dispatcher._effects import VALID_PRE_TASK_ACCESS, PreTaskAccess

__all__ = ["ToolSpec", "ToolLoadError", "Registry", "VALID_CATEGORIES"]

# ─── Constants ────────────────────────────────────────────────────────────────

VALID_CATEGORIES = frozenset({
    "code", "data", "memory", "planning", "debugging", "diagnostics",
})

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


# ─── Exceptions ───────────────────────────────────────────────────────────────

class ToolLoadError(Exception):
    """Raised when a tool's entry_point cannot be imported or resolved."""
    pass


# ─── ToolSpec Dataclass ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class ToolSpec:
    """Immutable specification for a registered tool, parsed from tool.json."""

    name: str
    version: str
    description: str
    category: str
    entry_point: str
    renderer: str | None = None
    requires_planning: bool = False
    accepts_output_format: bool = False
    inputs_required: tuple[str, ...] = field(default_factory=tuple)
    inputs_optional: tuple[str, ...] = field(default_factory=tuple)
    output_contract: str = "StandardContract"
    output_kind: str = ""
    dependencies: tuple[str, ...] = field(default_factory=tuple)
    tags: tuple[str, ...] = field(default_factory=tuple)
    allowed_effects: frozenset[str] = field(default_factory=frozenset)
    pre_task_access: PreTaskAccess = "task_required"
    pre_task_access_declared: bool = False
    source_path: str = ""  # directory where tool.json was found

    @classmethod
    def from_dict(cls, data: dict[str, Any], source_path: str = "") -> "ToolSpec":
        """Build a ToolSpec from a parsed tool.json dict."""
        inputs = data.get("inputs", {})
        outputs = data.get("outputs", {})
        return cls(
            name=data["name"],
            version=data["version"],
            description=data["description"],
            category=data["category"],
            entry_point=data["entry_point"],
            renderer=data.get("renderer"),
            requires_planning=data.get("requires_planning", False),
            accepts_output_format=data.get("accepts_output_format", False),
            inputs_required=tuple(inputs.get("required", [])),
            inputs_optional=tuple(inputs.get("optional", [])),
            output_contract=outputs.get("contract", "StandardContract"),
            output_kind=outputs.get("kind", data["name"]),
            dependencies=tuple(data.get("dependencies", [])),
            tags=tuple(data.get("tags", [])),
            allowed_effects=frozenset(data["allowed_effects"]),
            pre_task_access=data.get("pre_task_access", "task_required"),
            pre_task_access_declared="pre_task_access" in data,
            source_path=source_path,
        )


# ─── Registry Class ──────────────────────────────────────────────────────────

class Registry:
    """Dynamic tool registry — discovers, validates, and resolves tools.

    Thread-safe for reads after bootstrap. Not designed for concurrent writes.
    """

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._callables: dict[str, Callable] = {}  # lazy cache: name -> resolved fn
        self._search_paths: list[str] = []
        self._warnings: list[str] = []

    # ── Discovery ─────────────────────────────────────────────────────────────

    def discover_tools(self, paths: list[str | Path]) -> int:
        """Scan directories for tool.json manifests and register valid ones.

        Args:
            paths: List of directory paths to scan. Each should contain
                   subdirectories with tool.json files.

        Returns:
            Number of tools successfully discovered and registered.
        """
        count = 0
        for base_path in paths:
            base = Path(base_path)
            if not base.exists() or not base.is_dir():
                continue
            self._search_paths.append(str(base))
            # Each subdirectory may contain a tool.json
            for child in sorted(base.iterdir()):
                if not child.is_dir():
                    continue
                manifest = child / "tool.json"
                if not manifest.exists():
                    continue
                try:
                    with open(manifest, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except (json.JSONDecodeError, OSError) as e:
                    self._warnings.append(
                        f"Skipped {manifest}: parse error — {e}"
                    )
                    continue

                errors = self.validate_spec(data)
                if errors:
                    self._warnings.append(
                        f"Skipped {manifest}: validation failed — {'; '.join(errors)}"
                    )
                    continue

                name = data["name"]
                if name in self._specs:
                    # Higher-priority path already registered this name
                    self._warnings.append(
                        f"Duplicate tool '{name}' at {child} — "
                        f"already registered from {self._specs[name].source_path}"
                    )
                    continue

                spec = ToolSpec.from_dict(data, source_path=str(child))
                self._specs[name] = spec
                count += 1

        return count

    # ── Access ────────────────────────────────────────────────────────────────

    def get_tool(self, name: str) -> ToolSpec | None:
        """Return the ToolSpec for a registered tool, or None if not found."""
        return self._specs.get(name)

    def list_tools(
        self,
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> list[ToolSpec]:
        """Return specs matching optional category and tag filters.

        Args:
            category: Filter by category (exact match).
            tags: Filter by tags (AND semantics — all must be present).

        Returns:
            List of matching ToolSpec objects, sorted by name.
        """
        results = list(self._specs.values())

        if category:
            results = [s for s in results if s.category == category]

        if tags:
            tag_set = set(tags)
            results = [s for s in results if tag_set.issubset(set(s.tags))]

        return sorted(results, key=lambda s: s.name)

    def register_path(self, path: str | Path) -> int:
        """Register tools from a single directory path at runtime.

        Args:
            path: Path to a directory containing tool subdirectories with
                  tool.json manifests, OR a single tool directory itself.

        Returns:
            Number of tools registered from this path.
        """
        p = Path(path)
        if not p.exists():
            raise ValueError(f"Path does not exist: {path}")

        # Check if this IS a tool directory (contains tool.json directly)
        if (p / "tool.json").exists():
            return self._register_single(p)

        # Otherwise scan subdirectories
        return self.discover_tools([p])

    def _register_single(self, tool_dir: Path) -> int:
        """Register a single tool from its directory."""
        manifest = tool_dir / "tool.json"
        try:
            with open(manifest, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            raise ValueError(f"Cannot parse {manifest}: {e}") from e

        errors = self.validate_spec(data)
        if errors:
            raise ValueError(
                f"Invalid tool.json at {manifest}: {'; '.join(errors)}"
            )

        name = data["name"]
        if name in self._specs:
            raise ValueError(
                f"Tool '{name}' already registered from {self._specs[name].source_path}"
            )

        spec = ToolSpec.from_dict(data, source_path=str(tool_dir))
        self._specs[name] = spec
        return 1

    # ── Validation ────────────────────────────────────────────────────────────

    @staticmethod
    def validate_spec(data: dict[str, Any]) -> list[str]:
        """Validate a parsed tool.json dict. Returns list of error strings.

        Empty list means the spec is valid.
        """
        errors: list[str] = []

        # Required fields
        for field_name in ("name", "version", "description", "category", "entry_point", "allowed_effects"):
            if field_name not in data:
                errors.append(f"Missing required field: '{field_name}'")

        if errors:
            return errors  # Can't validate further without required fields

        # Name format
        name = data["name"]
        if not isinstance(name, str) or not _NAME_RE.match(name):
            errors.append(
                f"Invalid name '{name}': must match [a-z][a-z0-9_]*"
            )

        # Category validation
        category = data["category"]
        if category not in VALID_CATEGORIES:
            errors.append(
                f"Invalid category '{category}': "
                f"must be one of {sorted(VALID_CATEGORIES)}"
            )

        # Entry point format: module.path:callable_name
        entry_point = data["entry_point"]
        if ":" not in entry_point:
            errors.append(
                f"Invalid entry_point '{entry_point}': "
                f"must be 'module.path:callable_name'"
            )

        # Inputs structure
        inputs = data.get("inputs", {})
        if not isinstance(inputs, dict):
            errors.append("'inputs' must be a dict with 'required' and optional 'optional' keys")
        elif "required" not in inputs:
            errors.append("'inputs.required' is a required field")

        # Outputs structure
        outputs = data.get("outputs", {})
        if not isinstance(outputs, dict):
            errors.append("'outputs' must be a dict with 'contract' and 'kind' keys")
        else:
            if "contract" not in outputs:
                errors.append("'outputs.contract' is a required field")
            if "kind" not in outputs:
                errors.append("'outputs.kind' is a required field")

        # Version format (basic semver check)
        version = data.get("version", "")
        if version and not re.match(r"^\d+\.\d+\.\d+", version):
            errors.append(f"Invalid version '{version}': must be semver (e.g. '1.0.0')")

        from odibi_anchor._dispatcher._effects import VALID_EFFECTS
        effects = data.get("allowed_effects")
        if not isinstance(effects, list) or not effects:
            errors.append("'allowed_effects' must be a non-empty list")
        elif any(not isinstance(effect, str) or effect not in VALID_EFFECTS for effect in effects):
            errors.append(f"'allowed_effects' values must be one of {sorted(VALID_EFFECTS)}")
        elif len(effects) != 1:
            errors.append("'allowed_effects' must contain exactly one unique effect until resolvers are supported")

        if "pre_task_access" in data:
            pre_task_access = data["pre_task_access"]
            if not isinstance(pre_task_access, str) or pre_task_access not in VALID_PRE_TASK_ACCESS:
                errors.append(
                    f"'pre_task_access' must be one of {sorted(VALID_PRE_TASK_ACCESS)}"
                )

        return errors

    # ── Callable Resolution ───────────────────────────────────────────────────

    def resolve_callable(self, spec: ToolSpec) -> Callable:
        """Import and return the callable from a tool's entry_point.

        Lazily resolves on first access and caches the result.
        Raises ToolLoadError on import or attribute failures.

        Args:
            spec: The ToolSpec whose entry_point to resolve.

        Returns:
            The callable function.
        """
        if spec.name in self._callables:
            return self._callables[spec.name]

        entry_point = spec.entry_point
        if ":" not in entry_point:
            raise ToolLoadError(
                f"Invalid entry_point format '{entry_point}' for tool '{spec.name}': "
                f"expected 'module.path:callable_name'"
            )

        module_path, callable_name = entry_point.rsplit(":", 1)

        # Add tool source_path to sys.path if not already there
        source = spec.source_path
        if source and source not in sys.path:
            sys.path.insert(0, source)

        try:
            module = importlib.import_module(module_path)
        except ImportError as e:
            raise ToolLoadError(
                f"Cannot import module '{module_path}' for tool '{spec.name}': {e}"
            ) from e

        fn = getattr(module, callable_name, None)
        if fn is None:
            raise ToolLoadError(
                f"Module '{module_path}' has no attribute '{callable_name}' "
                f"(tool: '{spec.name}')"
            )
        if not callable(fn):
            raise ToolLoadError(
                f"'{module_path}:{callable_name}' is not callable "
                f"(tool: '{spec.name}')"
            )

        self._callables[spec.name] = fn
        return fn

    # ── Diagnostics ───────────────────────────────────────────────────────────

    @property
    def tool_count(self) -> int:
        """Number of registered tools."""
        return len(self._specs)

    @property
    def warnings(self) -> list[str]:
        """Warnings accumulated during discovery."""
        return list(self._warnings)

    def clear(self) -> None:
        """Reset the registry (useful for testing)."""
        self._specs.clear()
        self._callables.clear()
        self._search_paths.clear()
        self._warnings.clear()
