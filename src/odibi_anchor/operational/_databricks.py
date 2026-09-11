"""Read-only Databricks retrieval behind small, injected executor contracts.

This module deliberately imports no Databricks package.  Executors are supplied by
the embedding application and receive either a complete, adapter-owned SQL statement
or a fixed API operation plus a parameter mapping.
"""

from __future__ import annotations

import importlib
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from odibi_anchor._repository_snapshot import (
    DatabricksGitFolderIdentity,
    databricks_repository_capabilities,
)

from ._contract import CollectorResult, ContractError, normalize_json
from ._delta import delta_changes
from ._redaction import redact
from ._spark import spark_diagnose
from ._uc import uc_context

SQLExecutor = Callable[[str], Any]
APIExecutor = Callable[[str, Mapping[str, Any]], Any]

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
_DENIED = ("permission_denied", "permission denied", "not authorized", "forbidden", "insufficient privileges")
_UNAVAILABLE = ("table_or_view_not_found", "not found", "unsupported", "not available", "does not exist")


def _canonical_workspace_path(value: str | os.PathLike[str]) -> str:
    raw = str(value).strip().replace("\\", "/")
    if raw == "/Workspace":
        return "/"
    if raw.startswith("/Workspace/"):
        raw = raw[len("/Workspace"):]
    path = PurePosixPath(raw)
    if not raw.startswith("/") or ".." in path.parts:
        raise ContractError("Databricks Git Folder path must be an absolute workspace path")
    return "/" + "/".join(part for part in path.parts if part != "/")


class DatabricksGitFolderRepositoryProvider:
    """Strict read-only Repos identity provider using the injected API boundary."""

    provider_id = "odibi-anchor.databricks-repos"

    def __init__(
        self,
        api_executor: APIExecutor,
        *,
        workspace_path: str | None = None,
        target_worktree: str | os.PathLike[str] | None = None,
    ) -> None:
        if not callable(api_executor):
            raise TypeError("api_executor must be callable")
        self.api_executor = api_executor
        self.workspace_path = (
            _canonical_workspace_path(workspace_path) if workspace_path is not None else None
        )
        self.target_worktree = (
            Path(target_worktree).resolve(strict=False) if target_worktree is not None else None
        )

    def _api(self, operation: str, parameters: Mapping[str, Any]) -> Mapping[str, Any]:
        if operation not in {"workspace_get_status", "repos_get"}:
            raise ContractError("unsupported repository identity operation")
        response = self.api_executor(operation, normalize_json(parameters))
        if not isinstance(response, Mapping):
            raise ContractError(f"{operation} response must be a mapping")
        return response

    def capture_identity(
        self, target_worktree: str | os.PathLike[str],
    ) -> DatabricksGitFolderIdentity:
        """Attest REPO object type and current host identity or raise."""
        if (
            self.target_worktree is not None
            and Path(target_worktree).resolve(strict=False) != self.target_worktree
        ):
            raise ContractError("target worktree does not match the configured Databricks Git Folder")
        expected_path = self.workspace_path or _canonical_workspace_path(target_worktree)
        status = self._api("workspace_get_status", {"path": expected_path})
        object_type = str(status.get("object_type", "")).upper()
        if object_type != "REPO" and not (
            object_type == "DIRECTORY" and status.get("is_git_folder") is True
        ):
            raise ContractError("workspace path is not a Databricks Git Folder")
        repository_id = status.get("object_id")
        if isinstance(repository_id, bool) or not isinstance(repository_id, (int, str)):
            raise ContractError("workspace Git Folder status is missing object_id")
        repository_id = str(repository_id).strip()
        if not repository_id:
            raise ContractError("workspace Git Folder status has an empty object_id")
        status_path = status.get("path")
        if not isinstance(status_path, str) or _canonical_workspace_path(status_path) != expected_path:
            raise ContractError("workspace status path does not match the requested Git Folder")

        repo = self._api("repos_get", {"repo_id": repository_id})
        repo_id = repo.get("id")
        if isinstance(repo_id, bool) or not isinstance(repo_id, (int, str)):
            raise ContractError("Repos response is missing id")
        if str(repo_id).strip() != repository_id:
            raise ContractError("Workspace and Repos object IDs do not match")
        repo_path = repo.get("path")
        if not isinstance(repo_path, str) or _canonical_workspace_path(repo_path) != expected_path:
            raise ContractError("Repos path does not match the requested Git Folder")
        fields = {
            "branch": repo.get("branch"),
            "head_sha": repo.get("head_commit_id"),
            "remote_url": repo.get("url"),
        }
        if any(not isinstance(value, str) or not value.strip() for value in fields.values()):
            raise ContractError("Repos response is missing branch, HEAD, or URL")
        git_provider = repo.get("provider")
        if git_provider is not None and not isinstance(git_provider, str):
            raise ContractError("Repos provider must be a string when available")
        normalized_git_provider = None if git_provider is None else git_provider.strip() or None
        head_sha = str(fields["head_sha"]).strip().lower()
        if re.fullmatch(r"[0-9a-f]{40}", head_sha) is None:
            raise ContractError("Repos head_commit_id must be an exact 40-character commit SHA")
        return DatabricksGitFolderIdentity(
            repository_id=repository_id,
            workspace_path=expected_path,
            branch=str(fields["branch"]).strip(),
            head_sha=head_sha,
            remote_url=str(fields["remote_url"]).strip(),
            git_provider=normalized_git_provider,
        )


