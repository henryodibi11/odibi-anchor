"""A disposable fake Databricks runtime for end-to-end incident replay.

The fake patches only the seams product code already resolves at call time:

* ``sys.modules["databricks.sdk"]`` (and ``.errors``/``.service.workspace``), which
  ``importlib.import_module`` returns to ``durability._databricks_files_api``,
  ``host_setup._setup_databricks_workspace`` and the Git Folder provider;
* ``os.geteuid`` and ``pwd.getpwuid``, read by ``startup._databricks_compute_identity``;
* ``DATABRICKS_RUNTIME_VERSION``, read by ``startup``/``_boot`` host detection.

Product modules are never monkeypatched, so the fake survives ``bootstrap.init``
evicting and re-importing every ``odibi_anchor`` module, and it runs unchanged
against an installed wheel.
"""
from __future__ import annotations

import os
import pwd
import shutil
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.fake_databricks import apis, crash

DEFAULT_RUNTIME_VERSION = "15.4"
DEFAULT_VOLUME = "/Volumes/main/anchor/state"
DEFAULT_USER = "henry@example.invalid"


@dataclass(frozen=True)
class ComputeIdentity:
    uid: int
    account: str


class FakeWorkspaceClient:
    """``databricks.sdk.WorkspaceClient`` bound to one fake environment."""

    def __init__(self, environment: FakeDatabricks, *, config: apis.Config | None = None) -> None:
        self.workspace = environment.workspace
        self.files = environment.files
        self.config = config or apis.Config()
        environment.clients.append(self)


