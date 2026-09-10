"""Provider-neutral operational evidence primitives."""

from ._artifacts import Artifact, persist_artifact, read_artifact
from ._contract import (
    CapabilityRequest,
    CollectionAttempt,
    CollectorResult,
    ContractError,
    EvidenceClaim,
    EvidenceIntake,
    ProviderDeclaration,
    canonical_json,
    normalize_json,
    utc_now,
)
from ._databricks import (
    DatabricksEvidenceAdapter,
    DatabricksGitFolderRepositoryProvider,
    DatabricksOperationalAdapter,
    autoconfigure_databricks_git_folder_repository,
    databricks_workspace_path_from_checkout,
    quote_sql_identifier,
)
from ._delta import delta_changes
from ._environment import capture_environment, compare_environments
from ._git_snapshot import LOCAL_GIT_SNAPSHOT_PROVIDER, capture_local_git_snapshot_evidence
from ._incident import incident_snapshot, render_incident_markdown
from ._redaction import REDACTED, redact
from ._render import render_operational_markdown
from ._runs import run_diff
from ._spark import spark_diagnose
from ._tables import (
    observe_table,
    render_table_observation_markdown,
    render_table_trend_markdown,
    table_trend,
)
from ._uc import uc_context

__all__ = [
    "LOCAL_GIT_SNAPSHOT_PROVIDER",
    "REDACTED",
    "Artifact",
    "CapabilityRequest",
    "CollectionAttempt",
    "CollectorResult",
    "ContractError",
    "DatabricksEvidenceAdapter",
    "DatabricksGitFolderRepositoryProvider",
    "DatabricksOperationalAdapter",
    "EvidenceClaim",
    "EvidenceIntake",
    "ProviderDeclaration",
    "autoconfigure_databricks_git_folder_repository",
    "canonical_json",
    "capture_environment",
    "capture_local_git_snapshot_evidence",
    "compare_environments",
    "databricks_workspace_path_from_checkout",
    "delta_changes",
    "incident_snapshot",
    "normalize_json",
    "observe_table",
    "persist_artifact",
    "quote_sql_identifier",
    "read_artifact",
    "redact",
    "render_incident_markdown",
    "render_operational_markdown",
    "render_table_observation_markdown",
    "render_table_trend_markdown",
    "run_diff",
    "spark_diagnose",
    "table_trend",
    "uc_context",
    "utc_now",
]