def databricks_workspace_path_from_checkout(
    checkout_path: str | os.PathLike[str],
) -> str | None:
    """Derive an API path only from one exact normalized ``/Workspace`` checkout."""
    raw = os.fspath(checkout_path)
    if not isinstance(raw, str) or not raw.startswith("/") or "\\" in raw:
        return None
    lexical = PurePosixPath(raw)
    if ".." in lexical.parts:
        return None
    normalized = lexical.as_posix()
    if raw != normalized or not normalized.startswith("/Workspace/"):
        return None
    workspace_path = normalized.removeprefix("/Workspace")
    return _canonical_workspace_path(workspace_path)


def _sdk_scalar(value: Any) -> Any:
    return getattr(value, "value", value)


def _sdk_field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return _sdk_scalar(value.get(name))
    return _sdk_scalar(getattr(value, name, None))


class _DatabricksSDKReadExecutor:
    """Map exactly two WorkspaceClient reads to the provider executor contract."""

    def __init__(self, workspace_client: Any) -> None:
        self.workspace_client = workspace_client

    def __call__(self, operation: str, parameters: Mapping[str, Any]) -> Mapping[str, Any]:
        if operation == "workspace_get_status":
            if set(parameters) != {"path"} or not isinstance(parameters["path"], str):
                raise ContractError("workspace_get_status requires exactly one path")
            result = self.workspace_client.workspace.get_status(path=parameters["path"])
            directory_info = _sdk_field(result, "directory_info")
            return {
                "object_type": _sdk_field(result, "object_type"),
                "object_id": _sdk_field(result, "object_id"),
                "path": _sdk_field(result, "path"),
                "is_git_folder": _sdk_field(directory_info, "is_git_folder"),
            }
        if operation == "repos_get":
            if set(parameters) != {"repo_id"}:
                raise ContractError("repos_get requires exactly one repo_id")
            raw_id = parameters["repo_id"]
            if isinstance(raw_id, bool) or not str(raw_id).isdigit():
                raise ContractError("repos_get repo_id must be an integer identifier")
            result = self.workspace_client.repos.get(repo_id=int(str(raw_id)))
            return {
                "id": _sdk_field(result, "id"),
                "path": _sdk_field(result, "path"),
                "branch": _sdk_field(result, "branch"),
                "head_commit_id": _sdk_field(result, "head_commit_id"),
                "url": _sdk_field(result, "url"),
                "provider": _sdk_field(result, "provider"),
            }
        raise ContractError("unsupported repository identity operation")


def _unavailable_repository_capabilities() -> dict[str, str]:
    return {name: "unavailable" for name in databricks_repository_capabilities()}


