"""Resolve one Odibi Anchor checkout and run its authoritative bootstrap."""

from __future__ import annotations

if "__file__" not in globals():
    raise RuntimeError(
        "Odibi Anchor bootstrap requires __file__; use "
        "runpy.run_path(<exact .assistant/agent_bootstrap.py>) in the persistent Python process"
    )

import json
import os
import re
import runpy
import time
import urllib.request
from collections.abc import Mapping
from importlib import metadata
from pathlib import Path


def _validated_checkout(raw: object, *, source: str) -> Path:
    if not isinstance(raw, (str, os.PathLike)) or isinstance(raw, bytes):
        raise RuntimeError(f"{source} must be an absolute, non-tilde path")
    value = os.fspath(raw)
    if not isinstance(value, str) or not value or value.startswith("~") or not Path(value).is_absolute():
        raise RuntimeError(f"{source} must be an absolute, non-tilde path")
    checkout = Path(value).resolve(strict=False)
    markers = (
        checkout / "agent_bootstrap.py",
        checkout / "src" / "odibi_anchor" / "bootstrap.py",
    )
    if not checkout.is_dir() or not all(marker.is_file() for marker in markers):
        raise RuntimeError(
            f"{source} is not a Odibi Anchor source checkout: {checkout}; "
            "expected agent_bootstrap.py and src/odibi_anchor/bootstrap.py"
        )
    return checkout.resolve(strict=True)


_LAUNCHER_STARTED = time.perf_counter()
_LAUNCHER_PHASES: list[dict[str, object]] = []
_PYPI_URL = "https://pypi.org/pypi/odibi-anchor/json"
_VERSION_CHECK_TIMEOUT_ENV = "ANCHOR_VERSION_CHECK_TIMEOUT_SECONDS"
_STABLE_RELEASE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
# Older managed launchers ignore pins and demand the latest release, so pinning one
# would make the next bootstrap refuse to start after host guidance is downgraded.
_MINIMUM_PINNABLE_RELEASE = (0, 3, 24)

try:
    _launcher = Path(__file__).resolve(strict=True)
except (OSError, TypeError) as exc:
    raise RuntimeError("Odibi Anchor launcher __file__ is invalid") from exc


def _record_launcher_phase(name: str, started: float, outcome: str, **details: object) -> None:
    _LAUNCHER_PHASES.append({
        "phase": name,
        "scope": "launcher",
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "outcome": outcome,
        **details,
    })


def _structured(
    exc: BaseException,
    *,
    error_code: str,
    context: Mapping[str, object],
    next_operations: tuple[Mapping[str, object], ...] = (),
) -> BaseException:
    """Attach the odibi_anchor._recovery.attach_recovery fields before the package import.

    The version check runs before importing a possibly mismatched package, so this
    mirrors that contract exactly instead of importing it.
    """
    operations = [dict(operation) for operation in next_operations]
    exc.error_code = error_code  # type: ignore[attr-defined]
    exc.context = dict(context)  # type: ignore[attr-defined]
    exc.next_operations = operations  # type: ignore[attr-defined]
    if operations:
        exc.next_operation = operations[0]  # type: ignore[attr-defined]
        for field in ("copy_ready", "requires_owner", "retry_safety"):
            if field in operations[0]:
                setattr(exc, field, operations[0][field])
    return exc


def _launcher_call(project_id: str | None, **extra: str) -> str:
    init_globals = {"ANCHOR_PROJECT_ID": project_id or "<project-id>", **extra}
    return f"runpy.run_path({str(_launcher)!r}, init_globals={init_globals!r})"


def _version_check_timeout() -> float:
    raw = os.environ.get(_VERSION_CHECK_TIMEOUT_ENV)
    if raw is None:
        return 5.0
    try:
        seconds = float(raw)
    except ValueError:
        seconds = float("nan")
    if not 0 < seconds <= 600:
        raise RuntimeError(
            f"{_VERSION_CHECK_TIMEOUT_ENV} must be a number of seconds greater than 0 and at "
            f"most 600; got {raw!r}"
        )
    return seconds