class FakeDatabricks:
    """One fake Databricks workspace, Volume store, and current compute.

    ``local_root`` stands in for the compute's local disk (``/tmp``); it is wiped by
    :meth:`new_compute`. The Workspace and Volumes persist across computes.

    The effective UID defaults to the real one because ``startup`` compares it with
    the owner of directories the test process creates; :meth:`new_compute` changes
    the OS account instead, which changes the identity fingerprint and therefore
    the identity-isolated runtime root.
    """

    def __init__(self, root: Path, *, runtime_version: str = DEFAULT_RUNTIME_VERSION) -> None:
        self.root = root
        self.runtime_version = runtime_version
        self.control = apis.CallControl()
        self.clients: list[FakeWorkspaceClient] = []
        self.workspace = apis.FakeWorkspaceAPI(self.control)
        self.files = apis.FakeFilesAPI(self.control, root / "volumes")
        self.workspace.add_directory(f"/Users/{DEFAULT_USER}")
        self.files.provision_volume(DEFAULT_VOLUME)
        self.compute_generation = 1
        self.identity = ComputeIdentity(os.geteuid(), "spark-compute-1")
        self.local_root = root / "compute-local"
        self.local_root.mkdir(parents=True)
        self._real_getpwuid = pwd.getpwuid

    # ── seams ────────────────────────────────────────────────────────────────
    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeDatabricks:
        def module(name: str, **attributes: Any) -> types.ModuleType:
            created = types.ModuleType(name)
            for key, value in attributes.items():
                setattr(created, key, value)
            return created

        errors = module("databricks.sdk.errors", **{
            name: getattr(apis, name) for name in (
                "DatabricksError", "NotFound", "ResourceDoesNotExist", "AlreadyExists",
                "ResourceAlreadyExists",
            )
        })
        workspace_types = module(
            "databricks.sdk.service.workspace", ImportFormat=apis.ImportFormat,
            ObjectType=apis.ObjectType, ObjectInfo=apis.ObjectInfo,
        )
        service = module("databricks.sdk.service", __path__=[], workspace=workspace_types)
        config = module("databricks.sdk.config", Config=apis.Config)
        sdk = module(
            "databricks.sdk", __path__=[], errors=errors, service=service, config=config,
            WorkspaceClient=lambda **kwargs: FakeWorkspaceClient(self, **kwargs),
        )
        package = module("databricks", __path__=[], sdk=sdk)
        for name, module_object in (
            ("databricks", package), ("databricks.sdk", sdk), ("databricks.sdk.errors", errors),
            ("databricks.sdk.config", config), ("databricks.sdk.service", service),
            ("databricks.sdk.service.workspace", workspace_types),
        ):
            monkeypatch.setitem(sys.modules, name, module_object)
        monkeypatch.setenv("DATABRICKS_RUNTIME_VERSION", self.runtime_version)
        monkeypatch.setattr(os, "geteuid", lambda: self.identity.uid)
        monkeypatch.setattr(pwd, "getpwuid", self._getpwuid)
        return self

    def _getpwuid(self, uid: int) -> Any:
        if uid != self.identity.uid:
            return self._real_getpwuid(uid)
        real = self._real_getpwuid(uid)
        return types.SimpleNamespace(
            pw_name=self.identity.account, pw_uid=uid, pw_gid=real.pw_gid,
            pw_dir=str(self.local_root), pw_shell=real.pw_shell, pw_gecos="", pw_passwd="x",
        )

    # ── controls ─────────────────────────────────────────────────────────────
    @property
    def calls(self) -> list[tuple[str, str, str]]:
        return self.control.calls

    def set_latency(self, seconds: float, *, api: str | None = None, method: str | None = None) -> None:
        """Delay matching calls; the most specific (api, method) setting wins."""
        self.control.latency[(api, method)] = seconds

    def inject_fault(self, api: str, method: str, **kwargs: Any) -> apis.Fault:
        """Fail matching calls; see :class:`apis.Fault` for ``error``/``match``/``times``/``when``."""
        if "error" not in kwargs:
            kwargs["error"] = lambda path: apis.DatabricksError(f"injected {api}.{method} failure: {path}")
        fault = apis.Fault(api=api, method=method, **kwargs)
        self.control.faults.append(fault)
        return fault

    def volume_path(self, path: str) -> Path:
        """Local backing path for a Volume path, for direct test inspection."""
        return self.files.backing_path(path)

    @property
    def local_state_root(self) -> Path:
        """Configured portfolio ``local_state_root`` on the current compute (initially absent)."""
        return self.local_root / "anchor-state"

    def runtime_roots(self) -> list[Path]:
        """Identity-isolated runtime roots present on the current compute."""
        return sorted(self.local_root.glob("anchor-state.identity-*"))

    # ── lifecycle ────────────────────────────────────────────────────────────
    @staticmethod
    def new_process() -> None:
        """Model a Python restart on the same compute: drop Anchor process bindings."""
        memory_db = sys.modules.get("odibi_anchor.codebase._memory_db")
        close_all = getattr(memory_db, "close_all_dbs", None)
        if callable(close_all):
            close_all()
        for name in [name for name in os.environ if name.startswith("ANCHOR_")]:
            del os.environ[name]

    def new_compute(self, *, account: str | None = None, uid: int | None = None) -> ComputeIdentity:
        """Discard all local compute state and attach a compute with a new identity."""
        self.new_process()
        with crash.suppressed():
            shutil.rmtree(self.local_root)
            self.local_root.mkdir()
        self.compute_generation += 1
        self.identity = ComputeIdentity(
            self.identity.uid if uid is None else uid,
            account or f"spark-compute-{self.compute_generation}",
        )
        return self.identity


def write_databricks_portfolio(
    config_path: Path,
    *,
    environment: FakeDatabricks,
    instruction_root: Path,
    projects: dict[str, str],
    host_id: str = "databricks",
    durable_root: str | None = DEFAULT_VOLUME + "/anchor",
    authority_id: str = "henry",
    retention: tuple[int, int] | None = None,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Write one PortfolioV1 with a databricks host through the public portfolio API."""
    from odibi_anchor.portfolio import write_portfolio

    host: dict[str, Any] = {
        "adapter": "databricks",
        "local_state_root": str(environment.local_state_root),
        "instruction_root": str(instruction_root),
    }
    if durable_root is not None:
        host["durable_root"] = durable_root
    portfolio: dict[str, Any] = {
        "schema_version": 1,
        "authority": {"id": authority_id, "trust_domain": "work"},
        "hosts": {host_id: host},
        "projects": {
            project_id: {"targets": {host_id: target}} for project_id, target in projects.items()
        },
        "personas": {},
    }
    if retention is not None:
        portfolio["durability"] = {
            "retention": {"days": retention[0], "minimum_snapshots": retention[1]},
        }
    return write_portfolio(config_path, portfolio, expected_sha256=expected_sha256)


__all__ = [
    "DEFAULT_VOLUME",
    "ComputeIdentity",
    "FakeDatabricks",
    "FakeWorkspaceClient",
    "apis",
    "crash",
    "write_databricks_portfolio",
]
