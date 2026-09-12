"""Durability and fail-closed tests for accepted task authority."""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from odibi_anchor._repository_snapshot import capture_task_repository_baseline
from odibi_anchor.codebase._adopted_dirty import (
    AdoptionUnavailable,
    inspect_adoptions,
    prepare_adoption,
    record_adoption_refusal,
    record_touched_path,
    request_adoption_approval,
    withdraw_adoption,
)
from odibi_anchor.codebase._task_authority import (
    TaskAuthorityUnavailable,
    close_accepted_task,
    inspect_task_authority,
    persist_accepted_task,
    rebind_latest_open_task,
)
from odibi_anchor.human_input import HumanInputReply
from odibi_anchor.planning._task_policy import BpsKernel, EvidenceEntry
from odibi_anchor.planning._task_profile import normalize_task_profile


def state(tmp_path, *, project="project-a", target=None):
    target = target or tmp_path / "target"
    target.mkdir(exist_ok=True)
    if not (target / ".git").exists():
        def git(*args):
            subprocess.run(
                ["git", *args], cwd=target, check=True, capture_output=True,
                text=True, encoding="utf-8",
            )
        git("init", "-b", "main")
        git("config", "user.email", "tests@example.invalid")
        git("config", "user.name", "Tests")
        git("config", "commit.gpgsign", "false")
        (target / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
        git("add", ".")
        git("commit", "-m", "base")
    artifact = tmp_path / "artifacts"
    artifact.mkdir(exist_ok=True)
    profile = normalize_task_profile(
        work_type="change", execution_mode="source_change", risk="high", rigor="full",
    )
    return SimpleNamespace(
        task_window_id="ltw_durable", session_id="session-a", active_project=project,
        anchor_home=str(tmp_path / "anchor"), project_root=str(artifact), artifact_root=str(artifact),
        target_root=str(target), repository_provider=None, active_task_mode="implementation",
        task_goal="Preserve accepted authority", task_tags=["durability"],
        active_task_profile=profile, active_assurance_plan=None,
        bps_kernel=BpsKernel("Lost process state", "Restart-safe continuation"),
        referenced_facts=("The task was accepted.",), linked_problem="PRB-2026-0019",
        linked_spec="DURABLE_TASK_AUTHORITY_AND_ADOPTED_DIRTY_SPEC",
        linked_work_item="WI-2026-0021", persisted_spec_name="DURABLE_TASK_AUTHORITY_AND_ADOPTED_DIRTY_SPEC",
        active_spec={"status": "ready"}, explicit_problem_requested=True,
        explicit_spec_requested=True, explicit_pr_draft_requested=None, phase_count=3,
        current_phase=1,
        task_handoff_context={
            "intent": {"goal": "Preserve accepted authority"},
            "scope": {"in_scope": ["src"]},
            "verification": {"acceptance_criteria": ["Restart restores context."]},
        },
        task_repository_baseline=capture_task_repository_baseline(target, "main"),
        task_repository_write_fingerprints={},
        evidence_ledger=[EvidenceEntry(
            "E1", "observation", "pass", "test", "2026-09-03T00:00:00+00:00", {},
        )], managed_artifact_ledger=[], guidance_attestations=[], observed_effects=[],
        intended_pr_paths=(), task_verification_epoch=7, learning_obligation_id=None,
        latest_closed_obligation_id=None, terminal_status=None, terminal_basis=None,
        terminal_reason=None, active_problem="PRB-2026-0019", spec_persisted=True,
        reviewed_spec_name=None, spec_review_rating=None, trust_domain=None,
    )


def result():
    return {
        "acceptance_criteria": ["Restart restores the same baseline."],
        "required_skills": [{"skill": "writing-tests", "status": "required"}],
    }


def fresh_state(original, **changes):
    values = vars(original).copy()
    values.update(
        task_window_id="ltw_new", session_id="session-b", active_task_profile=None,
        active_assurance_plan=None, bps_kernel=None, task_repository_baseline=None,
        evidence_ledger=[], managed_artifact_ledger=[], guidance_attestations=[],
        observed_effects=[], intended_pr_paths=(), task_repository_write_fingerprints={},
        active_problem=None, linked_problem=None, linked_spec=None, linked_work_item=None,
    )
    values.update(changes)
    return SimpleNamespace(**values)


def test_persist_rebind_and_repeat_are_integrity_checked_and_idempotent(tmp_path):
    db = tmp_path / "memory.db"
    original = state(tmp_path)
    accepted = persist_accepted_task(
        db, session_state=original, task_stage={"repository_scope": ("src",)},
        task_result=result(),
    )

    restarted = fresh_state(original)
    first = rebind_latest_open_task(db, session_state=restarted)
    second = rebind_latest_open_task(db, session_state=restarted)

    assert first["task_window_id"] == accepted["task_window_id"] == "ltw_durable"
    assert first["event_created"] is True
    assert second["event_created"] is False
    assert restarted.active_task_profile.to_dict() == original.active_task_profile.to_dict()
    assert vars(restarted.task_repository_baseline) == vars(original.task_repository_baseline)
    assert [item.to_dict() for item in restarted.evidence_ledger] == [
        item.to_dict() for item in original.evidence_ledger
    ]
    assert restarted.linked_work_item == "WI-2026-0021"
    assert restarted.task_handoff_context == original.task_handoff_context
    assert restarted.task_verification_epoch == 0
    assert first["obligations"] == {
        "acceptance_criteria": ["Restart restores the same baseline."],
        "required_skills": [{"skill": "writing-tests", "status": "required"}],
        "assurance": None,
    }
    assert inspect_task_authority(db) == {
        "schema_status": "ready", "durable_windows": 1, "rebindings": 1, "closures": 0,
    }


def test_repeat_persistence_is_idempotent(tmp_path):
    db = tmp_path / "memory.db"
    original = state(tmp_path)

    first = persist_accepted_task(db, session_state=original, task_stage={}, task_result=result())
    second = persist_accepted_task(db, session_state=original, task_stage={}, task_result=result())

    assert second == first
    assert inspect_task_authority(db)["durable_windows"] == 1


def test_closed_or_wrong_identity_authority_is_unavailable(tmp_path):
    db = tmp_path / "memory.db"
    original = state(tmp_path)
    persist_accepted_task(db, session_state=original, task_stage={}, task_result=result())

    with pytest.raises(TaskAuthorityUnavailable, match="no open accepted task"):
        rebind_latest_open_task(db, session_state=fresh_state(original, active_project="other"))

    close_accepted_task(db, task_window_id="ltw_durable", terminal_status="completed")
    with pytest.raises(TaskAuthorityUnavailable, match="no open accepted task"):
        rebind_latest_open_task(db, session_state=fresh_state(original))
    assert inspect_task_authority(db)["closures"] == 1


def test_rebind_fails_closed_when_multiple_exact_open_tasks_match(tmp_path):
    db = tmp_path / "memory.db"
    first = state(tmp_path)
    persist_accepted_task(db, session_state=first, task_stage={}, task_result=result())
    second = state(tmp_path)
    second.task_window_id = "ltw_second"
    second.session_id = "session-second"
    persist_accepted_task(db, session_state=second, task_stage={}, task_result=result())

    restarted = fresh_state(first)
    with pytest.raises(TaskAuthorityUnavailable, match="multiple open accepted tasks") as exc:
        rebind_latest_open_task(db, session_state=restarted)

    assert "ltw_durable" in str(exc.value)
    assert "ltw_second" in str(exc.value)
    assert restarted.task_window_id == "ltw_new"


def test_rebind_requires_exact_trust_domain(tmp_path):
    db = tmp_path / "memory.db"
    original = state(tmp_path)
    persist_accepted_task(
        db, session_state=original,
        task_stage={"trust_domain": "private-a"}, task_result=result(),
    )

    restarted = fresh_state(original, trust_domain="private-b")
    with pytest.raises(TaskAuthorityUnavailable, match="exact project"):
        rebind_latest_open_task(db, session_state=restarted)

    restarted.trust_domain = "private-a"
    assert rebind_latest_open_task(db, session_state=restarted)["status"] == "rebound"


def test_repeat_closure_is_idempotent_but_conflicting_status_fails(tmp_path):
    db = tmp_path / "memory.db"
    original = state(tmp_path)
    persist_accepted_task(db, session_state=original, task_stage={}, task_result=result())

    first = close_accepted_task(db, task_window_id="ltw_durable", terminal_status="blocked")
    second = close_accepted_task(db, task_window_id="ltw_durable", terminal_status="blocked")

    assert first["created"] is True
    assert second["created"] is False
    with pytest.raises(RuntimeError, match="event idempotency conflict"):
        close_accepted_task(db, task_window_id="ltw_durable", terminal_status="completed")


def test_databricks_baseline_rebind_preserves_record_and_preimage(tmp_path):
    snapshots = importlib.import_module("odibi_anchor._repository_snapshot")

    class Provider:
        provider_id = "test.databricks"

        def __init__(self, identity):
            self.identity = identity

        def capture_identity(self, _target):
            return self.identity

    db = tmp_path / "memory.db"
    original = state(tmp_path)
    identity = snapshots.DatabricksGitFolderIdentity(
        repository_id="42", workspace_path="/Repos/odibi_anchor", branch="main",
        head_sha="a" * 40, remote_url="https://example.invalid/repository.git",
        git_provider="gitHub",
    )
    provider = Provider(identity)
    preimage = b"VALUE = 1\r\n\x00binary"
    original.repository_provider = provider
    original.task_repository_baseline = snapshots.DatabricksGitFolderTaskBaseline(
        target_worktree=original.target_root, repository_scope=("source.py",),
        directory_scopes=(), identity=identity,
        preimages=(snapshots.ScopedFilePreimage("source.py", hashlib.sha256(preimage).hexdigest(), len(preimage), preimage),),
        captured_at="2026-09-03T00:00:00Z", identity_provider=provider,
    )
    accepted = persist_accepted_task(db, session_state=original, task_stage={}, task_result=result())
    restarted = fresh_state(original, repository_provider=provider)

    first = rebind_latest_open_task(db, session_state=restarted)
    second = rebind_latest_open_task(db, session_state=restarted)

    assert first["event_created"] is True
    assert second["event_created"] is False
    assert restarted.task_repository_baseline.preimages[0].content == preimage
    connection = sqlite3.connect(db)
    stored_digest = connection.execute(
        "SELECT record_sha256 FROM accepted_task_records WHERE task_window_id='ltw_durable'",
    ).fetchone()[0]
    connection.close()
    assert stored_digest == accepted["record_sha256"]


def test_incomplete_or_tampered_authority_fails_closed_without_restoring(tmp_path):
    db = tmp_path / "memory.db"
    original = state(tmp_path)
    persist_accepted_task(db, session_state=original, task_stage={}, task_result=result())
    connection = sqlite3.connect(db)
    connection.execute("DROP TRIGGER accepted_task_records_no_update")
    raw = connection.execute(
        "SELECT record_json FROM accepted_task_records WHERE task_window_id='ltw_durable'",
    ).fetchone()[0]
    payload = json.loads(raw)
    payload["obligations"].pop("acceptance_criteria")
    connection.execute(
        "UPDATE accepted_task_records SET record_json=? WHERE task_window_id='ltw_durable'",
        (json.dumps(payload, sort_keys=True, separators=(",", ":")),),
    )
    connection.commit()
    connection.close()

    restarted = fresh_state(original)
    with pytest.raises(TaskAuthorityUnavailable, match="storage is inconsistent"):
        rebind_latest_open_task(db, session_state=restarted)
    assert restarted.task_window_id == "ltw_new"
    assert restarted.active_task_profile is None


def test_absent_authority_is_unavailable_and_does_not_initialize_storage(tmp_path):
    db = tmp_path / "absent.db"
    with pytest.raises(TaskAuthorityUnavailable, match="unavailable"):
        rebind_latest_open_task(db, session_state=state(tmp_path))
    assert not db.exists()


def test_schema_rejects_record_and_event_mutation(tmp_path):
    db = tmp_path / "memory.db"
    original = state(tmp_path)
    persist_accepted_task(db, session_state=original, task_stage={}, task_result=result())
    close_accepted_task(db, task_window_id="ltw_durable", terminal_status="blocked")
    connection = sqlite3.connect(db)
    with pytest.raises(sqlite3.IntegrityError, match="records are immutable"):
        connection.execute("UPDATE accepted_task_records SET project_id='other'")
    with pytest.raises(sqlite3.IntegrityError, match="events are immutable"):
        connection.execute("DELETE FROM accepted_task_events")
    connection.close()


def test_event_timestamp_tampering_fails_integrity_check(tmp_path):
    db = tmp_path / "memory.db"
    original = state(tmp_path)
    persist_accepted_task(db, session_state=original, task_stage={}, task_result=result())
    close_accepted_task(db, task_window_id="ltw_durable", terminal_status="blocked")
    connection = sqlite3.connect(db)
    trigger_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name='accepted_task_events_no_update'",
    ).fetchone()[0]
    connection.execute("DROP TRIGGER accepted_task_events_no_update")
    connection.execute("UPDATE accepted_task_events SET created_at='tampered'")
    connection.execute(trigger_sql)
    connection.commit()
    connection.close()

    with pytest.raises(TaskAuthorityUnavailable, match="event integrity mismatch"):
        inspect_task_authority(db)