# A just-published release can take minutes to reach pip's index view; --no-cache-dir skips a
# stale local cache, and Anchor never publishes pre-releases, so --pre is never the fix.
_INDEX_LAG_GUIDANCE = (
    "If pip reports no matching distribution for a release published in the last few minutes, "
    "wait a minute and rerun the same command; do not add --pre (Anchor releases are never "
    "pre-releases)."
)


def _latest_stable_release() -> str:
    """Resolve the newest non-yanked stable release from the public package index."""
    request = urllib.request.Request(
        _PYPI_URL,
        headers={"Accept": "application/json", "User-Agent": "odibi-anchor-managed-launcher"},
    )
    timeout = _version_check_timeout()
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except Exception as exc:
        if isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError):
            elapsed = round((time.perf_counter() - started) * 1000.0, 3)
            larger = f"{min(600.0, 2 * timeout):g}"
            raise _structured(
                TimeoutError(
                    f"bootstrap phase version_check timed out after {elapsed:.0f} ms at the pypi "
                    f"layer while accessing {_PYPI_URL} (limit {timeout:g} s). Classification: "
                    "the latest-stable lookup exceeded its bounded deadline; startup was refused "
                    "rather than assuming a version. Retry with a larger "
                    f"{_VERSION_CHECK_TIMEOUT_ENV}, or set an exact ANCHOR_PACKAGE_VERSION pin, "
                    "which skips the package index."
                ),
                error_code="bootstrap_phase_timeout",
                context={
                    "phase": "version_check", "sub_phase": None, "layer": "pypi",
                    "item": _PYPI_URL, "elapsed_ms": elapsed, "limit_seconds": timeout,
                    "setting": _VERSION_CHECK_TIMEOUT_ENV,
                },
                next_operations=(
                    {
                        "operation": "rerun_launcher_with_larger_timeout",
                        "environment": {_VERSION_CHECK_TIMEOUT_ENV: larger},
                        "copy_ready": (
                            f'os.environ["{_VERSION_CHECK_TIMEOUT_ENV}"] = "{larger}"  '
                            "# then rerun the launcher"
                        ),
                        "reason": "Retry the idempotent package-index lookup with a larger deadline.",
                        "requires_owner": False,
                        "retry_safety": "idempotent",
                    },
                    {
                        "operation": "rerun_launcher_with_version_pin",
                        "copy_ready": _launcher_call(
                            globals().get("ANCHOR_PROJECT_ID") or os.environ.get("ANCHOR_PROJECT_ID"),
                            ANCHOR_PACKAGE_VERSION="<exact-version>",
                        ),
                        "reason": "An exact pin skips the package index; choosing it is a version-policy decision.",
                        "requires_owner": True,
                        "retry_safety": "idempotent",
                    },
                ),
            ) from exc
        raise RuntimeError(
            "cannot verify the latest stable odibi-anchor release from PyPI; "
            "retry when package-index access is available, or set an exact "
            "ANCHOR_PACKAGE_VERSION pin, which skips the package index"
        ) from exc
    releases = payload.get("releases") if isinstance(payload, dict) else None
    if not isinstance(releases, dict):
        raise RuntimeError("PyPI returned an invalid odibi-anchor release index")
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for version, files in releases.items():
        if not isinstance(version, str) or re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
            continue
        if not isinstance(files, list) or not any(
            isinstance(item, dict) and item.get("yanked") is not True for item in files
        ):
            continue
        candidates.append((tuple(int(part) for part in version.split(".")), version))
    if not candidates:
        raise RuntimeError("PyPI reports no non-yanked stable odibi-anchor release")
    return max(candidates)[1]


def _requested_project_id() -> str | None:
    explicit = globals().get("ANCHOR_PROJECT_ID")
    environment = os.environ.get("ANCHOR_PROJECT_ID")
    if explicit is not None and (not isinstance(explicit, str) or not explicit):
        raise RuntimeError("ANCHOR_PROJECT_ID init global must be a non-empty string")
    if explicit is not None and environment is not None and explicit != environment:
        raise RuntimeError("ANCHOR_PROJECT_ID init global conflicts with the environment")
    return explicit or environment


