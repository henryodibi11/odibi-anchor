"""Clean-wheel qualification for the installed Odibi Anchor runtime."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import textwrap
import time
import tomllib
import zipfile
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

#: Directory holding pre-downloaded build and runtime wheels for offline qualification.
#: Populate with, for example:
#:   python -m pip download --dest <dir> "hatchling>=1.32,<2" "fastmcp>=3.0" \
#:       "pytest>=7.0" "pytest-timeout>=2.2"
#: then export ANCHOR_OFFLINE_WHEELHOUSE=<dir>. See WI-2026-0019.
OFFLINE_WHEELHOUSE_ENVIRONMENT_VARIABLE = "ANCHOR_OFFLINE_WHEELHOUSE"
DEFAULT_OFFLINE_WHEELHOUSE = REPOSITORY_ROOT / "build-wheelhouse"


def offline_wheelhouse() -> Path | None:
    """Return the configured wheelhouse directory when it holds wheels."""
    configured = os.environ.get(OFFLINE_WHEELHOUSE_ENVIRONMENT_VARIABLE)
    candidate = Path(configured) if configured else DEFAULT_OFFLINE_WHEELHOUSE
    if candidate.is_dir() and any(candidate.glob("*.whl")):
        return candidate
    return None


#: Set to 1/true in CI so an unresolvable build backend FAILS instead of skipping.
#: A silent skip would delete installed-path coverage without any signal, which is
#: exactly what happened when the offline resolver was introduced (WI-2026-0019).
REQUIRE_INSTALLED_QUALIFICATION_VARIABLE = "ANCHOR_REQUIRE_INSTALLED_QUALIFICATION"


def installed_qualification_is_required() -> bool:
    """Return whether unavailable installed qualification must fail rather than skip."""
    return os.environ.get(REQUIRE_INSTALLED_QUALIFICATION_VARIABLE, "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _unavailable(reason: str) -> None:
    """Fail when installed qualification is required, otherwise skip explicitly."""
    message = f"installed-wheel qualification unavailable: {reason}"
    if installed_qualification_is_required():
        pytest.fail(
            f"{message}\n${REQUIRE_INSTALLED_QUALIFICATION_VARIABLE} is set, so this "
            "coverage may not be skipped. Install the pinned build backend "
            "(python -m pip install --group quality) or configure "
            f"${OFFLINE_WHEELHOUSE_ENVIRONMENT_VARIABLE}."
        )
    pytest.skip(message)


def _build_backend_is_installed() -> bool:
    """Return whether this interpreter can build the wheel without downloading."""
    return importlib.util.find_spec("hatchling") is not None


def build_pip_arguments() -> list[str]:
    """Return pip arguments that build the wheel from locally available build deps.

    Preference order: the pinned build backend already installed in this environment
    (`--no-build-isolation`, which downloads nothing), then an explicit wheelhouse
    (`--no-index`). Package-index reachability is never treated as acceptance
    evidence for the shipped artifact, so neither path consults an index.
    """
    if _build_backend_is_installed():
        return ["--no-build-isolation"]
    wheelhouse = offline_wheelhouse()
    if wheelhouse is not None:
        return ["--no-index", "--find-links", str(wheelhouse)]
    _unavailable(
        "the pinned build backend (hatchling) is not importable and no wheelhouse is "
        f"configured at ${OFFLINE_WHEELHOUSE_ENVIRONMENT_VARIABLE} or "
        f"{DEFAULT_OFFLINE_WHEELHOUSE}"
    )
    raise AssertionError("unreachable")  # pragma: no cover - _unavailable always raises


def install_pip_arguments() -> list[str]:
    """Return pip arguments for installing the built wheel and its runtime extras.

    A configured wheelhouse pins the install offline too. Without one the ambient
    index supplies third-party runtime extras; that affects only how dependencies are
    fetched, never whether the qualification is allowed to be skipped.
    """
    wheelhouse = offline_wheelhouse()
    if wheelhouse is None:
        return []
    return ["--no-index", "--find-links", str(wheelhouse)]


def os_required_environment() -> dict[str, str]:
    """Return the variables an isolated subprocess needs to be a working OS process.

    Probe environments are deliberately scrubbed (no PYTHONPATH, no proxy settings,
    isolated HOME) but scrubbing must not remove what the operating system itself
    requires. On Windows, omitting ``SystemRoot`` prevents Winsock from initializing,
    so ``import asyncio`` fails with ``WinError 10106`` and any name resolution fails
    with ``getaddrinfo failed`` -- failures that look like product defects but are
    artifacts of the harness (WI-2026-0019).

    These variables carry no credentials and no index configuration, so including
    them does not weaken isolation or reintroduce network dependence.
    """
    if os.name != "nt":
        return {}
    return {
        name: os.environ[name]
        for name in ("SystemRoot", "SystemDrive", "COMSPEC", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE")
        if name in os.environ
    }


def isolated_home_environment(home: Path) -> dict[str, str]:
    """Point a probe's home directory at ``home`` on every supported platform.

    ``Path.home()`` consults ``HOME`` on POSIX but ``USERPROFILE`` on Windows, so
    setting ``HOME`` alone leaves Windows probes with no resolvable home directory and
    bootstrap fails with "Could not determine home directory" (WI-2026-0019).
    """
    home.mkdir(parents=True, exist_ok=True)
    environment = {
        "HOME": str(home),
        "PIP_CACHE_DIR": str(home / "pip-cache"),
    }
    if os.name == "nt":
        environment["USERPROFILE"] = str(home)
        environment["HOMEDRIVE"] = home.drive
        environment["HOMEPATH"] = str(home)[len(home.drive):]
    return environment


def test_installed_qualification_requirement_turns_unavailability_into_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(__name__ + "._build_backend_is_installed", lambda: False)
    monkeypatch.setattr(__name__ + ".offline_wheelhouse", lambda: None)
    monkeypatch.setenv(REQUIRE_INSTALLED_QUALIFICATION_VARIABLE, "1")

    with pytest.raises(pytest.fail.Exception, match="coverage may not be skipped"):
        build_pip_arguments()


def test_local_installed_qualification_reports_explicit_skip_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(__name__ + "._build_backend_is_installed", lambda: False)
    monkeypatch.setattr(__name__ + ".offline_wheelhouse", lambda: None)
    monkeypatch.delenv(REQUIRE_INSTALLED_QUALIFICATION_VARIABLE, raising=False)

    with pytest.raises(pytest.skip.Exception, match="qualification unavailable"):
        build_pip_arguments()


def test_installed_qualification_uses_local_backend_without_an_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(__name__ + "._build_backend_is_installed", lambda: True)
    assert build_pip_arguments() == ["--no-build-isolation"]


EXPECTED_OPTIONAL_REQUIREMENTS = [
    "cryptography<50,>=44; extra == 'all'",
    "cryptography<50,>=44; extra == 'dev'",
    "cryptography<50,>=44; extra == 'governance'",
    "fastmcp>=3.0; extra == 'all'",
    "fastmcp>=3.0; extra == 'mcp'",
    "libcst>=1.0; extra == 'all'",
    "libcst>=1.0; extra == 'dev'",
    "libcst>=1.0; extra == 'semantic'",
    "numpy>=1.21; extra == 'all'",
    "numpy>=1.21; extra == 'dev'",
    "numpy>=1.21; extra == 'pandas'",
    "pandas<3,>=1.5; extra == 'all'",
    "pandas<3,>=1.5; extra == 'dev'",
    "pandas<3,>=1.5; extra == 'pandas'",
    "pyspark>=3.4; extra == 'all'",
    "pyspark>=3.4; extra == 'spark'",
    "pytest-timeout>=2.2; extra == 'dev'",
    "pytest>=7.0; extra == 'dev'",
    "rfc8785==0.1.4; extra == 'all'",
    "rfc8785==0.1.4; extra == 'dev'",
    "rfc8785==0.1.4; extra == 'governance'",
    "ruff>=0.4; extra == 'dev'",
]
EXPECTED_DISTRIBUTION_METADATA = {
    "Name": ["odibi-anchor"],
    "Version": ["0.2.0"],
    "Summary": ["Provider-neutral reliability, context, and evidence tooling for engineering agents."],
    "Requires-Python": [">=3.11"],
    "License-Expression": ["Apache-2.0"],
    "Author": ["Henry Odibi"],
    "Provides-Extra": ["all", "dev", "governance", "mcp", "pandas", "semantic", "spark"],
    "Requires-Dist": EXPECTED_OPTIONAL_REQUIREMENTS,
}
EXPECTED_NATIVE_SKILLS = {
    "auditing-memory-governance",
    "authoring-governed-memories",
    "building-memory-packs",
    "code-comprehension",
    "cross-functional-pr",
    "data-onboarding",
    "data-operations",
    "data-reconciliation",
    "debugging",
    "dependency-management",
    "documentation",
    "incident-response",
    "performance-investigation",
    "schema-design",
    "work-item-management",
    "writing-specs",
    "writing-tests",
}
HOUSE_REFERENCE_PATHS = (
    "development/code-standards.md",
    "development/code-standards-python.md",
    "development/code-standards-data-platform.md",
)
EXPECTED_TOOL_DIRECTORIES = {
    "coerce_fix_tool",
    "delta_diff_tool",
    "diagnose_empty_tool",
    "echo_tool",
    "explain_row_tool",
    "partition_check_tool",
    "pre_join_tool",
    "pre_merge_tool",
    "schema_migrate_tool",
    "suggest_rules_tool",
    "table_profiler_tool",
    "watermark_tool",
}

_MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


def _validate_native_skill_layout(assistant_root: Path) -> None:
    """Validate native skill discovery/resources without importing Odibi Anchor."""
    skills_root = assistant_root / "skills"
    discovered = {
        path.parent.name for path in skills_root.glob("*/SKILL.md")
    }
    assert discovered == EXPECTED_NATIVE_SKILLS
    for name in sorted(discovered):
        skill_root = (skills_root / name).resolve()
        skill_file = skill_root / "SKILL.md"
        text = skill_file.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        frontmatter = text.split("---\n", 2)[1]
        fields = {
            key.strip(): value.strip().strip('"\'')
            for line in frontmatter.splitlines()
            if ":" in line
            for key, value in [line.split(":", 1)]
        }
        assert fields.get("name") == name
        assert fields.get("description")
        for markdown in skill_root.rglob("*.md"):
            for raw_target in _MARKDOWN_LINK.findall(
                markdown.read_text(encoding="utf-8")
            ):
                target_text = raw_target.split("#", 1)[0].strip()
                if not target_text or "://" in target_text or target_text.startswith("mailto:"):
                    continue
                target = (markdown.parent / target_text).resolve()
                assert target.is_relative_to(skill_root), (
                    f"{markdown} resource escapes skill root: {raw_target}"
                )
                assert target.is_file(), f"{markdown} resource is missing: {raw_target}"


def test_source_native_skill_layout_is_runtime_independent() -> None:
    """Discover and resolve the canonical source tree with stdlib filesystem reads."""
    _validate_native_skill_layout(REPOSITORY_ROOT / ".assistant")


def _remove_tree(path: Path) -> None:
    """Retry transient Windows cleanup failures from recently exited probes."""
    def remove_readonly(function, value, _error) -> None:
        os.chmod(value, stat.S_IWRITE)
        function(value)

    for delay in (0.0, 0.1, 0.3, 1.0):
        if delay:
            time.sleep(delay)
        with contextlib.suppress(OSError):
            shutil.rmtree(path, onerror=remove_readonly)
        if not path.exists():
            return


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
    input_text: str | None = None,
    expected_codes: tuple[int, ...] = (0,),
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    """Run one bounded subprocess without echoing its environment or output."""
    if input_text is None:
        result = subprocess.run(
            command, cwd=cwd, env=environment, text=True, encoding="utf-8",
            errors="replace", capture_output=True, timeout=timeout, check=False,
        )
    else:
        binary = subprocess.run(
            command, cwd=cwd, env=environment, input=input_text.encode("utf-8"),
            text=False, capture_output=True, timeout=timeout, check=False,
        )
        result = subprocess.CompletedProcess(
            binary.args, binary.returncode,
            binary.stdout.decode("utf-8", errors="replace"),
            binary.stderr.decode("utf-8", errors="replace"),
        )
    if result.returncode not in expected_codes:
        executable = Path(command[0]).name
        # Subprocess probes run with deliberately scrubbed environments, but keep
        # failure diagnostics bounded and redact anything shaped like a secret in
        # case a dependency happens to echo one.
        diagnostic = (result.stderr or result.stdout)[-4_000:]
        diagnostic = re.sub(
            r"(?i)(token|password|secret|authorization|api[_-]?key)(\s*[:=]\s*)\S+",
            r"\1\2<redacted>",
            diagnostic,
        )
        diagnostic = diagnostic.replace(str(Path.home()), "<home>")
        raise AssertionError(
            f"{executable} exited {result.returncode}; expected {expected_codes}\n"
            f"sanitized subprocess output:\n{diagnostic}"
        )
    return result


def _write_script(root: Path, name: str, source: str) -> Path:
    """Write one disposable UTF-8 qualification script."""
    path = root / name
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


def _sha256(path: Path) -> str:
    """Return the SHA-256 digest for one file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_hashes(root: Path) -> dict[str, str]:
    """Return stable hashes for every non-Git file below a target."""
    return {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def _git(repo: Path, *args: str, expected_codes: tuple[int, ...] = (0,)) -> str:
    """Run one Git inspection or fixture command."""
    return _run(
        ["git", "-C", str(repo), *args],
        expected_codes=expected_codes,
    ).stdout


def _git_snapshot(repo: Path) -> dict[str, object]:
    """Capture exact source, index, branch, HEAD, and status evidence."""
    head = _git(repo, "rev-parse", "HEAD").strip()
    branch = _git(repo, "branch", "--show-current").strip()
    status = _git(repo, "status", "--porcelain=v2", "--branch")
    worktree_diff = _git(repo, "diff", "--binary")
    index_diff = _git(repo, "diff", "--cached", "--binary")
    # Read the index after Git inspection has completed any benign stat refresh.
    index = repo / ".git" / "index"
    return {
        "files": _tree_hashes(repo),
        "index": _sha256(index) if index.is_file() else None,
        "head": head,
        "branch": branch,
        "status": status,
        "worktree_diff": worktree_diff,
        "index_diff": index_diff,
    }


def _unborn_git_snapshot(repo: Path) -> dict[str, object]:
    """Capture exact source, index, branch, optional HEAD, refs, and status."""
    head = (
        _git(
            repo,
            "rev-parse",
            "--verify",
            "--quiet",
            "HEAD^{commit}",
            expected_codes=(0, 1),
        ).strip()
        or None
    )
    branch = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD").strip()
    status = _git(repo, "status", "--porcelain=v2", "--branch")
    worktree_diff = _git(repo, "diff", "--binary")
    index_diff = _git(repo, "diff", "--cached", "--binary")
    git_dir = repo / ".git"
    refs = {
        path.relative_to(git_dir).as_posix(): _sha256(path)
        for path in sorted((git_dir / "refs").rglob("*"))
        if path.is_file()
    }
    index = git_dir / "index"
    return {
        "files": _tree_hashes(repo),
        "index": _sha256(index) if index.is_file() else None,
        "head": head,
        "branch": branch,
        "refs": refs,
        "status": status,
        "worktree_diff": worktree_diff,
        "index_diff": index_diff,
    }


def _init_unborn_repo(path: Path, *, branch: str = "main") -> None:
    """Create one configured repository whose branch has no commit."""
    path.mkdir()
    _git(path, "init", "-q", "-b", branch)
    _git(path, "config", "user.email", "odibi-anchor@example.invalid")
    _git(path, "config", "user.name", "Odibi Anchor Test")
    _git(path, "config", "commit.gpgsign", "false")


def _init_committed_repo(path: Path) -> None:
    """Create one committed non-Python Git fixture."""
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "odibi-anchor@example.invalid")
    _git(path, "config", "user.name", "Odibi Anchor Test")
    (path / "package.json").write_text('{"name":"installed-fixture"}\n', encoding="utf-8")
    (path / "app.js").write_text("export const answer = 42;\n", encoding="utf-8")
    (path / "test.js").write_text("console.assert(true);\n", encoding="utf-8")
    _git(path, "add", "package.json", "app.js", "test.js")
    _git(path, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "fixture")


def _clean_environment(audit_root: Path) -> dict[str, str]:
    """Return an installed-runtime environment with no source or user-site path."""
    environment = os.environ.copy()
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "ANCHOR_HOME",
        "ANCHOR_MEMORY_DB",
        "ANCHOR_PROFILE",
        "ANCHOR_PROJECT_ID",
        "ANCHOR_PROJECT_ROOT",
        "ANCHOR_ALLOW_LEGACY_SELECTOR",
        "ANCHOR_RUNTIME_INSTANCE_ID",
        "ANCHOR_BOOT_CONFIG_ROOT",
    ):
        environment.pop(name, None)
    user_home = audit_root / "user-home"
    environment.update(
        {
            "HOME": str(user_home),
            "USERPROFILE": str(user_home),
            "LOCALAPPDATA": str(audit_root / "local-app-data"),
            "PYTHONNOUSERSITE": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }
    )
    return environment


def _json_lines(output: str) -> list[dict[str, object]]:
    """Decode strict JSON-lines transport output."""
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def test_package_metadata_has_one_source_authority() -> None:
    """Keep distribution declarations exclusively in the PEP 621 project table."""
    pyproject = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    assert project["name"] == "odibi-anchor"
    assert project["version"] == "0.2.0"
    assert project["description"] == "Provider-neutral reliability, context, and evidence tooling for engineering agents."
    assert project["requires-python"] == ">=3.11"
    assert project["license"] == "Apache-2.0"
    assert project["dependencies"] == []
    assert project["scripts"] == {
        "anchor": "odibi_anchor.cli:main",
        "anchor-governance-sidecar": "odibi_anchor._governance_sidecar.__main__:main",
    }
    assert project["optional-dependencies"] == {
        "governance": ["cryptography>=44,<50", "rfc8785==0.1.4"],
        "pandas": ["pandas>=1.5,<3", "numpy>=1.21"],
        "spark": ["pyspark>=3.4"],
        "semantic": ["libcst>=1.0"],
        "mcp": ["fastmcp>=3.0"],
        "all": [
            "pandas>=1.5,<3",
            "numpy>=1.21",
            "pyspark>=3.4",
            "libcst>=1.0",
            "cryptography>=44,<50",
            "rfc8785==0.1.4",
            "fastmcp>=3.0",
        ],
        "dev": [
            "pytest>=7.0",
            "pytest-timeout>=2.2",
            "ruff>=0.4",
            "pandas>=1.5,<3",
            "numpy>=1.21",
            "libcst>=1.0",
            "cryptography>=44,<50",
            "rfc8785==0.1.4",
        ],
    }

    init_source = (REPOSITORY_ROOT / "src" / "odibi_anchor" / "__init__.py").read_text(encoding="utf-8")
    assert '"0.2.0"' not in init_source
    assert '"0.7.1"' not in init_source
    assert not (REPOSITORY_ROOT / "src" / "odibi_anchor" / "_version.py").exists()


def _copy_package_candidate(destination: Path) -> None:
    """Copy only the files consumed by the configured Hatch build."""
    destination.mkdir()
    for name in (
        "pyproject.toml", "README.md", ".assistant_instructions.md", "agent_bootstrap.py"
    ):
        shutil.copy2(REPOSITORY_ROOT / name, destination / name)
    for name in ("src", ".assistant", "tools"):
        shutil.copytree(
            REPOSITORY_ROOT / name, destination / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )


def _create_venv(root: Path, environment: dict[str, str]) -> Path:
    """Create an isolated environment and return its Python executable."""
    _run([sys.executable, "-m", "venv", str(root)], environment=environment)
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _metadata_contract(metadata_bytes: bytes) -> dict[str, list[str]]:
    """Project the distribution fields that must agree across artifacts."""
    message = BytesParser(policy=policy.default).parsebytes(metadata_bytes)
    fields = ("Name", "Version", "Summary", "Requires-Python", "License-Expression", "Author", "Provides-Extra", "Requires-Dist")
    return {field: sorted(message.get_all(field, [])) for field in fields}