def test_replacement_atomically_supersedes_prior_open_window(tmp_path):
    db = tmp_path / "memory.db"
    original = state(tmp_path)
    persist_accepted_task(db, session_state=original, task_stage={}, task_result=result())
    replacement = fresh_state(original, task_window_id="ltw_replacement")
    replacement.active_task_profile = original.active_task_profile
    replacement.bps_kernel = original.bps_kernel

    accepted = persist_accepted_task(
        db, session_state=replacement, task_stage={}, task_result=result(),
        supersede_task_window_id="ltw_durable",
    )
    rebound = rebind_latest_open_task(db, session_state=fresh_state(original))

    assert accepted["superseded_task_window_id"] == "ltw_durable"
    assert rebound["task_window_id"] == "ltw_replacement"
    assert inspect_task_authority(db) == {
        "schema_status": "ready", "durable_windows": 2, "rebindings": 1, "closures": 1,
    }


class ApprovalTransport:
    name = "test-authenticated-owner"

    def __init__(self, *, owner="owner-1"):
        self.owner = owner
        self.request = None

    def deliver(self, request):
        self.request = request
        return "private-transport-reference"

    def replies(self, _transport_ref):
        challenge = re.search(r"APPROVE [0-9a-f]{64}", self.request.message).group(0)
        return [HumanInputReply(challenge, self.owner, "message-1", self.request.created_at + 1)]