def _validated_pin(raw: object, source: str) -> str:
    if not isinstance(raw, str) or _STABLE_RELEASE.fullmatch(raw) is None:
        raise _structured(
            RuntimeError(
                f"{source} must be an exact stable odibi-anchor release such as 0.3.24; "
                f"got {raw!r}"
            ),
            error_code="package_version_pin_invalid",
            context={"source": source, "value": repr(raw)},
        )
    if tuple(int(part) for part in raw.split(".")) < _MINIMUM_PINNABLE_RELEASE:
        minimum = ".".join(str(part) for part in _MINIMUM_PINNABLE_RELEASE)
        raise _structured(
            RuntimeError(
                f"{source} pins {raw}, but pins below {minimum} are unsupported: older managed "
                "launchers ignore pins and require the latest stable release, so the next "
                "bootstrap would refuse to start"
            ),
            error_code="package_version_pin_invalid",
            context={"source": source, "value": raw, "minimum_pinnable": minimum},
        )
    return raw


def _portfolio_pin(config_path: Path, instruction_root: Path) -> tuple[str, str] | None:
    """Read hosts.<id>.package_version for the one host matching this launcher."""
    if not config_path.is_file():
        return None
    try:
        import tomllib

        document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise _structured(
            RuntimeError(
                f"cannot read the managed portfolio {config_path} to resolve its package "
                f"version policy: {type(exc).__name__}: {exc}"
            ),
            error_code="managed_portfolio_unreadable",
            context={"config_path": str(config_path), "error_type": type(exc).__name__},
        ) from exc
    hosts = document.get("hosts")
    if not isinstance(hosts, dict):
        return None
    matches = []
    for host_id, settings in hosts.items():
        configured = settings.get("instruction_root") if isinstance(settings, dict) else None
        if isinstance(configured, str) and Path(configured).resolve() == instruction_root:
            matches.append((host_id, settings))
    # Zero or several matches are refused later with exact host diagnostics.
    if len(matches) != 1 or "package_version" not in matches[0][1]:
        return None
    host_id, settings = matches[0]
    source = f"portfolio {config_path} hosts.{host_id}.package_version"
    return _validated_pin(settings["package_version"], source), source


def _package_version_policy(config_path: Path, instruction_root: Path) -> dict[str, str]:
    """Precedence: init global, then environment, then portfolio host field, then latest."""
    if "ANCHOR_PACKAGE_VERSION" in globals():
        source = "ANCHOR_PACKAGE_VERSION init global"
        return {"policy": "pinned", "source": source,
                "required": _validated_pin(globals()["ANCHOR_PACKAGE_VERSION"], source)}
    if "ANCHOR_PACKAGE_VERSION" in os.environ:
        source = "ANCHOR_PACKAGE_VERSION environment variable"
        return {"policy": "pinned", "source": source,
                "required": _validated_pin(os.environ["ANCHOR_PACKAGE_VERSION"], source)}
    pinned = _portfolio_pin(config_path, instruction_root)
    if pinned is not None:
        return {"policy": "pinned", "source": pinned[1], "required": pinned[0]}
    return {"policy": "latest_stable", "source": "default"}


_HOST_BINDING = ".odibi-anchor-host-binding.json"
_BINDING_SOURCE = "host binding sidecar (<instruction-root>/.odibi-anchor-host-binding.json)"
_DEFAULT_SOURCE = "instruction-root default (<launcher>/../.odibi-anchor/anchor.toml)"


def _nested_install(instruction_root: Path) -> dict[str, object] | None:
    """Describe a launcher inside a nested ``.assistant/.assistant`` tree; never select from it."""
    if instruction_root.name != ".assistant":
        return None
    parent = instruction_root.parent
    launcher = parent / ".assistant" / "agent_bootstrap.py"
    portfolio = parent / ".odibi-anchor" / "anchor.toml"
    return {
        "detected": True,
        "instruction_root": str(instruction_root),
        "probable_instruction_root": str(parent),
        "probable_launcher": str(launcher),
        "probable_launcher_exists": launcher.is_file(),
        "probable_portfolio": str(portfolio),
        "probable_portfolio_exists": portfolio.is_file(),
    }


def _nested_install_text(nested: Mapping[str, object] | None) -> str:
    if not nested:
        return ""
    state = "exists" if nested["probable_portfolio_exists"] else "does not exist"
    return (
        f" Nested install detected: this launcher's instruction root {nested['instruction_root']} "
        "is itself named .assistant, so the launcher sits in a nested .assistant/.assistant tree "
        "and searched portfolio paths below it. The probable intended launcher is "
        f"{nested['probable_launcher']} (instruction root {nested['probable_instruction_root']}); "
        f"its default portfolio {nested['probable_portfolio']} {state}."
    )


