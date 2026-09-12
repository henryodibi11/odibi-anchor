"""Deterministic, fail-closed PortfolioV1 configuration primitives.

The public functions return JSON-compatible dictionaries.  Configuration dictionaries use
the TOML shape documented by :func:`portfolio_schema`; write/scaffold/add responses include
``sha256`` and one machine-readable ``next_operation``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import tomllib
from contextlib import suppress
from copy import deepcopy
from pathlib import Path, PureWindowsPath
from typing import Any

_MAX_BYTES = 1024 * 1024
_IS_WINDOWS = os.name == "nt"
_ADAPTERS = frozenset({"amp", "claude", "databricks", "chatgpt"})
_ID = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,127})\Z")
_PROJECT_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SECRET = re.compile(r"(?:^|_)(?:secret|token|password|passwd|credential|api_key|private_key)(?:$|_)", re.I)
_PERSONA_FORBIDDEN = frozenset(
    {
        "host",
        "host_id",
        "project",
        "project_id",
        "target",
        "target_root",
        "adapter",
        "route",
        "routing",
        "permission",
        "permissions",
        "allow",
        "deny",
    }
)
_TOP = frozenset(
    {
        "schema_version",
        "incomplete_fields",
        "authority",
        "durability",
        "hosts",
        "projects",
        "personas",
    }
)


def portfolio_schema() -> dict[str, Any]:
    """Return the bounded PortfolioV1 schema as a machine-readable dictionary."""
    return {
        "schema_version": 1,
        "authority": {"required": ["id", "trust_domain"], "trust_domain": "work"},
        "durability": {
            "optional": True,
            "retention": {
                "required": ["days", "minimum_snapshots"],
                "days": {"minimum": 1, "maximum": 3650},
                "minimum_snapshots": {"minimum": 1, "maximum": 1000},
            },
        },
        "hosts": {
            "key": "safe_id",
            "required": ["adapter", "local_state_root"],
            "optional": ["instruction_root", "durable_root"],
            "adapters": sorted(_ADAPTERS),
        },
        "projects": {
            "key": "safe_id",
            "required": ["targets"],
            "optional": ["repository", "artifact_namespace"],
            "targets": {"key": "host_id", "value": "absolute_path"},
        },
        "personas": {"key": "safe_id", "fields": ["task_mode", "memory_scope"], "advisory": True},
    }


def _config_path(path: str | os.PathLike[str], *, must_exist: bool = False) -> Path:
    raw = os.fspath(path)
    if not isinstance(raw, str) or not raw or "\n" in raw or "\r" in raw or "\x00" in raw:
        raise ValueError("config path must be an explicit, non-empty, single-line path")
    value = Path(raw)
    if not value.is_absolute():
        raise ValueError("config path must be absolute")
    try:
        mode = value.lstat().st_mode
    except FileNotFoundError:
        if must_exist:
            raise ValueError("config file does not exist") from None
    else:
        if stat.S_ISLNK(mode):
            raise ValueError("config path must not be a symlink")
        if not stat.S_ISREG(mode):
            raise ValueError("config path must be a regular file")
    return value


def _safe_id(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if not value and allow_empty:
        return value
    if not _ID.fullmatch(value):
        raise ValueError(f"{label} is not a safe ID")
    return value


def _safe_project_id(value: Any, label: str = "project ID") -> str:
    if not isinstance(value, str) or len(value) > 128 or not _PROJECT_ID.fullmatch(value):
        raise ValueError(f"{label} must be a canonical lowercase hyphenated project ID")
    return value


def _absolute(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    windows = PureWindowsPath(value)
    native_absolute = os.path.isabs(value)
    windows_absolute = windows.is_absolute() and bool(windows.drive)
    if not value or "\n" in value or "\r" in value or "\x00" in value or not (native_absolute or windows_absolute):
        raise ValueError(f"{label} must be a non-empty absolute path")
    return str(windows) if windows_absolute else os.path.normpath(value)


def _path_identity(value: str) -> tuple[str, str]:
    windows = PureWindowsPath(value)
    if windows.is_absolute() and windows.drive:
        return "windows", str(windows).casefold()
    return "posix", os.path.normpath(value)


def _check_keys(table: dict[str, Any], allowed: set[str] | frozenset[str], label: str) -> None:
    for key in table:
        if _SECRET.search(key):
            raise ValueError(f"secret-like field is forbidden: {label}.{key}")
    unknown = sorted(set(table) - set(allowed))
    if unknown:
        raise ValueError(f"unknown {label} field(s): {', '.join(unknown)}")


def _table(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a table")
    return value


def _structural(portfolio: Any) -> dict[str, Any]:
    root = _table(portfolio, "portfolio")
    if ".active_project" in root or "active_project" in root:
        raise ValueError("active-project authority is forbidden")
    _check_keys(root, _TOP, "top-level")
    version = root.get("schema_version")
    if type(version) is not int or version != 1:
        raise ValueError(f"unsupported schema_version: {version!r}")
    incomplete = root.get("incomplete_fields", [])
    if not isinstance(incomplete, list) or not all(isinstance(item, str) and item for item in incomplete):
        raise ValueError("incomplete_fields must be an array of non-empty strings")

    authority = _table(root.get("authority", {}), "authority")
    _check_keys(authority, {"id", "trust_domain"}, "authority")
    if "id" in authority:
        _safe_id(authority["id"], "authority.id", allow_empty=True)
    if authority.get("trust_domain") not in {None, "work"}:
        raise ValueError("authority.trust_domain must be 'work'")

    durability = _table(root.get("durability", {}), "durability")
    _check_keys(durability, {"retention"}, "durability")
    if "retention" in durability:
        retention = _table(durability["retention"], "durability.retention")
        _check_keys(retention, {"days", "minimum_snapshots"}, "durability.retention")
        if set(retention) != {"days", "minimum_snapshots"}:
            raise ValueError(
                "durability.retention requires days and minimum_snapshots"
            )
        for key, maximum in (("days", 3650), ("minimum_snapshots", 1000)):
            value = retention[key]
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(
                    f"durability.retention.{key} must be an integer from 1 through {maximum}"
                )

    hosts = _table(root.get("hosts", {}), "hosts")
    for host_id, host_value in hosts.items():
        _safe_id(host_id, "host ID")
        host = _table(host_value, f"hosts.{host_id}")
        _check_keys(host, {"adapter", "local_state_root", "instruction_root", "durable_root"}, f"hosts.{host_id}")
        if "adapter" in host and (not isinstance(host["adapter"], str) or host["adapter"] not in _ADAPTERS):
            raise ValueError(f"hosts.{host_id}.adapter is unsupported")
        for key in ("local_state_root", "instruction_root", "durable_root"):
            if key in host:
                _absolute(host[key], f"hosts.{host_id}.{key}")

    projects = _table(root.get("projects", {}), "projects")
    roots_by_host: dict[str, dict[str, str]] = {}
    for project_id, project_value in projects.items():
        _safe_project_id(project_id)
        project = _table(project_value, f"projects.{project_id}")
        _check_keys(project, {"repository", "artifact_namespace", "targets"}, f"projects.{project_id}")
        for key in ("repository", "artifact_namespace"):
            if key in project and not isinstance(project[key], str):
                raise ValueError(f"projects.{project_id}.{key} must be a string")
        targets = _table(project.get("targets", {}), f"projects.{project_id}.targets")
        for target_host, target in targets.items():
            _safe_id(target_host, "target host ID")
            normalized = _absolute(target, f"projects.{project_id}.targets.{target_host}")
            identity = "\0".join(_path_identity(normalized))
            prior = roots_by_host.setdefault(target_host, {}).get(identity)
            if prior is not None:
                raise ValueError(f"duplicate target root for host {target_host}: projects {prior} and {project_id}")
            roots_by_host[target_host][identity] = project_id

    personas = _table(root.get("personas", {}), "personas")
    for persona_id, persona_value in personas.items():
        _safe_id(persona_id, "persona ID")
        persona = _table(persona_value, f"personas.{persona_id}")
        forbidden = sorted(set(persona) & _PERSONA_FORBIDDEN)
        if forbidden:
            raise ValueError(f"persona routing/permission field(s) forbidden: {', '.join(forbidden)}")
        _check_keys(persona, {"task_mode", "memory_scope"}, f"personas.{persona_id}")
        if not all(isinstance(value, str) for value in persona.values()):
            raise ValueError(f"personas.{persona_id} values must be strings")
    return root


def validate_portfolio(portfolio: Any, *, host_id: str | None = None) -> dict[str, Any]:
    """Validate structure and readiness, returning grouped findings and one next operation."""
    root = _structural(portfolio)
    required: list[dict[str, str]] = []
    recommended: list[dict[str, str]] = []
    verified: list[dict[str, str]] = []
    configured: list[dict[str, str]] = []
    na: list[dict[str, str]] = []

    def missing(field: str, reason: str) -> None:
        required.append({"field": field, "reason": reason})

    authority = root.get("authority", {})
    if not authority.get("id"):
        missing("authority.id", "A non-empty authority ID is required for launch.")
    else:
        configured.append({"field": "authority.id", "reason": "A safe non-empty ID is configured."})
    if authority.get("trust_domain") != "work":
        missing("authority.trust_domain", "PortfolioV1 requires the work trust domain.")
    else:
        configured.append({"field": "authority.trust_domain", "reason": "The fixed work trust domain is configured."})

    retention = root.get("durability", {}).get("retention")
    if retention is None:
        na.append(
            {
                "field": "durability.retention",
                "reason": "Automatic durable snapshot retention is not configured.",
            }
        )
    else:
        configured.extend(
            {
                "field": f"durability.retention.{key}",
                "reason": "A bounded automatic snapshot retention value is configured.",
            }
            for key in ("days", "minimum_snapshots")
        )

    hosts = root.get("hosts", {})
    if not hosts:
        missing("hosts", "At least one host is required for launch.")
    for key, host in sorted(hosts.items()):
        for field in ("adapter", "local_state_root"):
            if not host.get(field):
                missing(f"hosts.{key}.{field}", f"Host {field} is required for launch.")
            else:
                configured.append({"field": f"hosts.{key}.{field}", "reason": "A value is configured but not host-probed."})
        for field in ("instruction_root", "durable_root"):
            required_on_host = field == "durable_root" and host.get("adapter") == "databricks"
            target = (required if required_on_host else recommended) if field not in host else configured
            target.append(
                {
                    "field": f"hosts.{key}.{field}",
                    "reason": (
                        "Databricks local state requires a separate durable snapshot root."
                        if required_on_host
                        else "Optional host separation is recommended."
                    ) if field not in host
                    else "An absolute path is configured.",
                }
            )
        state_root = host.get("local_state_root")
        if (
            host.get("adapter") == "databricks"
            and state_root
            and _path_identity(state_root)[0] == "posix"
            and any(
                state_root == prefix or state_root.startswith(prefix + "/")
                for prefix in ("/Workspace", "/Volumes", "/dbfs")
            )
        ):
            raise ValueError(f"hosts.{key}.local_state_root must use Databricks local compute")

    projects = root.get("projects", {})
    if not projects:
        missing("projects.id", "At least one non-empty project ID is required for launch.")
    for key, project in sorted(projects.items()):
        for field in ("repository", "artifact_namespace"):
            if project.get(field):
                configured.append({"field": f"projects.{key}.{field}", "reason": "A non-empty value is configured."})
            else:
                na.append({"field": f"projects.{key}.{field}", "reason": "Optional metadata is not required for routing."})
        if not project.get("targets"):
            missing(f"projects.{key}.targets", "At least one host target is required for launch.")

    selected: dict[str, Any] | None = None
    if host_id is None:
        na.append({"field": "selected_host", "reason": "No host_id was requested; local existence was not inferred."})
    else:
        normalized_host = _safe_id(host_id, "host_id")
        if normalized_host not in hosts:
            missing(f"hosts.{normalized_host}", "The requested host is not configured.")
            selected = {"host_id": normalized_host, "status": "missing", "targets": []}
        else:
            target_status = []
            state_root = hosts[normalized_host].get("local_state_root")
            for project_id, project in sorted(projects.items()):
                if normalized_host in project.get("targets", {}):
                    target = project["targets"][normalized_host]
                    target_status.append(
                        {"project_id": project_id, "target_root": target, "exists": Path(target).is_dir()}
                    )
            selected = {
                "host_id": normalized_host,
                "status": "configured",
                "local_state_root": state_root,
                "local_state_root_exists": Path(state_root).is_dir() if state_root else False,
                "targets": target_status,
            }
            verified.append(
                {
                    "field": f"hosts.{normalized_host}.local_state_root.exists",
                    "reason": "The selected host path was probed on this runtime.",
                }
            )
            verified.extend(
                {
                    "field": f"projects.{item['project_id']}.targets.{normalized_host}.exists",
                    "reason": "The selected host target was probed on this runtime.",
                }
                for item in target_status
            )

    declared = set(root.get("incomplete_fields", []))
    actual = {item["field"] for item in required}
    stale = sorted(declared - actual)
    if stale:
        raise ValueError(f"incomplete_fields contains resolved field(s): {', '.join(stale)}")
    status = "incomplete" if required else "valid"
    next_operation = (
        {"operation": "supply_field", "field": required[0]["field"], "reason": required[0]["reason"]}
        if required
        else {
            "operation": "resolve_project",
            "reason": "PortfolioV1 is structurally ready for an explicit host-bound resolution.",
        }
    )
    return {
        "status": status,
        "required_missing": required,
        "recommended_missing": recommended,
        "prefilled_verified": verified,
        "configured": configured,
        "not_applicable": na,
        "selected_host": selected,
        "next_operation": next_operation,
    }


def load_portfolio(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load and structurally validate one explicit PortfolioV1 TOML file."""
    config = _config_path(path, must_exist=True)
    if config.stat().st_size > _MAX_BYTES:
        raise ValueError("config file exceeds the 1 MiB limit")
    data = config.read_bytes()
    try:
        portfolio = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"invalid PortfolioV1 TOML: {exc}") from exc
    _structural(portfolio)
    return portfolio