def _approved_dirty_task(tmp_path, *, repository_scope=("source.py",)):
    db = tmp_path / "memory.db"
    original = state(tmp_path)
    persist_accepted_task(
        db, session_state=original,
        task_stage={"repository_scope": repository_scope, "trust_domain": "private"},
        task_result=result(),
    )
    source = Path(original.target_root) / "source.py"
    source.write_text("VALUE = 2\n", encoding="utf-8")
    approval = request_adoption_approval(
        db, prior_task_window_id="ltw_durable", project_id="project-a",
        target_root=original.target_root, artifact_root=original.artifact_root,
        trust_domain="private", expected_owner_id="owner-1",
        state_path=tmp_path / "human.sqlite3", transport=ApprovalTransport(),
        timeout_minutes=1,
    )
    prepared = prepare_adoption(
        db, approval_id=approval["approval_id"], project_id="project-a",
        target_root=original.target_root, artifact_root=original.artifact_root,
        trust_domain="private",
    )
    return db, original, approval, prepared


def test_authenticated_approval_is_consumed_atomically_by_adopted_task(tmp_path):
    db, original, approval, prepared = _approved_dirty_task(tmp_path)
    replacement = fresh_state(original, task_window_id="ltw_adopted")
    replacement.active_task_profile = original.active_task_profile
    replacement.bps_kernel = original.bps_kernel
    replacement.task_repository_baseline = prepared["baseline"]

    accepted = persist_accepted_task(
        db, session_state=replacement,
        task_stage={"repository_scope": (), "trust_domain": "private"},
        task_result=result(), supersede_task_window_id="ltw_durable",
        prepared_adoption=prepared,
    )

    assert accepted["adoption"]["approval_id"] == approval["approval_id"]
    assert accepted["superseded_task_window_id"] == "ltw_durable"
    assert replacement.task_repository_baseline.authority_kind == "adopted"
    assert inspect_adoptions(db) == {
        "schema_status": "ready", "approvals": 1, "adoptions": 1,
        "active_adoptions": 1, "refused_adoptions": 0,
        "refusals_by_precondition": {}, "withdrawals": 0, "touched_paths": 0,
    }
    with pytest.raises(AdoptionUnavailable, match="no replay"):
        prepare_adoption(
            db, approval_id=approval["approval_id"], project_id="project-a",
            target_root=original.target_root, artifact_root=original.artifact_root,
            trust_domain="private",
        )


