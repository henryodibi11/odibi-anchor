import errno
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

import odibi_anchor._dispatcher._work_item as work_item_module
from odibi_anchor._dispatcher._post_dispatch import run_post_dispatch
from odibi_anchor._dispatcher._work_item import work_item_action
from odibi_anchor._utils._session_state import SessionState


def call(root, *args, **kwargs):
    return work_item_action(root, *args, output_format="dict", **kwargs)


@pytest.mark.parametrize("unsupported_errno", [errno.ENOSYS, errno.EPERM])
def test_create_falls_back_when_hard_links_are_unavailable(
    tmp_path, monkeypatch, unsupported_errno,
):
    def unsupported_link(*_args, **_kwargs):
        raise OSError(unsupported_errno, "hard links are unavailable")

    monkeypatch.setattr(work_item_module.os, "link", unsupported_link)

    created = call(tmp_path, "create", title="T", outcome="O")

    assert Path(created["artifact_path"]).is_file()
    assert not list((tmp_path / "work_items").glob("*.tmp"))


def test_create_propagates_unexpected_hard_link_error(tmp_path, monkeypatch):
    def failed_link(*_args, **_kwargs):
        raise OSError(errno.EIO, "storage failure")

    monkeypatch.setattr(work_item_module.os, "link", failed_link)

    with pytest.raises(OSError, match="storage failure"):
        call(tmp_path, "create", title="T", outcome="O")

    assert not list((tmp_path / "work_items").glob("*.tmp"))


def test_create_fallback_preserves_existing_destination(tmp_path, monkeypatch):
    created = call(tmp_path, "create", title="T", outcome="O")
    destination = Path(created["artifact_path"])
    record = work_item_module._parse(destination)
    destination.unlink()

    def colliding_link(_source, destination):
        Path(destination).write_text("existing\n", encoding="utf-8")
        raise OSError(errno.EPERM, "hard links are unavailable")

    monkeypatch.setattr(work_item_module.os, "link", colliding_link)

    with pytest.raises(FileExistsError, match="work item already exists"):
        work_item_module._write(destination, record, create_only=True)

    artifacts = list((tmp_path / "work_items").glob("WI-*.md"))
    assert len(artifacts) == 1
    assert artifacts[0].read_text(encoding="utf-8") == "existing\n"
    assert not list((tmp_path / "work_items").glob("*.tmp"))


@pytest.mark.parametrize("unsupported_errno", [errno.ENOSYS, errno.EPERM])
def test_create_fallback_preserves_dangling_destination_symlink(
    tmp_path, monkeypatch, unsupported_errno,
):
    created = call(tmp_path, "create", title="T", outcome="O")
    destination = Path(created["artifact_path"])
    record = work_item_module._parse(destination)
    destination.unlink()

    def unsupported_link(_source, target):
        Path(target).symlink_to("missing.md")
        raise OSError(unsupported_errno, "hard links are unavailable")

    monkeypatch.setattr(work_item_module.os, "link", unsupported_link)

    with pytest.raises(FileExistsError, match="work item already exists"):
        work_item_module._write(destination, record, create_only=True)

    assert destination.is_symlink()
    assert destination.readlink() == Path("missing.md")
    assert not list((tmp_path / "work_items").glob("*.tmp"))


def test_local_lifecycle_round_trip_and_markdown(tmp_path):
    assert call(tmp_path)["work_items"] == []
    created = call(
        tmp_path,
        "create",
        title="Ship outcome",
        outcome="Users benefit",
        context="why",
        acceptance_criteria=["It works"],
        publication_plan={"project": "Delivery"},
    )
    assert created["work_item_id"].startswith("WI-")
    assert created["write_performed"] is True
    path = Path(created["artifact_path"])
    assert path.parent == (tmp_path / "work_items").resolve()
    before = path.read_text(encoding="utf-8")
    shown = call(tmp_path, "show", created["work_item_id"])
    assert shown["fingerprint"] == created["fingerprint"]
    assert shown["record"]["acceptance_criteria"] == ["It works"]
    assert shown["implementation_disposition"] == "unknown"
    assert shown["reopening_triggers"] == []
    assert path.read_text(encoding="utf-8") == before
    assert call(tmp_path, "list")["count"] == 1
    rendered = work_item_action(tmp_path, "show", created["work_item_id"])
    assert "Ship outcome" in rendered
    assert "Implementation disposition:** unknown" in rendered