def _runtime_probe(
    python: Path,
    environment: dict[str, str],
    *,
    cwd: Path,
    metadata_prepend: Path | None = None,
) -> dict[str, object]:
    """Read runtime and distribution provenance without a checkout path leak."""
    script = """
        import importlib.metadata
        import json
        import sys

        if sys.argv[1]:
            sys.path.insert(0, sys.argv[1])
        import odibi_anchor

        installed = importlib.metadata.distribution("odibi-anchor")
        print(json.dumps({
            "module": odibi_anchor.__file__,
            "runtime_version": odibi_anchor.__version__,
            "distribution_version": installed.version,
            "summary": installed.metadata["Summary"],
            "author": installed.metadata["Author"],
            "license": installed.metadata["License-Expression"],
            "requires_python": installed.metadata["Requires-Python"],
            "extras": sorted(installed.metadata.get_all("Provides-Extra") or []),
            "requirements": sorted(installed.requires or []),
            "direct_url": installed.read_text("direct_url.json"),
        }))
    """
    result = _run(
        [str(python), "-B", "-c", textwrap.dedent(script), str(metadata_prepend or "")],
        cwd=cwd,
        environment=environment,
    )
    return json.loads(result.stdout)


def _runtime_version_probe(
    python: Path,
    environment: dict[str, str],
    *,
    cwd: Path,
    metadata_prepend: Path | None = None,
) -> str:
    """Import the package and return only its resolved runtime version."""
    script = """
        import sys

        if sys.argv[1]:
            sys.path.insert(0, sys.argv[1])
        import odibi_anchor
        print(odibi_anchor.__version__)
    """
    return _run(
        [str(python), "-B", "-c", textwrap.dedent(script), str(metadata_prepend or "")],
        cwd=cwd,
        environment=environment,
    ).stdout.strip()


def _repository_factory_probe(
    python: Path,
    environment: dict[str, str],
    *,
    cwd: Path,
) -> dict[str, object]:
    """Prove the public optional factory imports without a Databricks SDK."""
    script = """
        import json
        from odibi_anchor.operational import (
            autoconfigure_databricks_git_folder_repository,
            databricks_workspace_path_from_checkout,
        )

        provider, evidence = autoconfigure_databricks_git_folder_repository("/outside/workspace")
        print(json.dumps({
            "provider": provider is not None,
            "workspace_path": databricks_workspace_path_from_checkout(
                "/Workspace/Users/test@example.invalid/odibi_anchor"
            ),
            "evidence": evidence,
        }))
    """
    return json.loads(
        _run(
            [str(python), "-B", "-c", textwrap.dedent(script)],
            cwd=cwd,
            environment=environment,
        ).stdout
    )


def _assert_runtime_metadata(probe: dict[str, object]) -> None:
    """Assert installed metadata and runtime expose the authoritative contract."""
    assert probe["runtime_version"] == probe["distribution_version"] == "0.2.0"
    assert probe["summary"] == EXPECTED_DISTRIBUTION_METADATA["Summary"][0]
    assert probe["author"] == "Henry Odibi"
    assert probe["license"] == "Apache-2.0"
    assert probe["requires_python"] == ">=3.11"
    assert probe["extras"] == EXPECTED_DISTRIBUTION_METADATA["Provides-Extra"]
    assert probe["requirements"] == EXPECTED_OPTIONAL_REQUIREMENTS


def _qualify_package_metadata_matrix(audit_root: Path) -> None:
    """Build and inspect raw, editable, wheel, sdist, and legacy provenance."""
    candidate = audit_root / "candidate%20source"
    artifacts = audit_root / "artifacts"
    artifacts.mkdir(parents=True)
    _copy_package_candidate(candidate)
    environment = _clean_environment(audit_root)

    builder_python = _create_venv(audit_root / "builder", environment)
    _run([str(builder_python), "-m", "pip", "install", "build"], environment=environment)
    _run(
        [str(builder_python), "-m", "build", "--sdist", "--wheel", "--outdir", str(artifacts), str(candidate)],
        environment=environment,
        timeout=300,
    )
    wheel = next(artifacts.glob("odibi_anchor-0.2.0-*.whl"))
    sdist = artifacts / "odibi_anchor-0.2.0.tar.gz"
    assert sdist.is_file()

    with zipfile.ZipFile(wheel) as wheel_archive:
        wheel_names = wheel_archive.namelist()
        wheel_metadata_name = next(name for name in wheel_names if name.endswith(".dist-info/METADATA"))
        wheel_entry_points_name = next(name for name in wheel_names if name.endswith(".dist-info/entry_points.txt"))
        wheel_metadata = _metadata_contract(wheel_archive.read(wheel_metadata_name))
        assert "odibi_anchor/_governance_sidecar/__main__.py" in wheel_names
        assert "odibi_anchor/_governance_host_probe/__main__.py" in wheel_names
        assert "odibi_anchor/behavior_runner.py" in wheel_names
        assert "odibi_anchor/durability.py" in wheel_names
        assert "odibi_anchor/host_setup.py" in wheel_names
        assert "odibi_anchor/portfolio.py" in wheel_names
        assert wheel_archive.read(wheel_entry_points_name).decode("utf-8") == (
            "[console_scripts]\nanchor = odibi_anchor.cli:main\n"
            "anchor-governance-sidecar = odibi_anchor._governance_sidecar.__main__:main\n"
        )
        packaged_init = wheel_archive.read("odibi_anchor/__init__.py").decode("utf-8")
        assert '"0.2.0"' not in packaged_init
        assert wheel_archive.read(".assistant_instructions.md") == (
            candidate / ".assistant_instructions.md"
        ).read_bytes()
        assert wheel_archive.read("agent_bootstrap.py") == (candidate / "agent_bootstrap.py").read_bytes()
        assert wheel_archive.read(".assistant/agent_bootstrap.py") == (
            candidate / ".assistant" / "agent_bootstrap.py"
        ).read_bytes()
        for relative in HOUSE_REFERENCE_PATHS:
            packaged = f".assistant/references/{relative}"
            assert wheel_archive.read(packaged) == (
                candidate / ".assistant" / "references" / relative
            ).read_bytes()
        assert wheel_archive.read(".assistant/references/registry.json") == (
            candidate / ".assistant" / "references" / "registry.json"
        ).read_bytes()
        for resource in ("scenario_manifest.json", "promotion_policy.json"):
            packaged = wheel_archive.read(f"odibi_anchor/assurance/resources/{resource}")
            source = (candidate / "src/odibi_anchor/assurance/resources" / resource).read_bytes()
            assert packaged == source
        packaged_assurance_resources = b"\n".join(
            wheel_archive.read(name) for name in wheel_names
            if name.startswith("odibi_anchor/assurance/resources/")
        )
        for forbidden in (b"expected_answer", b"seeded_defect_location", b"reviewer_rubric",
                          b"evaluator_payload"):
            assert forbidden not in packaged_assurance_resources

    with tarfile.open(sdist, "r:gz") as sdist_archive:
        sdist_names = sdist_archive.getnames()
        pkg_info_name = next(name for name in sdist_names if name.endswith("/PKG-INFO"))
        instructions_name = next(name for name in sdist_names if name.endswith("/.assistant_instructions.md"))
        assistant_launcher_name = next(
            name for name in sdist_names if name.endswith("/.assistant/agent_bootstrap.py")
        )
        agent_bootstrap_name = next(
            name
            for name in sdist_names
            if name.endswith("/agent_bootstrap.py") and name != assistant_launcher_name
        )
        pkg_info_file = sdist_archive.extractfile(pkg_info_name)
        instructions_file = sdist_archive.extractfile(instructions_name)
        agent_bootstrap_file = sdist_archive.extractfile(agent_bootstrap_name)
        assistant_launcher_file = sdist_archive.extractfile(assistant_launcher_name)
        assert pkg_info_file is not None
        assert instructions_file is not None
        assert agent_bootstrap_file is not None
        assert assistant_launcher_file is not None
        sdist_metadata = _metadata_contract(pkg_info_file.read())
        assert instructions_file.read() == (candidate / ".assistant_instructions.md").read_bytes()
        assert agent_bootstrap_file.read() == (candidate / "agent_bootstrap.py").read_bytes()
        assert assistant_launcher_file.read() == (
            candidate / ".assistant" / "agent_bootstrap.py"
        ).read_bytes()
        for relative in (*HOUSE_REFERENCE_PATHS, "registry.json"):
            suffix = f"/.assistant/references/{relative}"
            resource_name = next(name for name in sdist_names if name.endswith(suffix))
            resource_file = sdist_archive.extractfile(resource_name)
            assert resource_file is not None
            assert resource_file.read() == (
                candidate / ".assistant" / "references" / relative
            ).read_bytes()
        for resource in ("scenario_manifest.json", "promotion_policy.json"):
            suffix = f"/src/odibi_anchor/assurance/resources/{resource}"
            resource_name = next(name for name in sdist_names if name.endswith(suffix))
            resource_file = sdist_archive.extractfile(resource_name)
            assert resource_file is not None
            assert resource_file.read() == (
                candidate / "src/odibi_anchor/assurance/resources" / resource
            ).read_bytes()

    assert wheel_metadata == sdist_metadata == EXPECTED_DISTRIBUTION_METADATA

    raw_hashes_before = {
        "pyproject.toml": _sha256(candidate / "pyproject.toml"),
        "__init__.py": _sha256(candidate / "src" / "odibi_anchor" / "__init__.py"),
    }
    raw_script = """
        import json
        import sys
        sys.path.insert(0, sys.argv[1])
        import odibi_anchor
        print(json.dumps({"module": odibi_anchor.__file__, "version": odibi_anchor.__version__}))
    """
    raw_environment = {**environment, "PYTHONDONTWRITEBYTECODE": "1"}
    raw = json.loads(
        _run(
            [sys.executable, "-B", "-S", "-c", textwrap.dedent(raw_script), str(candidate / "src")],
            cwd=audit_root,
            environment=raw_environment,
        ).stdout
    )
    assert raw["version"] == "0.2.0"
    assert Path(raw["module"]).is_relative_to(candidate)

    collision = audit_root / "collision"
    fake_metadata = collision / "odibi_anchor-9.9.9.dist-info"
    fake_metadata.mkdir(parents=True)
    (fake_metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: odibi-anchor\nVersion: 9.9.9\n",
        encoding="utf-8",
    )
    collision_script = raw_script.replace(
        "sys.path.insert(0, sys.argv[1])",
        "sys.path[:0] = [sys.argv[1], sys.argv[2]]",
    )
    collision_result = json.loads(
        _run(
            [
                sys.executable,
                "-B",
                "-S",
                "-c",
                textwrap.dedent(collision_script),
                str(candidate / "src"),
                str(collision),
            ],
            cwd=audit_root,
            environment=raw_environment,
        ).stdout
    )
    assert collision_result["version"] == "0.2.0"
    assert raw_hashes_before == {
        "pyproject.toml": _sha256(candidate / "pyproject.toml"),
        "__init__.py": _sha256(candidate / "src" / "odibi_anchor" / "__init__.py"),
    }

    wrong_project = audit_root / "wrong-project"
    _copy_package_candidate(wrong_project)
    wrong_pyproject = wrong_project / "pyproject.toml"
    wrong_pyproject.write_text(
        wrong_pyproject.read_text(encoding="utf-8").replace(
            'name = "odibi-anchor"',
            'name = "unrelated-project"',
            1,
        ),
        encoding="utf-8",
    )
    wrong_source = json.loads(
        _run(
            [sys.executable, "-B", "-S", "-c", textwrap.dedent(raw_script), str(wrong_project / "src")],
            cwd=audit_root,
            environment=raw_environment,
        ).stdout
    )
    assert wrong_source["version"] == "0+unknown"

    wheel_python = _create_venv(audit_root / "wheel-venv", environment)
    _run([str(wheel_python), "-m", "pip", "install", "--no-deps", str(wheel)], environment=environment)
    wheel_probe = _runtime_probe(wheel_python, environment, cwd=audit_root)
    _assert_runtime_metadata(wheel_probe)
    installed_feature_script = """
        import json
        import sqlite3
        import sys
        from pathlib import Path
        from odibi_anchor import scaffold_portfolio, setup_host, snapshot_state

        root = Path(sys.argv[1])
        root.mkdir()
        host = root / "host"
        host.mkdir()
        setup = setup_host(host, adapter="amp")
        scaffold = scaffold_portfolio(
            root / "anchor.toml", host_id="local", adapter="amp",
            target_root=str(root), project_id="project", authority_id="work",
        )
        database = root / "memory.db"
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE evidence (value TEXT)")
        connection.execute("INSERT INTO evidence VALUES ('verified')")
        connection.commit()
        connection.close()
        durable = root / "durable"
        durable.mkdir()
        snapshot = snapshot_state(
            source_db=database, durable_root=durable, authority_id="work",
        )
        print(json.dumps({
            "setup": setup["status"], "scaffold": scaffold["status"],
            "snapshot": snapshot["action"],
        }))
    """
    installed_features = json.loads(_run(
        [str(wheel_python), "-c", textwrap.dedent(installed_feature_script), str(audit_root / "features")],
        cwd=audit_root, environment=environment,
    ).stdout)
    assert installed_features == {
        "setup": "installed", "scaffold": "created", "snapshot": "created",
    }
    portfolio_schema = json.loads(_run(
        [str(wheel_python.parent / "anchor"), "portfolio", "schema"],
        cwd=audit_root, environment=environment,
    ).stdout)
    assert portfolio_schema["ok"] is True
    assert portfolio_schema["result"]["schema"]["schema_version"] == 1
    assert Path(wheel_probe["module"]).is_relative_to(audit_root / "wheel-venv")
    qualification_script = """
        import hashlib
        import json
        from odibi_anchor.assurance import (
            PromotionPolicy, QualificationRun, ScenarioJudgement, build_scorecard,
            canonical_json_bytes, load_scenario_manifest,
        )
        from odibi_anchor.assurance.runner import packaged_resource

        manifest = packaged_resource("scenario_manifest.json")
        policy = packaged_resource("promotion_policy.json")
        scenarios = load_scenario_manifest(manifest)
        loaded_policy = PromotionPolicy.from_path(policy)
        digest = "sha256:" + "a" * 64
        run = QualificationRun(
            "synthetic-run", "public-t1-localized-fix", "1.0", "a" * 40, "0.2.0",
            digest, "synthetic-family", "1", "producer", "direct-python", "direct",
            "T1", "localized", "source-change", "Anchor-T1-TESTS@1.0",
            "sha256:" + "0" * 63 + "5", "2026-08-20T00:00:00Z",
            "2026-08-20T00:01:00Z", "completed", (digest,), (),
        )
        judgement = ScenarioJudgement(
            "synthetic-judgement", "synthetic-run", "public-t1-localized-fix", "behavior",
            "pass", "none", False, False, False, 0, ("evidence",), "reviewer", True,
            True, True, 1, 1, 1, 1, 5, 1, 1, 1, 0,
        )
        scorecard_sha256 = hashlib.sha256(
            canonical_json_bytes(build_scorecard((run,), (judgement,)).to_dict())
        ).hexdigest()
        print(json.dumps({
            "cells": sorted({(item.tier, item.scope) for item in scenarios}),
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "policy_digest": loaded_policy.policy_digest,
            "policy_sha256": hashlib.sha256(policy.read_bytes()).hexdigest(),
            "scenario_count": len(scenarios),
            "scorecard_sha256": scorecard_sha256,
        }, sort_keys=True))
    """
    qualification_first = _run(
        [str(wheel_python), "-c", textwrap.dedent(qualification_script)],
        cwd=audit_root, environment=environment,
    ).stdout
    qualification_second = _run(
        [str(wheel_python), "-c", textwrap.dedent(qualification_script)],
        cwd=audit_root, environment=environment,
    ).stdout
    assert qualification_first == qualification_second
    qualification = json.loads(qualification_first)
    assert qualification["scenario_count"] == 16
    assert len(qualification["cells"]) == 8
    assert qualification["manifest_sha256"] == _sha256(
        candidate / "src/odibi_anchor/assurance/resources/scenario_manifest.json"
    )
    assert qualification["policy_sha256"] == _sha256(
        candidate / "src/odibi_anchor/assurance/resources/promotion_policy.json"
    )
    unavailable_evaluators = audit_root / "unavailable-evaluators"
    unavailable_evaluators.mkdir()
    qualification_cli = _run(
        [str(wheel_python), "-m", "odibi_anchor.assurance.runner", "validate",
         "--evaluator-root", str(unavailable_evaluators)],
        cwd=audit_root, environment=environment, expected_codes=(3,),
    )
    assert '"status": "unavailable"' in qualification_cli.stderr
    routing_script = """
        import hashlib
        import json
        from pathlib import Path
        import odibi_anchor._dispatcher._references as references

        root = Path(references.__file__).resolve().parents[2] / ".assistant" / "references"
        paths = [root / "registry.json"] + [root / value for value in json.loads(__import__("os").environ["HOUSE_REFERENCE_PATHS"])]
        before = {path.relative_to(root).as_posix(): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) for path in paths}
        matrix = [
            references.resolve_reference_guidance(root, {"execution_mode": "source_change", "domains": ["code"], "traits": []}),
            references.resolve_reference_guidance(root, {"execution_mode": "read_only", "domains": ["general"], "traits": []}, ["src/job.py"]),
            references.resolve_reference_guidance(root, {"execution_mode": "source_change", "domains": ["code"], "traits": []}, ["src/job.py", "queries/load.sql"], ["databricks"]),
        ]
        after = {path.relative_to(root).as_posix(): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) for path in paths}
        print(json.dumps({"before": before, "after": after, "matrix": matrix}))
    """
    installed_routing = json.loads(_run(
        [str(wheel_python), "-c", textwrap.dedent(routing_script)],
        cwd=audit_root,
        environment={
            **environment,
            "HOUSE_REFERENCE_PATHS": json.dumps(HOUSE_REFERENCE_PATHS),
        },
    ).stdout)
    assert installed_routing["before"] == installed_routing["after"]
    assert {
        relative: values[0]
        for relative, values in installed_routing["before"].items()
    } == {
        relative: _sha256(candidate / ".assistant" / "references" / relative)
        for relative in ("registry.json", *HOUSE_REFERENCE_PATHS)
    }
    assert [
        [item["id"] for item in guidance]
        for guidance in installed_routing["matrix"]
    ] == [
        ["development.house"],
        ["development.house", "development.python"],
        ["development.house", "development.python", "development.data-platform"],
    ]
    wheel_factory = _repository_factory_probe(wheel_python, environment, cwd=audit_root)
    assert wheel_factory["provider"] is False
    assert wheel_factory["workspace_path"] == "/Users/test@example.invalid/odibi_anchor"
    assert wheel_factory["evidence"]["acquisition_outcome"] == "not_applicable"
    fake_metadata_root = audit_root / "fake-metadata"
    fake_metadata = fake_metadata_root / "odibi_anchor-9.9.9.dist-info"
    fake_metadata.mkdir(parents=True)
    (fake_metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: odibi-anchor\nVersion: 9.9.9\n",
        encoding="utf-8",
    )
    masked_wheel_probe = _runtime_probe(
        wheel_python,
        environment,
        cwd=audit_root,
        metadata_prepend=fake_metadata_root,
    )
    assert masked_wheel_probe["runtime_version"] == "0.2.0"
    assert Path(masked_wheel_probe["module"]).is_relative_to(audit_root / "wheel-venv")
    (fake_metadata / "RECORD").write_bytes(b"\xff")
    assert _runtime_version_probe(
        wheel_python,
        environment,
        cwd=audit_root,
        metadata_prepend=fake_metadata_root,
    ) == "0.2.0"
    (fake_metadata / "RECORD").unlink()
    wheel_site_packages = Path(wheel_probe["module"]).parent.parent
    colocated_fake_metadata = wheel_site_packages / "odibi_anchor-9.9.9.dist-info"
    shutil.copytree(fake_metadata, colocated_fake_metadata)
    try:
        assert _runtime_probe(wheel_python, environment, cwd=audit_root)["runtime_version"] == "0.2.0"
        (colocated_fake_metadata / "RECORD").write_text(
            "odibi_anchor/__init__.py,,\n",
            encoding="utf-8",
        )
        ambiguous_wheel_probe = _runtime_probe(wheel_python, environment, cwd=audit_root)
        assert ambiguous_wheel_probe["runtime_version"] == "0+unknown"
    finally:
        shutil.rmtree(colocated_fake_metadata)
    wheel_metadata_path = next(wheel_site_packages.glob("odibi_anchor-0.2.0.dist-info")) / "METADATA"
    wheel_metadata_bytes = wheel_metadata_path.read_bytes()
    try:
        wheel_metadata_path.write_bytes(b"\xff")
        assert _runtime_version_probe(wheel_python, environment, cwd=audit_root) == "0+unknown"
    finally:
        wheel_metadata_path.write_bytes(wheel_metadata_bytes)
    wheel_cw = audit_root / "wheel-venv" / ("Scripts/anchor.exe" if os.name == "nt" else "bin/anchor")
    _run([str(wheel_cw), "--help"], cwd=audit_root, environment=environment)
    wheel_sidecar = audit_root / "wheel-venv" / (
        "Scripts/anchor-governance-sidecar.exe" if os.name == "nt" else "bin/anchor-governance-sidecar"
    )
    unavailable_sidecar = _run(
        [str(wheel_sidecar)],
        cwd=audit_root,
        environment=environment,
        expected_codes=(1,),
    )
    assert unavailable_sidecar.stdout == ""
    assert unavailable_sidecar.stderr == ""
    passive_probe = _run(
        [str(wheel_python), "-m", "odibi_anchor._governance_host_probe"],
        cwd=audit_root,
        environment=environment,
        expected_codes=(2,),
    )
    assert passive_probe.stderr == ""
    passive_report = json.loads(passive_probe.stdout)
    assert passive_report["document_type"] == "governance_host_capability_probe"
    assert passive_report["outcome"] == "unsupported"
    assert passive_report["authorizes_readiness"] is False
    _run(
        [str(wheel_python), "-m", "pip", "install", f"{wheel}[governance]"],
        environment=environment,
        timeout=300,
    )
    available_sidecar = _run(
        [str(wheel_sidecar)],
        cwd=audit_root,
        environment=environment,
        input_text=(
            '{"protocol_version":1,"request_id":"installed-shutdown",'
            '"operation":"shutdown","thread_id":"installed","payload":{}}\n'
        ),
    )
    assert available_sidecar.stderr == ""
    sidecar_lines = [json.loads(line) for line in available_sidecar.stdout.splitlines()]
    assert len(sidecar_lines) == 2
    assert set(sidecar_lines[0]) == {
        "handshake_version", "sidecar_instance_id", "challenge", "closure_public_key_base64",
        "closure_public_key_digest", "issued_at_ms",
    }
    assert sidecar_lines[1]["result"]["closed"] is True

    editable_python = _create_venv(audit_root / "editable-venv", environment)
    _run(
        [str(editable_python), "-m", "pip", "install", "--no-deps", "--use-pep517", "-e", str(candidate)],
        environment=environment,
        timeout=300,
    )
    editable_probe = _runtime_probe(editable_python, environment, cwd=audit_root)
    _assert_runtime_metadata(editable_probe)
    masked_editable_probe = _runtime_probe(
        editable_python,
        environment,
        cwd=audit_root,
        metadata_prepend=fake_metadata_root,
    )
    assert masked_editable_probe["runtime_version"] == "0.2.0"
    assert Path(masked_editable_probe["module"]).is_relative_to(candidate)
    assert Path(editable_probe["module"]).is_relative_to(candidate)
    editable_direct_url = json.loads(editable_probe["direct_url"])
    assert editable_direct_url["dir_info"]["editable"] is True
    parsed_editable_url = urlparse(editable_direct_url["url"])
    assert parsed_editable_url.scheme == "file"
    assert Path(url2pathname(parsed_editable_url.path)).resolve() == candidate.resolve()
    candidate_pyproject = candidate / "pyproject.toml"
    authoritative_pyproject = candidate_pyproject.read_text(encoding="utf-8")
    try:
        conflicting_pyproject = authoritative_pyproject.replace('version = "0.2.0"', 'version = "9.9.9"', 1)
        assert conflicting_pyproject != authoritative_pyproject
        candidate_pyproject.write_text(conflicting_pyproject, encoding="utf-8")
        editable_metadata_probe = _runtime_probe(editable_python, environment, cwd=audit_root)
        _assert_runtime_metadata(editable_metadata_probe)

        fake_editable_root = audit_root / "fake-editable-metadata"
        fake_editable_metadata = fake_editable_root / "odibi_anchor-9.9.9.dist-info"
        fake_editable_metadata.mkdir(parents=True)
        (fake_editable_metadata / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: odibi-anchor\nVersion: 9.9.9\n",
            encoding="utf-8",
        )
        fake_direct_url = {
            "url": candidate.resolve().as_uri(),
            "dir_info": {"editable": True},
        }
        fake_direct_url_path = fake_editable_metadata / "direct_url.json"
        fake_direct_url_path.write_text(json.dumps(fake_direct_url), encoding="utf-8")
        ambiguous_editable_probe = _runtime_probe(
            editable_python,
            environment,
            cwd=audit_root,
            metadata_prepend=fake_editable_root,
        )
        assert ambiguous_editable_probe["runtime_version"] == "0+unknown"

        fake_direct_url["url"] = f"file://unrelated-host{parsed_editable_url.path}"
        fake_direct_url_path.write_text(json.dumps(fake_direct_url), encoding="utf-8")
        nonlocal_authority_probe = _runtime_probe(
            editable_python,
            environment,
            cwd=audit_root,
            metadata_prepend=fake_editable_root,
        )
        assert nonlocal_authority_probe["runtime_version"] == "0.2.0"

        fake_direct_url["url"] = "file://[malformed"
        fake_direct_url_path.write_text(json.dumps(fake_direct_url), encoding="utf-8")
        malformed_url_probe = _runtime_probe(
            editable_python,
            environment,
            cwd=audit_root,
            metadata_prepend=fake_editable_root,
        )
        assert malformed_url_probe["runtime_version"] == "0.2.0"

        for relative_url in ("file:.", "file://localhost"):
            fake_direct_url["url"] = relative_url
            fake_direct_url_path.write_text(json.dumps(fake_direct_url), encoding="utf-8")
            assert _runtime_version_probe(
                editable_python,
                environment,
                cwd=candidate,
                metadata_prepend=fake_editable_root,
            ) == "0.2.0"

        fake_direct_url_path.write_bytes(b"\xff")
        assert _runtime_version_probe(
            editable_python,
            environment,
            cwd=candidate,
            metadata_prepend=fake_editable_root,
        ) == "0.2.0"
    finally:
        candidate_pyproject.write_text(authoritative_pyproject, encoding="utf-8")

    extracted = audit_root / "sdist-source"
    shutil.unpack_archive(str(sdist), extracted)
    sdist_root = extracted / "odibi_anchor-0.2.0"
    _validate_native_skill_layout(sdist_root / ".assistant")
    sdist_wheelhouse = audit_root / "sdist-wheelhouse"
    _run(
        [str(builder_python), "-m", "build", "--wheel", "--outdir", str(sdist_wheelhouse), str(sdist_root)],
        environment=environment,
        timeout=300,
    )
    sdist_wheel = next(sdist_wheelhouse.glob("odibi_anchor-0.2.0-*.whl"))
    with zipfile.ZipFile(sdist_wheel) as sdist_wheel_archive:
        sdist_wheel_names = sdist_wheel_archive.namelist()
        sdist_metadata_name = next(name for name in sdist_wheel_names if name.endswith(".dist-info/METADATA"))
        sdist_entry_points_name = next(
            name for name in sdist_wheel_names if name.endswith(".dist-info/entry_points.txt")
        )
        assert _metadata_contract(sdist_wheel_archive.read(sdist_metadata_name)) == EXPECTED_DISTRIBUTION_METADATA
        assert sdist_wheel_archive.read(sdist_entry_points_name).decode("utf-8") == (
            "[console_scripts]\nanchor = odibi_anchor.cli:main\n"
            "anchor-governance-sidecar = odibi_anchor._governance_sidecar.__main__:main\n"
        )
        assert sdist_wheel_archive.read(".assistant_instructions.md") == (
            candidate / ".assistant_instructions.md"
        ).read_bytes()
    sdist_python = _create_venv(audit_root / "sdist-venv", environment)
    _run([str(sdist_python), "-m", "pip", "install", "--no-deps", str(sdist_wheel)], environment=environment)
    sdist_probe = _runtime_probe(sdist_python, environment, cwd=audit_root)
    _assert_runtime_metadata(sdist_probe)
    assert Path(sdist_probe["module"]).is_relative_to(audit_root / "sdist-venv")
    sdist_factory = _repository_factory_probe(sdist_python, environment, cwd=audit_root)
    assert sdist_factory == wheel_factory
    sdist_cw = audit_root / "sdist-venv" / ("Scripts/anchor.exe" if os.name == "nt" else "bin/anchor")
    _run([str(sdist_cw), "--help"], cwd=audit_root, environment=environment)

    python_floor_probe = _run(
        [
            str(builder_python),
            "-c",
            (
                "from pip._internal.utils.packaging import check_requires_python; "
                "print(check_requires_python('>=3.11', (3, 10, 0))); "
                "print(check_requires_python('>=3.11', (3, 11, 0)))"
            ),
        ],
        environment=environment,
    )
    assert python_floor_probe.stdout.splitlines() == ["False", "True"]