def _sdk_acquisition_outcome(exc: Exception) -> str:
    status = str(getattr(exc, "status_code", "")).strip().upper()
    code = str(getattr(exc, "error_code", "")).strip().upper()
    combined = f"{status} {code}"
    if status in {"401", "403"} or any(
        marker in combined
        for marker in ("PERMISSION_DENIED", "UNAUTHENTICATED", "UNAUTHORIZED", "FORBIDDEN")
    ):
        return "denied"
    if status in {"404", "501"} or any(
        marker in combined
        for marker in ("NOT_FOUND", "DOES_NOT_EXIST", "UNAVAILABLE", "NOT_IMPLEMENTED")
    ):
        return "unavailable"
    return "failed"


def autoconfigure_databricks_git_folder_repository(
    checkout_path: str | os.PathLike[str],
    *,
    workspace_client: Any | None = None,
) -> tuple[DatabricksGitFolderRepositoryProvider | None, dict[str, Any]]:
    """Optionally build and attest a read-only SDK-backed Git Folder provider.

    The SDK import is deferred until an exact Databricks checkout is observed. All
    failures become structured evidence so ordinary bootstrap and read-only work can
    continue. Supplying ``workspace_client`` supports embedding and simulated tests.
    """
    workspace_path = databricks_workspace_path_from_checkout(checkout_path)
    if workspace_path is None:
        return None, {
            "kind": "unavailable",
            "provider_id": None,
            "acquisition_outcome": "not_applicable",
            "reason": "checkout_outside_databricks_workspace",
            "capabilities": _unavailable_repository_capabilities(),
        }

    if workspace_client is None:
        try:
            sdk = importlib.import_module("databricks.sdk")
            workspace_client_type = sdk.WorkspaceClient
            workspace_client = workspace_client_type()
        except (ImportError, ModuleNotFoundError):
            return None, {
                "kind": "unavailable",
                "provider_id": None,
                "acquisition_outcome": "unavailable",
                "reason": "databricks_sdk_unavailable",
                "capabilities": _unavailable_repository_capabilities(),
            }
        except Exception as exc:
            outcome = _sdk_acquisition_outcome(exc)
            return None, {
                "kind": "unavailable",
                "provider_id": None,
                "acquisition_outcome": outcome,
                "reason": f"databricks_sdk_client_{outcome}",
                "capabilities": _unavailable_repository_capabilities(),
            }

    provider = DatabricksGitFolderRepositoryProvider(
        _DatabricksSDKReadExecutor(workspace_client),
        workspace_path=workspace_path,
        target_worktree=checkout_path,
    )
    try:
        identity = provider.capture_identity(checkout_path)
    except Exception as exc:
        outcome = _sdk_acquisition_outcome(exc)
        return provider, {
            "kind": "databricks_git_folder",
            "provider_id": provider.provider_id,
            "acquisition_outcome": outcome,
            "reason": f"repository_identity_{outcome}",
            "capabilities": _unavailable_repository_capabilities(),
        }
    return provider, {
        "kind": "databricks_git_folder",
        "provider_id": provider.provider_id,
        "acquisition_outcome": "available",
        "reason": None,
        "capabilities": databricks_repository_capabilities(),
        "identity": {
            "repository_id": identity.repository_id,
            "workspace_path": identity.workspace_path,
            "branch": identity.branch,
            "head_sha": identity.head_sha,
            "git_provider": identity.git_provider,
        },
    }


def quote_sql_identifier(identifier: str) -> str:
    """Validate and quote a one-to-three-part UC identifier.

    Backticks, whitespace, comments, and SQL punctuation are rejected rather than
    escaped, making it impossible for a resource name to alter adapter-owned SQL.
    """
    if not isinstance(identifier, str):
        raise ContractError("SQL identifier must be a string")
    parts = identifier.split(".")
    if not 1 <= len(parts) <= 3 or any(not _IDENTIFIER.fullmatch(part) for part in parts):
        raise ContractError("invalid SQL identifier")
    return ".".join(f"`{part}`" for part in parts)


