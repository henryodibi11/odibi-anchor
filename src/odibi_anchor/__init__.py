"""odibi_anchor — Standalone context-generator toolkit.

Reusable planning, validation, profiling, debugging, and table-comparison tools.
Zero dependency on external framework packages.

All submodule imports are lazy-loaded to avoid ~540ms FUSE latency at boot.
"""
# pyright: reportUnsupportedDunderAll=false

import ast
import csv
import json
from importlib.metadata import Distribution, distributions
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

_DISTRIBUTION_NAME = "odibi-anchor"
_UNKNOWN_VERSION = "0+unknown"


def _source_project() -> tuple[Path, Path] | None:
    """Return the one supported raw-source project and pyproject paths."""
    project_root = Path(__file__).resolve().parents[2]
    pyproject_path = project_root / "pyproject.toml"
    if not pyproject_path.is_file():
        return None
    return project_root, pyproject_path


def _editable_distribution_matches(installed_distribution: Distribution, project_root: Path) -> bool:
    """Return whether PEP 660 metadata points at this exact source project."""
    try:
        direct_url_text = installed_distribution.read_text("direct_url.json")
    except (OSError, UnicodeError):
        return False
    if direct_url_text is None:
        return False
    try:
        direct_url = json.loads(direct_url_text)
        parsed_url = urlparse(direct_url["url"])
        editable = direct_url["dir_info"]["editable"] is True
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return False
    if parsed_url.scheme != "file" or not editable:
        return False
    authority = parsed_url.netloc
    if authority and authority.lower() != "localhost":
        return False
    file_url_path = parsed_url.path
    try:
        editable_path = Path(url2pathname(file_url_path))
        if not file_url_path or not editable_path.is_absolute():
            return False
        editable_root = editable_path.resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return editable_root == project_root


def _wheel_distribution_matches(installed_distribution: Distribution, imported_path: Path) -> bool:
    """Return whether an artifact RECORD owns this imported package file."""
    try:
        files = installed_distribution.files
        if files is None:
            return False
        for file in files:
            if file.as_posix() != "odibi_anchor/__init__.py":
                continue
            return Path(installed_distribution.locate_file(file)).resolve() == imported_path
    except (csv.Error, OSError, RuntimeError, TypeError, UnicodeError, ValueError):
        return False
    return False


def _installed_version(source_project: tuple[Path, Path] | None) -> tuple[bool, str | None]:
    """Return whether metadata owns this import and its unambiguous version."""
    imported_path = Path(__file__).resolve()
    matching_distributions = []
    for installed_distribution in distributions(name=_DISTRIBUTION_NAME):
        editable_match = source_project is not None and _editable_distribution_matches(
            installed_distribution, source_project[0]
        )
        if editable_match or _wheel_distribution_matches(installed_distribution, imported_path):
            matching_distributions.append(installed_distribution)
    if not matching_distributions:
        return False, None
    if len(matching_distributions) != 1:
        return True, None
    try:
        version = matching_distributions[0].version
    except (KeyError, OSError, TypeError, UnicodeError, ValueError):
        return True, None
    return True, version if isinstance(version, str) and version else None


def _raw_source_version(pyproject_path: Path) -> str:
    """Read only the authoritative project version from a verified source checkout."""
    try:
        lines = pyproject_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return _UNKNOWN_VERSION
    in_project_table = False
    project_values: dict[str, list[str]] = {"name": [], "version": []}
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_project_table:
                break
            in_project_table = stripped == "[project]"
            continue
        key, separator, value = stripped.partition("=")
        project_key = key.strip()
        if not in_project_table or not separator or project_key not in project_values:
            continue
        try:
            parsed_value = ast.literal_eval(value.strip())
        except (SyntaxError, ValueError):
            return _UNKNOWN_VERSION
        if not isinstance(parsed_value, str) or not parsed_value:
            return _UNKNOWN_VERSION
        project_values[project_key].append(parsed_value)
    if project_values["name"] != [_DISTRIBUTION_NAME] or len(project_values["version"]) != 1:
        return _UNKNOWN_VERSION
    return project_values["version"][0]


def _resolve_version() -> str:
    """Resolve the runtime version from installed or narrow raw-source provenance."""
    source_project = _source_project()
    metadata_owns_import, installed_version = _installed_version(source_project)
    if metadata_owns_import:
        return installed_version or _UNKNOWN_VERSION
    if source_project is not None:
        return _raw_source_version(source_project[1])
    return _UNKNOWN_VERSION