def test_material_fingerprint_and_close(tmp_path):
    item = call(tmp_path, "create", title="T", outcome="O")
    unchanged = call(tmp_path, "update", item["work_item_id"], context="")
    assert unchanged["write_performed"] is False
    updated = call(tmp_path, "update", item["work_item_id"], context="material")
    assert updated["fingerprint"] != item["fingerprint"]
    closed = call(
        tmp_path,
        "close",
        item["work_item_id"],
        outcome="completed",
        implementation_disposition="implemented",
    )
    assert closed["status"] == "completed" and closed["write_performed"]
    assert closed["fingerprint"] != updated["fingerprint"]


def test_approval_receipt_subset_attestation_and_consumption(tmp_path):
    item = call(tmp_path, "create", title="T", outcome="O")
    preview = call(
        tmp_path, "preview", item["work_item_id"], provider="asana", operations=["update_tasks", "create_tasks"]
    )
    assert preview["preview"]["fingerprint"] == item["fingerprint"]
    approved = call(
        tmp_path,
        "approve",
        item["work_item_id"],
        provider="asana",
        operations=["create_tasks", "update_tasks"],
        expected_fingerprint=item["fingerprint"],
        approver="owner",
        source="user message",
    )
    receipt = call(
        tmp_path,
        "record_publish",
        item["work_item_id"],
        approval_id=approved["approval_id"],
        provider="asana",
        provider_id="123",
        provider_url="https://app.asana.test/123",
        performed_operations=["create_tasks"],
        recorded_by="agent",
    )
    assert receipt["attestation"]["kind"] == "work-item"
    assert receipt["attestation"]["observed_at"].endswith("+00:00")
    assert receipt["attestation"]["provenance"]["approver"] == "owner"
    assert receipt["binding"]["provider_id"] == "123"
    assert receipt["status"] == "published"
    with pytest.raises(ValueError, match="consumed"):
        call(
            tmp_path,
            "record_publish",
            item["work_item_id"],
            approval_id=approved["approval_id"],
            provider_id="124",
            provider_url="https://x",
            performed_operations=["create_tasks"],
            recorded_by="agent",
        )


@pytest.mark.parametrize("operation", ["delete_tasks", "create_project", "admin"])
def test_only_bounded_operations_are_accepted(tmp_path, operation):
    item = call(tmp_path, "create", title="T", outcome="O")
    with pytest.raises(ValueError, match="operations"):
        call(tmp_path, "preview", item["work_item_id"], provider="p", operations=[operation])


def test_failed_approval_and_receipt_do_not_mutate(tmp_path):
    item = call(tmp_path, "create", title="T", outcome="O")
    path = Path(item["artifact_path"])
    before = path.read_bytes()
    with pytest.raises(ValueError, match="fingerprint"):
        call(
            tmp_path,
            "approve",
            item["work_item_id"],
            provider="p",
            operations=["add_comment"],
            expected_fingerprint="sha256:wrong",
            approver="a",
            source="s",
        )
    assert path.read_bytes() == before
    approved = call(
        tmp_path,
        "approve",
        item["work_item_id"],
        provider="p",
        operations=["add_comment"],
        expected_fingerprint=item["fingerprint"],
        approver="a",
        source="s",
    )
    before = path.read_bytes()
    with pytest.raises(ValueError, match="scope"):
        call(
            tmp_path,
            "record_publish",
            item["work_item_id"],
            approval_id=approved["approval_id"],
            provider_id="1",
            provider_url="https://x",
            performed_operations=["update_tasks"],
            recorded_by="agent",
        )
    assert path.read_bytes() == before
    call(tmp_path, "update", item["work_item_id"], scope="changed")
    stale_before = path.read_bytes()
    with pytest.raises(ValueError, match="stale"):
        call(
            tmp_path,
            "record_publish",
            item["work_item_id"],
            approval_id=approved["approval_id"],
            provider_id="1",
            provider_url="https://x",
            performed_operations=["add_comment"],
            recorded_by="agent",
        )
    assert path.read_bytes() == stale_before