def _rows(response: Any) -> list[Mapping[str, Any]]:
    if isinstance(response, Mapping):
        response = response.get("rows", response.get("data", response.get("result")))
    if not isinstance(response, Sequence) or isinstance(response, (str, bytes, bytearray)):
        raise ContractError("executor response must contain mapping rows")
    rows = list(response)
    if any(not isinstance(row, Mapping) for row in rows):
        raise ContractError("executor response contains a non-mapping row")
    return rows


def _error_result(collector: str, exc: Exception, channel: str) -> CollectorResult:
    text = str(exc)
    lowered = text.lower()
    code = str(getattr(exc, "error_code", getattr(exc, "status_code", ""))).lower()
    combined = f"{code} {lowered}"
    if any(marker in combined for marker in _DENIED) or code in {"401", "403"}:
        status = "denied"
    elif any(marker in combined for marker in _UNAVAILABLE) or code in {"404", "501"}:
        status = "unavailable"
    else:
        status = "failed"
    cleaned, summary = redact({"type": type(exc).__name__, "message": text[:1000]})
    return CollectorResult(
        collector=collector,
        status=status,
        source={"provider": "databricks", "channel": channel},
        limitations=("provider evidence could not be collected",),
        error=cleaned,
        redaction=summary,
    )


class DatabricksOperationalAdapter:
    """Bounded Databricks evidence retrieval using injected SQL/API executors."""

    def __init__(self, *, sql_executor: SQLExecutor | None = None,
                 api_executor: APIExecutor | None = None) -> None:
        self.sql_executor = sql_executor
        self.api_executor = api_executor

    def _sql(self, statement: str) -> list[Mapping[str, Any]]:
        if self.sql_executor is None:
            raise RuntimeError("SQL evidence channel is not available")
        return _rows(self.sql_executor(statement))

    def _api(self, operation: str, parameters: Mapping[str, Any]) -> Mapping[str, Any]:
        if self.api_executor is None:
            raise RuntimeError("API evidence channel is not available")
        response = self.api_executor(operation, normalize_json(parameters))
        if not isinstance(response, Mapping):
            raise ContractError("API executor response must be a mapping")
        return response

    def collect_spark(self, query_id: str) -> CollectorResult:
        """Collect a serverless query profile without requiring classic stage APIs."""
        try:
            profile = self._api("query_profile", {"query_id": query_id})
            plan = profile.get("plan_text", profile.get("physical_plan"))
            if not isinstance(plan, str):
                raise ContractError("query profile is missing plan_text")
            return spark_diagnose(plan, query_profile=profile,
                                  source={"provider": "databricks", "channel": "query_profile"},
                                  environment={"execution_mode": "serverless", "evidence_channel": "query_profile"})
        except Exception as exc:  # executors define their own exception types
            return _error_result("spark_diagnose", exc, "query_profile")

    def collect_uc(self, table: str) -> CollectorResult:
        quoted = quote_sql_identifier(table)
        try:
            detail_rows = self._sql(f"DESCRIBE TABLE EXTENDED {quoted} AS JSON")
            grant_rows = self._sql(f"SHOW GRANTS ON TABLE {quoted}")
            if len(detail_rows) != 1:
                raise ContractError("DESCRIBE TABLE AS JSON schema drift: expected one row")
            row = detail_rows[0]
            raw_detail = (next(iter(row.values())) if len(row) == 1
                          else row.get("json_string", row.get("json")))
            if isinstance(raw_detail, str):
                try:
                    detail = json.loads(raw_detail)
                except json.JSONDecodeError as exc:
                    raise ContractError("DESCRIBE TABLE AS JSON returned invalid JSON") from exc
            elif isinstance(raw_detail, Mapping):
                detail = dict(raw_detail)
            else:
                raise ContractError("DESCRIBE TABLE AS JSON schema drift: expected JSON object")
            if not isinstance(detail, Mapping):
                raise ContractError("DESCRIBE TABLE AS JSON schema drift: expected JSON object")
            return uc_context({"object": {"name": table, "type": "table"},
                               "detail": normalize_json(detail),
                               "direct_grants": [normalize_json(row) for row in grant_rows],
                               "source": {"provider": "databricks", "channel": "sql"},
                               "limitations": ["tags, dependencies, lineage, filters, masks, and execution principal were not queried"]})
        except Exception as exc:
            return _error_result("uc_context", exc, "sql")

    def collect_delta(self, table: str, *, start_version: int | None = None,
                      end_version: int | None = None) -> CollectorResult:
        quoted = quote_sql_identifier(table)
        cdf_name = ".".join(table.split("."))  # safe after strict identifier validation above
        try:
            detail_rows = self._sql(f"DESCRIBE DETAIL {quoted}")
            history = self._sql(f"DESCRIBE HISTORY {quoted} LIMIT 100")
            if len(detail_rows) != 1 or "format" not in detail_rows[0]:
                raise ContractError("DESCRIBE DETAIL schema drift: expected one row with format")
            detail = dict(detail_rows[0])
            properties = detail.get("properties")
            if isinstance(properties, str):
                try:
                    properties = json.loads(properties)
                except json.JSONDecodeError:
                    properties = None
            detail["cdf_enabled"] = (properties.get("delta.enableChangeDataFeed")
                                     if isinstance(properties, Mapping) else None)
            cdf: dict[str, Any] = {}
            if start_version is not None or end_version is not None:
                if type(start_version) is not int or type(end_version) is not int or start_version < 0 or end_version < start_version:
                    raise ContractError("CDF versions must be bounded non-negative integers")
                # Aggregate projection is intentional: raw CDF records never cross the executor boundary.
                rows = self._sql(
                    f"SELECT _change_type, COUNT(*) AS change_count FROM table_changes('{cdf_name}', "
                    f"{start_version}, {end_version}) GROUP BY _change_type LIMIT 4"
                )
                if any(not {"_change_type", "change_count"} <= set(row) for row in rows):
                    raise ContractError("CDF aggregate schema drift")
                cdf = {"readable_from": start_version, "readable_to": end_version,
                       "change_counts": {str(row["_change_type"]): row["change_count"] for row in rows}}
            return delta_changes(detail, history=history, cdf_summary=cdf)
        except Exception as exc:
            return _error_result("delta_changes", exc, "sql")

    def collect_run(self, run_id: int) -> CollectorResult:
        try:
            if type(run_id) is not int or run_id < 0:
                raise ContractError("run_id must be a non-negative integer")
            response = self._api("run_get", {"run_id": run_id})
            required = {"run_id", "status"}
            if not required <= set(response):
                raise ContractError("run response schema drift: expected run_id and status")
            allowed = ("run_id", "status", "code_revision", "parameters", "runtime", "packages", "inputs",
                       "watermarks", "target_schema", "duration", "failure_metadata", "identity")
            facts, summary = redact({key: response[key] for key in allowed if key in response})
            return CollectorResult(collector="run_context", status="collected",
                                   source={"provider": "databricks", "channel": "jobs_api"},
                                   facts={"run": normalize_json(facts)}, redaction=summary)
        except Exception as exc:
            return _error_result("run_context", exc, "jobs_api")

    def collect_environment(self) -> CollectorResult:
        try:
            response = self._api("environment", {})
            allowed = ("runtime", "execution_mode", "identity", "workspace_id", "packages")
            facts, summary = redact({key: response[key] for key in allowed if key in response})
            return CollectorResult(collector="databricks_environment", status="collected",
                                   source={"provider": "databricks", "channel": "workspace_api"},
                                   environment={"execution_mode": facts.get("execution_mode", "unknown")},
                                   facts=normalize_json(facts), redaction=summary)
        except Exception as exc:
            return _error_result("databricks_environment", exc, "workspace_api")

    spark = collect_spark
    uc = collect_uc
    delta = collect_delta
    run = collect_run
    environment = collect_environment


DatabricksEvidenceAdapter = DatabricksOperationalAdapter