def _discovery_operations(
    instruction_root: Path, nested: Mapping[str, object] | None, *paths: str,
) -> tuple[dict[str, object], ...]:
    project_id = globals().get("ANCHOR_PROJECT_ID") or os.environ.get("ANCHOR_PROJECT_ID")
    operations: list[dict[str, object]] = [
        {
            "operation": "rerun_launcher_with_portfolio",
            "copy_ready": _launcher_call(project_id, ANCHOR_PORTFOLIO_CONFIG=path),
            "reason": "Select the exact existing portfolio explicitly.",
            "requires_owner": True,
            "retry_safety": "idempotent",
        }
        for path in (paths or ("<absolute path to anchor.toml>",))
    ]
    operations.append({
        "operation": "setup_host.record_portfolio",
        "copy_ready": (
            f"anchor setup-host <adapter> --target {instruction_root} "
            "--portfolio <absolute path to anchor.toml>"
        ),
        "reason": "Record the intended portfolio and host in the host binding sidecar.",
        "requires_owner": True,
        "retry_safety": "idempotent",
    })
    if nested and nested["probable_launcher_exists"]:
        operations.append({
            "operation": "run_probable_launcher",
            "copy_ready": (
                f"runpy.run_path({str(nested['probable_launcher'])!r}, "
                f"init_globals={{'ANCHOR_PROJECT_ID': {project_id or '<project-id>'!r}}})"
            ),
            "reason": "Run the launcher of the probable intended instruction root instead.",
            "requires_owner": True,
            "retry_safety": "idempotent",
        })
    return tuple(operations)


def _host_binding(instruction_root: Path, nested: Mapping[str, object] | None) -> dict[str, str]:
    """Strictly validate the sidecar written by ``anchor setup-host --portfolio``."""
    path = instruction_root / _HOST_BINDING

    def invalid(reason: str) -> BaseException:
        return _structured(
            RuntimeError(
                f"host binding {path} is invalid: {reason}. Refusing to guess a portfolio."
                + _nested_install_text(nested)
                + " Supported correction: rerun `anchor setup-host <adapter> --target "
                f"{instruction_root} --portfolio <absolute path to anchor.toml>`, or rerun this "
                "launcher with the exact ANCHOR_PORTFOLIO_CONFIG. Do not hand-edit the sidecar."
            ),
            error_code="managed_host_binding_invalid",
            context={
                "binding_path": str(path), "reason": reason,
                "instruction_root": str(instruction_root), "nested_install": nested,
            },
            next_operations=_discovery_operations(instruction_root, nested),
        )

    try:
        if path.is_symlink() or not path.is_file():
            raise invalid("not a regular file")
        content = path.read_bytes()
        value = json.loads(content)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise invalid(f"unreadable JSON ({type(exc).__name__})") from exc
    keys = {"config_path", "host_id", "instruction_root", "version"}
    if (
        not isinstance(value, dict) or set(value) != keys or value["version"] != 1
        or not all(isinstance(value[key], str) and value[key] for key in keys - {"version"})
    ):
        raise invalid("unsupported schema")
    if content != (json.dumps(value, indent=2, sort_keys=True) + "\n").encode():
        raise invalid("non-canonical JSON")
    config = value["config_path"]
    if config.startswith("~") or "\n" in config or not Path(config).is_absolute():
        raise invalid("config_path is not an absolute path")
    if Path(value["instruction_root"]).resolve() != instruction_root:
        raise invalid(f"it names instruction root {value['instruction_root']}")
    if Path(config).is_file():
        try:
            import tomllib

            hosts = tomllib.loads(Path(config).read_text(encoding="utf-8")).get("hosts")
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise invalid(f"its portfolio is unreadable ({type(exc).__name__})") from exc
        settings = hosts.get(value["host_id"]) if isinstance(hosts, dict) else None
        configured = settings.get("instruction_root") if isinstance(settings, dict) else None
        if not isinstance(configured, str) or Path(configured).resolve() != instruction_root:
            raise invalid(
                f"portfolio host {value['host_id']!r} does not declare this instruction root"
            )
    return value


