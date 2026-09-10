"""PR config, readiness, and atomic-rendering coverage with real Git evidence."""

from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from odibi_anchor._pr_readiness import (
    PR_CONFIG_DEFAULTS,
    PRReadinessResult,
    evaluate_pr_readiness,
    load_pr_config,
    pr_checks_to_evidence,
    render_pr_draft,
)
from odibi_anchor._repository_snapshot import capture_repository_snapshot
from odibi_anchor.planning._task_policy import EvidenceEntry


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def changed_repository(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "tests@example.invalid")
    git(tmp_path, "config", "user.name", "Tests")
    (tmp_path / "m.py").write_text('"""Module."""\n\ndef f():\n    """Function."""\n', encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "base")
    git(tmp_path, "switch", "-c", "feature")
    (tmp_path / "m.py").write_text('"""Module."""\n\ndef f():\n    """Function."""\n    return 1\n', encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "feature")
    return tmp_path


def test_duplicate_config_keys_and_tightening_rules(tmp_path: Path) -> None:
    directory = tmp_path / ".odibi-anchor"
    directory.mkdir()
    path = directory / "pr.json"
    path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_pr_config(tmp_path)
    path.unlink()
    assert load_pr_config(tmp_path, {"max_changed_line_length": 88})["max_changed_line_length"] == 88
    with pytest.raises(ValueError, match="title_prefixes"):
        load_pr_config(tmp_path, {"title_prefixes": ["unsafe"]})


def test_objective_checks_and_committed_path(changed_repository: Path) -> None:
    snapshot = capture_repository_snapshot(changed_repository, "main")
    result = evaluate_pr_readiness(snapshot, PR_CONFIG_DEFAULTS, intended_pr_paths=("m.py",))
    statuses = {check.id: check.status for check in result.checks}
    assert statuses["snapshot.committed"] == "pass"
    assert statuses["python.parse:m.py"] == "pass"
    assert statuses["subjective.docstring-usefulness:m.py"] == "unknown"
    assert result.scope == "local" and not any(result.external_actions.values())
    ledger_ids = {item.id for item in pr_checks_to_evidence(result)}
    for check in result.checks:
        for evidence_id in check.evidence_ids:
            assert evidence_id in ledger_ids


def test_repository_ruff_line_length_is_deterministic_and_blocking(changed_repository: Path) -> None:
    (changed_repository / "pyproject.toml").write_text(
        "[tool.ruff]\nline-length = 40\n", encoding="utf-8",
    )
    (changed_repository / "m.py").write_text(
        'def f():\n    return "this repository-configured line is deliberately longer than forty"\n',
        encoding="utf-8",
    )
    git(changed_repository, "add", ".")
    git(changed_repository, "commit", "-m", "configure style")

    result = evaluate_pr_readiness(
        capture_repository_snapshot(changed_repository, "main"),
        PR_CONFIG_DEFAULTS,
        intended_pr_paths=("m.py", "pyproject.toml"),
    )
    check = next(item for item in result.checks if item.id == "python.line-length:m.py")

    assert check.status == "fail"
    assert check.provenance["limit"] == 40
    assert check.provenance["source"] == "pyproject.toml:[tool.ruff].line-length"
    assert result.ready is False


def test_unconfigured_style_and_non_google_docstring_are_advisory(tmp_path: Path) -> None:
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "tests@example.invalid")
    git(tmp_path, "config", "user.name", "Tests")
    (tmp_path / "m.py").write_text("def f(value):\n    return value\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "base")
    git(tmp_path, "switch", "-c", "feature")
    (tmp_path / "m.py").write_text(
        'def f(value):\n    """Return the value without requiring Google sections."""\n'
        '    return value + "a deliberately long but unconfigured line"\n',
        encoding="utf-8",
    )
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "feature")

    result = evaluate_pr_readiness(
        capture_repository_snapshot(tmp_path, "main"),
        PR_CONFIG_DEFAULTS,
        intended_pr_paths=("m.py",),
    )
    statuses = {check.id: check.status for check in result.checks}

    assert statuses["subjective.docstring-usefulness:m.py"] == "unknown"
    assert statuses["subjective.line-length:m.py"] == "unknown"
    assert not any("docstring-contract" in identifier for identifier in statuses)
    assert result.ready is True


def test_deny_globs_remain_non_overridable(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="deny_globs"):
        load_pr_config(tmp_path, {"deny_globs": ["*.env"]})


def test_uncommitted_and_denied_paths_block(changed_repository: Path) -> None:
    (changed_repository / ".env").write_text("SECRET=x", encoding="utf-8")
    result = evaluate_pr_readiness(capture_repository_snapshot(changed_repository, "main"),
                                   PR_CONFIG_DEFAULTS)
    statuses = {check.id: check.status for check in result.checks}
    assert statuses["snapshot.committed"] == "fail"
    assert statuses["paths.denied"] == "fail"
    assert result.ready is False


def test_renderer_preserves_bytes_and_rejects_bad_markers(changed_repository: Path) -> None:
    snapshot = capture_repository_snapshot(changed_repository, "main")
    result = PRReadinessResult(False, ())
    digest = hashlib.sha256(b"feature").hexdigest()[:12]
    destination = changed_repository / "pull_requests" / f"feature-{digest}" / "PR_DRAFT.md"
    destination.parent.mkdir(parents=True)
    prefix = b"human prefix\r\n"
    destination.write_bytes(prefix)
    render_pr_draft(changed_repository, snapshot, result)
    assert destination.read_bytes().startswith(prefix)
    prior = destination.read_bytes()
    destination.write_text("<!-- odibi-anchor:generated:start -->\nbroken", encoding="utf-8")
    malformed = destination.read_bytes()
    with pytest.raises(ValueError, match="malformed"):
        render_pr_draft(changed_repository, snapshot, result)
    assert destination.read_bytes() == malformed
    assert prior != malformed