def test_adoption_drift_rolls_back_new_authority_and_consumption(tmp_path):
    current_adoption = importlib.import_module("odibi_anchor.codebase._adopted_dirty")
    db, original, _, prepared = _approved_dirty_task(tmp_path)
    source = Path(original.target_root) / "source.py"
    source.write_text("VALUE = 3\n", encoding="utf-8")
    replacement = fresh_state(original, task_window_id="ltw_adopted")
    replacement.active_task_profile = original.active_task_profile
    replacement.bps_kernel = original.bps_kernel
    replacement.task_repository_baseline = prepared["baseline"]

    with pytest.raises(current_adoption.AdoptionUnavailable, match="drift after fingerprinting"):
        persist_accepted_task(
            db, session_state=replacement,
            task_stage={"repository_scope": (), "trust_domain": "private"},
            task_result=result(), supersede_task_window_id="ltw_durable",
            prepared_adoption=prepared,
        )

    assert inspect_task_authority(db)["durable_windows"] == 1
    diagnostics = inspect_adoptions(db)
    assert diagnostics["adoptions"] == 0
    assert diagnostics["refused_adoptions"] == 1
    assert diagnostics["refusals_by_precondition"] == {"drift after fingerprinting": 1}


def test_adoption_rejects_wrong_domain_owner_and_out_of_scope_path(tmp_path):
    db = tmp_path / "memory.db"
    original = state(tmp_path)
    persist_accepted_task(
        db, session_state=original,
        task_stage={"repository_scope": ("source.py",), "trust_domain": "private"},
        task_result=result(),
    )
    (Path(original.target_root) / "outside.py").write_text("VALUE = 2\n", encoding="utf-8")

    for kwargs, message in (
        ({"project_id": "other", "trust_domain": "private"}, "same domain"),
        ({"project_id": "project-a", "trust_domain": "other"}, "same domain"),
        ({"project_id": "project-a", "trust_domain": "private"}, "every path in prior scope"),
    ):
        with pytest.raises(AdoptionUnavailable, match=message):
            request_adoption_approval(
                db, prior_task_window_id="ltw_durable",
                target_root=original.target_root, artifact_root=original.artifact_root,
                expected_owner_id="owner-1", state_path=tmp_path / "human.sqlite3",
                transport=ApprovalTransport(), timeout_minutes=1, **kwargs,
            )
    assert inspect_adoptions(db)["approvals"] == 0