@pytest.mark.parametrize(
    "command,kwargs",
    [
        ("create", {"title": "", "outcome": "O"}),
        ("create", {"title": "T", "outcome": ""}),
    ],
)
def test_malformed_create(command, kwargs, tmp_path):
    with pytest.raises(ValueError):
        call(tmp_path, command, **kwargs)


def test_bad_ids_formats_and_deferred_renderer(tmp_path):
    with pytest.raises(ValueError, match="WI-YYYY-NNNN"):
        call(tmp_path, "show", "../escape")
    with pytest.raises(ValueError, match="output_format"):
        work_item_action(tmp_path, output_format="yaml")
    item = call(tmp_path, "create", title="T", outcome="O")
    assert (
        work_item_action(
            tmp_path,
            "show",
            item["work_item_id"],
            renderer=lambda command, result: command + ":" + result["work_item_id"],
        )
        == "show:" + item["work_item_id"]
    )


def test_metadata_validation_and_published_change_state(tmp_path):
    with pytest.raises(ValueError, match="one line"):
        call(tmp_path, "create", title="bad\ntitle", outcome="O")
    with pytest.raises(ValueError, match="reserved"):
        call(tmp_path, "create", title="T", outcome="O\n## Context\ninjected")
    item = call(tmp_path, "create", title="T", outcome="O")
    with pytest.raises(ValueError, match="unknown preview"):
        call(tmp_path, "preview", item["work_item_id"], provider="asana", operations=["create_tasks"], typo=True)
    approved = call(
        tmp_path,
        "approve",
        item["work_item_id"],
        provider="ASANA",
        operations=["create_tasks"],
        expected_fingerprint=item["fingerprint"],
        approver="owner",
        source="user message",
    )
    for invalid_url in ("not-a-url", "http://app.asana.test/123"):
        with pytest.raises(ValueError, match="HTTPS"):
            call(
                tmp_path,
                "record_publish",
                item["work_item_id"],
                approval_id=approved["approval_id"],
                provider_id="123",
                provider_url=invalid_url,
                performed_operations=["create_tasks"],
                recorded_by="agent",
            )
    published = call(
        tmp_path,
        "record_publish",
        item["work_item_id"],
        approval_id=approved["approval_id"],
        provider_id="123",
        provider_url="https://app.asana.test/123",
        performed_operations=["create_tasks"],
        recorded_by="agent",
    )
    changed = call(tmp_path, "update", item["work_item_id"], scope="new scope")
    assert published["status"] == "published"
    assert changed["status"] == "changes_pending"


def test_post_dispatch_ledgers_only_attest_after_receipt(tmp_path):
    state = SessionState(artifact_root=str(tmp_path), target_root=str(tmp_path))
    item = call(tmp_path, "create", title="T", outcome="O")
    run_post_dispatch(
        "work_item",
        item,
        None,
        ("create",),
        {},
        session_timings=[],
        session_files_changed=set(),
        session_state=state,
        planning_required_actions=frozenset(),
    )
    assert len(state.managed_artifact_ledger) == 1
    assert state.guidance_attestations == []
    approved = call(
        tmp_path,
        "approve",
        item["work_item_id"],
        provider="asana",
        operations=["create_tasks"],
        expected_fingerprint=item["fingerprint"],
        approver="owner",
        source="user message",
    )
    receipt = call(
        tmp_path,
        "record_publish",
        item["work_item_id"],
        approval_id=approved["approval_id"],
        provider_id="123",
        provider_url="https://app.asana.test/123",
        performed_operations=["create_tasks"],
        recorded_by="agent",
    )
    run_post_dispatch(
        "work_item",
        receipt,
        None,
        ("record_publish",),
        {},
        session_timings=[],
        session_files_changed=set(),
        session_state=state,
        planning_required_actions=frozenset(),
    )
    assert len(state.guidance_attestations) == 1
    assert state.guidance_attestations[0].kind == "work-item"