def _portfolio_search(
    instruction_root: Path,
) -> tuple[Path, list[dict[str, object]], dict[str, str] | None]:
    """Select explicit input, then the host binding sidecar, then the instruction-root default.

    The sidecar entry appears only when the sidecar exists, so hosts without one keep the
    v0.3.24 search exactly. A sidecar that disagrees with an existing default is ambiguous.
    """
    default = instruction_root / ".odibi-anchor" / "anchor.toml"
    nested = _nested_install(instruction_root)
    explicit = "ANCHOR_PORTFOLIO_CONFIG" in globals() or "ANCHOR_PORTFOLIO_CONFIG" in os.environ
    has_binding = os.path.lexists(instruction_root / _HOST_BINDING)
    binding = _host_binding(instruction_root, nested) if has_binding and not explicit else None
    candidates: list[tuple[str, object, bool]] = [
        ("ANCHOR_PORTFOLIO_CONFIG init global", globals().get("ANCHOR_PORTFOLIO_CONFIG"),
         "ANCHOR_PORTFOLIO_CONFIG" in globals()),
        ("ANCHOR_PORTFOLIO_CONFIG environment variable", os.environ.get("ANCHOR_PORTFOLIO_CONFIG"),
         "ANCHOR_PORTFOLIO_CONFIG" in os.environ),
    ]
    if has_binding:
        candidates.append((_BINDING_SOURCE, binding["config_path"] if binding else None, True))
    candidates.append((_DEFAULT_SOURCE, str(default), True))
    selected: Path | None = None
    searched: list[dict[str, object]] = []
    for precedence, (source, raw, provided) in enumerate(candidates, start=1):
        entry: dict[str, object] = {"precedence": precedence, "source": source}
        if source == _BINDING_SOURCE:
            entry["binding_path"] = str(instruction_root / _HOST_BINDING)
        if not provided:
            entry.update(path=None, status="unset")
        elif raw is None:
            entry.update(path=None, status="not_consulted_lower_precedence")
        else:
            path = Path(os.fspath(raw))  # type: ignore[arg-type]
            exists = path.is_file()
            entry.update(path=str(path), exists=exists)
            if selected is None:
                selected = path
                entry["status"] = "selected" if exists else "selected_missing"
            else:
                entry["status"] = "not_consulted_lower_precedence"
        searched.append(entry)
    assert selected is not None
    if binding is not None and default.is_file() and default.resolve() != selected.resolve():
        searched[-1]["status"] = "conflicts_with_selected"
        raise _structured(
            RuntimeError(
                f"managed portfolio is ambiguous for instruction root {instruction_root}: the host "
                f"binding sidecar names {selected} (host {binding['host_id']}), but a different "
                f"portfolio exists at the instruction-root default {default}. Searched in "
                "precedence order: "
                + "; ".join(f"{item['precedence']}. {item['source']}: {item['path']}" for item in searched)
                + "." + _nested_install_text(nested)
                + " Supported correction: the owner chooses one portfolio, then reruns `anchor "
                f"setup-host <adapter> --target {instruction_root} --portfolio <chosen>` or passes "
                "it as ANCHOR_PORTFOLIO_CONFIG. Do not copy, move, delete or hand-edit either "
                "portfolio or the sidecar."
            ),
            error_code="managed_portfolio_ambiguous",
            context={
                "instruction_root": str(instruction_root),
                "binding": dict(binding),
                "default_path": str(default),
                "searched_paths": searched,
                "nested_install": nested,
            },
            next_operations=_discovery_operations(
                instruction_root, nested, str(selected), str(default)
            ),
        )
    return selected, searched, binding


if "ANCHOR_SOURCE_CHECKOUT" in globals():
    _checkout = _validated_checkout(
        globals()["ANCHOR_SOURCE_CHECKOUT"], source="ANCHOR_SOURCE_CHECKOUT init global"
    )
elif "ANCHOR_SOURCE_CHECKOUT" in os.environ:
    _checkout = _validated_checkout(
        os.environ["ANCHOR_SOURCE_CHECKOUT"], source="ANCHOR_SOURCE_CHECKOUT environment variable"
    )