def test_adoption_rejects_different_repository_and_missing_prior_authority(tmp_path):
    db = tmp_path / "memory.db"
    original = state(tmp_path)
    persist_accepted_task(
        db, session_state=original,
        task_stage={"repository_scope": ("source.py",), "trust_domain": "private"},
        task_result=result(),
    )
    (Path(original.target_root) / "source.py").write_text("VALUE = 2\n", encoding="utf-8")
    different = tmp_path / "different-repository"
    different.mkdir()

    with pytest.raises(AdoptionUnavailable, match="same domain"):
        request_adoption_approval(
            db, prior_task_window_id="ltw_durable", project_id="project-a",
            target_root=different, artifact_root=original.artifact_root,
            trust_domain="private", expected_owner_id="owner-1",
            state_path=tmp_path / "human.sqlite3", transport=ApprovalTransport(),
            timeout_minutes=1,
        )
    with pytest.raises(AdoptionUnavailable, match="recoverable prior authority"):
        request_adoption_approval(
            db, prior_task_window_id="ltw-missing", project_id="project-a",
            target_root=original.target_root, artifact_root=original.artifact_root,
            trust_domain="private", expected_owner_id="owner-1",
            state_path=tmp_path / "human.sqlite3", transport=ApprovalTransport(),
            timeout_minutes=1,
        )

    assert inspect_adoptions(db)["approvals"] == 0