def test_renderer_title_narrative_and_external_flags(changed_repository: Path) -> None:
    snapshot = capture_repository_snapshot(changed_repository, "main")
    result = evaluate_pr_readiness(snapshot, PR_CONFIG_DEFAULTS, intended_pr_paths=("m.py",))
    with pytest.raises(ValueError, match="allowed prefix"):
        render_pr_draft(changed_repository, snapshot, result, title="bad title")
    path = render_pr_draft(changed_repository, snapshot, result,
                           narratives={"Summary": {"text": "unsupported", "evidence_ids": ["missing"]}})
    text = path.read_text(encoding="utf-8")
    assert "Unknown — unsupported" in text
    assert "fetched: false" in text and "PR created: false" in text


def test_python_deletion_is_verified_from_base_not_reported_as_parse_failure(changed_repository: Path) -> None:
    (changed_repository / "m.py").unlink()
    git(changed_repository, "add", "m.py")
    git(changed_repository, "commit", "-m", "delete module")
    result = evaluate_pr_readiness(
        capture_repository_snapshot(changed_repository, "main"), PR_CONFIG_DEFAULTS,
        intended_pr_paths=("m.py",),
    )
    statuses = {check.id: check.status for check in result.checks}
    assert statuses["python.deleted:m.py"] == "pass"
    assert "python.parse:m.py" not in statuses


def test_post_persist_failure_atomically_restores_existing_draft(changed_repository: Path) -> None:
    snapshot = capture_repository_snapshot(changed_repository, "main")
    result = PRReadinessResult(False, ())
    path = render_pr_draft(changed_repository, snapshot, result)
    prior = path.read_bytes()

    def reject(_path: Path, _sha256: str) -> None:
        raise RuntimeError("ledger rejected")

    with pytest.raises(RuntimeError, match="ledger rejected"):
        render_pr_draft(changed_repository, snapshot, result, post_persist=reject)
    assert path.read_bytes() == prior


def test_attested_work_item_link_is_rendered_but_local_draft_is_not(changed_repository: Path) -> None:
    snapshot = capture_repository_snapshot(changed_repository, "main")
    without_receipt = evaluate_pr_readiness(snapshot, PR_CONFIG_DEFAULTS)
    local_check = next(check for check in without_receipt.checks if check.id == "work-item")
    assert local_check.status == "na"
    assert local_check.evidence_ids == ()

    attestation = EvidenceEntry(
        "work-item:WI-2026-0001:asana:123", "work-item", "pass",
        "https://app.asana.test/0/123", datetime.now(timezone.utc).isoformat(),
        {"attestor": "owner", "work_item_id": "WI-2026-0001", "provider": "asana",
         "provider_id": "123", "provider_url": "https://app.asana.test/0/123",
         "fingerprint": "sha256:" + "a" * 64, "operations": ["create_tasks"]},
    )
    with_receipt = evaluate_pr_readiness(snapshot, PR_CONFIG_DEFAULTS, attestations=(attestation,))
    linked_check = next(check for check in with_receipt.checks if check.id == "work-item")
    assert linked_check.status == "pass"
    path = render_pr_draft(changed_repository, snapshot, with_receipt)
    text = path.read_text(encoding="utf-8")
    assert "[WI-2026-0001](https://app.asana.test/0/123) on asana" in text
    assert attestation.id in text


@pytest.mark.parametrize("provenance,source", [
    ({"attestor": "owner"}, "https://app.asana.test/123"),
    ({"attestor": "owner", "work_item_id": "WI-2026-0001", "provider": "asana",
      "provider_id": "123", "provider_url": "not-a-url"}, "not-a-url"),
    ({"attestor": "owner", "work_item_id": "WI-2026-0001", "provider": "asana",
      "provider_id": "123", "provider_url": "https://app.asana.test/123"},
     "https://different.test/123"),
    ({"attestor": "owner", "work_item_id": "WI-2026-0001", "provider": "asana",
      "provider_id": "123", "provider_url": "http://app.asana.test/123",
      "fingerprint": "sha256:" + "a" * 64, "operations": ["create_tasks"]},
     "http://app.asana.test/123"),
    ({"attestor": "owner", "work_item_id": "WI-2026-0001", "provider": "asana",
      "provider_id": "123", "provider_url": "https://app.asana.test/123",
      "fingerprint": "not-a-fingerprint", "operations": ["create_tasks"]},
     "https://app.asana.test/123"),
    ({"attestor": "owner", "work_item_id": "WI-2026-0001", "provider": "asana",
      "provider_id": "123", "provider_url": "https://app.asana.test/123",
      "fingerprint": "sha256:" + "a" * 64, "operations": ["delete_tasks"]},
     "https://app.asana.test/123"),
])
def test_incomplete_work_item_attestation_does_not_satisfy_readiness(
    changed_repository: Path, provenance: dict, source: str,
) -> None:
    entry = EvidenceEntry(
        "candidate", "work-item", "pass", source,
        datetime.now(timezone.utc).isoformat(), provenance,
    )
    result = evaluate_pr_readiness(
        capture_repository_snapshot(changed_repository, "main"),
        {**PR_CONFIG_DEFAULTS, "work_item_required": True}, attestations=(entry,),
    )
    check = next(item for item in result.checks if item.id == "work-item")
    assert check.status == "unknown"
    assert check.evidence_ids == ()
