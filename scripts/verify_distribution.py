#!/usr/bin/env python
"""Build and smoke-test the distribution; retain audit/SBOM as advisory observations."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import venv
import zipfile
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
PACKAGE_NAME = "odibi-anchor"
COMMAND_TIMEOUT_SECONDS = 600
DIAGNOSTIC_LIMIT = 2_000
EXPECTED_NATIVE_SKILL_COUNT = 17
Runner = Callable[..., subprocess.CompletedProcess[str]]


class DistributionError(RuntimeError):
    """A required local distribution check failed or became unavailable."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _clean_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "ANCHOR_HOME", "ANCHOR_MEMORY_DB", "ANCHOR_PROFILE"):
        environment.pop(name, None)
    environment.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"})
    return environment


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout: int = COMMAND_TIMEOUT_SECONDS,
    runner: Runner = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    try:
        return runner(
            list(command),
            cwd=cwd,
            env=environment,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DistributionError(f"required executable is unavailable: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DistributionError(f"command timed out after {timeout}s: {Path(command[0]).name}") from exc
    except OSError as exc:
        raise DistributionError(f"command could not run: {Path(command[0]).name}: {exc}") from exc


def _diagnostic(result: subprocess.CompletedProcess[str]) -> str:
    value = (result.stderr or result.stdout)[-DIAGNOSTIC_LIMIT:]
    value = re.sub(
        r"(?i)(token|password|secret|authorization|api[_-]?key)(\s*[:=]\s*)\S+",
        r"\1\2<redacted>",
        value,
    )
    value = value.replace(str(Path.home()), "<home>")
    return value.strip()


def _require_success(result: subprocess.CompletedProcess[str], label: str) -> None:
    if result.returncode != 0:
        raise DistributionError(f"{label} exited {result.returncode}: {_diagnostic(result)}")


def _metadata(archive: zipfile.ZipFile) -> tuple[Message, str]:
    names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
    if len(names) != 1:
        raise DistributionError(f"wheel must contain exactly one METADATA file; found {len(names)}")
    return BytesParser(policy=policy.default).parsebytes(archive.read(names[0])), names[0]


def _requirement_name(value: str) -> str:
    match = re.match(r"^\s*([A-Za-z0-9_.-]+)", value)
    if not match:
        raise DistributionError(f"cannot parse requirement name: {value!r}")
    return re.sub(r"[-_.]+", "-", match.group(1)).lower()


def _extra_marker(value: str) -> str | None:
    match = re.search(r"\bextra\s*==\s*(['\"])([A-Za-z0-9_.-]+)\1", value)
    return match.group(2) if match else None


def _verify_metadata(message: Message, project: dict[str, Any]) -> dict[str, Any]:
    if message["Name"] != project["name"] or message["Version"] != project["version"]:
        raise DistributionError("wheel name/version does not match pyproject.toml")
    if message["Requires-Python"] != project["requires-python"]:
        raise DistributionError("wheel Requires-Python does not match pyproject.toml")
    expected_extras = sorted(project["optional-dependencies"])
    actual_extras = sorted(message.get_all("Provides-Extra") or [])
    if actual_extras != expected_extras:
        raise DistributionError("wheel extras do not match pyproject.toml")
    requirements = message.get_all("Requires-Dist") or []
    if project["dependencies"]:
        raise DistributionError("distribution verifier expects the declared zero-dependency base install")
    actual = Counter()
    for requirement in requirements:
        extra = _extra_marker(requirement)
        if extra is None:
            raise DistributionError(f"unexpected unconditional runtime requirement: {requirement}")
        actual[(extra, _requirement_name(requirement))] += 1
    expected = Counter(
        (extra, _requirement_name(requirement))
        for extra, values in project["optional-dependencies"].items()
        for requirement in values
    )
    if actual != expected:
        raise DistributionError("wheel optional requirement names do not match pyproject.toml")
    return {
        "name": message["Name"],
        "version": message["Version"],
        "requires_python": message["Requires-Python"],
        "extras": actual_extras,
        "optional_requirements": len(requirements),
    }


def _verify_wheel_files(archive: zipfile.ZipFile) -> dict[str, Any]:
    names = archive.namelist()
    allowed_files = {".assistant_instructions.md", "agent_bootstrap.py"}
    allowed_roots = {".assistant", "odibi_anchor", "tools"}
    unexpected = []
    dist_info_roots = set()
    for name in names:
        clean = name.rstrip("/")
        if not clean:
            continue
        root = clean.split("/", 1)[0]
        if root.endswith(".dist-info"):
            dist_info_roots.add(root)
        elif clean not in allowed_files and root not in allowed_roots:
            unexpected.append(name)
    if len(dist_info_roots) != 1 or unexpected:
        raise DistributionError(
            f"unexpected wheel package files: dist-info={sorted(dist_info_roots)}, unexpected={unexpected[:10]}"
        )
    required = {
        ".assistant_instructions.md",
        ".assistant/agent_bootstrap.py",
        "agent_bootstrap.py",
        "odibi_anchor/__init__.py",
        "odibi_anchor/cli.py",
    }
    missing = sorted(required - set(names))
    if missing:
        raise DistributionError(f"wheel is missing required package files: {missing}")
    skills = {
        Path(name).parts[2]
        for name in names
        if len(Path(name).parts) == 4
        and Path(name).parts[:2] == (".assistant", "skills")
        and Path(name).name == "SKILL.md"
    }
    if len(skills) != EXPECTED_NATIVE_SKILL_COUNT:
        raise DistributionError(
            f"wheel must contain exactly {EXPECTED_NATIVE_SKILL_COUNT} native skills; found {len(skills)}"
        )
    return {"files": len(names), "native_skills": len(skills)}


def _verify_sdist(path: Path, wheel_message: Message) -> dict[str, Any]:
    with tarfile.open(path, "r:gz") as archive:
        names = archive.getnames()
        metadata_names = [name for name in names if name.endswith("/PKG-INFO")]
        if len(metadata_names) != 1:
            raise DistributionError(f"sdist must contain exactly one PKG-INFO; found {len(metadata_names)}")
        extracted = archive.extractfile(metadata_names[0])
        if extracted is None:
            raise DistributionError("sdist PKG-INFO is unreadable")
        message = BytesParser(policy=policy.default).parsebytes(extracted.read())
        for field in ("Name", "Version", "Requires-Python", "Provides-Extra", "Requires-Dist"):
            if sorted(message.get_all(field) or []) != sorted(wheel_message.get_all(field) or []):
                raise DistributionError(f"sdist and wheel metadata disagree for {field}")
        required_suffixes = (
            "/.assistant_instructions.md",
            "/.assistant/agent_bootstrap.py",
            "/agent_bootstrap.py",
            "/src/odibi_anchor/__init__.py",
        )
        missing = [suffix for suffix in required_suffixes if not any(name.endswith(suffix) for name in names)]
        if missing:
            raise DistributionError(f"sdist is missing required files: {missing}")
    return {"files": len(names)}


def _venv_python(path: Path) -> Path:
    return path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _venv_command(path: Path, name: str) -> Path:
    if os.name == "nt":
        return path / "Scripts" / f"{name}.exe"
    return path / "bin" / name


def _installed_resource_probe(
    python: Path,
    *,
    cwd: Path,
    environment: dict[str, str],
    runner: Runner,
) -> dict[str, Any]:
    script = """
import importlib.metadata
import json
from pathlib import Path

import odibi_anchor

distribution = importlib.metadata.distribution("odibi-anchor")
assistant = Path(distribution.locate_file(".assistant"))
skills = sorted(path.parent.name for path in assistant.glob("skills/*/SKILL.md"))
print(json.dumps({
    "module": str(Path(odibi_anchor.__file__).resolve()),
    "version": odibi_anchor.__version__,
    "distribution_version": distribution.version,
    "skills": skills,
    "launcher": Path(distribution.locate_file("agent_bootstrap.py")).is_file(),
    "assistant_launcher": (assistant / "agent_bootstrap.py").is_file(),
    "instructions": Path(distribution.locate_file(".assistant_instructions.md")).is_file(),
}))
"""
    result = _run(
        [str(python), "-B", "-c", script],
        cwd=cwd,
        environment=environment,
        runner=runner,
    )
    _require_success(result, "installed resource probe")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DistributionError("installed resource probe returned malformed JSON") from exc
    module = Path(payload.get("module", ""))
    if (
        not module.is_relative_to(python.parents[1])
        or payload.get("version") != payload.get("distribution_version")
        or len(payload.get("skills", [])) != EXPECTED_NATIVE_SKILL_COUNT
        or not all(payload.get(key) is True for key in ("launcher", "assistant_launcher", "instructions"))
    ):
        raise DistributionError(f"installed resource probe failed: {payload}")
    return {"version": payload["version"], "native_skills": len(payload["skills"])}


def _site_packages(
    python: Path,
    *,
    cwd: Path,
    environment: dict[str, str],
    runner: Runner,
) -> Path:
    result = _run(
        [str(python), "-B", "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        cwd=cwd,
        environment=environment,
        runner=runner,
    )
    _require_success(result, "site-packages discovery")
    return Path(result.stdout.strip())


def _dependency_audit(
    python: Path,
    *,
    cwd: Path,
    environment: dict[str, str],
    runner: Runner,
) -> dict[str, Any]:
    executable = shutil.which("pip-audit")
    if executable is None:
        return {"id": "dependency-audit", "advisory": True, "status": "unavailable", "diagnostic": "pip-audit not found"}
    try:
        site_packages = _site_packages(python, cwd=cwd, environment=environment, runner=runner)
        result = _run(
            [
                executable,
                "--format", "json",
                "--progress-spinner", "off",
                "--path", str(site_packages),
            ],
            cwd=cwd,
            environment=environment,
            timeout=120,
            runner=runner,
        )
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict) or not isinstance(payload.get("dependencies"), list):
            raise ValueError("missing dependencies")
        vulnerabilities = sum(
            len(item.get("vulns", []))
            for item in payload["dependencies"]
            if isinstance(item, dict) and isinstance(item.get("vulns", []), list)
        )
        if result.returncode not in {0, 1}:
            raise DistributionError(f"pip-audit exited {result.returncode}: {_diagnostic(result)}")
        return {
            "id": "dependency-audit",
            "advisory": True,
            "status": "findings" if vulnerabilities else "observed",
            "packages": len(payload["dependencies"]),
            "vulnerabilities": vulnerabilities,
            "limitations": "Online advisory data may be unavailable or stale; this observation is not a security claim.",
        }
    except (DistributionError, json.JSONDecodeError, ValueError) as exc:
        return {"id": "dependency-audit", "advisory": True, "status": "unavailable", "diagnostic": str(exc)[:1000]}


def _contains_secret_value(value: object) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if re.search(r"(?i)(token|password|secret|authorization|api[_-]?key)", str(key)) and child:
                return True
            if _contains_secret_value(child):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_value(child) for child in value)
    return False


def _sbom(
    python: Path,
    *,
    cwd: Path,
    environment: dict[str, str],
    expected_version: str,
    runner: Runner,
) -> dict[str, Any]:
    executable = shutil.which("cyclonedx-py")
    if executable is None:
        return {"id": "sbom-generation", "advisory": True, "status": "unavailable", "diagnostic": "cyclonedx-py not found"}
    output = cwd / "odibi-anchor.cdx.json"
    try:
        result = _run(
            [
                executable,
                "environment",
                "--output-reproducible",
                "--output-format", "JSON",
                "--output-file", str(output),
                str(python),
            ],
            cwd=cwd,
            environment=environment,
            timeout=120,
            runner=runner,
        )
        _require_success(result, "CycloneDX generation")
        raw = output.read_text(encoding="utf-8")
        payload = json.loads(raw)
        components = payload.get("components") if isinstance(payload, dict) else None
        if not isinstance(components, list) or not any(
            isinstance(item, dict)
            and item.get("name") == PACKAGE_NAME
            and item.get("version") == expected_version
            for item in components
        ):
            raise DistributionError("SBOM does not contain the built odibi-anchor package/version")
        if str(REPO_ROOT) in raw or _contains_secret_value(payload):
            raise DistributionError("SBOM contains an absolute workspace path or an obvious secret value")
        return {
            "id": "sbom-generation",
            "advisory": True,
            "status": "observed",
            "components": len(components),
            "limitations": "Generated CI evidence is unsigned and does not establish completeness or provenance.",
        }
    except (DistributionError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"id": "sbom-generation", "advisory": True, "status": "unavailable", "diagnostic": str(exc)[:1000]}


def _record(
    checks: list[dict[str, Any]],
    check_id: str,
    operation: Callable[[], dict[str, Any]],
) -> bool:
    started_at = _utc_now()
    started = time.monotonic()
    try:
        details = operation()
    except DistributionError as exc:
        checks.append({
            "id": check_id,
            "required": True,
            "status": "fail",
            "started_at": started_at,
            "completed_at": _utc_now(),
            "duration_ms": round((time.monotonic() - started) * 1000),
            "diagnostic": str(exc)[:DIAGNOSTIC_LIMIT],
        })
        return False
    checks.append({
        "id": check_id,
        "required": True,
        "status": "pass",
        "started_at": started_at,
        "completed_at": _utc_now(),
        "duration_ms": round((time.monotonic() - started) * 1000),
        "details": details,
    })
    return True


def verify(
    *,
    root: Path = REPO_ROOT,
    runner: Runner = subprocess.run,
) -> tuple[dict[str, Any], int]:
    """Run the network-independent required path and advisory online observations."""
    root = root.resolve()
    started_at = _utc_now()
    started = time.monotonic()
    checks: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    environment = _clean_environment()
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    with tempfile.TemporaryDirectory(prefix="odibi-anchor-distribution-") as temporary:
        work = Path(temporary)
        artifacts = work / "dist"
        artifacts.mkdir()
        build_result: subprocess.CompletedProcess[str] | None = None

        def build() -> dict[str, Any]:
            nonlocal build_result
            build_result = _run(
                [
                    sys.executable,
                    "-m", "build",
                    "--no-isolation",
                    "--sdist",
                    "--wheel",
                    "--outdir", str(artifacts),
                    str(root),
                ],
                cwd=work,
                environment=environment,
                runner=runner,
            )
            _require_success(build_result, "distribution build")
            return {"command": [sys.executable, "-m", "build", "--no-isolation", "--sdist", "--wheel"]}

        if not _record(checks, "build", build):
            return _finish(started_at, started, checks, observations)
        wheels = sorted(artifacts.glob("*.whl"))
        sdists = sorted(artifacts.glob("*.tar.gz"))

        def cardinality() -> dict[str, Any]:
            if len(wheels) != 1 or len(sdists) != 1:
                raise DistributionError(f"expected one wheel and one sdist; found {len(wheels)} wheel(s), {len(sdists)} sdist(s)")
            return {"wheel": wheels[0].name, "sdist": sdists[0].name}

        if not _record(checks, "artifact-cardinality", cardinality):
            return _finish(started_at, started, checks, observations)
        wheel = wheels[0]
        sdist = sdists[0]
        with zipfile.ZipFile(wheel) as archive:
            message, _ = _metadata(archive)
            if not _record(checks, "dependency-metadata", lambda: _verify_metadata(message, project)):
                return _finish(started_at, started, checks, observations)
            if not _record(checks, "package-files", lambda: _verify_wheel_files(archive)):
                return _finish(started_at, started, checks, observations)
        if not _record(checks, "sdist-metadata", lambda: _verify_sdist(sdist, message)):
            return _finish(started_at, started, checks, observations)

        isolated = work / "venv"

        def install() -> dict[str, Any]:
            venv.EnvBuilder(with_pip=True, clear=True).create(isolated)
            python = _venv_python(isolated)
            result = _run(
                [str(python), "-m", "pip", "install", "--no-deps", str(wheel)],
                cwd=work,
                environment=environment,
                runner=runner,
            )
            _require_success(result, "isolated wheel install")
            return {"no_deps": True, "python": f"Python {sys.version_info.major}.{sys.version_info.minor}"}

        if not _record(checks, "isolated-install", install):
            return _finish(started_at, started, checks, observations)
        isolated_python = _venv_python(isolated)
        if not _record(
            checks,
            "installed-resources",
            lambda: _installed_resource_probe(
                isolated_python,
                cwd=work,
                environment=environment,
                runner=runner,
            ),
        ):
            return _finish(started_at, started, checks, observations)

        def cli_help() -> dict[str, Any]:
            result = _run(
                [str(_venv_command(isolated, "anchor")), "--help"],
                cwd=work,
                environment=environment,
                runner=runner,
            )
            _require_success(result, "installed anchor --help")
            return {"help": "available", "output_bytes": len((result.stdout + result.stderr).encode("utf-8"))}

        if not _record(checks, "cli-help", cli_help):
            return _finish(started_at, started, checks, observations)

        def installed_tests() -> dict[str, Any]:
            selectors = [
                "tests/test_installed_distribution.py::test_source_native_skill_layout_is_runtime_independent",
                "tests/test_installed_distribution.py::test_package_metadata_has_one_source_authority",
            ]
            result = _run(
                [sys.executable, str(root / "scripts/run_tests.py"), *selectors],
                cwd=root,
                environment=environment,
                runner=runner,
            )
            _require_success(result, "installed-distribution focused tests")
            try:
                summary = json.loads(result.stdout.splitlines()[-1])
            except (IndexError, json.JSONDecodeError) as exc:
                raise DistributionError("canonical test launcher returned a malformed summary") from exc
            if summary.get("exit_code") != 0 or summary.get("failed") or summary.get("errors"):
                raise DistributionError(f"canonical test launcher reported failure: {summary}")
            return {"selectors": selectors, "summary": summary}

        if not _record(checks, "installed-distribution-tests", installed_tests):
            return _finish(started_at, started, checks, observations)
        observations.append(_dependency_audit(
            isolated_python,
            cwd=work,
            environment=environment,
            runner=runner,
        ))
        observations.append(_sbom(
            isolated_python,
            cwd=work,
            environment=environment,
            expected_version=project["version"],
            runner=runner,
        ))
    return _finish(started_at, started, checks, observations)


def _finish(
    started_at: str,
    started: float,
    checks: list[dict[str, Any]],
    observations: list[dict[str, Any]],
) -> tuple[dict[str, Any], int]:
    passed = bool(checks) and all(item["status"] == "pass" for item in checks)
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": "pass" if passed else "fail",
        "started_at": started_at,
        "completed_at": _utc_now(),
        "duration_ms": round((time.monotonic() - started) * 1000),
        "checks": checks,
        "observations": observations,
        "claims": [
            "Required local build/install checks passed." if passed else "One or more required local build/install checks failed.",
            "Dependency audit and SBOM results are advisory observations only.",
        ],
    }
    return report, 0 if passed else 1


def _print_text(report: dict[str, Any]) -> None:
    print(f"Distribution verification: {report['status']}")
    for check in report["checks"]:
        print(f"{check['id']}: {check['status']}")
        if check.get("diagnostic"):
            print(f"  {check['diagnostic']}")
    for observation in report["observations"]:
        print(f"{observation['id']}: {observation['status']} (advisory)")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report, exit_code = verify()
    except (DistributionError, OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "unavailable",
            "started_at": _utc_now(),
            "completed_at": _utc_now(),
            "duration_ms": 0,
            "checks": [],
            "observations": [],
            "claims": ["Distribution verification was unavailable."],
            "diagnostic": str(exc)[:DIAGNOSTIC_LIMIT],
        }
        exit_code = 2
    if args.format == "json":
        print(_canonical_json(report), end="")
    else:
        _print_text(report)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