def load_portfolio_document(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load a portfolio with its explicit path and content digest."""
    config = _config_path(path, must_exist=True)
    portfolio = load_portfolio(config)
    return {"path": str(config), "sha256": _digest(config.read_bytes()), "portfolio": portfolio}


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _render(portfolio: dict[str, Any], *, comments: bool = False) -> bytes:
    root = _structural(portfolio)
    lines = [
        "# Odibi Anchor PortfolioV1 — contains routing metadata, never secrets." if comments else None,
        "# Complete every field listed in incomplete_fields before launch." if comments else None,
        "schema_version = 1",
    ]
    incomplete = sorted(root.get("incomplete_fields", []))
    if incomplete:
        lines.append("incomplete_fields = [" + ", ".join(_quote(item) for item in incomplete) + "]")
    authority = root.get("authority", {})
    lines += ["", "[authority]"]
    for key in ("id", "trust_domain"):
        if key in authority:
            lines.append(f"{key} = {_quote(authority[key])}")
    retention = root.get("durability", {}).get("retention")
    if retention is not None:
        lines += ["", "[durability.retention]"]
        lines.append(f"days = {retention['days']}")
        lines.append(f"minimum_snapshots = {retention['minimum_snapshots']}")
    for host_id in sorted(root.get("hosts", {})):
        host = root["hosts"][host_id]
        lines += ["", f"[hosts.{_quote(host_id)}]"]
        for key in ("adapter", "local_state_root", "instruction_root", "durable_root"):
            if key in host:
                lines.append(f"{key} = {_quote(host[key])}")
    for project_id in sorted(root.get("projects", {})):
        project = root["projects"][project_id]
        lines += ["", f"[projects.{_quote(project_id)}]"]
        for key in ("repository", "artifact_namespace"):
            if key in project:
                lines.append(f"{key} = {_quote(project[key])}")
        lines += ["", f"[projects.{_quote(project_id)}.targets]"]
        for target_host in sorted(project.get("targets", {})):
            lines.append(f"{_quote(target_host)} = {_quote(project['targets'][target_host])}")
    for persona_id in sorted(root.get("personas", {})):
        persona = root["personas"][persona_id]
        lines += ["", f"[personas.{_quote(persona_id)}]"]
        for key in ("task_mode", "memory_scope"):
            if key in persona:
                lines.append(f"{key} = {_quote(persona[key])}")
    return ("\n".join(line for line in lines if line is not None) + "\n").encode()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes, expected_sha256: str | None) -> str:
    if len(data) > _MAX_BYTES:
        raise ValueError("rendered config exceeds the 1 MiB limit")
    current: bytes | None = None
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISLNK(mode):
            raise ValueError("config path must not be a symlink")
        if not stat.S_ISREG(mode):
            raise ValueError("config path must be a regular file")
        current = path.read_bytes()
    if expected_sha256 is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ValueError("expected_sha256 must be 64 lowercase hexadecimal characters")
        actual = _digest(current) if current is not None else None
        if actual != expected_sha256:
            raise ValueError(f"base digest changed: expected {expected_sha256}, found {actual or 'absent'}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        if path.is_symlink():
            raise ValueError("config path became a symlink during write")
        os.replace(temporary, path)
        if not _IS_WINDOWS:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
    return _digest(data)


def write_portfolio(
    path: str | os.PathLike[str], portfolio: dict[str, Any], *, expected_sha256: str | None = None
) -> dict[str, Any]:
    """Atomically render and write a portfolio after an optional optimistic digest check."""
    config = _config_path(path)
    validation = validate_portfolio(portfolio)
    digest = _atomic_write(config, _render(portfolio), expected_sha256)
    return {
        "status": "written",
        "path": str(config),
        "sha256": digest,
        "validation_status": validation["status"],
        "next_operation": validation["next_operation"],
    }


def scaffold_portfolio(
    path: str | os.PathLike[str],
    *,
    host_id: str,
    adapter: str,
    target_root: str,
    project_id: str | None = None,
    authority_id: str | None = None,
    local_state_root: str | None = None,
    instruction_root: str | None = None,
    durable_root: str | None = None,
) -> dict[str, Any]:
    """Create a commented portfolio containing only caller-supplied facts."""
    config = _config_path(path)
    if config.exists():
        raise ValueError("refusing to overwrite an existing portfolio scaffold")
    host = _safe_id(host_id, "host_id")
    if adapter not in _ADAPTERS:
        raise ValueError("adapter is unsupported")
    target = _absolute(target_root, "target_root")
    authority = "" if authority_id is None else _safe_id(authority_id, "authority_id")
    host_config: dict[str, str] = {"adapter": adapter}
    for field, value in (
        ("local_state_root", local_state_root),
        ("instruction_root", instruction_root),
        ("durable_root", durable_root),
    ):
        if value is not None:
            host_config[field] = _absolute(value, field)
    portfolio: dict[str, Any] = {
        "schema_version": 1,
        "incomplete_fields": ([] if authority else ["authority.id"]),
        "authority": {"id": authority, "trust_domain": "work"},
        "hosts": {host: host_config},
        "projects": {},
        "personas": {},
    }
    if project_id is None:
        if local_state_root is None:
            portfolio["incomplete_fields"].append(f"hosts.{host}.local_state_root")
        portfolio["incomplete_fields"].append("projects.id")
    else:
        project = _safe_project_id(project_id, "project_id")
        portfolio["projects"][project] = {"targets": {host: target}}
        if local_state_root is None:
            portfolio["incomplete_fields"].append(f"hosts.{host}.local_state_root")
    validation = validate_portfolio(portfolio, host_id=host)
    data = _render(portfolio, comments=True)
    digest = _atomic_write(config, data, None)
    return {
        "status": "created",
        "path": str(config),
        "sha256": digest,
        "portfolio": portfolio,
        "validation": validation,
        "next_operation": validation["next_operation"],
    }


def add_project(
    path: str | os.PathLike[str],
    *,
    project_id: str,
    host_id: str,
    target_root: str,
    repository: str | None = None,
    artifact_namespace: str | None = None,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Add one exact project/host binding after an optimistic base-digest check."""
    config = _config_path(path, must_exist=True)
    current = config.read_bytes()
    actual = _digest(current)
    if expected_sha256 is not None and expected_sha256 != actual:
        raise ValueError(f"base digest changed: expected {expected_sha256}, found {actual}")
    portfolio = load_portfolio(config)
    project = _safe_project_id(project_id, "project_id")
    host = _safe_id(host_id, "host_id")
    if host not in portfolio.get("hosts", {}):
        raise ValueError(f"host is not configured: {host}")
    if project in portfolio.get("projects", {}):
        raise ValueError(f"project ID already exists: {project}")
    entry: dict[str, Any] = {"targets": {host: _absolute(target_root, "target_root")}}
    if repository is not None:
        if not isinstance(repository, str) or not repository:
            raise ValueError("repository must be a non-empty string")
        entry["repository"] = repository
    if artifact_namespace is not None:
        if not isinstance(artifact_namespace, str) or not artifact_namespace:
            raise ValueError("artifact_namespace must be a non-empty string")
        entry["artifact_namespace"] = artifact_namespace
    updated = deepcopy(portfolio)
    updated.setdefault("projects", {})[project] = entry
    updated["incomplete_fields"] = [item for item in updated.get("incomplete_fields", []) if item != "projects.id"]
    validate_portfolio(updated, host_id=host)
    result = write_portfolio(config, updated, expected_sha256=actual)
    return {
        **result,
        "project_id": project,
        "host_id": host,
        "target_root": entry["targets"][host],
        "next_operation": {
            "operation": "portfolio.prepare",
            "arguments": {
                "config_path": str(config),
                "host_id": host,
                "project_id": project,
            },
            "reason": "Prepare and register the newly configured exact project route.",
        },
    }


def resolve_project(
    portfolio: dict[str, Any], *, host_id: str, project_id: str | None = None,
    target_root: str | None = None, persona_id: str | None = None,
) -> dict[str, Any]:
    """Resolve only explicit inputs or one exact canonical host target; never ambient state."""
    validation = validate_portfolio(portfolio, host_id=host_id)
    if validation["status"] != "valid":
        raise ValueError(f"portfolio is incomplete: {validation['required_missing'][0]['field']}")
    host = _safe_id(host_id, "host_id")
    persona = None
    if persona_id is not None:
        normalized_persona = _safe_id(persona_id, "persona_id")
        if normalized_persona not in portfolio.get("personas", {}):
            raise ValueError(f"persona is not configured: {normalized_persona}")
        persona = {
            "persona_id": normalized_persona,
            "advisory": True,
            **portfolio["personas"][normalized_persona],
        }
    projects = portfolio["projects"]
    target = _absolute(target_root, "target_root") if target_root is not None else None
    if project_id is not None:
        project = _safe_project_id(project_id, "project_id")
        if project not in projects:
            raise ValueError(f"project is not configured: {project}")
        configured = projects[project].get("targets", {}).get(host)
        if configured is None:
            raise ValueError(f"project {project} has no target for host {host}")
        if target is not None and _path_identity(configured) != _path_identity(target):
            raise ValueError(f"explicit project {project} does not match target_root {target}")
        selected = (project, configured)
        source = "explicit_project"
    else:
        if target is None:
            raise ValueError("project_id or target_root is required; ambient selectors are forbidden")
        matches = [
            (key, value["targets"][host])
            for key, value in projects.items()
            if host in value.get("targets", {})
            and _path_identity(value["targets"][host]) == _path_identity(target)
        ]
        if not matches:
            raise ValueError(f"no project target matches host {host} and target_root {target}")
        if len(matches) != 1:
            raise ValueError(f"ambiguous project target for host {host} and target_root {target}")
        selected = matches[0]
        source = "exact_target"
    return {
        "status": "resolved",
        "project_id": selected[0],
        "host_id": host,
        "target_root": selected[1],
        "binding_inputs": {"host_id": host, "project_id": selected[0], "target_root": selected[1]},
        "binding_source": source,
        "persona": persona,
        "environment": {
            "ANCHOR_HOME": portfolio["hosts"][host]["local_state_root"],
            "ANCHOR_MEMORY_DB": os.path.join(
                portfolio["hosts"][host]["local_state_root"], ".agent_memory.db"
            ).replace("\\", "/"),
            "ANCHOR_AUTHORITY_ID": portfolio["authority"]["id"],
            "ANCHOR_TRUST_DOMAIN": portfolio["authority"]["trust_domain"],
            "ANCHOR_PROJECT_ID": selected[0],
            "ANCHOR_PROJECT_ROOT": selected[1],
            **(
                {"ANCHOR_DURABLE_ROOT": portfolio["hosts"][host]["durable_root"]}
                if portfolio["hosts"][host].get("durable_root")
                else {}
            ),
            **(
                {
                    "ANCHOR_RETENTION_DAYS": str(
                        portfolio["durability"]["retention"]["days"]
                    ),
                    "ANCHOR_RETENTION_MINIMUM_SNAPSHOTS": str(
                        portfolio["durability"]["retention"]["minimum_snapshots"]
                    ),
                }
                if portfolio.get("durability", {}).get("retention") is not None
                else {}
            ),
        },
        "next_operation": {"operation": "bind_project", "reason": "Use exactly these canonical binding inputs."},
    }