def test_package_metadata_provenance_matrix(tmp_path: Path) -> None:
    """Qualify isolated Hatch artifacts, installs, provenance, and cleanup."""
    audit_root = tmp_path / "package-metadata-audit"
    try:
        _qualify_package_metadata_matrix(audit_root)
    finally:
        _remove_tree(audit_root)
    assert not audit_root.exists()


def test_installed_wheel_runner_honors_ambient_canonical_plugin_requests(tmp_path: Path) -> None:
    """Qualify exact plugin provenance through every ambient request surface."""
    audit_root = tmp_path / "installed-pytest-runner"
    wheelhouse = audit_root / "wheelhouse"
    target = audit_root / "target"
    wheelhouse.mkdir(parents=True)
    target.mkdir()
    environment = _clean_environment(audit_root)
    _run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheelhouse), "."],
        cwd=REPOSITORY_ROOT,
        environment=environment,
        timeout=300,
    )
    wheel = next(wheelhouse.glob("odibi_anchor-*.whl"))
    python = _create_venv(audit_root / "venv", environment)
    _run(
        [str(python), "-m", "pip", "install", str(wheel), "pytest"],
        environment=environment,
        timeout=300,
    )

    (target / "odibi_anchor").mkdir()
    (target / "odibi_anchor" / "__init__.py").write_text(
        "TARGET_PACKAGE = True\n", encoding="utf-8"
    )
    (target / "conftest.py").write_text(
        "pytest_plugins = ('odibi_anchor.pytest_runner',)\n", encoding="utf-8"
    )
    (target / "test_smoke.py").write_text(
        textwrap.dedent(
            """
            import os
            from pathlib import Path

            import odibi_anchor


            def test_smoke(pytestconfig):
                manager = pytestconfig.pluginmanager
                plugin = manager.get_plugin("odibi_anchor.pytest_runner")
                summaries = [
                    candidate
                    for candidate in manager.get_plugins()
                    if candidate.__class__.__name__ == "_SummaryPlugin"
                ]
                assert Path(plugin.__file__).resolve() == Path(os.environ["EXPECTED_RUNNER"]).resolve()
                assert len(summaries) == 1
                assert manager.get_plugin("odibi-anchor-summary") is summaries[0]
                assert odibi_anchor.TARGET_PACKAGE is True
            """
        ),
        encoding="utf-8",
    )
    probe = _write_script(
        audit_root,
        "pytest_runner_probe.py",
        """
        import json
        import os
        from pathlib import Path

        import odibi_anchor.pytest_runner as runner

        os.environ["EXPECTED_RUNNER"] = str(Path(runner.__file__).resolve())
        summary, proc = runner.run_pytest(["-q"], cwd=os.environ["TARGET"], capture_output=True)
        print(json.dumps({
            "runner": os.environ["EXPECTED_RUNNER"],
            "returncode": proc.returncode,
            "summary": summary,
        }))
        """,
    )
    result = json.loads(
        _run(
            [str(python), str(probe)],
            cwd=audit_root,
            environment={
                **environment,
                "TARGET": str(target),
                "PYTEST_PLUGINS": "odibi_anchor.pytest_runner",
                "PYTEST_ADDOPTS": "-p odibi_anchor.pytest_runner",
            },
        ).stdout
    )

    assert result["returncode"] == 0
    assert result["summary"]["passed"] == 1
    assert result["summary"]["failed"] == result["summary"]["errors"] == 0
    assert Path(result["runner"]).is_relative_to((audit_root / "venv").resolve())


def test_installed_wheel_exposes_governed_owner_memory_promotion(tmp_path: Path) -> None:
    """Exercise both owner transitions through the installed dispatcher path."""
    audit_root = tmp_path / "owner-promotion"
    wheelhouse, target = audit_root / "wheelhouse", audit_root / "target"
    wheelhouse.mkdir(parents=True)
    target.mkdir()
    environment = {
        **os_required_environment(),
        **isolated_home_environment(audit_root / "os-home"),
        "PATH": os.environ.get("PATH", ""),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
    _run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", *build_pip_arguments(),
         "-w", str(wheelhouse), "."],
        cwd=REPOSITORY_ROOT, environment=environment, timeout=300,
    )
    wheel = next(wheelhouse.glob("odibi_anchor-*.whl"))
    python = _create_venv(audit_root / "venv", environment)
    _run(
        [str(python), "-m", "pip", "install", str(wheel)],
        environment=environment, timeout=300,
    )
    probe = _write_script(
        audit_root,
        "owner_promotion_probe.py",
        """
        import json, os, sqlite3
        from types import SimpleNamespace
        import odibi_anchor.human_input_owner as human_input_owner
        import odibi_anchor.human_input_windows as human_input_windows
        from odibi_anchor._dispatcher._memory_actions import memory_action
        from odibi_anchor.codebase.memory_context import append_memory

        db = os.environ["ANCHOR_MEMORY_DB"]
        memory = append_memory(
            os.environ["TARGET"], entry_type="convention",
            content="Installed owner-governed convention.",
            project="project:installed", db_path=db,
        )
        human_input_owner._is_windows = lambda: True
        human_input_windows._windows_username = lambda: "installed-owner"
        human_input_windows._message_box = lambda *_args: 6
        os.environ["ANCHOR_HUMAN_INPUT_STATE_PATH"] = os.path.join(
            os.path.dirname(db), "human-input.db",
        )
        for variable in ("ANCHOR_SLACK_BOT_TOKEN", "ANCHOR_SLACK_CHANNEL_ID", "ANCHOR_SLACK_USER_ID"):
            os.environ.pop(variable, None)
        state = SimpleNamespace(active_project="project:installed", task_window_id="ltw-owner")
        def transition(command):
            return memory_action(
                os.environ["TARGET"], ("promotion",), {
                    "command":command, "memory_id":memory["id"], "db_path":db,
                    "timeout_minutes":1,
                }, session_state=state, query_fn=None, render_fn=None,
            )
        activated = transition("request_owner_activation")
        confirmed = transition("request_owner_confirmation")
        with sqlite3.connect(db) as connection:
            authority = connection.execute(
                "SELECT status,confirmation_count FROM memories WHERE id=?", (memory["id"],),
            ).fetchone()
            receipts = connection.execute(
                "SELECT transport,payload_json FROM memory_human_authority_receipts "
                "ORDER BY created_at"
            ).fetchall()
        with sqlite3.connect(os.environ["ANCHOR_HUMAN_INPUT_STATE_PATH"]) as connection:
            requests = connection.execute(
                "SELECT count(*) FROM human_input_requests WHERE status='answered'"
            ).fetchone()[0]
        print(json.dumps({
            "activated":activated, "confirmed":confirmed, "authority":authority,
            "receipts":receipts, "requests":requests,
        }))
        """,
    )
    result = json.loads(_run(
        [str(python), str(probe)], cwd=audit_root,
        environment={
            **environment, "TARGET":str(target),
            "ANCHOR_MEMORY_DB":str(audit_root / "memory.db"),
        },
    ).stdout)
    assert result["activated"]["event"]["authority_lane"] == "human_owner"
    assert result["confirmed"]["event"]["authority_lane"] == "human_owner"
    assert result["authority"] == ["confirmed", 0]
    assert result["requests"] == 2
    assert len(result["receipts"]) == 2
    assert all(row[0] == "local-windows-owner-presence" for row in result["receipts"])
    assert all(
        json.loads(row[1])["owner_assurance"]
        == "interactive_local_windows_account_presence"
        for row in result["receipts"]
    )