def test_durable_touched_path_can_authorize_scope(tmp_path):
    db = tmp_path / "memory.db"
    original = state(tmp_path)
    persist_accepted_task(
        db, session_state=original,
        task_stage={"repository_scope": (), "trust_domain": "private"},
        task_result=result(),
    )
    first = record_touched_path(db, task_window_id="ltw_durable", touched_path="extra.py")
    repeated = record_touched_path(db, task_window_id="ltw_durable", touched_path="extra.py")
    (Path(original.target_root) / "extra.py").write_text("VALUE = 2\n", encoding="utf-8")

    approval = request_adoption_approval(
        db, prior_task_window_id="ltw_durable", project_id="project-a",
        target_root=original.target_root, artifact_root=original.artifact_root,
        trust_domain="private", expected_owner_id="owner-1",
        state_path=tmp_path / "human.sqlite3", transport=ApprovalTransport(), timeout_minutes=1,
    )

    assert first["created"] is True
    assert repeated["created"] is False
    assert approval["created"] is True
    assert inspect_adoptions(db)["touched_paths"] == 1


def test_repository_root_scope_authorizes_nested_dirty_paths(tmp_path):
    db = tmp_path / "memory.db"
    original = state(tmp_path)
    persist_accepted_task(
        db, session_state=original,
        task_stage={"repository_scope": (".",), "trust_domain": "private"},
        task_result=result(),
    )
    nested = Path(original.target_root) / "nested" / "source.py"
    nested.parent.mkdir()
    nested.write_text("VALUE = 2\n", encoding="utf-8")

    approval = request_adoption_approval(
        db, prior_task_window_id="ltw_durable", project_id="project-a",
        target_root=original.target_root, artifact_root=original.artifact_root,
        trust_domain="private", expected_owner_id="owner-1",
        state_path=tmp_path / "human.sqlite3", transport=ApprovalTransport(), timeout_minutes=1,
    )

    assert approval["created"] is True


def test_authenticated_response_from_wrong_owner_cannot_create_approval(tmp_path):
    db = tmp_path / "memory.db"
    original = state(tmp_path)
    persist_accepted_task(
        db, session_state=original,
        task_stage={"repository_scope": ("source.py",), "trust_domain": "private"},
        task_result=result(),
    )
    (Path(original.target_root) / "source.py").write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(AdoptionUnavailable, match="explicit owner authority"):
        request_adoption_approval(
            db, prior_task_window_id="ltw_durable", project_id="project-a",
            target_root=original.target_root, artifact_root=original.artifact_root,
            trust_domain="private", expected_owner_id="owner-1",
            state_path=tmp_path / "human.sqlite3",
            transport=ApprovalTransport(owner="different-owner"), timeout_minutes=1,
        )

    assert inspect_adoptions(db)["approvals"] == 0