def test_random_atomic_temp_does_not_follow_predictable_symlink(tmp_path):
    directory = tmp_path / "work_items"
    directory.mkdir()
    sentinel = tmp_path / "outside.txt"
    sentinel.write_text("safe", encoding="utf-8")
    old_predictable = directory / f"WI-{datetime.now(timezone.utc).year}-0001.md.tmp"
    try:
        old_predictable.symlink_to(sentinel)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    call(tmp_path, "create", title="T", outcome="O")
    assert sentinel.read_text(encoding="utf-8") == "safe"


def test_work_item_directory_symlink_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (tmp_path / "work_items").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")
    with pytest.raises(ValueError, match=r"symlink|reparse"):
        call(tmp_path, "create", title="T", outcome="O")


def test_concurrent_creates_receive_distinct_ids(tmp_path):
    def create(index):
        return call(tmp_path, "create", title=f"T{index}", outcome="O")["work_item_id"]

    with ThreadPoolExecutor(max_workers=4) as executor:
        ids = list(executor.map(create, range(8)))
    assert len(ids) == len(set(ids)) == 8
    assert call(tmp_path, "list")["count"] == 8


def test_closed_item_rejects_old_approval(tmp_path):
    item = call(tmp_path, "create", title="T", outcome="O")
    approved = call(
        tmp_path,
        "approve",
        item["work_item_id"],
        provider="asana",
        operations=["create_tasks"],
        expected_fingerprint=item["fingerprint"],
        approver="owner",
        source="user message",
    )
    call(
        tmp_path,
        "close",
        item["work_item_id"],
        outcome="cancelled",
        implementation_disposition="rejected",
    )
    with pytest.raises(ValueError, match="closed"):
        call(
            tmp_path,
            "record_publish",
            item["work_item_id"],
            approval_id=approved["approval_id"],
            provider_id="123",
            provider_url="https://app.asana.test/123",
            performed_operations=["create_tasks"],
            recorded_by="agent",
        )


def test_parser_rejects_duplicate_metadata_and_section_ids(tmp_path):
    item = call(tmp_path, "create", title="T", outcome="O")
    path = Path(item["artifact_path"])
    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace("status: ", 'status: "draft"\nstatus: ', 1), encoding="utf-8")
    with pytest.raises(ValueError, match="frontmatter"):
        call(tmp_path, "show", item["work_item_id"])
    path.write_text(original.replace(f"# {item['work_item_id']}:", "# WI-2000-9999:", 1), encoding="utf-8")
    with pytest.raises(ValueError, match="body ID"):
        call(tmp_path, "show", item["work_item_id"])


def test_read_round_trip_preserves_deterministic_artifact_bytes(tmp_path):
    item = call(tmp_path, "create", title="T", outcome="O", acceptance_criteria=["A"])
    path = Path(item["artifact_path"])
    before = path.read_bytes()
    call(tmp_path, "show", item["work_item_id"])
    assert path.read_bytes() == before


def trigger(**overrides):
    value = {
        "id": "repeat.failure",
        "condition": "Two real workflows fail",
        "evidence_required": ["Decision affected", "Failure details"],
        "minimum_count": 2,
        "status": "unmet",
    }
    value.update(overrides)
    return value


def test_new_records_render_disposition_metadata_deterministically(tmp_path):
    item = call(tmp_path, "create", title="T", outcome="O")
    path = Path(item["artifact_path"])
    text = path.read_text(encoding="utf-8")
    assert 'implementation_disposition: "unknown"' in text
    assert "reopening_triggers: []" in text
    shown = call(tmp_path, "show", item["work_item_id"])["record"]
    assert shown["implementation_disposition"] == "unknown"
    assert shown["reopening_triggers"] == []