def test_revision_8_structured_learning_across_installed_transports(tmp_path: Path) -> None:
    """Qualify revision-8 learning through a fresh wheel and every stateful transport."""
    wheelhouse, venv = tmp_path / "wheelhouse", tmp_path / "venv"
    target, cwd = tmp_path / "target", tmp_path / "cwd"
    wheelhouse.mkdir()
    target.mkdir()
    cwd.mkdir()
    environment = {
        **os_required_environment(),
        **isolated_home_environment(tmp_path / "os-home"),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONIOENCODING": "utf-8",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    build_arguments = build_pip_arguments()
    install_arguments = install_pip_arguments()
    _run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", *build_arguments, "-w", str(wheelhouse), "."],
        cwd=REPOSITORY_ROOT,
        environment=environment,
        timeout=300,
    )
    wheel = next(wheelhouse.glob("odibi_anchor-*.whl"))
    python = _create_venv(venv, environment)
    _run(
        [
            str(python), "-m", "pip", "install", *install_arguments,
            f"{wheel}[mcp]", "pytest>=7.0",
        ],
        environment=environment,
        timeout=300,
    )
    anchor_executable = venv / ("Scripts/anchor.exe" if os.name == "nt" else "bin/anchor")

    probe = _write_script(
        tmp_path,
        "revision8_probe.py",
        r'''
        import asyncio, contextlib, hashlib, io, json, os, sqlite3, subprocess
        from pathlib import Path

        TASK = dict(
            arg0="Qualify revision-8 structured learning.", goal="Exercise the complete learning lifecycle.",
            mode="implementation", execution_mode="artifact_only",
            current_state="An isolated installed-wheel target exists.",
            desired_outcome="Learning is assessed without transport drift.", constraints=["Read only target."],
            known_facts=["The candidate is an installed wheel."],
            evidence=[{"source":"qualification","observation":"fresh isolated home"}],
            in_scope=["structured learning"], out_of_scope=["target source"], risks=["transport drift"],
            acceptance_criteria=["Gate and assessment succeed."], stop_conditions=["target mutation"],
            deliverables=["transport evidence"],
        )

        def lifecycle(call, name, capture=False):
            for action in ("status", "memory", "audit_history", "orient"):
                call(action, {})
            call("new_session", {"name": name, "inline": True})
            task = call("task", dict(TASK))
            for skill in task["required_skills"]:
                if skill["path"] != "n/a": call("skill_loaded", {"arg0": skill["skill"]})
            call("gate", {})
            if capture:
                item = call("learning", {"arg0":"capture", "observation_type":"friction",
                    "summary":"Installed transports preserve structured learning.",
                    "signal_key":"installed.transport.learning", "impact":"medium",
                    "applicability_scope":"workbench", "project_refs":["project:A", "project:B"],
                    "work_package_refs":[], "environment_refs":["wheel"],
                    "provenance":{"source_action":"gate","source_version":"revision-8"},
                    "evidence":[{"reference_type":"test","reference":"tests/test_installed_distribution.py"}]})["item"]
                assessment = call("learning", {"arg0":"assess", "outcome":"observations_recorded",
                                               "observation_ids":[item["item_id"]]})
                return {"obligation_id": assessment["assessment"]["obligation_id"]}, item, assessment
            assessment = call("learning", {"arg0":"assess", "outcome":"nothing_reusable_learned",
                                            "notes":"No reusable finding in this isolated transport."})
            return {"obligation_id": assessment["assessment"]["obligation_id"]}, None, assessment

        def complete_learning(call, name):
            obligation, item, assessment = lifecycle(call, name, True)
            derived = call("learning", {"arg0":"triage", "decision":"derive_lesson",
                "source_item_ids":[item["item_id"]],
                "expected_source_versions":{item["item_id"]:item["version"]},
                "summary":"Human review classified installed transport behavior.", "impact":"high",
                "applicability_scope":"cross_project", "project_refs":["project:A","project:B"],
                "work_package_refs":[], "environment_refs":["wheel"],
                "provenance":{"source_action":"review","source_version":"revision-8"},
                "evidence":[{"reference_type":"test","reference":"tests/test_installed_distribution.py"}],
                "actor_kind":"human", "actor_ref":"reviewer:installed",
                "decision_source":"installed:revision8:derive", "rationale":"Explicit installed-route review."})
            listed = call("learning", {"arg0":"list", "kind":"lesson", "status":"active",
                                      "applicability_project":"project:B"})
            shown = call("learning", {"arg0":"show", "item_id":derived["item"]["item_id"],
                                      "include_history":True})
            insights = call("learning", {"arg0":"insights", "applicability_project":"project:A"})
            no_learning, _, no_assessment = lifecycle(call, name + "_no_learning", False)
            stats = call("memory_stats", {"output_format":"dict"})
            return {"obligation":obligation,"item":item,"assessment":assessment,"derived":derived,
                    "listed":listed,"shown":shown,"insights":insights,"stats":stats,
                    "no_learning":no_learning,"no_assessment":no_assessment}

        mode = os.environ["MODE"]
        if mode in {"callable", "stdio"}:
            from odibi_anchor._dispatcher._project import (
                project_action, resolve_route_binding,
            )
            home = Path(os.environ["ANCHOR_HOME"])
            home.mkdir(parents=True, exist_ok=True)
            try:
                resolve_route_binding(
                    home, runtime_instance_id="installed-transport-setup",
                    target_hint=os.environ["TARGET"],
                )
            except FileNotFoundError:
                project_action(
                    home, "create", name="Transport", target=os.environ["TARGET"],
                    output_format="dict",
                )
        if mode == "routing-read":
            from odibi_anchor.codebase.structured_learning_context import structured_learning_context
            item_id = os.environ["ITEM_ID"]
            print(json.dumps({
                "shown": structured_learning_context(command="show", item_id=item_id),
                "project_a": structured_learning_context(
                    command="list", applicability_project="project:A"
                ),
                "project_b": structured_learning_context(
                    command="list", applicability_project="project:B"
                ),
                "exported": structured_learning_context(command="export"),
            }))
        elif mode == "promotion":
            from odibi_anchor.codebase._memory_promotion import (
                evaluate_shadow_promotion,
                inspect_shadow_promotion,
            )
            from odibi_anchor.codebase.memory_context import append_memory
            db = os.environ["ANCHOR_MEMORY_DB"]
            memory = append_memory(
                os.environ["TARGET"], entry_type="gotcha",
                content="Installed shadow promotion remains fail closed.",
                project="project:installed", db_path=db,
            )
            first = evaluate_shadow_promotion(
                db, memory_id=memory["id"], project_id="project:installed",
            )
            second = evaluate_shadow_promotion(
                db, memory_id=memory["id"], project_id="project:installed",
            )
            inspected = inspect_shadow_promotion(db, project_id="project:installed")
            with sqlite3.connect(db) as connection:
                authority = connection.execute(
                    "SELECT status,confidence,confirmation_count FROM memories WHERE id=?",
                    (memory["id"],),
                ).fetchone()
            print(json.dumps({
                "first": first, "second": second, "inspected": inspected,
                "authority": authority,
            }))
        elif mode == "promotion-verifier":
            from odibi_anchor.codebase import structured_learning_context as learning
            from odibi_anchor.codebase._memory_promotion import (
                finalize_verifier_attestation,
                inspect_shadow_promotion,
            )
            from odibi_anchor.codebase._memory_verifier import (
                canonical_pytest_result_claim,
                inspect_verifier_runs,
                run_installed_verifier,
            )
            from odibi_anchor.codebase._task_authority import initialize_schema as init_tasks
            from odibi_anchor.codebase._task_execution import (
                FORMAT as TERMINAL_FORMAT,
                UNOBSERVED,
                persist_terminal_record,
            )

            root = Path(os.environ["TARGET"]) / "installed-verifier"
            (root / "tests").mkdir(parents=True)
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True,
                           capture_output=True)
            subprocess.run(["git", "config", "user.email", "tests@example.invalid"],
                           cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Tests"], cwd=root, check=True)
            (root / "tests" / "test_claim.py").write_text(
                "def test_claim():\n    assert 2 + 2 == 4\n", encoding="utf-8"
            )
            (root / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "source"], cwd=root, check=True,
                           capture_output=True)
            source_head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, check=True,
                capture_output=True, text=True,
            ).stdout.strip()

            db = Path(os.environ["ANCHOR_MEMORY_DB"])
            obligation = learning.activate_learning_obligation(
                task_window_id="ltw_installed_source", session_ref="session:installed-source",
                checkpoint_ref="gate:installed-source", project_ref="project:installed",
            )
            item = learning._structured_learning_dispatch(
                command="capture", _obligation_id=obligation["obligation_id"],
                _project_id="project:installed", _task_window_id="ltw_installed_source",
                observation_type="reusable_practice",
                summary=canonical_pytest_result_claim(
                    project_id="project:installed",
                    selectors=["tests/test_claim.py::test_claim"],
                ),
                signal_key="installed.verifier", impact="medium",
                applicability_scope="project_local", project_refs=["project:installed"],
                work_package_refs=[], environment_refs=[],
                provenance={"source_action":"test","source_version":"installed"},
                evidence=[{"reference_type":"test",
                           "reference":"tests/test_claim.py::test_claim"}],
            )["item"]
            assessed = learning._structured_learning_dispatch(
                command="assess", _obligation_id=obligation["obligation_id"],
                _project_id="project:installed", _task_window_id="ltw_installed_source",
                outcome="observations_recorded", observation_ids=[item["item_id"]],
            )
            memory_id = assessed["semantic_candidate_projections"][0]["memory_id"]

            def terminal(task, revision):
                return persist_terminal_record(db, {
                    "format": TERMINAL_FORMAT,
                    "identities": {"task_window_id":task,"session_id":"session:"+task,
                                   "project_id":"project:installed","problem_id":None,
                                   "spec_id":None,"work_item_id":None},
                    "started_at":"2026-09-01T00:00:00Z",
                    "ended_at":"2026-09-01T00:01:00Z",
                    "repository":{"start_revision":revision,"end_revision":revision,
                                  "branch":"main"},
                    "terminal":{"status":"completed"},
                    "coverage":{"unobserved":list(UNOBSERVED),"unavailable":[],
                                "replay_claim":"none"},
                })

            terminal("ltw_installed_source", source_head)
            (root / "source.py").write_text("VALUE = 2\n", encoding="utf-8")
            subprocess.run(["git", "add", "source.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "verifier"], cwd=root, check=True,
                           capture_output=True)
            verifier_head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, check=True,
                capture_output=True, text=True,
            ).stdout.strip()

            init_tasks(db)
            task = "ltw_installed_verifier"
            accepted_at = "2026-09-01T00:02:00Z"
            record_id = "atr_" + hashlib.sha256(task.encode()).hexdigest()
            record = {
                "format":"odibi-anchor-accepted-task-v1", "record_id":record_id,
                "accepted_at":accepted_at,
                "identity":{"task_window_id":task,"session_id":"session:"+task,
                            "project_id":"project:installed","anchor_home":os.environ["ANCHOR_HOME"],
                            "project_root":str(root),"artifact_root":str(root),
                            "target_root":str(root.resolve()),"repository_provider_id":None,
                            "trust_domain":"project:installed"},
                "task":{"mode":"implementation","goal":"Installed verification","tags":[],
                        "profile":{"execution_mode":"artifact_only"}},
                "obligations":{"acceptance_criteria":["Verifier passes"],"required_skills":[]},
                "repository_baseline":{"kind":"TaskRepositoryBaseline","value":{
                    "branch":"main","captured_at":accepted_at,"configured_target_ref":"main",
                    "merge_base_sha":verifier_head,"target_sha":verifier_head,
                    "target_worktree":str(root.resolve()),"task_start_head_sha":verifier_head,
                }},"initial_ledgers":{},
            }
            raw = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            with sqlite3.connect(db) as connection:
                connection.execute(
                    "INSERT INTO accepted_task_records VALUES(?,?,?,?,?,?,?,?,?)",
                    (task,record_id,"project:installed",str(root.resolve()),"artifact_only",
                     accepted_at,raw,hashlib.sha256(raw.encode()).hexdigest(),accepted_at),
                )
            verified = run_installed_verifier(
                db, memory_id=memory_id, project_id="project:installed",
                task_window_id=task, target_root=root,
            )
            if verified["run"]["result_state"] != "passed":
                raise RuntimeError(json.dumps(verified, sort_keys=True))
            terminal_result = terminal(task, verifier_head)
            attested = terminal_result["memory_promotion_finalization"]["results"][0]
            with sqlite3.connect(db) as connection:
                authority = connection.execute(
                    "SELECT status,confidence,confirmation_count FROM memories WHERE id=?",
                    (memory_id,),
                ).fetchone()
            print(json.dumps({
                "verified":verified,"attested":attested,
                "runs":inspect_verifier_runs(db, project_id="project:installed"),
                "promotion":inspect_shadow_promotion(db, project_id="project:installed"),
                "authority":authority,
            }))
        elif mode == "routing":
            from odibi_anchor.bootstrap import init
            from odibi_anchor._dispatcher._project import project_action
            Path(os.environ["ANCHOR_HOME"]).mkdir(parents=True, exist_ok=True)
            project_action(os.environ["ANCHOR_HOME"], "create", name="A", target=os.environ["PROJECT_A"], output_format="dict")
            project_action(os.environ["ANCHOR_HOME"], "create", name="B", target=os.environ["PROJECT_B"], output_format="dict")
            with contextlib.redirect_stdout(io.StringIO()): anchor, _, _ = init(root=os.environ["TARGET"], output_format="dict")
            def call(action, args):
                args=dict(args); positional=[args.pop("arg0")] if "arg0" in args else []
                if "arg1" in args: positional.append(args.pop("arg1"))
                return anchor(action, *positional, **args, output_format="dict")
            for action in ("status","memory","audit_history","orient"): call(action,{})
            call("new_session",{"name":"routing","inline":True}); task=call("task",dict(TASK))
            for skill in task["required_skills"]:
                if skill["path"] != "n/a": call("skill_loaded",{"arg0":skill["skill"]})
            used_a=call("project",{"arg0":"use","arg1":"a"})
            with contextlib.redirect_stdout(io.StringIO()): anchor, _, _ = init(root=os.environ["PROJECT_A"], output_format="dict")
            for action in ("status","memory","audit_history","orient"): call(action,{})
            call("new_session",{"name":"routing_a","inline":True}); task=call("task",dict(TASK))
            for skill in task["required_skills"]:
                if skill["path"] != "n/a": call("skill_loaded",{"arg0":skill["skill"]})
            call("gate", {})
            item = call("learning", {"arg0":"capture", "observation_type":"friction",
                "summary":"Project routing preserves Workbench-owned learning.",
                "signal_key":"installed.project.routing", "impact":"medium",
                "applicability_scope":"cross_project", "project_refs":["project:A","project:B"],
                "work_package_refs":[], "environment_refs":["wheel"],
                "provenance":{"source_action":"gate","source_version":"revision-8"},
                "evidence":[{"reference_type":"test","reference":"tests/test_installed_distribution.py"}]})["item"]
            call("learning", {"arg0":"assess", "outcome":"observations_recorded",
                              "observation_ids":[item["item_id"]]})
            used_b=call("project",{"arg0":"use","arg1":"b"})
            with contextlib.redirect_stdout(io.StringIO()): anchor, _, _ = init(root=os.environ["PROJECT_B"], output_format="dict")
            print(json.dumps({"used_a":used_a,"used_b":used_b,"status":call("project",{}),
                "item":item, "shown":call("learning", {"arg0":"show","item_id":item["item_id"]}),
                "project_a":call("learning", {"arg0":"list","applicability_project":"project:A"}),
                "project_b":call("learning", {"arg0":"list","applicability_project":"project:B"}),
                "exported":call("learning", {"arg0":"export"})}))
        elif mode == "legacy-setup":
            from odibi_anchor.bootstrap import init
            with contextlib.redirect_stdout(io.StringIO()):
                anchor, _, _ = init(root=os.environ["TARGET"], output_format="dict")
            def call(action, args):
                args = dict(args)
                positional = [args.pop("arg0")] if "arg0" in args else []
                return anchor(action, *positional, **args, output_format="dict")
            for action in ("status", "memory", "audit_history", "orient"):
                call(action, {})
            call("new_session", {"name":"legacy_recovery", "inline":True})
            task = call("task", dict(TASK))
            for skill in task["required_skills"]:
                if skill["path"] != "n/a":
                    call("skill_loaded", {"arg0":skill["skill"]})
            call("gate", {})
            from odibi_anchor.codebase._memory_db import resolve_project
            from odibi_anchor.codebase.structured_learning_context import active_learning_obligation
            owner = task["operating_protocol"]["authority"]
            print(json.dumps({"active": active_learning_obligation(
                project_id=resolve_project(os.environ["TARGET"]),
                task_window_id=owner["task_window_id"],
            )}))
        elif mode == "legacy-close":
            from odibi_anchor.bootstrap import init
            with contextlib.redirect_stdout(io.StringIO()):
                anchor, _, _ = init(root=os.environ["TARGET"], output_format="dict")
            learned = anchor("learn", session_events=[{
                "type":"discovery",
                "detail":"Legacy recovery closed the exact restarted obligation.",
            }], output_format="dict")
            exported = anchor("learning", "export", output_format="dict")
            print(json.dumps({"learned":learned, "exported":exported}))
        elif mode == "direct":
            from odibi_anchor.bootstrap import init
            from odibi_anchor._dispatcher._project import project_action
            Path(os.environ["ANCHOR_HOME"]).mkdir(parents=True, exist_ok=True)
            project_action(
                os.environ["ANCHOR_HOME"], "create", name="Direct", target=os.environ["TARGET"],
                output_format="dict",
            )
            with contextlib.redirect_stdout(io.StringIO()): anchor, _, _ = init(root=os.environ["TARGET"], output_format="dict")
            call = lambda action, args: anchor(action, *([args.pop("arg0")] if "arg0" in args else []), **args, output_format="dict")
            second, item, assessed2 = lifecycle(call, "direct_two_cycles", True)
            call("gate", {})
            assessed1 = call("learning", {"arg0":"assess", "outcome":"nothing_reusable_learned",
                "notes":"The second cycle in the same accepted task had no reusable learning."})
            first = {"obligation_id": assessed1["assessment"]["obligation_id"]}
            listed = call("learning", {"arg0":"list"})
            triaged = call("learning", {"arg0":"triage", "decision":"defer", "item_id":item["item_id"],
                "expected_version":item["version"], "actor_kind":"human", "actor_ref":"reviewer:test",
                "decision_source":"installed:revision8", "rationale":"Keep visible for both projects."})
            shown = call("learning", {"arg0":"show", "item_id":item["item_id"], "include_history":True})
            exported, backup = call("learning", {"arg0":"export"}), call("learning", {"arg0":"backup"})
            # An active obligation is durable debt and blocks all task-window replacements.
            lifecycle_start = lambda: lifecycle(call, "debt_setup")
            for action, args in (("status",{}),("memory",{}),("audit_history",{}),("orient",{})):
                call(action,args)
            call("new_session", {"name":"debt_setup", "inline":True}); task=call("task", dict(TASK))
            for skill in task["required_skills"]:
                if skill["path"] != "n/a": call("skill_loaded", {"arg0":skill["skill"]})
            call("gate", {})
            blocked=[]
            for action,args in (("task",TASK),("new_session",{"name":"blocked_session","inline":True}),
                                ("project",{"arg0":"create","arg1":"blocked_project"})):
                try: call(action, dict(args))
                except Exception as exc: blocked.append(str(exc))
            call("learning", {"arg0":"assess", "outcome":"nothing_reusable_learned", "notes":"Debt closed."})
            print(json.dumps({"first":first,"second":second,"item":item,"a1":assessed1,"a2":assessed2,
                "listed":listed,"triaged":triaged,"shown":shown,"exported":exported,"backup":backup,
                "blocked":blocked}))
        elif mode == "callable":
            import odibi_anchor.mcp_server as server
            version=int(os.environ["VERSION"])
            def call(action, args):
                payload=json.dumps(args) if args else None
                value=json.loads(server.anchor_execute(action, payload, response_version=version, response_detail="full"))
                if version == 2:
                    assert set(value) == {"ok","result"} and value["ok"] is True
                    return value["result"]
                return value
            initial=call("learning", {"arg0":"list"}); completed=complete_learning(call, f"callable_v{version}")
            if version == 1:
                try: server.anchor_execute("not_an_action", response_version=1)
                except ValueError as exc: error={"text":str(exc),"type":type(exc).__name__}
                else: raise AssertionError("v1 error was not raised")
            else:
                error=json.loads(server.anchor_execute("not_an_action", response_version=2))
                assert set(error)=={"ok","error"} and error["ok"] is False
            print(json.dumps({"initial":initial,"completed":completed,"error":error}))
        else:
            from fastmcp import Client
            from fastmcp.client.transports import StdioTransport
            async def run():
                transport=StdioTransport(command=os.environ["PYTHON"], args=["-m","odibi_anchor.mcp_server"],
                    env=os.environ.copy(), cwd=os.environ["TARGET"])
                async with Client(transport, timeout=60) as client:
                    assert sorted(x.name for x in await client.list_tools()) == ["anchor_execute","anchor_help"]
                    version=int(os.environ["VERSION"])
                    async def acall(action,args):
                        request={"action":action,"response_version":version,"response_detail":"full"}
                        if args: request["args"]=json.dumps(args)
                        result=await client.call_tool("anchor_execute",request,timeout=60)
                        value=json.loads(result.content[0].text)
                        if version==2: assert value["ok"] is True; value=value["result"]
                        return value
                    initial=await acall("learning",{"arg0":"list"})
                    # Async spelling of the same complete lifecycle.
                    for action in ("status","memory","audit_history","orient"): await acall(action,{})
                    await acall("new_session",{"name":f"stdio_v{version}","inline":True})
                    task=await acall("task",TASK)
                    for skill in task["required_skills"]:
                        if skill["path"]!="n/a": await acall("skill_loaded",{"arg0":skill["skill"]})
                    gate=await acall("gate",{})
                    item=(await acall("learning",{"arg0":"capture","observation_type":"friction",
                        "summary":"Installed stdio preserves structured learning.","signal_key":f"installed.stdio.v{version}",
                        "impact":"medium","applicability_scope":"cross_project","project_refs":["project:A","project:B"],
                        "work_package_refs":[],"environment_refs":["wheel"],
                        "provenance":{"source_action":"gate","source_version":"revision-8"},
                        "evidence":[{"reference_type":"test","reference":"tests/test_installed_distribution.py"}]}))["item"]
                    assessment=await acall("learning",{"arg0":"assess","outcome":"observations_recorded",
                                                        "observation_ids":[item["item_id"]]})
                    derived=await acall("learning",{"arg0":"triage","decision":"derive_watch",
                        "source_item_ids":[item["item_id"]],"expected_source_versions":{item["item_id"]:item["version"]},
                        "summary":"Human-attested stdio watch.","impact":"high","applicability_scope":"cross_project",
                        "project_refs":["project:A","project:B"],"work_package_refs":[],"environment_refs":["wheel"],
                        "provenance":{"source_action":"review","source_version":"revision-8"},
                        "evidence":[{"reference_type":"test","reference":"tests/test_installed_distribution.py"}],
                        "actor_kind":"human","actor_ref":"reviewer:installed","decision_source":f"stdio:v{version}",
                        "rationale":"Explicit installed stdio review."})
                    listed=await acall("learning",{"arg0":"list","kind":"watch_item","status":"active",
                                                   "applicability_project":"project:B"})
                    shown=await acall("learning",{"arg0":"show","item_id":derived["item"]["item_id"],"include_history":True})
                    insights=await acall("learning",{"arg0":"insights","applicability_project":"project:A"})
                    for action in ("status","memory","audit_history","orient"): await acall(action,{})
                    await acall("new_session",{"name":f"stdio_v{version}_second","inline":True})
                    task=await acall("task",TASK)
                    for skill in task["required_skills"]:
                        if skill["path"]!="n/a": await acall("skill_loaded",{"arg0":skill["skill"]})
                    await acall("gate",{})
                    no_assessment=await acall("learning",{"arg0":"assess","outcome":"nothing_reusable_learned",
                                                           "notes":"No learning on second stdio cycle."})
                    stats=await acall("memory_stats",{"output_format":"dict"})
                    print(json.dumps({"initial":initial,"gate":gate,"item":item,"assessment":assessment,
                        "derived":derived,"listed":listed,"shown":shown,"insights":insights,
                        "stats":stats,"no_assessment":no_assessment}))
            asyncio.run(run())
        ''',
    )

    def run_probe(mode: str, home: Path, version: int | None = None) -> dict[str, object]:
        env = {**environment, "ANCHOR_HOME": str(home), "ANCHOR_MEMORY_DB": str(home / ".agent_memory.db"),
               "ANCHOR_PROJECT_ROOT": str(target), "TARGET": str(target), "MODE": mode,
               "PYTHON": str(python)}
        env["PROJECT_A"], env["PROJECT_B"] = str(tmp_path / "project-a"), str(tmp_path / "project-b")
        if version is not None:
            env["VERSION"] = str(version)
        return json.loads(_run([str(python), str(probe)], cwd=cwd, environment=env, timeout=180).stdout)

    direct_home = tmp_path / "home-direct"
    project_a, project_b = tmp_path / "project-a", tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    (project_a / "sentinel.txt").write_text("project A bytes\n", encoding="utf-8")
    (project_b / "sentinel.txt").write_text("project B bytes\n", encoding="utf-8")
    target_bytes = {"a": _tree_hashes(project_a), "b": _tree_hashes(project_b)}
    routing_home = tmp_path / "home-routing"
    routing = run_probe("routing", routing_home)
    assert routing["used_a"]["active_project"] == "a"
    assert routing["used_b"]["active_project"] == routing["status"]["active_project"] == "b"
    assert routing["used_a"]["target_root"] == str(project_a.resolve())
    assert routing["used_b"]["target_root"] == str(project_b.resolve())
    routing_item_id = routing["item"]["item_id"]
    assert routing["shown"]["item"]["item_id"] == routing_item_id
    assert {item["item_id"] for item in routing["project_a"]["items"]} == {routing_item_id}
    assert {item["item_id"] for item in routing["project_b"]["items"]} == {routing_item_id}
    routing_content_hash = routing["exported"]["content_sha256"]
    managed_before = _tree_hashes(routing_home / "workspace")
    descriptor = routing_home / "workspace" / "projects" / "a" / "PROJECT.md"
    descriptor.write_text(descriptor.read_text(encoding="utf-8").replace("status: active", "status: archived", 1),
                          encoding="utf-8")
    managed_after = _tree_hashes(routing_home / "workspace")
    changed = {path for path in managed_before | managed_after if managed_before.get(path) != managed_after.get(path)}
    assert changed == {"projects/a/PROJECT.md"}
    assert "status: archived" in descriptor.read_text(encoding="utf-8")
    assert _tree_hashes(project_a) == target_bytes["a"] and _tree_hashes(project_b) == target_bytes["b"]
    archived_environment = {
        **environment,
        "ANCHOR_HOME": str(routing_home),
        "ANCHOR_MEMORY_DB": str(routing_home / ".agent_memory.db"),
        "ANCHOR_PROJECT_ROOT": str(target),
        "TARGET": str(target),
        "MODE": "routing-read",
        "ITEM_ID": routing_item_id,
        "PYTHON": str(python),
    }
    archived = json.loads(
        _run([str(python), str(probe)], cwd=cwd, environment=archived_environment).stdout
    )
    assert archived["shown"]["item"]["item_id"] == routing_item_id
    assert {item["item_id"] for item in archived["project_a"]["items"]} == {routing_item_id}
    assert {item["item_id"] for item in archived["project_b"]["items"]} == {routing_item_id}
    assert archived["exported"]["content_sha256"] == routing_content_hash
    promotion = run_probe("promotion", tmp_path / "home-promotion")
    assert promotion["first"]["decision_id"] == promotion["second"]["decision_id"]
    assert promotion["first"]["decision"]["reason"] == "no_verifier_bound_attestations"
    assert promotion["first"]["decision"]["authority_mutation"] == "none"
    assert promotion["inspected"]["counts"] == {
        "attestations": 0, "human_receipts": 0, "decisions": 1,
        "outcomes": {"rejected": 1}, "events": 0, "event_types": {},
        "authority_lanes": {}, "effective_active": 0, "effective_confirmed": 0,
    }
    assert promotion["authority"] == ["candidate", 0.5, 0]
    verifier = run_probe("promotion-verifier", tmp_path / "home-promotion-verifier")
    assert verifier["verified"]["run"]["result_state"] == "passed"
    assert verifier["verified"]["run"]["selectors"] == ["tests/test_claim.py::test_claim"]
    assert verifier["attested"]["status"] == "recorded"
    assert verifier["attested"]["activation"]["status"] == "activated"
    assert verifier["runs"]["counts"] == {"runs": 1, "states": {"passed": 1}}
    assert verifier["promotion"]["counts"]["attestations"] == 1
    assert verifier["promotion"]["counts"]["event_types"] == {"activation": 1}
    assert verifier["authority"] == ["active", 0.35, 0]
    direct = run_probe("direct", direct_home)
    assert direct["a1"]["assessment"]["outcome"] == "nothing_reusable_learned"
    assert direct["a2"]["assessment"]["outcome"] == "observations_recorded"
    assert direct["first"]["obligation_id"] != direct["second"]["obligation_id"]
    direct_obligations = {
        row["obligation_id"]: row for row in direct["exported"]["obligations"]
    }
    first_obligation = direct_obligations[direct["first"]["obligation_id"]]
    second_obligation = direct_obligations[direct["second"]["obligation_id"]]
    assert first_obligation["status"] == second_obligation["status"] == "assessed"
    assert first_obligation["task_window_id"] == second_obligation["task_window_id"]
    assert len(direct["exported"]["items"]) == 1
    assert direct["shown"]["item"]["project_refs"] == ["project:A", "project:B"]
    assert direct["listed"]["items"] and direct["exported"]["complete"] is True
    assert Path(direct["backup"]["backup_path"]).is_relative_to(direct_home)
    assert len(direct["blocked"]) == 3
    assert all("learning recovery" in text and "closure" in text for text in direct["blocked"])
    restarted = run_probe("callable", direct_home, 2)
    assert any(entry["item_id"] == direct["item"]["item_id"] for entry in restarted["initial"]["items"])
    legacy_home = tmp_path / "home-legacy-recovery"
    legacy_active = run_probe("legacy-setup", legacy_home)
    assert legacy_active["active"]["status"] == "active"
    legacy_closed = run_probe("legacy-close", legacy_home)
    assert legacy_closed["exported"]["obligations"][-1]["status"] == "legacy_closed"
    assert legacy_closed["exported"]["items"] == []

    # Stateful CLI batch and shell preserve one JSON result per request and exit zero.
    for transport in ("batch", "shell"):
        home = tmp_path / f"home-cli-{transport}"
        cli_task = {
            "arg0":"CLI learning lifecycle.", "goal":"Qualify CLI learning.", "mode":"implementation",
            "execution_mode":"artifact_only", "current_state":"fresh",
            "desired_outcome":"assessed", "constraints":["read only"], "known_facts":["wheel"],
            "evidence":[{"source":"test","observation":"wheel"}], "in_scope":["learning"], "out_of_scope":["source"],
            "risks":["drift"], "acceptance_criteria":["gate"], "stop_conditions":["failure"], "deliverables":["result"]}
        def cli(
            requests: list[dict[str, object]],
            transport: str = transport,
            home: Path = home,
        ) -> list[dict[str, object]]:
            text = json.dumps(requests) if transport == "batch" else "\n".join(map(json.dumps, requests)) + "\n"
            result = _run([str(anchor_executable), "--root", str(target), transport, "-"] if transport == "batch" else
                          [str(anchor_executable), "--root", str(target), transport], cwd=cwd,
                          environment={**environment,"ANCHOR_HOME":str(home),"ANCHOR_MEMORY_DB":str(home/".agent_memory.db"),
                                       "ANCHOR_PROJECT_ROOT":str(target)},
                          input_text=text)
            rows = _json_lines(result.stdout)
            assert len(rows) == len(requests)
            assert all(set(row) == {"ok", "result", "metadata"} and row["ok"] is True for row in rows), rows
            assert all(row["metadata"] == {"process_state_shared": True, "schema_version": 1} for row in rows)
            return rows
        setup = [{"action":"learning","arg0":"list"}]
        setup += [{"action": action} for action in ("status", "memory", "audit_history", "orient")]
        setup += [{"action":"new_session","name":f"cli_{transport}","inline":True}, {"action":"task", **cli_task}]
        setup += [{"action":"gate"}, {"action":"learning","arg0":"capture",
                   "observation_type":"friction",
                   "summary":"Installed CLI preserves structured learning.",
                   "signal_key":f"installed.cli.{transport}", "impact":"medium",
                   "applicability_scope":"cross_project", "project_refs":["project:A","project:B"],
                   "work_package_refs":[], "environment_refs":["wheel"],
                   "provenance":{"source_action":"gate","source_version":"revision-8"},
                   "evidence":[{"reference_type":"test","reference":"tests/test_installed_distribution.py"}]}]
        first = cli(setup)
        assert first[0]["result"]["items"] == []
        item = first[-1]["result"]["item"]
        assessed = cli([
            {"action":"learning","arg0":"assess", "outcome":"observations_recorded",
             "observation_ids":[item["item_id"]]},
        ])
        assert assessed[0]["result"]["assessment"]["outcome"] == "observations_recorded"
        review_setup = [{"action": action} for action in ("status", "memory", "audit_history", "orient")]
        review_setup += [
            {"action":"new_session","name":f"cli_{transport}_review","inline":True},
            {"action":"task", **cli_task},
        ]
        reviewed = cli([
            *review_setup,
            {"action":"learning","arg0":"triage", "decision":"derive_watch",
             "source_item_ids":[item["item_id"]],
             "expected_source_versions":{item["item_id"]:item["version"]},
             "summary":"Human-attested CLI watch.", "impact":"high",
             "applicability_scope":"cross_project", "project_refs":["project:A","project:B"],
             "work_package_refs":[], "environment_refs":["wheel"],
             "provenance":{"source_action":"review","source_version":"revision-8"},
             "evidence":[{"reference_type":"test","reference":"tests/test_installed_distribution.py"}],
             "actor_kind":"human", "actor_ref":"reviewer:installed",
             "decision_source":f"cli:{transport}:revision8", "rationale":"Explicit installed CLI review."},
            {"action":"learning","arg0":"list", "kind":"watch_item", "status":"active",
             "applicability_project":"project:B"},
            {"action":"learning","arg0":"show", "item_id":item["item_id"], "include_history":True},
            {"action":"learning","arg0":"insights", "applicability_project":"project:A"},
        ])
        review_results = reviewed[len(review_setup):]
        derived = review_results[0]["result"]["item"]
        assert review_results[1]["result"]["items"][0]["item_id"] == derived["item_id"]
        assert review_results[2]["result"]["item"]["item_id"] == item["item_id"]
        assert review_results[3]["result"]["insights"]

        second_cycle = [{"action": action} for action in ("status", "memory", "audit_history", "orient")]
        second_cycle += [
            {"action":"new_session","name":f"cli_{transport}_second","inline":True},
            {"action":"task", **cli_task},
        ]
        second_cycle += [
            {"action":"memory", "arg0":"disposition", "all_pending":True,
             "disposition":"irrelevant",
             "reason":{"basis":"The prior CLI watch is unrelated to this bounded closure cycle."}},
            {"action":"gate"},
            {"action":"learning","arg0":"assess", "outcome":"nothing_reusable_learned",
             "notes":"No reusable learning on the second bounded CLI cycle."},
        ]
        closed = cli(second_cycle)
        assert closed[-1]["result"]["assessment"]["outcome"] == "nothing_reusable_learned"

    for mode in ("callable", "stdio"):
        for version in (1, 2):
            result = run_probe(mode, tmp_path / f"home-{mode}-v{version}", version)
            completed = result["completed"] if mode == "callable" else result
            assert completed["assessment"]["assessment"]["outcome"] == "observations_recorded"
            assert completed["no_assessment"]["assessment"]["outcome"] == "nothing_reusable_learned"
            assert completed["listed"]["items"][0]["item_id"] == completed["derived"]["item"]["item_id"]
            assert completed["shown"]["item"]["events"][0]["actor_kind"] == "human"
            assert completed["insights"]["insights"]
            # Installed-path ingress stays candidate-only: the derive projection
            # route ran above, so no route may have raised lifecycle authority.
            assert completed["derived"]["semantic_projection"]["memory_id"]
            by_status = completed["stats"]["by_status"]
            assert set(by_status) == {"candidate"}, by_status
            if mode == "callable":
                assert (result["error"]["type"] == "ValueError") if version == 1 else (result["error"]["ok"] is False)

    site_packages = Path(json.loads(_run([str(python), "-c", "import json,site;print(json.dumps(site.getsitepackages()))"], environment=environment).stdout)[0])
    forbidden = (".agent_memory.db", "learning-export.json", "learning-backup.db", ".agent_memory.db.lock")
    assert (direct_home / ".agent_memory.db").is_file()
    for root in (target, cwd, site_packages):
        assert not any(path.name in forbidden or path.suffix == ".lock" for path in root.rglob("*"))