else:
    _assistant_parent = _launcher.parent.parent
    _source_candidate = _assistant_parent
    _copied_candidate = _assistant_parent / "odibi_anchor"
    try:
        _checkout = _validated_checkout(_source_candidate, source="source-tree topology")
    except RuntimeError:
        try:
            _checkout = _validated_checkout(_copied_candidate, source="copied-tree topology")
        except RuntimeError:
            _checkout = None

if _checkout is None:
    _instruction_root = _launcher.parent.parent
    _is_databricks = _launcher.as_posix().startswith("/Workspace/") or bool(
        os.environ.get("DATABRICKS_RUNTIME_VERSION")
    )
    _phase_started = time.perf_counter()
    _nested = _nested_install(_instruction_root)
    try:
        _config_path, _portfolio_searched, _binding = _portfolio_search(_instruction_root)
    except BaseException as _discovery_error:
        _record_launcher_phase(
            "portfolio_discovery", _phase_started, "raised",
            error_code=getattr(_discovery_error, "error_code", None),
            nested_install=_nested is not None,
        )
        raise
    _record_launcher_phase(
        "portfolio_discovery", _phase_started, "ok" if _config_path.is_file() else "not_found",
        config_path=str(_config_path),
        source=next(
            entry["source"] for entry in _portfolio_searched
            if entry.get("status") in {"selected", "selected_missing"}
        ),
        nested_install=_nested is not None,
    )
    _phase_started = time.perf_counter()
    if _is_databricks:
        try:
            _policy = _package_version_policy(_config_path, _instruction_root)
            try:
                _installed = metadata.version("odibi-anchor")
            except metadata.PackageNotFoundError:
                _installed = None
            if _policy["policy"] == "pinned":
                _pinned = _policy["required"]
                if _installed != _pinned:
                    raise _structured(
                        RuntimeError(
                            f"Odibi Anchor requires the pinned release {_pinned} in this Python "
                            f"process ({_policy['source']}). Run "
                            f'`%pip install --no-cache-dir "odibi-anchor[databricks]=={_pinned}"`, then '
                            "`dbutils.library.restartPython()` and rerun this launcher. "
                            f"Pinned: {_pinned}; installed: {_installed or 'missing'}. "
                            + _INDEX_LAG_GUIDANCE
                        ),
                        error_code="package_version_mismatch",
                        context={**_policy, "installed": _installed},
                        next_operations=({
                            "operation": "install_package",
                            "copy_ready": f'%pip install --no-cache-dir "odibi-anchor[databricks]=={_pinned}"',
                            "then": "dbutils.library.restartPython()",
                            "reason": "Install the pinned release, restart Python, and rerun. "
                                      + _INDEX_LAG_GUIDANCE,
                            "requires_owner": False,
                            "retry_safety": "idempotent",
                        },),
                    )
            else:
                _latest = _latest_stable_release()
                _policy["required"] = _latest
                if _installed != _latest:
                    raise _structured(
                        RuntimeError(
                            "Odibi Anchor requires the latest stable release in this Python "
                            "process. Run "
                            f'`%pip install --no-cache-dir "odibi-anchor[databricks]=={_latest}"`, then '
                            "`dbutils.library.restartPython()` and rerun this launcher. "
                            f"Resolved latest stable: {_latest}; installed: {_installed or 'missing'}. "
                            + _INDEX_LAG_GUIDANCE
                        ),
                        error_code="package_version_mismatch",
                        context={**_policy, "installed": _installed},
                        next_operations=({
                            "operation": "install_package",
                            "copy_ready": f'%pip install --no-cache-dir "odibi-anchor[databricks]=={_latest}"',
                            "then": "dbutils.library.restartPython()",
                            "reason": "Install the latest stable release, restart Python, and rerun. "
                                      + _INDEX_LAG_GUIDANCE,
                            "requires_owner": False,
                            "retry_safety": "idempotent",
                        },),
                    )
        except BaseException as _version_error:
            _record_launcher_phase(
                "version_check", _phase_started,
                "timeout" if getattr(_version_error, "error_code", None) == "bootstrap_phase_timeout"
                else "raised",
                error_type=type(_version_error).__name__,
            )
            raise
        _record_launcher_phase(
            "version_check", _phase_started, "ok", installed=_installed, **_policy
        )
    else:
        _record_launcher_phase(
            "version_check", _phase_started, "not_applicable",
            reason="the managed version policy applies to Databricks launchers",
        )

    _phase_started = time.perf_counter()
    from odibi_anchor import __version__ as _runtime_version

    _record_launcher_phase("package_import", _phase_started, "ok")

    try:
        _distribution_version = metadata.version("odibi-anchor")
    except metadata.PackageNotFoundError as exc:
        raise RuntimeError("odibi-anchor is not installed in this Python process") from exc
    if _distribution_version != _runtime_version:
        raise RuntimeError(
            "odibi-anchor distribution and runtime versions differ; restart Python before bootstrap"
        )

    _project_id = _requested_project_id()
    if _config_path.is_file():
        if _project_id is None:
            raise RuntimeError(
                "managed startup requires ANCHOR_PROJECT_ID as an init global or environment value"
            )
        from odibi_anchor import bootstrap_managed_project

        _managed = bootstrap_managed_project(
            config_path=_config_path,
            project_id=_project_id,
            instruction_root=_instruction_root,
            create_if_missing=globals().get("ANCHOR_CREATE_PROJECT") is True,
            project_root=globals().get("ANCHOR_PROJECT_ROOT"),
            **({"host_id": _binding["host_id"]} if _binding is not None else {}),
        )
        anchor = _managed["anchor"]
        ROOT = _managed["root"]
        MANIFEST = _managed["manifest"]
        ORIENTATION = _managed["orientation"]
        STARTUP_PACKET = _managed["startup_packet"]
        _timings = STARTUP_PACKET.get("timings") if isinstance(STARTUP_PACKET, dict) else None
        if isinstance(_timings, dict) and isinstance(_timings.get("phases"), list):
            _timings["phases"][:0] = _LAUNCHER_PHASES
            _timings["launcher_elapsed_ms"] = round(
                (time.perf_counter() - _LAUNCHER_STARTED) * 1000.0, 3
            )
        BOOTSTRAP = {
            "success": True,
            "kind": "agent_bootstrap",
            "runtime": "managed_installed_distribution",
            "repository": ROOT,
            "state_home": _managed["preparation"]["environment"]["ANCHOR_HOME"],
            "project_id": _project_id,
            "target_root": ROOT,
            "version": _runtime_version,
        }
    else:
        from odibi_anchor import launch

        _home = os.environ.get("ANCHOR_HOME")
        # A host binding declares a managed host, so its missing portfolio never falls back.
        if not _home or _binding is not None:
            from odibi_anchor._recovery import attach_recovery

            _searched_text = "; ".join(
                f"{entry['precedence']}. {entry['source']}: "
                + (f"{entry['path']} ({entry['status']})" if entry["path"] else str(entry["status"]))
                for entry in _portfolio_searched
            )
            _fallback = (
                "the host binding sidecar selected it, so the installed fallback is refused"
                if _binding is not None
                else "installed fallback requires one explicit ANCHOR_HOME"
            )
            raise attach_recovery(
                RuntimeError(
                    f"managed portfolio is missing at {_config_path}; {_fallback}. Searched in "
                    f"precedence order: {_searched_text}. Classification: no portfolio exists at "
                    "the selected path." + _nested_install_text(_nested) + " Supported "
                    "correction: rerun this launcher with the exact absolute portfolio path as "
                    "the ANCHOR_PORTFOLIO_CONFIG init global or environment variable, or record "
                    "it with `anchor setup-host <adapter> --target <instruction-root> --portfolio "
                    "<path>`. Do not copy, move, or hand-edit the portfolio TOML."
                ),
                error_code="managed_portfolio_not_found",
                context={
                    "selected_path": str(_config_path),
                    "searched_paths": _portfolio_searched,
                    "instruction_root": str(_instruction_root),
                    "launcher": str(_launcher),
                    "binding": _binding,
                    "nested_install": _nested,
                },
                next_operations=_discovery_operations(_instruction_root, _nested),
            )
        _target = os.environ.get("ANCHOR_PROJECT_ROOT") or str(_instruction_root)
        anchor = launch(
            anchor_home=_home,
            project_id=_project_id,
            project_root=_target,
            output_format="dict",
        )
        ROOT = str(Path(_target).resolve())
        MANIFEST = getattr(anchor, "manifest", None)
        ORIENTATION = anchor("orient", output_format="dict")
        if not isinstance(ORIENTATION, Mapping) or ORIENTATION.get("kind") != "orientation":
            raise RuntimeError("Odibi Anchor orientation returned an invalid structured result")
        _status = ORIENTATION.get("status")
        _runtime = _status.get("runtime") if isinstance(_status, Mapping) else None
        _binding = _runtime.get("route_binding") if isinstance(_runtime, Mapping) else None
        _project_id = _binding.get("project_id") if isinstance(_binding, Mapping) else None
        STARTUP_PACKET = {
            "kind": "managed_startup_packet",
            "status": "ready",
            "project_id": _project_id,
            "target_root": ROOT,
            "managed_artifact_actions": ORIENTATION.get("managed_artifact_actions", []),
            "next_required_action": (ORIENTATION.get("metrics") or {}).get(
                "next_required_action"
            ),
        }
        BOOTSTRAP = {
            "success": True,
            "kind": "agent_bootstrap",
            "runtime": "installed_distribution",
            "repository": ROOT,
            "state_home": str(Path(_home).resolve()),
            "project_id": _project_id,
            "target_root": ROOT,
            "version": _runtime_version,
        }
