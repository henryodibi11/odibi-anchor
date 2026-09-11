"""Simulated-only coverage for the read-only Databricks Repos identity adapter."""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

import odibi_anchor.operational._databricks as databricks_module
from odibi_anchor.operational import (
    DatabricksGitFolderRepositoryProvider,
    autoconfigure_databricks_git_folder_repository,
    databricks_workspace_path_from_checkout,
)
from odibi_anchor.operational._contract import ContractError


def valid_responses() -> dict[str, dict[str, object]]:
    return {
        "workspace_get_status": {
            "object_type": "REPO",
            "object_id": 42,
            "path": "/Users/test@example.invalid/odibi_anchor",
        },
        "repos_get": {
            "id": 42,
            "path": "/Users/test@example.invalid/odibi_anchor",
            "branch": "main",
            "head_commit_id": "A" * 40,
            "url": "https://example.invalid/repository.git",
            "provider": "gitHub",
        },
    }


class SimulatedAPI:
    def __init__(self, responses: Mapping[str, Mapping[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, operation: str, parameters: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append((operation, dict(parameters)))
        return self.responses[operation]


def test_repository_provider_uses_only_read_operations_and_normalizes_workspace_path() -> None:
    api = SimulatedAPI(valid_responses())
    provider = DatabricksGitFolderRepositoryProvider(api)

    identity = provider.capture_identity(
        "/Workspace/Users/test@example.invalid/odibi_anchor",
    )

    assert api.calls == [
        (
            "workspace_get_status",
            {"path": "/Users/test@example.invalid/odibi_anchor"},
        ),
        ("repos_get", {"repo_id": "42"}),
    ]
    assert identity.repository_id == "42"
    assert identity.workspace_path == "/Users/test@example.invalid/odibi_anchor"
    assert identity.branch == "main"
    assert identity.head_sha == "a" * 40
    assert identity.remote_url == "https://example.invalid/repository.git"
    assert identity.git_provider == "gitHub"


def test_repository_provider_rejects_plain_workspace_directory() -> None:
    responses = valid_responses()
    responses["workspace_get_status"]["object_type"] = "DIRECTORY"
    responses["workspace_get_status"]["is_git_folder"] = False
    api = SimulatedAPI(responses)

    with pytest.raises(ContractError, match="not a Databricks Git Folder"):
        DatabricksGitFolderRepositoryProvider(api).capture_identity(
            "/Users/test@example.invalid/odibi_anchor",
        )

    assert [operation for operation, _ in api.calls] == ["workspace_get_status"]


def test_repository_provider_accepts_git_folder_directory_without_provider_label() -> None:
    responses = valid_responses()
    responses["workspace_get_status"].update({
        "object_type": "DIRECTORY",
        "is_git_folder": True,
    })
    responses["repos_get"]["provider"] = ""

    identity = DatabricksGitFolderRepositoryProvider(
        SimulatedAPI(responses)
    ).capture_identity("/Users/test@example.invalid/odibi_anchor")

    assert identity.repository_id == "42"
    assert identity.git_provider is None


@pytest.mark.parametrize(
    ("operation", "field", "value", "message"),
    [
        ("workspace_get_status", "object_id", None, "missing object_id"),
        ("repos_get", "id", 99, "object IDs do not match"),
        ("repos_get", "path", "/Users/other/repo", "path does not match"),
        ("repos_get", "branch", "", "missing branch, HEAD, or URL"),
        ("repos_get", "provider", object(), "provider must be a string"),
        ("repos_get", "head_commit_id", "short", "exact 40-character commit SHA"),
    ],
)
def test_repository_provider_rejects_incomplete_or_inconsistent_identity(
    operation: str,
    field: str,
    value: object,
    message: str,
) -> None:
    responses = valid_responses()
    responses[operation][field] = value

    with pytest.raises(ContractError, match=message):
        DatabricksGitFolderRepositoryProvider(SimulatedAPI(responses)).capture_identity(
            "/Users/test@example.invalid/odibi_anchor",
        )


def test_repository_provider_can_pin_workspace_path_independent_of_local_mount() -> None:
    api = SimulatedAPI(valid_responses())
    provider = DatabricksGitFolderRepositoryProvider(
        api,
        workspace_path="/Workspace/Users/test@example.invalid/odibi_anchor",
    )

    identity = provider.capture_identity("/local/fuse/mount/odibi_anchor")

    assert identity.workspace_path == "/Users/test@example.invalid/odibi_anchor"
    assert api.calls[0][1] == {"path": "/Users/test@example.invalid/odibi_anchor"}


@pytest.mark.parametrize(
    ("checkout", "expected"),
    [
        (
            "/Workspace/Users/test@example.invalid/odibi_anchor",
            "/Users/test@example.invalid/odibi_anchor",
        ),
        ("/Workspace", None),
        ("/Workspace/Users/test/../other", None),
        ("/Workspace//Users/test/repo", None),
        ("/home/test/odibi_anchor", None),
        ("Workspace/Users/test/repo", None),
    ],
)
def test_workspace_path_is_derived_only_from_exact_normalized_checkout(
    checkout: str,
    expected: str | None,
) -> None:
    assert databricks_workspace_path_from_checkout(checkout) == expected


class FakeWorkspaceService:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def get_status(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            object_type=SimpleNamespace(value="REPO"),
            object_id=42,
            path="/Users/test@example.invalid/odibi_anchor",
            directory_info=None,
        )

    def delete(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("write method must never be called")


class FakeReposService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            id=42,
            path="/Users/test@example.invalid/odibi_anchor",
            branch="main",
            head_commit_id="A" * 40,
            url="https://example.invalid/repository.git",
            provider=SimpleNamespace(value="gitHub"),
        )

    def update(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("write method must never be called")


class FakeWorkspaceClient:
    def __init__(self, *, workspace_error: Exception | None = None) -> None:
        self.workspace = FakeWorkspaceService(error=workspace_error)
        self.repos = FakeReposService()


def test_sdk_factory_normalizes_current_git_folder_workspace_shape() -> None:
    client = FakeWorkspaceClient()
    client.workspace.get_status = lambda **_kwargs: SimpleNamespace(
        object_type=SimpleNamespace(value="DIRECTORY"),
        object_id=42,
        path="/Users/test@example.invalid/odibi_anchor",
        directory_info=SimpleNamespace(is_git_folder=True),
    )
    client.repos.get = lambda **_kwargs: SimpleNamespace(
        id=42,
        path="/Users/test@example.invalid/odibi_anchor",
        branch="main",
        head_commit_id="A" * 40,
        url="https://example.invalid/repository.git",
        provider="",
    )

    provider, evidence = autoconfigure_databricks_git_folder_repository(
        "/Workspace/Users/test@example.invalid/odibi_anchor",
        workspace_client=client,
    )

    assert provider is not None
    assert evidence["acquisition_outcome"] == "available"
    assert evidence["identity"]["git_provider"] is None


def test_sdk_factory_attests_with_exactly_two_read_methods_and_pins_target() -> None:
    client = FakeWorkspaceClient()
    checkout = "/Workspace/Users/test@example.invalid/odibi_anchor"

    provider, evidence = autoconfigure_databricks_git_folder_repository(
        checkout,
        workspace_client=client,
    )

    assert provider is not None
    assert client.workspace.calls == [{"path": "/Users/test@example.invalid/odibi_anchor"}]
    assert client.repos.calls == [{"repo_id": 42}]
    assert evidence == {
        "kind": "databricks_git_folder",
        "provider_id": "odibi-anchor.databricks-repos",
        "acquisition_outcome": "available",
        "reason": None,
        "capabilities": {
            "host_repository_identity": "available",
            "local_worktree_status": "unavailable",
            "git_changed_paths_and_diff": "unavailable",
            "merge_base_and_history": "unavailable",
            "task_scoped_content_diff": "available",
            "task_scoped_write_tracking": "available",
            "pr_readiness": "unavailable",
        },
        "identity": {
            "repository_id": "42",
            "workspace_path": "/Users/test@example.invalid/odibi_anchor",
            "branch": "main",
            "head_sha": "a" * 40,
            "git_provider": "gitHub",
        },
    }
    with pytest.raises(ContractError, match="does not match"):
        provider.capture_identity("/Workspace/Users/test@example.invalid/other")
    assert client.workspace.calls == [{"path": "/Users/test@example.invalid/odibi_anchor"}]
    assert client.repos.calls == [{"repo_id": 42}]


def test_sdk_factory_imports_sdk_only_for_databricks_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imports: list[str] = []

    def import_module(name: str) -> Any:
        imports.append(name)
        return SimpleNamespace(WorkspaceClient=FakeWorkspaceClient)

    monkeypatch.setattr(databricks_module.importlib, "import_module", import_module)

    provider, outside = autoconfigure_databricks_git_folder_repository("/home/test/repo")
    assert provider is None
    assert outside["acquisition_outcome"] == "not_applicable"
    assert imports == []

    provider, inside = autoconfigure_databricks_git_folder_repository(
        "/Workspace/Users/test@example.invalid/odibi_anchor"
    )
    assert provider is not None
    assert inside["acquisition_outcome"] == "available"
    assert imports == ["databricks.sdk"]


def test_sdk_factory_degrades_missing_sdk_without_import_time_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(_name: str) -> Any:
        raise ModuleNotFoundError("simulated missing optional SDK")

    monkeypatch.setattr(databricks_module.importlib, "import_module", missing)

    provider, evidence = autoconfigure_databricks_git_folder_repository(
        "/Workspace/Users/test@example.invalid/odibi_anchor"
    )

    assert provider is None
    assert evidence["kind"] == "unavailable"
    assert evidence["acquisition_outcome"] == "unavailable"
    assert evidence["reason"] == "databricks_sdk_unavailable"
    assert set(evidence["capabilities"].values()) == {"unavailable"}


class SimulatedSDKError(RuntimeError):
    def __init__(self, status_code: int | None, error_code: str, secret: str) -> None:
        super().__init__(secret)
        self.status_code = status_code
        self.error_code = error_code


@pytest.mark.parametrize(
    ("status_code", "error_code", "outcome"),
    [
        (403, "PERMISSION_DENIED", "denied"),
        (404, "NOT_FOUND", "unavailable"),
        (None, "RESOURCE_DOES_NOT_EXIST", "unavailable"),
        (500, "INTERNAL_ERROR", "failed"),
    ],
)
def test_sdk_factory_degrades_read_errors_without_leaking_messages(
    status_code: int | None,
    error_code: str,
    outcome: str,
) -> None:
    client = FakeWorkspaceClient(
        workspace_error=SimulatedSDKError(status_code, error_code, "secret credential detail")
    )

    provider, evidence = autoconfigure_databricks_git_folder_repository(
        "/Workspace/Users/test@example.invalid/odibi_anchor",
        workspace_client=client,
    )

    assert provider is not None
    assert evidence["kind"] == "databricks_git_folder"
    assert evidence["acquisition_outcome"] == outcome
    assert evidence["reason"] == f"repository_identity_{outcome}"
    assert "secret" not in repr(evidence)
    assert set(evidence["capabilities"].values()) == {"unavailable"}
    assert client.repos.calls == []