def test_clean_wheel_runtime_contract(tmp_path: Path) -> None:
    """Qualify wheel resources, roots, transports, restart, and target safety."""
    audit_root = tmp_path / "installed-runtime-audit"
    wheelhouse = audit_root / "wheelhouse"
    venv = audit_root / "venv"
    writable_home = audit_root / "anchor-home"
    xdg_state = audit_root / "xdg-state"
    wheelhouse.mkdir(parents=True)
    writable_home.mkdir()
    xdg_state.mkdir()

    fresh = audit_root / "fresh-empty"
    _init_unborn_repo(fresh)
    unborn_before_birth = audit_root / "unborn-before-birth"
    _init_unborn_repo(unborn_before_birth)
    unborn_after_birth = audit_root / "unborn-after-birth"
    _init_unborn_repo(unborn_after_birth)
    cli_unborn = audit_root / "cli-unborn"
    _init_unborn_repo(cli_unborn)
    mcp_unborn = audit_root / "mcp-unborn"
    _init_unborn_repo(mcp_unborn)
    dirty_unborn = audit_root / "dirty-unborn"
    _init_unborn_repo(dirty_unborn)
    (dirty_unborn / "untracked.py").write_text("PRETASK = True\n", encoding="utf-8")
    mismatch_unborn = audit_root / "mismatch-unborn"
    _init_unborn_repo(mismatch_unborn, branch="trunk")
    committed = audit_root / "committed-non-python"
    _init_committed_repo(committed)
    dirty = audit_root / "dirty-unrelated"
    _run(["git", "clone", "-q", str(committed), str(dirty)])
    (dirty / "app.js").write_text("export const answer = 43;\n", encoding="utf-8")
    (dirty / "notes.txt").write_text("untracked and unrelated\n", encoding="utf-8")

    environment = _clean_environment(audit_root)
    try:
        _run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheelhouse), "."],
            cwd=REPOSITORY_ROOT,
            environment=environment,
        )
        wheels = list(wheelhouse.glob("odibi_anchor-*.whl"))
        assert len(wheels) == 1
        wheel = wheels[0]

        source_assistant = REPOSITORY_ROOT / ".assistant"
        expected_assistant = {
            ".assistant/" + path.relative_to(source_assistant).as_posix()
            for path in source_assistant.rglob("*") if path.is_file()
        }
        assert ".assistant/agent_bootstrap.py" in expected_assistant
        assert {
            path.parent.name
            for path in (source_assistant / "skills").glob("*/SKILL.md")
        } == EXPECTED_NATIVE_SKILLS
        _validate_native_skill_layout(source_assistant)
        source_instruction_bytes = (REPOSITORY_ROOT / ".assistant_instructions.md").read_bytes()
        source_instruction_bytes.decode("utf-8")
        source_agent_bootstrap_bytes = (REPOSITORY_ROOT / "agent_bootstrap.py").read_bytes()
        from odibi_anchor._dispatcher._guidance import guidance_distribution_manifest
        source_manifest = guidance_distribution_manifest(REPOSITORY_ROOT)
        expected_tools = {
            "tools/" + path.relative_to(REPOSITORY_ROOT / "tools").as_posix()
            for path in (REPOSITORY_ROOT / "tools").rglob("*")
            if path.is_file()
            and (
                path.name == "tool.json"
                or (path.suffix == ".py" and not path.name.startswith("test_") and "__pycache__" not in path.parts)
            )
        }
        assert len(expected_tools) == 58
        assert {name.split("/", 2)[1] for name in expected_tools} == EXPECTED_TOOL_DIRECTORIES
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            for name in expected_assistant | expected_tools:
                assert archive.read(name) == (REPOSITORY_ROOT / name).read_bytes()
            assert archive.read(".assistant_instructions.md") == source_instruction_bytes
            assert archive.read("agent_bootstrap.py") == source_agent_bootstrap_bytes
            wheel_native_root = audit_root / "wheel-native-layout" / ".assistant"
            for name in sorted(expected_assistant):
                destination = wheel_native_root.parent / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(name))
            _validate_native_skill_layout(wheel_native_root)
            for copy_name in ("wholesale-copy-a", "wholesale-copy-b"):
                copy_root = audit_root / copy_name
                for name in sorted(expected_assistant | {".assistant_instructions.md"}):
                    destination = copy_root / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(archive.read(name))
                assert {
                    path.relative_to(copy_root).as_posix(): path.read_bytes()
                    for path in copy_root.rglob("*") if path.is_file()
                } == {
                    path: (REPOSITORY_ROOT / path).read_bytes()
                    for path in expected_assistant | {".assistant_instructions.md"}
                }
                _validate_native_skill_layout(copy_root / ".assistant")
            # Claude qualification is a separately approved native install, but
            # its source contract is deterministic: every complete canonical
            # skill directory, byte-for-byte, with no curated subset.
            claude_root = audit_root / "claude-native-copy" / ".claude" / "skills"
            expected_claude = {
                name.removeprefix(".assistant/skills/"): archive.read(name)
                for name in expected_assistant
                if name.startswith(".assistant/skills/")
            }
            for relative, content in expected_claude.items():
                destination = claude_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            assert {
                path.relative_to(claude_root).as_posix(): path.read_bytes()
                for path in claude_root.rglob("*") if path.is_file()
            } == expected_claude
            assert {
                path.parent.name for path in claude_root.glob("*/SKILL.md")
            } == EXPECTED_NATIVE_SKILLS
        packaged_assistant = {name for name in names if name.startswith(".assistant/")}
        packaged_tools = {name for name in names if name.startswith("tools/")}
        assert packaged_assistant == expected_assistant
        assert packaged_tools == expected_tools
        assert ".assistant/skills/aliases.json" not in names
        assert "odibi_anchor/_runtime_paths.py" in names
        assert ".assistant/references/development/assurance-kernel.md" in names
        assert "odibi_anchor/assurance/__init__.py" in names
        prohibited_fragments = (".agent_memory.db", "/.git/", "/workspace/", "__pycache__", ".crc")
        assert not any(fragment in name for name in names for fragment in prohibited_fragments)
        assert not any("/docs/" in name or "/test_" in name for name in packaged_tools)

        _run([sys.executable, "-m", "venv", str(venv)], environment=environment)
        venv_python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        venv_cw = venv / ("Scripts/anchor.exe" if os.name == "nt" else "bin/anchor")
        _run(
            [str(venv_python), "-m", "pip", "install", f"{wheel}[mcp,pandas]", "pytest"],
            environment=environment,
            timeout=300,
        )

        resource_target = audit_root / "resource-target"
        resource_target.mkdir()
        poisoned_instructions = b"target-owned instructions must never be synced\n"
        (resource_target / ".assistant_instructions.md").write_bytes(poisoned_instructions)
        resource_target_before = _tree_hashes(resource_target)
        resource_probe_script = _write_script(
            audit_root,
            "resource_probe.py",
            """
            import contextlib
            import hashlib
            import io
            import json
            import os
            import sys
            from pathlib import Path

            from odibi_anchor.bootstrap import init

            with contextlib.redirect_stdout(io.StringIO()):
                anchor, _, _ = init(root=os.environ["TARGET"], output_format="dict")
            from odibi_anchor._dispatcher._boot import _RUNTIME_PATHS
            from odibi_anchor._dispatcher._guidance import (
                guidance_distribution_manifest,
                resolve_and_load_guidance,
            )
            import odibi_anchor._dispatcher._pre_dispatch as pre_dispatch

            # Phase 1 intentionally blocks every external mutation. Bypass only that
            # unchanged authorization gate to qualify the nested sync implementation.
            pre_dispatch.enforce_effects = lambda _action, _effects, _profile: None
            first_sync = anchor("sync", output_format="dict")
            second_sync = anchor("sync", output_format="dict")
            instructions = _RUNTIME_PATHS.instructions_file.read_bytes()
            manifest = guidance_distribution_manifest(_RUNTIME_PATHS.resource_root)
            direct_target, direct_content, direct_paths = resolve_and_load_guidance(
                _RUNTIME_PATHS.skills_dir, "writing-specs"
            )
            try:
                resolve_and_load_guidance(_RUNTIME_PATHS.skills_dir, "planning")
            except ValueError as exc:
                unknown = str(exc)
            print(json.dumps({
                "module": __import__("odibi_anchor").__file__,
                "instructions_path": str(_RUNTIME_PATHS.instructions_file),
                "instructions_sha256": hashlib.sha256(instructions).hexdigest(),
                "instructions_byte_count": len(instructions),
                "destination_bytes": Path(first_sync["dest"]).read_bytes().decode("utf-8"),
                "first_sync": first_sync,
                "second_sync": second_sync,
                "direct": {
                    "target": direct_target.skill,
                    "paths": [path.relative_to(_RUNTIME_PATHS.skills_dir).as_posix() for path in direct_paths],
                    "has_content": bool(direct_content),
                    "unknown": unknown,
                },
                "manifest": {
                    "file_count": manifest["file_count"],
                    "directory_count": manifest["directory_count"],
                    "byte_count": manifest["byte_count"],
                    "sha256": manifest["sha256"],
                    "files": [
                        [entry.relative_path, entry.byte_count, entry.sha256]
                        for entry in manifest["files"]
                    ],
                },
                "sys_path": sys.path,
            }))
            """,
        )
        resource_probe = json.loads(_run(
            [str(venv_python), str(resource_probe_script)],
            cwd=audit_root,
            environment={**environment, "ANCHOR_HOME": str(writable_home), "TARGET": str(resource_target)},
        ).stdout)
        source_manifest_files = [
            [entry.relative_path, entry.byte_count, entry.sha256]
            for entry in source_manifest["files"]
        ]
        assert Path(resource_probe["module"]).is_relative_to(venv.resolve())
        assert str(REPOSITORY_ROOT) not in resource_probe["sys_path"]
        assert Path(resource_probe["instructions_path"]).is_relative_to(venv.resolve())
        assert resource_probe["instructions_sha256"] == hashlib.sha256(source_instruction_bytes).hexdigest()
        assert resource_probe["instructions_byte_count"] == len(source_instruction_bytes)
        assert resource_probe["destination_bytes"].encode("utf-8") == source_instruction_bytes
        assert resource_probe["first_sync"]["changed"] is True
        assert resource_probe["second_sync"]["changed"] is False
        assert resource_probe["first_sync"]["source"] == resource_probe["instructions_path"]
        assert resource_probe["first_sync"]["byte_count"] == len(source_instruction_bytes)
        assert resource_probe["first_sync"]["sha256"] == resource_probe["instructions_sha256"]
        assert resource_probe["direct"]["target"] == "writing-specs"
        assert resource_probe["direct"]["paths"] == ["writing-specs/SKILL.md"]
        assert resource_probe["direct"]["has_content"] is True
        assert resource_probe["direct"]["unknown"].startswith("Unknown skill 'planning'")
        assert resource_probe["manifest"] == {
            "file_count": source_manifest["file_count"],
            "directory_count": source_manifest["directory_count"],
            "byte_count": source_manifest["byte_count"],
            "sha256": source_manifest["sha256"],
            "files": source_manifest_files,
        }
        assert _tree_hashes(resource_target) == resource_target_before
        assert (resource_target / ".assistant_instructions.md").read_bytes() == poisoned_instructions

        fallback_script = _write_script(
            audit_root,
            "fallback_probe.py",
            """
            import contextlib
            import hashlib
            import io
            import json
            import os
            import sys
            from pathlib import Path

            from odibi_anchor.bootstrap import init

            with contextlib.redirect_stdout(io.StringIO()):
                anchor, root, _ = init(root=os.environ["TARGET"], output_format="dict")
            from odibi_anchor._dispatcher._boot import _ENV, _RUNTIME_PATHS

            memory = Path(_ENV["memory_db"])
            memory_exists_after_boot = memory.exists()
            initial_memory_hash = (
                hashlib.sha256(memory.read_bytes()).hexdigest() if memory.exists() else None
            )
            anchor("orient", output_format="dict")
            anchor("new_session", name="fallback_orientation", inline=True, output_format="dict")
            anchor(
                "task",
                "Inspect installed fallback roots without changing source.",
                goal="Prove repeated orientation remains memory and target read only.",
                mode="analysis",
                current_state="Installed wheel in an empty unrelated repository.",
                desired_outcome="Stable memory and target hashes after orientation.",
                constraints=["Read only."],
                known_facts=["Writable state is outside the target."],
                evidence=[{"source": "runtime paths", "observation": "XDG state selected"}],
                in_scope=["orientation stability"],
                out_of_scope=["source task framing"],
                risks=["implicit durable writes"],
                acceptance_criteria=["Memory hash remains stable."],
                stop_conditions=["Target mutation."],
                deliverables=["fallback result"],
                output_format="dict",
            )
            anchor("skill_loaded", "code-comprehension", output_format="dict")
            anchor("orient", output_format="dict")
            final_memory_hash = hashlib.sha256(memory.read_bytes()).hexdigest()
            print(json.dumps({
                "module": __import__("odibi_anchor").__file__,
                "root": root,
                "anchor_home": str(_RUNTIME_PATHS.anchor_home),
                "resource_root": str(_RUNTIME_PATHS.resource_root),
                "skills_dir": str(_RUNTIME_PATHS.skills_dir),
                "tools_dir": str(_RUNTIME_PATHS.tools_dir),
                "source_checkout": _RUNTIME_PATHS.source_checkout,
                "memory": str(memory),
                "memory_exists_after_boot": memory_exists_after_boot,
                "memory_stable": initial_memory_hash == final_memory_hash,
                "sys_path": sys.path,
            }))
            """,
        )
        fallback_environment = {**environment, "XDG_STATE_HOME": str(xdg_state), "TARGET": str(fresh)}
        fresh_before = _git(fresh, "status", "--porcelain=v2", "--branch")
        fallback = json.loads(
            _run(
                [str(venv_python), str(fallback_script)],
                cwd=fresh,
                environment=fallback_environment,
            ).stdout
        )
        site_packages = Path(fallback["resource_root"])
        fallback_root = audit_root / ("local-app-data" if os.name == "nt" else "xdg-state")
        expected_fallback_home = (fallback_root / "odibi-anchor").resolve()
        assert Path(fallback["module"]).is_relative_to(venv.resolve())
        assert fallback["source_checkout"] is False
        assert Path(fallback["anchor_home"]) == expected_fallback_home
        assert Path(fallback["memory"]) == expected_fallback_home / ".agent_memory.db"
        assert Path(fallback["root"]) == fresh.resolve()
        assert fallback["memory_exists_after_boot"] is False
        # Accepted-task retrieval and forensic task journaling are durable memory facts.
        assert fallback["memory_stable"] is False
        assert all(str(REPOSITORY_ROOT) not in entry for entry in fallback["sys_path"])
        assert _tree_hashes(fresh) == {}
        assert _git(fresh, "status", "--porcelain=v2", "--branch") == fresh_before
        assert not (fresh / ".agent_memory.db").exists()
        assert not list(site_packages.rglob(".agent_memory.db"))

        portable_target = audit_root / "portable-target"
        portable_cwd = audit_root / "portable-cwd"
        portable_home = audit_root / "portable-home"
        portable_memory = audit_root / "portable-memory" / "agent.db"
        portable_target.mkdir()
        portable_cwd.mkdir()
        portable_probe_script = _write_script(
            audit_root,
            "portable_bootstrap_probe.py",
            """
            import contextlib
            import io
            import json
            import os
            import sys
            from pathlib import Path

            from odibi_anchor._dispatcher._boot import (
                resolve_installed_project_bootstrap,
            )
            from odibi_anchor.bootstrap import init

            resolved = resolve_installed_project_bootstrap(
                os.environ["ANCHOR_PROJECT_ROOT"], os.environ,
            )
            resolved.runtime_paths.anchor_home.mkdir(parents=True, exist_ok=True)
            resolved.memory_db.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.redirect_stdout(io.StringIO()):
                anchor, root, _ = init(root=str(resolved.canonical_target), output_format="dict")
            runtime = anchor("status", output_format="dict")["runtime"]
            print(json.dumps({
                "module": __import__("odibi_anchor").__file__,
                "requested_target": resolved.requested_target,
                "canonical_target": str(resolved.canonical_target),
                "target_source": resolved.target_source,
                "runtime_kind": resolved.runtime_kind,
                "source_checkout": resolved.runtime_paths.source_checkout,
                "resource_root": str(resolved.runtime_paths.resource_root),
                "anchor_home": str(resolved.runtime_paths.anchor_home),
                "memory_db": str(resolved.memory_db),
                "effective_root": root,
                "active_project": runtime["active_project"],
                "artifact_root": runtime["artifact_root"],
                "target_root": runtime["target_root"],
                "cwd": str(Path.cwd()),
                "sys_path": sys.path,
            }))
            """,
        )
        portable_environment = {
            **environment,
            "ANCHOR_PROJECT_ROOT": str(portable_target),
            "ANCHOR_HOME": str(portable_home),
            "ANCHOR_MEMORY_DB": str(portable_memory),
        }
        portable = json.loads(
            _run(
                [str(venv_python), str(portable_probe_script)],
                cwd=portable_cwd,
                environment=portable_environment,
            ).stdout
        )
        assert Path(portable["module"]).is_relative_to(venv.resolve())
        assert portable["source_checkout"] is False
        assert Path(portable["resource_root"]) == site_packages
        assert Path(portable["requested_target"]) == portable_target
        assert Path(portable["canonical_target"]) == portable_target.resolve()
        assert portable["target_source"] == "ANCHOR_PROJECT_ROOT"
        assert portable["runtime_kind"] == "installed_distribution"
        assert Path(portable["anchor_home"]) == portable_home.resolve()
        assert Path(portable["memory_db"]) == portable_memory.resolve()
        assert Path(portable["effective_root"]) == portable_target.resolve()
        assert portable["active_project"] is None
        assert Path(portable["artifact_root"]) == portable_home.resolve()
        assert Path(portable["target_root"]) == portable_target.resolve()
        assert Path(portable["cwd"]) == portable_cwd.resolve()
        assert all(str(REPOSITORY_ROOT) not in entry for entry in portable["sys_path"])
        assert _tree_hashes(portable_target) == {}
        assert _tree_hashes(portable_cwd) == {}
        assert portable_memory.is_file()
        assert not list(site_packages.rglob(".agent_memory.db"))

        ambient_target = audit_root / "ambient-target"
        explicit_direct_target = audit_root / "explicit-direct-target"
        direct_cwd = audit_root / "direct-cwd"
        direct_xdg = audit_root / "direct-xdg"
        ambient_home = audit_root / "ambient-profile-home"
        ambient_skills = audit_root / "ambient-profile-skills"
        ambient_target.mkdir()
        explicit_direct_target.mkdir()
        direct_cwd.mkdir()
        (ambient_target / ".anchor_config.json").write_text(
            json.dumps(
                {
                    "environment": {
                        "profiles": {
                            "ci": {
                                "platform": "ci",
                                "anchor_root": str(ambient_home),
                                "skills_dir": str(ambient_skills),
                            }
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        direct_probe_script = _write_script(
            audit_root,
            "direct_root_ambient_probe.py",
            """
            import contextlib
            import hashlib
            import io
            import json
            import os

            from odibi_anchor.assurance import CATALOG_VERSION, catalog_digest
            from odibi_anchor.bootstrap import init
            from odibi_anchor.codebase._adopted_dirty import initialize_schema as initialize_adoption
            from odibi_anchor.codebase._task_authority import initialize_schema

            with contextlib.redirect_stdout(io.StringIO()):
                anchor, root, _ = init(root=os.environ["DIRECT_TARGET"], output_format="dict")
            from odibi_anchor._dispatcher._boot import _ENV
            runtime = anchor("status", output_format="dict")["runtime"]
            references = anchor("references", output_format="dict")
            assurance_reference = anchor(
                "references", "load", "odibi-anchor.assurance-kernel",
                output_format="dict",
            )["reference"]
            matches = anchor(
                "references", "search", "selection parameter interval",
                reference_id="visualization.vega-lite", limit=3, output_format="dict",
            )
            section = anchor(
                "references", "load-section", matches["sections"][0]["section_id"],
                output_format="dict",
            )["section"]
            task_authority = initialize_schema(os.path.join(os.environ["HOME"], "authority.db"))
            task_adoption = initialize_adoption(os.path.join(os.environ["HOME"], "authority.db"))
            print(json.dumps({
                "root": root,
                "artifact_root": runtime["artifact_root"],
                "target_root": runtime["target_root"],
                "skills_dir": _ENV["skills_dir"],
                "reference_count": len(references["references"]),
                "assurance_reference_path": assurance_reference["path"],
                "assurance_reference_sha256": hashlib.sha256(
                    assurance_reference["content"].encode("utf-8")
                ).hexdigest(),
                "assurance_reference_boundary": (
                    "shadow findings do not block or authorize work"
                    in assurance_reference["content"].lower()
                ),
                "assurance_catalog_version": CATALOG_VERSION,
                "assurance_catalog_digest": catalog_digest(),
                "reference_section_path": section["path"],
                "reference_section_digest": section["section_sha256"],
                "task_authority": task_authority,
                "task_adoption": task_adoption,
            }))
            """,
        )
        direct_probe = json.loads(
            _run(
                [str(venv_python), str(direct_probe_script)],
                cwd=direct_cwd,
                environment={
                    **environment,
                    "CI": "1",
                    "ANCHOR_PROJECT_ROOT": str(ambient_target),
                    "DIRECT_TARGET": str(explicit_direct_target),
                    "XDG_STATE_HOME": str(direct_xdg),
                },
            ).stdout
        )
        expected_direct_home = (
            (Path(environment["LOCALAPPDATA"]) if os.name == "nt" else direct_xdg)
            / "odibi-anchor"
        ).resolve()
        assert Path(direct_probe["root"]) == explicit_direct_target.resolve()
        assert Path(direct_probe["artifact_root"]) == expected_direct_home
        assert Path(direct_probe["target_root"]) == explicit_direct_target.resolve()
        assert Path(direct_probe["skills_dir"]) == site_packages / ".assistant" / "skills"
        assert direct_probe["reference_count"] == 16
        assert direct_probe["assurance_reference_path"] == "development/assurance-kernel.md"
        assert direct_probe["assurance_reference_boundary"] is True
        assert len(direct_probe["assurance_reference_sha256"]) == 64
        assert direct_probe["assurance_catalog_version"] == "anchor-assurance-core/1.1"
        assert len(direct_probe["assurance_catalog_digest"]) == 64
        assert "parameter/select.md" in direct_probe["reference_section_path"]
        assert len(direct_probe["reference_section_digest"]) == 64
        assert direct_probe["task_authority"]["domain"] == "task_authority"
        assert direct_probe["task_authority"]["version"] == 1
        assert len(direct_probe["task_authority"]["schema_sha256"]) == 64
        assert direct_probe["task_adoption"]["domain"] == "task_adoption"
        assert direct_probe["task_adoption"]["version"] == 1
        assert len(direct_probe["task_adoption"]["schema_sha256"]) == 64
        assert not ambient_home.exists()
        assert not ambient_skills.exists()

        profile_drift_target = audit_root / "profile-drift-target"
        profile_drift_cwd = audit_root / "profile-drift-cwd"
        profile_drift_xdg = audit_root / "profile-drift-xdg"
        profile_drift_home = profile_drift_xdg / "odibi-anchor"
        profile_drift_local_app_data = audit_root / "profile-drift-local-app-data"
        profile_drift_initial_home = (
            profile_drift_local_app_data / "odibi-anchor"
            if os.name == "nt" else profile_drift_home
        )
        profile_drift_framework = audit_root / "profile-drift-framework"
        profile_drift_project = audit_root / "profile-drift-project"
        profile_drift_sync = audit_root / "profile-drift-sync" / "instructions.md"
        profile_drift_target.mkdir()
        profile_drift_cwd.mkdir()
        (profile_drift_target / ".anchor_config.json").write_text(
            json.dumps(
                {
                    "environment": {
                        "profiles": {
                            "newly-eligible": {
                                "platform": "local",
                                "home": str(profile_drift_home),
                                "framework_root": str(profile_drift_framework),
                                "project_roots": [str(profile_drift_project)],
                                "sync_target": str(profile_drift_sync),
                                "enforcement": "relaxed",
                            }
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        profile_drift_script = _write_script(
            audit_root,
            "profile_drift_probe.py",
            """
            import json
            import os

            import odibi_anchor.mcp_server as mcp_server

            try:
                mcp_server._initialize_dispatcher(reset=True)
            except (FileNotFoundError, RuntimeError) as exc:
                print(json.dumps({
                    "error": str(exc),
                    "dispatcher_available": mcp_server._CW is not None,
                    "root_available": mcp_server._ROOT is not None,
                    "effective_home": os.environ.get("ANCHOR_HOME"),
                    "effective_memory": os.environ.get("ANCHOR_MEMORY_DB"),
                }))
            else:
                raise AssertionError("profile drift was accepted")
            """,
        )
        profile_drift_environment = {
            **environment,
            "ANCHOR_PROJECT_ROOT": str(profile_drift_target),
            "XDG_STATE_HOME": str(profile_drift_xdg),
        }
        if os.name == "nt":
            profile_drift_environment["LOCALAPPDATA"] = str(profile_drift_local_app_data)
        profile_drift = json.loads(
            _run(
                [str(venv_python), str(profile_drift_script)],
                cwd=profile_drift_cwd,
                environment=profile_drift_environment,
            ).stdout
        )
        assert (
            "No managed project target matches" in profile_drift["error"]
            or "effective runtime does not match validated paths" in profile_drift["error"]
        )
        assert profile_drift["dispatcher_available"] is False
        assert profile_drift["root_available"] is False
        assert profile_drift["effective_home"] is None
        assert profile_drift["effective_memory"] is None
        assert profile_drift_initial_home.is_dir()
        assert not profile_drift_framework.exists()
        assert not profile_drift_project.exists()
        assert not profile_drift_sync.parent.exists()

        unborn_lifecycle_script = _write_script(
            audit_root,
            "unborn_lifecycle_probe.py",
            """
            import contextlib
            import hashlib
            import io
            import json
            import os
            import shutil
            import subprocess
            from pathlib import Path

            from odibi_anchor.bootstrap import init


            root = Path(os.environ["TARGET"])


            def git(*args):
                return subprocess.run(
                    ["git", *args], cwd=root, check=True, capture_output=True, text=True,
                ).stdout.strip()


            def target_state():
                status = git("status", "--porcelain=v2", "--branch")
                staged = git("diff", "--cached", "--binary")
                unstaged = git("diff", "--binary")
                git_dir = root / ".git"
                index = git_dir / "index"
                refs = {
                    path.relative_to(git_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in sorted((git_dir / "refs").rglob("*"))
                    if path.is_file()
                }
                files = {
                    path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in sorted(root.rglob("*"))
                    if path.is_file() and ".git" not in path.relative_to(root).parts
                }
                return {
                    "head": (git_dir / "HEAD").read_text(encoding="utf-8"),
                    "index": hashlib.sha256(index.read_bytes()).hexdigest() if index.is_file() else None,
                    "refs": refs,
                    "files": files,
                    "status": status,
                    "staged": staged,
                    "unstaged": unstaged,
                }


            def frame(anchor, label):
                return anchor(
                    "task",
                    f"Qualify installed unborn source-task lifecycle: {label}.",
                    goal=f"Prove {label} preserves exact task scope and replacement semantics.",
                    mode="implementation",
                    work_type="change",
                    execution_mode="source_change",
                    risk="low",
                    rigor="direct",
                    current_state="Disposable installed-wheel Git fixture.",
                    desired_outcome="Task scope remains complete before and after target birth.",
                    constraints=["Modify only disposable fixture source after acceptance."],
                    known_facts=["The configured target is the attached main branch."],
                    evidence=[{"source": "git", "observation": "fixture state observed"}],
                    in_scope=["app.py", "tests/test_app.py", "replacement state"],
                    out_of_scope=["installed package mutation"],
                    risks=["source omission or stale task authorization"],
                    acceptance_criteria=["Review, preflight, test, and gate consume exact scope."],
                    stop_conditions=["Target framing mutation or hidden source."],
                    deliverables=["installed lifecycle evidence"],
                    output_format="dict",
                )

            with contextlib.redirect_stdout(io.StringIO()):
                anchor, _, _ = init(root=os.environ["TARGET"], output_format="dict")
            from odibi_anchor._repository_snapshot import (
                TaskRepositoryBaseline,
                UnbornTaskRepositoryBaseline,
                capture_task_change_scope,
            )
            from odibi_anchor._utils._session_state import _SESSION_STATE

            for action in ("status", "memory", "audit_history", "orient"):
                anchor(action, output_format="dict")
            anchor("new_session", name="unborn_lifecycle", inline=True, output_format="dict")

            pristine_before = target_state()
            first_task = frame(anchor, "initial unborn acceptance")
            first_baseline = _SESSION_STATE.task_repository_baseline
            assert isinstance(first_baseline, UnbornTaskRepositoryBaseline)
            second_task = frame(anchor, "clean unborn replacement")
            second_baseline = _SESSION_STATE.task_repository_baseline
            assert isinstance(second_baseline, UnbornTaskRepositoryBaseline)
            assert second_baseline is not first_baseline
            pristine_after = target_state()
            assert pristine_after == pristine_before

            for required in second_task["required_skills"]:
                if required["path"] != "n/a":
                    anchor("skill_loaded", required["skill"], output_format="dict")
            changed_files = ["app.py", "tests/test_app.py"]
            anchor("known_bad", changed_files=changed_files, output_format="dict")
            (root / "tests").mkdir()
            (root / "app.py").write_text("VALUE = 1" + chr(10), encoding="utf-8")
            (root / "tests" / "test_app.py").write_text(
                chr(10).join((
                    "from pathlib import Path",
                    "",
                    "",
                    "def test_value():",
                    '    assert Path("app.py").read_text(encoding="utf-8") == "VALUE = 1" + chr(10)',
                    "",
                )),
                encoding="utf-8",
            )
            anchor("touched", "app.py", created=True, output_format="dict")
            anchor("touched", "tests/test_app.py", created=True, output_format="dict")

            phase = os.environ["PHASE"]
            if phase == "after":
                git("add", "app.py", "tests/test_app.py")
                git("commit", "-m", "first")
            scope = capture_task_change_scope(second_baseline)
            review = anchor("review", output_format="dict")
            preflight = anchor("preflight", output_format="dict")
            test = anchor("test", target="tests/test_app.py", output_format="dict")
            assert test["metrics"]["passed"] == 1, test
            gate = anchor("gate", output_format="dict")
            assessment = anchor(
                "learning", "assess",
                outcome="nothing_reusable_learned",
                notes="The bounded installed qualification produced no reusable observation.",
                output_format="dict",
            )

            born_replacement_readiness = None
            failed_replacement_preserved = False
            if phase == "after":
                for cache in root.rglob("__pycache__"):
                    shutil.rmtree(cache)
                (root / ".anchor_session_health.json").unlink(missing_ok=True)
                status_before_replacement = git("status", "--porcelain")
                assert status_before_replacement == "", status_before_replacement
                born_task = frame(anchor, "clean born replacement")
                born_baseline = _SESSION_STATE.task_repository_baseline
                assert isinstance(born_baseline, TaskRepositoryBaseline)
                assert capture_task_change_scope(born_baseline).changed_paths == ()
                prior_goal = _SESSION_STATE.task_goal
                (root / "dirty.py").write_text("DIRTY = True" + chr(10), encoding="utf-8")
                try:
                    frame(anchor, "dirty failed replacement")
                except RuntimeError as exc:
                    replacement_error = str(exc)
                else:
                    raise AssertionError("dirty replacement unexpectedly succeeded")
                assert replacement_error == (
                    "BLOCKED: source-change task requires a clean initial Git worktree"
                )
                # Failed dispatch rollback may reconstruct the serializable
                # baseline; its value and authorization scope must be preserved,
                # not its incidental Python object identity.
                assert _SESSION_STATE.task_repository_baseline == born_baseline
                assert _SESSION_STATE.task_goal == prior_goal
                assert capture_task_change_scope(born_baseline).changed_paths == ("dirty.py",)
                born_replacement_readiness = born_task["readiness"]["score"]
                failed_replacement_preserved = True

            def consumer_metrics(review, preflight, test, gate):
                return {
                    "review_files": review["metrics"]["files_changed"],
                    "review_target_start": review["metrics"]["target_start_sha"],
                    "review_target_current": review["metrics"]["target_current_sha"],
                    "preflight_files": preflight["metrics"]["files_checked"],
                    "preflight_target_start": preflight["metrics"]["target_start_sha"],
                    "preflight_target_current": preflight["metrics"]["target_current_sha"],
                    "tests_passed": test["metrics"]["passed"],
                    "gate_files": gate["metrics"]["files_changed_count"],
                    "gate_target_start": gate["metrics"]["target_start_sha"],
                    "gate_target_current": gate["metrics"]["target_current_sha"],
                    "gate_task_verified": "task_execution_context" in gate["metrics"]["timing_verified"],
                    "assessment_outcome": assessment["assessment"]["outcome"],
                }

            print(json.dumps({
                "module": __import__("odibi_anchor").__file__,
                "first_readiness": first_task["readiness"]["score"],
                "second_readiness": second_task["readiness"]["score"],
                "framing_preserved": pristine_after == pristine_before,
                "phase": phase,
                "scope": {
                    "paths": scope.changed_paths,
                    "created": scope.provenance["created_paths"],
                    "target_birth": scope.provenance["target_birth"],
                    "target_current": scope.provenance["target_current_sha"],
                },
                "consumers": consumer_metrics(review, preflight, test, gate),
                "born_replacement_readiness": born_replacement_readiness,
                "failed_replacement_preserved": failed_replacement_preserved,
                "replacement_error": replacement_error if phase == "after" else None,
            }))
            """,
        )
        lifecycle_results = {}
        for phase, target in (
            ("before", unborn_before_birth),
            ("after", unborn_after_birth),
        ):
            lifecycle_results[phase] = json.loads(
                _run(
                    [str(venv_python), str(unborn_lifecycle_script)],
                    cwd=target,
                    environment={
                        **environment,
                        "ANCHOR_HOME": str(writable_home),
                        "TARGET": str(target),
                        "PHASE": phase,
                    },
                ).stdout
            )

        expected_lifecycle_paths = ["app.py", "tests/test_app.py"]
        for phase, result in lifecycle_results.items():
            assert Path(result["module"]).is_relative_to(venv.resolve())
            assert result["phase"] == phase
            assert result["first_readiness"] == 100
            assert result["second_readiness"] == 100
            assert result["framing_preserved"] is True
            assert result["scope"]["paths"] == expected_lifecycle_paths
            assert result["scope"]["created"] == expected_lifecycle_paths
            assert result["consumers"]["review_files"] == 2
            assert result["consumers"]["preflight_files"] == 2
            assert result["consumers"]["tests_passed"] == 1
            assert result["consumers"]["gate_files"] == 2
            assert result["consumers"]["gate_task_verified"] is True
            assert result["consumers"]["assessment_outcome"] == "nothing_reusable_learned"
            assert result["consumers"]["review_target_start"] is None
            assert result["consumers"]["preflight_target_start"] is None
            assert result["consumers"]["gate_target_start"] is None
        before_result = lifecycle_results["before"]
        assert before_result["scope"]["target_birth"] is False
        assert before_result["scope"]["target_current"] is None
        assert before_result["consumers"]["review_target_current"] is None
        assert before_result["consumers"]["preflight_target_current"] is None
        assert before_result["consumers"]["gate_target_current"] is None
        after_result = lifecycle_results["after"]
        assert after_result["scope"]["target_birth"] is True
        assert len(after_result["scope"]["target_current"]) == 40
        assert after_result["consumers"]["review_target_current"] == after_result["scope"]["target_current"]
        assert after_result["consumers"]["preflight_target_current"] == after_result["scope"]["target_current"]
        assert after_result["consumers"]["gate_target_current"] == after_result["scope"]["target_current"]
        assert after_result["born_replacement_readiness"] == 100
        assert after_result["failed_replacement_preserved"] is True
        assert after_result["replacement_error"] == (
            "BLOCKED: source-change task requires a clean initial Git worktree"
        )
        assert _git(unborn_after_birth, "branch", "--show-current").strip() == "main"
        assert _git(unborn_after_birth, "rev-parse", "HEAD").strip() == after_result["scope"]["target_current"]
        assert "? dirty.py" in _git(unborn_after_birth, "status", "--porcelain=v2")
        assert not (unborn_before_birth / ".agent_memory.db").exists()
        assert not (unborn_after_birth / ".agent_memory.db").exists()
        assert _tree_hashes(fresh) == {}

        direct_script = _write_script(
            audit_root,
            "direct_probe.py",
            """
            import contextlib
            import io
            import json
            import os
            import sys
            from pathlib import Path

            from odibi_anchor.bootstrap import init


            def initialize(*, root=None, project=None):
                with contextlib.redirect_stdout(io.StringIO()):
                    return init(root=root, project=project, output_format="dict")[0]


            def prepare(anchor, name, mode="implementation"):
                anchor("orient", output_format="dict")
                anchor("new_session", name=name, inline=True, output_format="dict")
                task = anchor(
                    "task",
                    "Qualify installed project and artifact isolation.",
                    goal="Prove installed state remains isolated across project routing.",
                    mode=mode,
                    current_state="Disposable installed-wheel fixture.",
                    desired_outcome="Only the selected managed artifact root contains records.",
                    constraints=["Do not modify target source."],
                    known_facts=["The committed target is clean."],
                    evidence=[{"source": "git", "observation": "clean committed fixture"}],
                    in_scope=["project routing", "guidance", "registered tools"],
                    out_of_scope=["source changes", "deferred policy changes"],
                    risks=["cross-project artifact leakage"],
                    acceptance_criteria=["Records remain isolated after switching."],
                    stop_conditions=["Target bytes change."],
                    deliverables=["installed qualification result"],
                    output_format="dict",
                )
                skills = (
                    ("writing-specs",)
                    if mode == "implementation"
                    else ("code-comprehension",)
                )
                for skill in skills:
                    anchor("skill_loaded", skill, output_format="dict")
                return task


            committed = os.environ["COMMITTED"]
            fresh = os.environ["FRESH"]
            anchor = initialize(root=committed)
            prepare(anchor, "create_alpha")
            alpha_created = anchor("project", "create", name="alpha", target=committed, output_format="dict")
            try:
                anchor("status", output_format="dict")
            except RuntimeError as exc:
                initial_stale_error = str(exc)
            else:
                raise AssertionError("retained dispatcher did not fail closed")

            anchor = initialize(project="alpha")
            prepare(anchor, "alpha_records")
            skills = anchor("skills", output_format="dict")
            planning = anchor("skill_loaded", "writing-specs", include_content=True, output_format="dict")
            tools = anchor("tools", output_format="dict")
            echo = anchor("echo", message="installed-ok", output_format="dict")
            import tools.table_profiler_tool.lib.profiler as table_profiler

            problem = anchor(
                "problem",
                "create",
                title="Alpha-only installed problem",
                definition="This record must remain under alpha artifacts.",
                decision_needed="Keep installed roots isolated.",
                output_format="dict",
            )
            anchor(
                "problem",
                "update",
                problem["problem_id"],
                evidence={
                    "source": "installed direct qualification",
                    "observation": "Alpha artifact creation succeeded without target writes.",
                    "interpretation": "Writable home and target are independent.",
                    "confidence": "high",
                },
                output_format="dict",
            )
            spec = anchor("spec", "create", name="ALPHA_ONLY", output_format="dict")
            notebook = anchor(
                "new_session",
                name="installed_notebook",
                inline=False,
                features=1,
                output_format="dict",
            )
            notebook_document = json.loads(Path(notebook["metrics"]["notebook_path"]).read_text(encoding="utf-8"))
            bootstrap_source = "".join(notebook_document["cells"][2]["source"])

            prepare(anchor, "create_beta")
            beta_created = anchor("project", "create", name="beta", target=fresh, output_format="dict")
            anchor("project", "create", name="dirty", target=os.environ["DIRTY"], output_format="dict")
            anchor("project", "create", name="unborn", target=os.environ["MCP_UNBORN"], output_format="dict")
            alpha_after_beta = anchor("status", output_format="dict")["runtime"]

            anchor = initialize(project="beta")
            prepare(anchor, "inspect_beta", mode="analysis")
            beta_problems = anchor("problem", output_format="dict")
            beta_specs = anchor("spec", output_format="dict")
            from odibi_anchor._dispatcher._boot import _ENV, _RUNTIME_PATHS

            print(json.dumps({
                "module": __import__("odibi_anchor").__file__,
                "anchor_home": str(_RUNTIME_PATHS.anchor_home),
                "resource_root": str(_RUNTIME_PATHS.resource_root),
                "memory": _ENV["memory_db"],
                "skills": skills["metrics"],
                "planning_chars": planning["char_count"],
                "planning_paths": planning["resolved_paths"],
                "tools": tools["metrics"],
                "echo": echo["summary"],
                "table_profiler": table_profiler.__file__,
                "alpha_project_root": alpha_created["project_root"],
                "beta_project_root": beta_created["project_root"],
                "problem_id": problem["problem_id"],
                "spec_path": spec["artifact_path"],
                "notebook_path": notebook["metrics"]["notebook_path"],
                "notebook_bootstrap": bootstrap_source,
                "beta_problem_count": beta_problems["count"],
                "beta_spec_count": beta_specs["total"],
                "initial_stale_error": initial_stale_error,
                "alpha_after_beta": alpha_after_beta,
                "sys_path": sys.path,
            }))
            """,
        )
        direct_environment = {
            **environment,
            "ANCHOR_HOME": str(writable_home),
            "COMMITTED": str(committed),
            "FRESH": str(fresh),
            "DIRTY": str(dirty),
            "MCP_UNBORN": str(mcp_unborn),
        }
        committed_before = _git_snapshot(committed)
        resource_hashes_before = {
            **_tree_hashes(site_packages / ".assistant"),
            **{f"tools/{name}": digest for name, digest in _tree_hashes(site_packages / "tools").items()},
        }
        direct = json.loads(
            _run(
                [str(venv_python), str(direct_script)],
                cwd=committed,
                environment=direct_environment,
            ).stdout
        )
        assert Path(direct["module"]).is_relative_to(venv.resolve())
        assert Path(direct["anchor_home"]) == writable_home.resolve()
        assert Path(direct["memory"]) == writable_home.resolve() / ".agent_memory.db"
        assert Path(direct["resource_root"]) == site_packages
        assert direct["skills"]["total_skills"] == 17
        assert direct["planning_chars"] > 1_000
        assert direct["planning_paths"] == ["writing-specs/SKILL.md"]
        assert direct["tools"]["registered"] == 11
        assert direct["echo"] == "Echo: installed-ok"
        assert Path(direct["table_profiler"]).is_relative_to(site_packages)
        assert direct["beta_problem_count"] == 0
        assert direct["beta_spec_count"] == 0
        assert "dispatcher is stale" in direct["initial_stale_error"]
        assert direct["alpha_after_beta"]["active_project"] == "alpha"
        assert direct["alpha_after_beta"]["routing_stale_reason"] is None
        assert "from odibi_anchor.bootstrap import init" in direct["notebook_bootstrap"]
        assert "project='alpha'" in direct["notebook_bootstrap"]
        assert "agent_init.py" not in direct["notebook_bootstrap"]
        assert Path(direct["spec_path"]).is_relative_to(Path(direct["alpha_project_root"]))
        assert Path(direct["notebook_path"]).is_relative_to(Path(direct["alpha_project_root"]))
        assert _git_snapshot(committed) == committed_before
        assert not (committed / ".agent_memory.db").exists()

        notebook_script = _write_script(
            audit_root,
            "notebook_probe.py",
            """
            import json
            import os
            from pathlib import Path

            notebook = json.loads(Path(os.environ["NOTEBOOK"]).read_text(encoding="utf-8"))
            namespace = {}
            exec("".join(notebook["cells"][2]["source"]), namespace)
            print(json.dumps({
                "root": namespace["ROOT"],
                "module": __import__("odibi_anchor").__file__,
            }))
            """,
        )
        notebook_result = json.loads(
            _run(
                [str(venv_python), str(notebook_script)],
                cwd=committed,
                environment={**direct_environment, "NOTEBOOK": direct["notebook_path"]},
            ).stdout.splitlines()[-1]
        )
        assert Path(notebook_result["root"]) == committed.resolve()
        assert Path(notebook_result["module"]).is_relative_to(venv.resolve())
        assert _git_snapshot(committed) == committed_before

        unborn_source_task = {
            "action": "task",
            "arg0": "Frame an installed source change from a pristine unborn target branch.",
            "goal": "Prove the stateful CLI delegates exact unborn acceptance to the shared core.",
            "mode": "implementation",
            "work_type": "change",
            "execution_mode": "source_change",
            "risk": "low",
            "rigor": "direct",
            "current_state": "Disposable installed-wheel Git fixture.",
            "desired_outcome": "Accept only the exact pristine attached target branch.",
            "constraints": ["Do not mutate target source, index, refs, or status."],
            "known_facts": ["The configured target is main."],
            "evidence": [{"source": "git", "observation": "unborn fixture observed"}],
            "in_scope": ["source-task framing"],
            "out_of_scope": ["source creation"],
            "risks": ["transport-specific fallback"],
            "acceptance_criteria": ["Shared-core task acceptance succeeds."],
            "stop_conditions": ["Any target mutation."],
            "deliverables": ["CLI acceptance evidence"],
        }
        cli_unborn_before = _unborn_git_snapshot(cli_unborn)
        cli_unborn_requests = [
            {"action": "status"},
            {"action": "memory"},
            {"action": "audit_history"},
            {"action": "orient"},
            {"action": "new_session", "name": "cli_unborn", "inline": True},
            unborn_source_task,
        ]
        cli_unborn_batch = _run(
            [str(venv_cw), "--root", str(cli_unborn), "batch", "-"],
            cwd=cli_unborn,
            environment=direct_environment,
            input_text=json.dumps(cli_unborn_requests),
        )
        cli_unborn_results = _json_lines(cli_unborn_batch.stdout)
        assert len(cli_unborn_results) == len(cli_unborn_requests)
        assert all(result["ok"] is True for result in cli_unborn_results)
        assert cli_unborn_results[-1]["result"]["readiness"]["score"] == 100
        assert cli_unborn_results[-1]["metadata"]["process_state_shared"] is True
        assert _unborn_git_snapshot(cli_unborn) == cli_unborn_before
        assert not (cli_unborn / ".agent_memory.db").exists()

        for invalid_target, expected_code, expected_error in (
            (
                dirty_unborn,
                3,
                "BLOCKED: source-change task requires a clean initial Git worktree",
            ),
            (
                mismatch_unborn,
                4,
                "unborn source-change task requires its current branch to equal the configured target",
            ),
        ):
            invalid_before = _unborn_git_snapshot(invalid_target)
            invalid_requests = [
                {"action": "status"},
                {"action": "memory"},
                {"action": "audit_history"},
                {"action": "orient"},
                {"action": "new_session", "name": "invalid_unborn", "inline": True},
                unborn_source_task,
            ]
            invalid_shell = _run(
                [str(venv_cw), "--root", str(invalid_target), "shell"],
                cwd=invalid_target,
                environment=direct_environment,
                input_text="\n".join(json.dumps(request) for request in invalid_requests) + "\n",
                expected_codes=(expected_code,),
            )
            invalid_results = _json_lines(invalid_shell.stdout)
            assert all(result["ok"] is True for result in invalid_results[:-1])
            assert invalid_results[-1]["ok"] is False
            assert invalid_results[-1]["error"]["message"] == expected_error
            assert _unborn_git_snapshot(invalid_target) == invalid_before
            assert not (invalid_target / ".agent_memory.db").exists()

        analysis_task = {
            "action": "task",
            "arg0": "Inspect installed CLI resources without source changes.",
            "goal": "Prove CLI resource behavior matches direct Python.",
            "mode": "analysis",
            "current_state": "Clean committed installed-wheel fixture.",
            "desired_outcome": "CLI reports the same guidance and registered tools.",
            "constraints": ["Read only."],
            "known_facts": ["Candidate wheel is installed."],
            "evidence": [{"source": "wheel inspection", "observation": "resources packaged"}],
            "in_scope": ["CLI guidance and tools"],
            "out_of_scope": ["source changes"],
            "risks": ["transport drift"],
            "acceptance_criteria": ["CLI counts match direct Python."],
            "stop_conditions": ["Target mutation."],
            "deliverables": ["CLI result"],
        }
        cli_requests = [
            {"action": "orient"},
            {"action": "new_session", "name": "cli_batch", "inline": True},
            analysis_task,
            {"action": "skill_loaded", "arg0": "code-comprehension"},
            {"action": "skills"},
            {"action": "tools"},
            {"action": "echo", "message": "cli-ok"},
        ]
        cli_batch = _run(
            [str(venv_cw), "--root", str(committed), "batch", "-"],
            cwd=committed,
            environment=direct_environment,
            input_text=json.dumps(cli_requests),
        )
        cli_results = _json_lines(cli_batch.stdout)
        assert all(result["ok"] is True for result in cli_results)
        skills_result = cli_results[-3]["result"]
        assert isinstance(skills_result, dict)
        skills_metrics = skills_result["metrics"]
        assert isinstance(skills_metrics, dict)
        assert skills_metrics["total_skills"] == 17
        assert cli_results[-2]["result"]["metrics"]["registered"] == 11
        assert cli_results[-1]["result"]["summary"] == "Echo: cli-ok"
        assert _git_snapshot(committed) == committed_before

        access_shell_requests = [
            *(
                {"action": action}
                for action in (
                    "status",
                    "memory",
                    "audit_history",
                    "orient",
                    "manifest",
                    "tools",
                )
                * 2
            ),
            *({"action": "review"} for _ in range(8)),
        ]
        access_shell = _run(
            [str(venv_cw), "--root", str(committed), "shell"],
            cwd=committed,
            environment=direct_environment,
            input_text="\n".join(json.dumps(request) for request in access_shell_requests) + "\n",
            expected_codes=(3,),
        )
        access_results = _json_lines(access_shell.stdout)
        assert all(result["ok"] is True for result in access_results[:19])
        assert all("Planning not detected" not in str(result) for result in access_results[:12])
        assert ["Planning not detected" in str(result) for result in access_results[12:19]] == [
            False,
            False,
            True,
            True,
            True,
            True,
            True,
        ]
        assert access_results[19]["ok"] is False
        assert "8 task-required" in access_results[19]["error"]["message"]
        assert _git_snapshot(committed) == committed_before

        dirty_before = _git_snapshot(dirty)
        dirty_shell_requests = [
            {"action": "orient"},
            {"action": "new_session", "name": "dirty_shell", "inline": True},
            {
                "action": "task",
                "arg0": "Frame a source change against unrelated dirty work.",
                "goal": "Confirm the dirty-worktree guard preserves every Git byte.",
                "mode": "implementation",
                "current_state": "Tracked and untracked unrelated changes exist.",
                "desired_outcome": "The task rejects without target mutation.",
                "constraints": ["Do not alter the dirty target."],
                "known_facts": ["The initial worktree is dirty."],
                "evidence": [{"source": "git status", "observation": "unrelated dirt present"}],
                "in_scope": ["guard behavior"],
                "out_of_scope": ["exit-code remediation"],
                "risks": ["loss of unrelated work"],
                "acceptance_criteria": ["Exact Git snapshot is preserved."],
                "stop_conditions": ["Any byte changes."],
                "deliverables": ["expected rejection"],
            },
        ]
        dirty_shell = _run(
            [str(venv_cw), "--root", str(dirty), "shell"],
            cwd=dirty,
            environment=direct_environment,
            input_text="\n".join(json.dumps(request) for request in dirty_shell_requests) + "\n",
            expected_codes=(3,),
        )
        dirty_results = _json_lines(dirty_shell.stdout)
        assert dirty_results[0]["ok"] is True
        assert dirty_results[1]["ok"] is True
        assert dirty_results[2]["ok"] is False
        assert dirty_results[2]["error"] == {
            "message": "BLOCKED: source-change task requires a clean initial Git worktree",
            "type": "RuntimeError",
        }
        assert _git_snapshot(dirty) == dirty_before
        assert not (dirty / ".agent_memory.db").exists()

        active_project = writable_home / "workspace" / ".active_project"
        active_project.write_text("alpha\n", encoding="utf-8")
        mcp_script = _write_script(
            audit_root,
            "mcp_probe.py",
            """
            import asyncio
            import json
            import os
            from pathlib import Path

            from fastmcp import Client
            from fastmcp.client.transports import StdioTransport


            async def main():
                server_environment = os.environ.copy()
                server_environment.pop("PYTHONPATH", None)
                transport = StdioTransport(
                    command=os.environ["CANDIDATE_PYTHON"],
                    args=["-m", "odibi_anchor.mcp_server"],
                    env=server_environment,
                    cwd=os.environ["SERVER_CWD"],
                    log_file=Path(os.environ["MCP_LOG"]),
                )

                async with Client(transport, timeout=60) as client:
                    tool_names = sorted(tool.name for tool in await client.list_tools())

                    async def call(
                        action,
                        arguments=None,
                        *,
                        response_version=1,
                        return_envelope=False,
                        expect_error=False,
                    ):
                        request = {"action": action}
                        if arguments is not None:
                            request["args"] = json.dumps(arguments)
                        if response_version == 2:
                            request["response_version"] = 2
                        result = await client.call_tool("anchor_execute", request, timeout=60)
                        if result.is_error:
                            raise AssertionError(f"MCP action failed: {action}")
                        payload = json.loads(result.content[0].text)
                        if response_version == 2:
                            if expect_error:
                                assert payload["ok"] is False
                                return payload
                            assert payload["ok"] is True
                            return payload if return_envelope else payload["result"]
                        return payload

                    def source_task(label):
                        return {
                            "arg0": f"Frame installed MCP unborn source task: {label}.",
                            "goal": "Prove v1 and v2 delegate exact acceptance to the shared core.",
                            "mode": "implementation",
                            "work_type": "change",
                            "execution_mode": "source_change",
                            "risk": "low",
                            "rigor": "direct",
                            "current_state": "Pristine attached unborn main fixture.",
                            "desired_outcome": "Accepted task without target mutation.",
                            "constraints": ["Do not create target source."],
                            "known_facts": ["HEAD is absent and main is attached."],
                            "evidence": [{"source": "git", "observation": "unborn main observed"}],
                            "in_scope": ["MCP source-task framing"],
                            "out_of_scope": ["transport fallback"],
                            "risks": ["response envelope drift"],
                            "acceptance_criteria": ["Shared task result has readiness 100."],
                            "stop_conditions": ["Target mutation."],
                            "deliverables": ["MCP acceptance evidence"],
                        }

                    mode = os.environ["MODE"]
                    if mode == "switch":
                        await call("orient")
                        await call("new_session", {"name": "mcp_switch", "inline": True})
                        await call("task", {
                            "arg0": "Switch installed MCP routing between managed projects.",
                            "goal": "Prove MCP refreshes routing before returning success.",
                            "mode": "implementation",
                            "current_state": "Alpha is active on a clean committed target.",
                            "desired_outcome": "Beta is active without alpha artifact leakage.",
                            "constraints": ["Do not modify target source."],
                            "known_facts": ["Both project descriptors exist."],
                            "evidence": [{"source": "direct qualification", "observation": "projects created"}],
                            "in_scope": ["MCP routing refresh"],
                            "out_of_scope": ["source changes"],
                            "risks": ["stale dispatcher routing"],
                            "acceptance_criteria": ["Response reports routing_refreshed."],
                            "stop_conditions": ["Target mutation."],
                            "deliverables": ["MCP result"],
                        })
                        for skill in ("writing-specs",):
                            await call("skill_loaded", {"arg0": skill})
                        tools = await call("tools")
                        switched = await call("project", {"arg0": "use", "arg1": "beta"})
                        status = await call("project")
                        print(json.dumps({
                            "tool_names": tool_names,
                            "registered": tools["metrics"]["registered"],
                            "switched": switched,
                            "status": status,
                        }))
                    elif mode == "access":
                        safe_results = []
                        for action in (
                            "status", "memory", "audit_history", "orient", "manifest", "tools",
                        ) * 2:
                            safe_results.append(await call(
                                action, response_version=2, return_envelope=True,
                            ))
                        concurrency = await call(
                            "concurrency",
                            {"command": "inspect"},
                            response_version=2,
                        )
                        task_results = [
                            await call("review", response_version=2, return_envelope=True)
                            for _ in range(7)
                        ]
                        blocked = await call(
                            "review", response_version=2, return_envelope=True,
                            expect_error=True,
                        )
                        print(json.dumps({
                            "tool_names": tool_names,
                            "safe_results": safe_results,
                            "concurrency": concurrency,
                            "task_results": task_results,
                            "blocked": blocked,
                        }))
                    elif mode == "unborn":
                        for action in ("status", "memory", "audit_history", "orient"):
                            await call(action)
                        await call("new_session", {"name": "mcp_unborn_v1", "inline": True})
                        v1_task = await call("task", source_task("v1"))
                        await call(
                            "new_session",
                            {"name": "mcp_unborn_v2", "inline": True},
                            response_version=2,
                        )
                        v2_envelope = await call(
                            "task",
                            source_task("v2"),
                            response_version=2,
                            return_envelope=True,
                        )
                        print(json.dumps({
                            "tool_names": tool_names,
                            "v1_readiness": v1_task["readiness"]["score"],
                            "v2_envelope": v2_envelope,
                        }))
                    elif mode == "unborn-restart":
                        for action in ("status", "memory", "audit_history", "orient"):
                            await call(action, response_version=2)
                        await call(
                            "new_session",
                            {"name": "mcp_unborn_restart", "inline": True},
                            response_version=2,
                        )
                        missing_task = await call(
                            "review",
                            response_version=2,
                            return_envelope=True,
                        )
                        task_error = None
                        fresh_readiness = None
                        if os.environ.get("EXPECT_TASK_ERROR") == "1":
                            task_error = await call(
                                "task",
                                source_task("dirty restart v2"),
                                response_version=2,
                                return_envelope=True,
                                expect_error=True,
                            )
                        else:
                            fresh_task = await call(
                                "task",
                                source_task("restart v2"),
                                response_version=2,
                            )
                            fresh_readiness = fresh_task["readiness"]["score"]
                        print(json.dumps({
                            "tool_names": tool_names,
                            "missing_task": missing_task,
                            "task_error": task_error,
                            "fresh_readiness": fresh_readiness,
                        }))
                    elif mode == "dirty":
                        await call("orient")
                        await call("new_session", {"name": "mcp_dirty_v1", "inline": True})
                        request = {
                            "action": "task",
                            "args": json.dumps(source_task("dirty v1")),
                        }
                        v1_result = await client.call_tool(
                            "anchor_execute", request, timeout=60, raise_on_error=False,
                        )
                        await call(
                            "new_session",
                            {"name": "mcp_dirty_v2", "inline": True},
                            response_version=2,
                        )
                        v2_envelope = await call(
                            "task",
                            source_task("dirty v2"),
                            response_version=2,
                            return_envelope=True,
                            expect_error=True,
                        )
                        print(json.dumps({
                            "tool_names": tool_names,
                            "v1_is_error": v1_result.is_error,
                            "v1_content": [item.text for item in v1_result.content],
                            "v2_envelope": v2_envelope,
                        }))
                    else:
                        status = await call("project")
                        print(json.dumps({"tool_names": tool_names, "status": status}))


            asyncio.run(main())
            """,
        )
        mcp_environment = {
            **direct_environment,
            "CANDIDATE_PYTHON": str(venv_python),
            "ANCHOR_PROJECT_ID": "alpha",
            "ANCHOR_PROJECT_ROOT": str(committed),
            "SERVER_CWD": str(committed),
            "MCP_LOG": str(audit_root / "mcp-stderr.log"),
            "MODE": "switch",
        }
        mcp_switch = json.loads(
            _run(
                [str(venv_python), str(mcp_script)],
                cwd=committed,
                environment=mcp_environment,
                timeout=180,
            ).stdout
        )
        assert mcp_switch["tool_names"] == ["anchor_execute", "anchor_help"]
        assert mcp_switch["registered"] == 11
        assert mcp_switch["switched"]["runtime_binding_unchanged"] is True
        assert mcp_switch["switched"]["reinitialize_required"] is False
        assert mcp_switch["status"]["active_project"] == "beta"
        assert mcp_switch["status"]["route_binding"]["project_id"] == "alpha"

        mcp_restart = json.loads(
            _run(
                [str(venv_python), str(mcp_script)],
                cwd=committed,
                environment={
                    **mcp_environment,
                    "ANCHOR_PROJECT_ID": "beta",
                    "ANCHOR_PROJECT_ROOT": str(fresh),
                    "SERVER_CWD": str(fresh),
                    "MODE": "restart",
                },
                timeout=180,
            ).stdout
        )
        assert mcp_restart["tool_names"] == ["anchor_execute", "anchor_help"]
        assert mcp_restart["status"]["active_project"] == "beta"
        assert Path(mcp_restart["status"]["target_root"]) == fresh.resolve()
        assert _git_snapshot(committed) == committed_before
        assert _git_snapshot(dirty) == dirty_before
        assert _tree_hashes(fresh) == {}

        mcp_access = json.loads(
            _run(
                [str(venv_python), str(mcp_script)],
                cwd=committed,
                environment={**mcp_environment, "MODE": "access"},
                timeout=180,
            ).stdout
        )
        assert mcp_access["tool_names"] == ["anchor_execute", "anchor_help"]
        assert all(result["ok"] is True for result in mcp_access["safe_results"])
        assert all("Planning not detected" not in str(result) for result in mcp_access["safe_results"])
        assert mcp_access["concurrency"]["kind"] == "concurrent_project_operations"
        assert mcp_access["concurrency"]["command"] == "inspect"
        assert mcp_access["concurrency"]["atomic"] is False
        assert mcp_access["concurrency"]["support_scope"] == "distinct_managed_projects_only"
        assert [item["domain"] for item in mcp_access["concurrency"]["domains"]] == [
            "route",
            "continuity",
            "learning",
            "selection_recovery",
        ]
        assert ["Planning not detected" in str(result) for result in mcp_access["task_results"]] == [
            False,
            False,
            True,
            True,
            True,
            True,
            True,
        ]
        assert mcp_access["blocked"]["ok"] is False
        assert "8 task-required" in mcp_access["blocked"]["error"]["message"]
        assert _git_snapshot(committed) == committed_before

        mcp_orb_home = audit_root / "mcp-orb-home"
        mcp_orb_memory = audit_root / "mcp-orb-memory" / "agent.db"
        mcp_orb_home.mkdir()
        registration_script = _write_script(
            audit_root,
            "mcp_project_registration.py",
            """
            import os

            from odibi_anchor._dispatcher._project import project_action

            for name, target in (("dirty", os.environ["DIRTY"]), ("unborn", os.environ["MCP_UNBORN"])):
                project_action(os.environ["ANCHOR_HOME"], "create", name=name, target=target, output_format="dict")
            """,
        )
        _run(
            [str(venv_python), str(registration_script)],
            cwd=committed,
            environment={**direct_environment, "ANCHOR_HOME": str(mcp_orb_home)},
        )
        mcp_direct_environment = {
            **mcp_environment,
            "ANCHOR_HOME": str(mcp_orb_home),
            "ANCHOR_MEMORY_DB": str(mcp_orb_memory),
        }
        mcp_dirty_before = _git_snapshot(dirty)
        mcp_dirty = json.loads(
            _run(
                [str(venv_python), str(mcp_script)],
                cwd=dirty,
                environment={
                    **mcp_direct_environment,
                    "ANCHOR_PROJECT_ID": "dirty",
                    "ANCHOR_PROJECT_ROOT": str(dirty),
                    "SERVER_CWD": str(dirty),
                    "MCP_LOG": str(audit_root / "mcp-dirty-stderr.log"),
                    "MODE": "dirty",
                },
                timeout=180,
            ).stdout
        )
        dirty_message = "BLOCKED: source-change task requires a clean initial Git worktree"
        assert mcp_dirty["tool_names"] == ["anchor_execute", "anchor_help"]
        assert mcp_dirty["v1_is_error"] is True
        assert any(dirty_message in content for content in mcp_dirty["v1_content"])
        assert mcp_dirty["v2_envelope"] == {
            "error": {"message": dirty_message, "type": "RuntimeError"},
            "ok": False,
        }
        assert _git_snapshot(dirty) == mcp_dirty_before
        assert not (dirty / ".agent_memory.db").exists()

        mcp_unborn_before = _unborn_git_snapshot(mcp_unborn)
        mcp_unborn_environment = {
            **mcp_direct_environment,
            "ANCHOR_PROJECT_ID": "unborn",
            "ANCHOR_PROJECT_ROOT": str(mcp_unborn),
            "SERVER_CWD": str(mcp_unborn),
            "MCP_LOG": str(audit_root / "mcp-unborn-stderr.log"),
            "MODE": "unborn",
        }
        mcp_unborn_result = json.loads(
            _run(
                [str(venv_python), str(mcp_script)],
                cwd=mcp_unborn,
                environment=mcp_unborn_environment,
                timeout=180,
            ).stdout
        )
        assert mcp_unborn_result["tool_names"] == ["anchor_execute", "anchor_help"]
        assert mcp_unborn_result["v1_readiness"] == 100
        assert mcp_unborn_result["v2_envelope"]["ok"] is True
        assert mcp_unborn_result["v2_envelope"]["result"]["readiness"]["score"] == 100
        assert _unborn_git_snapshot(mcp_unborn) == mcp_unborn_before

        restart_source = mcp_unborn / "restart.py"
        restart_source.write_text("RESTART = True\n", encoding="utf-8")
        dirty_restart_before = _unborn_git_snapshot(mcp_unborn)
        mcp_dirty_restart = json.loads(
            _run(
                [str(venv_python), str(mcp_script)],
                cwd=mcp_unborn,
                environment={
                    **mcp_unborn_environment,
                    "MODE": "unborn-restart",
                    "EXPECT_TASK_ERROR": "1",
                },
                timeout=180,
            ).stdout
        )
        assert mcp_dirty_restart["missing_task"]["ok"] is True
        assert mcp_dirty_restart["task_error"]["ok"] is False
        assert mcp_dirty_restart["task_error"]["error"] == {
            "message": "BLOCKED: source-change task requires a clean initial Git worktree",
            "type": "RuntimeError",
        }
        assert mcp_dirty_restart["fresh_readiness"] is None
        assert _unborn_git_snapshot(mcp_unborn) == dirty_restart_before
        restart_source.unlink()
        assert _unborn_git_snapshot(mcp_unborn) == mcp_unborn_before

        mcp_unborn_restart = json.loads(
            _run(
                [str(venv_python), str(mcp_script)],
                cwd=mcp_unborn,
                environment={**mcp_unborn_environment, "MODE": "unborn-restart"},
                timeout=180,
            ).stdout
        )
        assert mcp_unborn_restart["tool_names"] == ["anchor_execute", "anchor_help"]
        assert mcp_unborn_restart["missing_task"]["ok"] is True
        assert mcp_unborn_restart["missing_task"]["result"]["kind"] == "review_context"
        assert mcp_unborn_restart["fresh_readiness"] == 100
        assert _unborn_git_snapshot(mcp_unborn) == mcp_unborn_before
        assert not (mcp_unborn / ".agent_memory.db").exists()

        resource_hashes_after = {
            **_tree_hashes(site_packages / ".assistant"),
            **{f"tools/{name}": digest for name, digest in _tree_hashes(site_packages / "tools").items()},
        }
        assert resource_hashes_after == resource_hashes_before
        assert not list(site_packages.rglob(".agent_memory.db"))
        assert not list(site_packages.rglob(".active_project"))
        assert not list(site_packages.rglob(".anchor_session_state.json"))

        if os.name != "nt":
            processes = _run(["ps", "-eo", "args="]).stdout
            server_command = f"{venv_python} -m odibi_anchor.mcp_server"
            assert server_command not in processes
    finally:
        _remove_tree(audit_root)

    assert not audit_root.exists()