__version__ = _resolve_version()

__all__ = [
    "HumanInputConfigurationError",
    "HumanInputDeliveryError",
    "HumanInputError",
    "HumanInputTimeout",
    "apply_legacy_import",
    "doctor",
    "get_human_input_request",
    "handoff_context",
    "install_guidance",
    "launch",
    "list_snapshots",
    "load_portfolio",
    "notify_human",
    "plan_legacy_import",
    "prepare_portfolio_runtime",
    "qualify_durability",
    "quick_context",
    "register_project",
    "render_handoff_report",
    "render_task_execution_report",
    "request_human_input",
    "request_human_input_record",
    "resolve_project",
    "restore_latest",
    "scaffold_portfolio",
    "setup_host",
    "snapshot_state",
    "task_execution_context",
    "validate_portfolio",
    "write_portfolio",
]

# Lazy import mapping: attribute → (module, name)
_LAZY_IMPORTS = {
    "launch": ("odibi_anchor.startup", "launch"),
    "doctor": ("odibi_anchor.startup", "doctor"),
    "install_guidance": ("odibi_anchor.startup", "install_guidance"),
    "register_project": ("odibi_anchor.startup", "register_project"),
    "prepare_portfolio_runtime": ("odibi_anchor.startup", "prepare_portfolio_runtime"),
    "setup_host": ("odibi_anchor.host_setup", "setup_host"),
    "list_snapshots": ("odibi_anchor.durability", "list_snapshots"),
    "load_portfolio": ("odibi_anchor.portfolio", "load_portfolio"),
    "qualify_durability": ("odibi_anchor.durability", "qualify_durability"),
    "resolve_project": ("odibi_anchor.portfolio", "resolve_project"),
    "restore_latest": ("odibi_anchor.durability", "restore_latest"),
    "scaffold_portfolio": ("odibi_anchor.portfolio", "scaffold_portfolio"),
    "validate_portfolio": ("odibi_anchor.portfolio", "validate_portfolio"),
    "write_portfolio": ("odibi_anchor.portfolio", "write_portfolio"),
    "snapshot_state": ("odibi_anchor.durability", "snapshot_state"),
    "plan_legacy_import": ("odibi_anchor.legacy_import", "plan_legacy_import"),
    "apply_legacy_import": ("odibi_anchor.legacy_import", "apply_legacy_import"),
    "task_execution_context": ("odibi_anchor.planning", "task_execution_context"),
    "render_task_execution_report": ("odibi_anchor.planning", "render_task_execution_report"),
    "quick_context": ("odibi_anchor.planning", "quick_context"),
    "handoff_context": ("odibi_anchor.planning", "handoff_context"),
    "render_handoff_report": ("odibi_anchor.planning", "render_handoff_report"),
    "HumanInputConfigurationError": ("odibi_anchor.human_input", "HumanInputConfigurationError"),
    "HumanInputDeliveryError": ("odibi_anchor.human_input", "HumanInputDeliveryError"),
    "HumanInputError": ("odibi_anchor.human_input", "HumanInputError"),
    "HumanInputTimeout": ("odibi_anchor.human_input", "HumanInputTimeout"),
    "get_human_input_request": ("odibi_anchor.human_input", "get_human_input_request"),
    "notify_human": ("odibi_anchor.human_input", "notify_human"),
    "request_human_input": ("odibi_anchor.human_input", "request_human_input"),
    "request_human_input_record": ("odibi_anchor.human_input", "request_human_input_record"),
}


def __getattr__(name):
    """Lazy imports for all submodules and planning functions."""
    if name in _LAZY_IMPORTS:
        import importlib

        module_path, attr_name = _LAZY_IMPORTS[name]
        mod = importlib.import_module(module_path)
        val = getattr(mod, attr_name)
        globals()[name] = val
        return val
    # Submodule lazy loading
    _submodules = ("validation", "tables", "profiling", "debugging", "codebase", "planning", "bootstrap")
    if name in _submodules:
        import importlib

        mod = importlib.import_module(f"odibi_anchor.{name}")
        globals()[name] = mod
        return mod
    raise AttributeError(f"module 'odibi_anchor' has no attribute {name!r}")