else:
    if globals().get("ANCHOR_CREATE_PROJECT") is True:
        raise RuntimeError("managed project creation requires installed portfolio startup")
    _delegate_globals = {}
    if "ANCHOR_REPOSITORY_PROVIDER" in globals():
        _delegate_globals["ANCHOR_REPOSITORY_PROVIDER"] = globals()["ANCHOR_REPOSITORY_PROVIDER"]
    _source_project = _requested_project_id()
    _previous_source_project = os.environ.get("ANCHOR_PROJECT_ID")
    if _source_project is not None:
        os.environ["ANCHOR_PROJECT_ID"] = _source_project
    try:
        _namespace = runpy.run_path(
            str(_checkout / "agent_bootstrap.py"),
            init_globals=_delegate_globals,
            run_name="__odibi_anchor_checkout_bootstrap__",
        )
    finally:
        if _previous_source_project is None:
            os.environ.pop("ANCHOR_PROJECT_ID", None)
        else:
            os.environ["ANCHOR_PROJECT_ID"] = _previous_source_project

    _required = ("anchor", "ROOT", "MANIFEST", "ORIENTATION", "BOOTSTRAP")
    _missing = tuple(name for name in _required if name not in _namespace)
    if _missing:
        raise RuntimeError(f"Odibi Anchor bootstrap omitted required values: {', '.join(_missing)}")
    _bootstrap = _namespace["BOOTSTRAP"]
    if not isinstance(_bootstrap, Mapping) or _bootstrap.get("success") is not True:
        raise RuntimeError("Odibi Anchor bootstrap did not report success")
    try:
        _reported_repository = Path(_bootstrap["repository"]).resolve(strict=True)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise RuntimeError("Odibi Anchor bootstrap reported an invalid repository") from exc
    if _reported_repository != _checkout:
        raise RuntimeError(
            "Odibi Anchor bootstrap repository does not match the selected checkout: "
            f"{_reported_repository} != {_checkout}"
        )

    anchor = _namespace["anchor"]
    ROOT = _namespace["ROOT"]
    MANIFEST = _namespace["MANIFEST"]
    ORIENTATION = _namespace["ORIENTATION"]
    BOOTSTRAP = _bootstrap
    STARTUP_PACKET = _namespace.get(
        "STARTUP_PACKET",
        {
            "kind": "managed_startup_packet",
            "status": "ready",
            "project_id": BOOTSTRAP.get("project_id"),
            "target_root": ROOT,
            "managed_artifact_actions": ORIENTATION.get("managed_artifact_actions", []),
            "next_required_action": (ORIENTATION.get("metrics") or {}).get(
                "next_required_action"
            ),
        },
    )

print("ODIBI_ANCHOR_STARTUP=" + json.dumps(STARTUP_PACKET, sort_keys=True, default=str))