def test_legacy_exact_frontmatter_defaults_without_rewriting(tmp_path):
    item = call(tmp_path, "create", title="T", outcome="O")
    path = Path(item["artifact_path"])
    legacy = "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.startswith(("implementation_disposition:", "reopening_triggers:"))
    )
    path.write_text(legacy, encoding="utf-8")
    before = path.read_bytes()
    record = call(tmp_path, "show", item["work_item_id"])["record"]
    assert record["implementation_disposition"] == "unknown"
    assert record["reopening_triggers"] == []
    assert path.read_bytes() == before
    call(tmp_path, "update", item["work_item_id"], implementation_disposition="deferred")
    rewritten = path.read_text(encoding="utf-8")
    assert 'implementation_disposition: "deferred"' in rewritten
    assert "reopening_triggers: []" in rewritten


@pytest.mark.parametrize(
    "value,match",
    [
        ([trigger(extra="x")], "exactly"),
        ([trigger(id="Bad")], "invalid format"),
        ([trigger(), trigger()], "unique"),
        ([trigger(evidence_required=["x"] * 17)], "at most 16"),
        ([trigger(condition="x" * 4097)], "at most 4096"),
        ([trigger(evidence_required=[""])], "nonempty"),
        ([trigger(minimum_count=True)], "integer"),
        ([trigger(minimum_count=0)], "integer"),
        ([trigger(minimum_count=1001)], "integer"),
        ([trigger(status="pending")], "unmet, met, or retired"),
        ([trigger()] * 17, "at most 16"),
    ],
)
def test_reopening_trigger_strict_schema_and_bounds(tmp_path, value, match):
    item = call(tmp_path, "create", title="T", outcome="O")
    with pytest.raises(ValueError, match=match):
        call(tmp_path, "update", item["work_item_id"], reopening_triggers=value)


def test_update_is_material_preserves_status_and_stales_approval(tmp_path):
    item = call(tmp_path, "create", title="T", outcome="O")
    approved = call(
        tmp_path,
        "approve",
        item["work_item_id"],
        provider="p",
        operations=["add_comment"],
        expected_fingerprint=item["fingerprint"],
        approver="a",
        source="s",
    )
    updated = call(
        tmp_path,
        "update",
        item["work_item_id"],
        implementation_disposition="deferred",
        reopening_triggers=[trigger()],
    )
    assert updated["status"] == "draft"
    assert updated["fingerprint"] != item["fingerprint"]
    with pytest.raises(ValueError, match="stale"):
        call(
            tmp_path,
            "record_publish",
            item["work_item_id"],
            approval_id=approved["approval_id"],
            provider_id="1",
            provider_url="https://x",
            performed_operations=["add_comment"],
            recorded_by="agent",
        )


def test_close_requires_explicit_disposition_and_applies_fields_atomically(tmp_path):
    item = call(tmp_path, "create", title="T", outcome="O")
    path = Path(item["artifact_path"])
    before = path.read_bytes()
    with pytest.raises(ValueError, match="non-unknown"):
        call(tmp_path, "close", item["work_item_id"], outcome="completed")
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="integer"):
        call(
            tmp_path,
            "close",
            item["work_item_id"],
            outcome="completed",
            implementation_disposition="deferred",
            reopening_triggers=[trigger(minimum_count=False)],
        )
    assert path.read_bytes() == before
    closed = call(
        tmp_path,
        "close",
        item["work_item_id"],
        outcome="completed",
        implementation_disposition="deferred",
        reopening_triggers=[trigger()],
    )
    record = call(tmp_path, "show", item["work_item_id"])["record"]
    assert closed["status"] == "completed"
    assert record["implementation_disposition"] == "deferred"
    assert record["reopening_triggers"] == [trigger()]