def test_index_only_drift_invalidates_approval(tmp_path):
    db, original, approval, _ = _approved_dirty_task(tmp_path)
    subprocess.run(
        ["git", "add", "source.py"], cwd=original.target_root, check=True,
        capture_output=True, text=True,
    )

    with pytest.raises(AdoptionUnavailable, match="exact worktree fingerprint"):
        prepare_adoption(
            db, approval_id=approval["approval_id"], project_id="project-a",
            target_root=original.target_root, artifact_root=original.artifact_root,
            trust_domain="private",
        )

    assert inspect_adoptions(db)["adoptions"] == 0


def test_kill_switch_blocks_only_adoption_without_mutating_evidence(tmp_path, monkeypatch):
    db, original, approval, _ = _approved_dirty_task(tmp_path)
    before = inspect_adoptions(db)
    monkeypatch.setenv("ANCHOR_DISABLE_ADOPTED_DIRTY", "1")

    with pytest.raises(AdoptionUnavailable, match="kill switch"):
        prepare_adoption(
            db, approval_id=approval["approval_id"], project_id="project-a",
            target_root=original.target_root, artifact_root=original.artifact_root,
            trust_domain="private",
        )
    ordinary = fresh_state(original, task_window_id="ltw_ordinary")
    ordinary.active_task_profile = original.active_task_profile
    ordinary.bps_kernel = original.bps_kernel
    ordinary.task_repository_baseline = original.task_repository_baseline
    persist_accepted_task(
        db, session_state=ordinary,
        task_stage={"repository_scope": ("source.py",), "trust_domain": "private"},
        task_result=result(),
    )

    assert inspect_adoptions(db) == before
    assert inspect_task_authority(db)["durable_windows"] == 2


def test_adoption_persistence_retry_is_idempotent(tmp_path):
    db, original, _, prepared = _approved_dirty_task(tmp_path)
    replacement = fresh_state(original, task_window_id="ltw_adopted")
    replacement.active_task_profile = original.active_task_profile
    replacement.bps_kernel = original.bps_kernel
    replacement.task_repository_baseline = prepared["baseline"]
    kwargs = {
        "session_state": replacement,
        "task_stage": {"repository_scope": (), "trust_domain": "private"},
        "task_result": result(),
        "supersede_task_window_id": "ltw_durable",
        "prepared_adoption": prepared,
    }

    first = persist_accepted_task(db, **kwargs)
    second = persist_accepted_task(db, **kwargs)

    assert first["adoption"] == second["adoption"]
    assert inspect_adoptions(db)["adoptions"] == 1
    assert inspect_task_authority(db)["durable_windows"] == 2


