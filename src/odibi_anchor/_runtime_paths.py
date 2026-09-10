"""Resolve immutable distribution resources and writable runtime state paths.

Source checkouts never default writable state into the repository. On Databricks,
ANCHOR_HOME must be set explicitly to a durable external path. On local machines, an
installed distribution resolves state beneath an explicit or operating-system user
location. Resolution is pure: callers create directories only when an action actually
needs writable state.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimePaths:
    """Immutable resource locations and the independently writable runtime home."""

    source_checkout: bool
    resource_root: Path
    instructions_file: Path
    skills_dir: Path
    tools_dir: Path
    anchor_home: Path


def _source_checkout_root(package_dir: Path) -> Path | None:
    """Return the verified repository root for a source/editable installation."""
    candidate = package_dir.parent.parent
    expected_package = candidate / "src" / "odibi_anchor"
    if (candidate / "tools").is_dir() and expected_package.resolve() == package_dir:
        return candidate
    return None


def _default_user_state_home(
    environment: Mapping[str, str],
    *,
    platform: str,
    user_home: Path,
) -> Path:
    """Return the standard-library user state location for the current platform."""
    if platform == "nt":
        local_app_data = environment.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "odibi-anchor"
        return user_home / "AppData" / "Local" / "odibi-anchor"

    xdg_state_home = environment.get("XDG_STATE_HOME")
    if xdg_state_home:
        return Path(xdg_state_home) / "odibi-anchor"
    return user_home / ".local" / "state" / "odibi-anchor"


def resolve_runtime_paths(
    profile_cw_root: str | os.PathLike[str] | None = None,
    *,
    package_file: str | os.PathLike[str] | None = None,
    environment: Mapping[str, str] | None = None,
    platform: str | None = None,
    user_home: str | os.PathLike[str] | None = None,
) -> RuntimePaths:
    """Resolve distribution resources and writable Odibi Anchor state.

    Args:
        profile_cw_root: Odibi Anchor home selected by an active environment
            profile. ``ANCHOR_HOME`` has higher precedence.
        package_file: Package-local file used for deterministic source detection.
            Defaults to this module and exists primarily for path-unit tests.
        environment: Environment mapping. Defaults to ``os.environ``.
        platform: Operating-system family. Defaults to ``os.name``.
        user_home: User home for deterministic fallback tests. Defaults to
            ``Path.home()``.

    Returns:
        Resolved immutable resources and writable home without creating directories.
    """
    active_environment = os.environ if environment is None else environment
    active_platform = os.name if platform is None else platform
    active_user_home = Path.home() if user_home is None else Path(user_home)
    module_file = Path(__file__) if package_file is None else Path(package_file)
    package_dir = module_file.resolve().parent

    source_root = _source_checkout_root(package_dir)
    resource_root = source_root if source_root is not None else package_dir.parent

    explicit_home = active_environment.get("ANCHOR_HOME")
    if explicit_home:
        anchor_home = Path(explicit_home)
    elif profile_cw_root:
        anchor_home = Path(profile_cw_root)
    elif active_environment.get("DATABRICKS_RUNTIME_VERSION"):
        raise RuntimeError(
            "ANCHOR_HOME must be set to a durable external path on Databricks. "
            "Ephemeral driver-local storage is not safe for writable state "
            "and the source checkout must not be used as a state directory."
        )
    else:
        anchor_home = _default_user_state_home(
            active_environment,
            platform=active_platform,
            user_home=active_user_home,
        )

    resolved_resources = resource_root.expanduser().resolve()
    resolved_home = anchor_home.expanduser().resolve()
    return RuntimePaths(
        source_checkout=source_root is not None,
        resource_root=resolved_resources,
        instructions_file=resolved_resources / ".assistant_instructions.md",
        skills_dir=resolved_resources / ".assistant" / "skills",
        tools_dir=resolved_resources / "tools",
        anchor_home=resolved_home,
    )


def resolve_resource_root(
    package_file: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve the distribution resource root without requiring ANCHOR_HOME.

    This is a lightweight alternative to ``resolve_runtime_paths`` for callers
    that only need the immutable resource root (e.g. config-file lookup) and
    must not trigger the Databricks ``ANCHOR_HOME`` guard.
    """
    module_file = Path(__file__) if package_file is None else Path(package_file)
    package_dir = module_file.resolve().parent
    source_root = _source_checkout_root(package_dir)
    resource_root = source_root if source_root is not None else package_dir.parent
    return resource_root.expanduser().resolve()