def test_adoption_transaction_fails_closed_under_database_contention(tmp_path):
    from odibi_anchor.codebase._sqlite_contention import PersistenceContentionError

    db, original, _, prepared = _approved_dirty_task(tmp_path)
    replacement = fresh_state(original, task_window_id="ltw_adopted")
    replacement.active_task_profile = original.active_task_profile
    replacement.bps_kernel = original.bps_kernel
    replacement.task_repository_baseline = prepared["baseline"]
    blocker = sqlite3.connect(db, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(PersistenceContentionError) as exc_info:
            persist_accepted_task(
                db, session_state=replacement,
                task_stage={"repository_scope": (), "trust_domain": "private"},
                task_result=result(), supersede_task_window_id="ltw_durable",
                prepared_adoption=prepared,
            )
    finally:
        blocker.rollback()
        blocker.close()

    assert exc_info.value.operation == "acquire_write"
    assert exc_info.value.timeout_ms == 5000
    assert isinstance(exc_info.value.__cause__, sqlite3.OperationalError)
    assert inspect_task_authority(db)["durable_windows"] == 1
    assert inspect_adoptions(db)["adoptions"] == 0
    accepted = persist_accepted_task(
        db, session_state=replacement,
        task_stage={"repository_scope": (), "trust_domain": "private"},
        task_result=result(), supersede_task_window_id="ltw_durable",
        prepared_adoption=prepared,
    )
    assert accepted["adoption"]["approval_id"] == prepared["approval_id"]


def test_adoption_records_are_immutable_and_diagnostics_reject_index_tampering(tmp_path):
    db, original, _, prepared = _approved_dirty_task(tmp_path)
    replacement = fresh_state(original, task_window_id="ltw_adopted")
    replacement.active_task_profile = original.active_task_profile
    replacement.bps_kernel = original.bps_kernel
    replacement.task_repository_baseline = prepared["baseline"]
    persist_accepted_task(
        db, session_state=replacement,
        task_stage={"repository_scope": (), "trust_domain": "private"},
        task_result=result(), supersede_task_window_id="ltw_durable",
        prepared_adoption=prepared,
    )
    with sqlite3.connect(db) as connection:
        for table in (
            "task_touched_paths", "dirty_adoption_approvals", "dirty_adoption_events",
        ):
            if table == "task_touched_paths":
                continue
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(f"DELETE FROM {table}")
        connection.execute("DROP TRIGGER dirty_adoption_events_no_update")
        connection.execute("UPDATE dirty_adoption_events SET created_at='tampered'")
        connection.execute(
            "CREATE TRIGGER dirty_adoption_events_no_update BEFORE UPDATE ON "
            "dirty_adoption_events BEGIN SELECT RAISE(ABORT,'dirty adoption events are immutable'); END"
        )

    with pytest.raises(AdoptionUnavailable, match="indexed column mismatch"):
        inspect_adoptions(db)


def test_adoption_schema_tampering_is_detected(tmp_path):
    db, _, _, _ = _approved_dirty_task(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TRIGGER dirty_adoption_approvals_no_delete")

    with pytest.raises(RuntimeError, match="schema checksum mismatch"):
        inspect_adoptions(db)


def test_refusal_diagnostics_and_withdrawal_are_durable_immutable_events(tmp_path):
    db, original, _, prepared = _approved_dirty_task(tmp_path)
    replacement = fresh_state(original, task_window_id="ltw_adopted")
    replacement.active_task_profile = original.active_task_profile
    replacement.bps_kernel = original.bps_kernel
    replacement.task_repository_baseline = prepared["baseline"]
    accepted = persist_accepted_task(
        db, session_state=replacement,
        task_stage={"repository_scope": (), "trust_domain": "private"},
        task_result=result(), supersede_task_window_id="ltw_durable",
        prepared_adoption=prepared,
    )
    refusal = record_adoption_refusal(
        db, AdoptionUnavailable("same domain", "project identity differs"),
        operation="request", prior_task_window_id="ltw-different",
    )
    withdrawal = withdraw_adoption(
        db, adoption_id=accepted["adoption"]["adoption_id"],
        reason="Owner withdrew authority", actor_ref="owner-1",
    )
    repeated = withdraw_adoption(
        db, adoption_id=accepted["adoption"]["adoption_id"],
        reason="Owner withdrew authority", actor_ref="owner-1",
    )

    assert withdrawal["created"] is True
    assert repeated["created"] is False
    assert repeated["withdrawal_id"] == withdrawal["withdrawal_id"]
    assert refusal["precondition"] == "same domain"
    diagnostics = inspect_adoptions(db)
    assert diagnostics["adoptions"] == 1
    assert diagnostics["active_adoptions"] == 0
    assert diagnostics["withdrawals"] == 1
    assert diagnostics["refused_adoptions"] == 1
    assert diagnostics["refusals_by_precondition"] == {"same domain": 1}
    with sqlite3.connect(db) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM dirty_adoption_refusal_events")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM dirty_adoption_withdrawal_events")
