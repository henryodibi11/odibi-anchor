"""H-002: anchor("skill_loaded", ...) returns complete validated guidance."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from odibi_anchor.bootstrap import init


@pytest.fixture
def anchor(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / "project"
    project.mkdir()
    central_skills = Path(__file__).resolve().parent.parent / ".assistant" / "skills"
    anchor_fn, _root, _manifest = init(root=str(project))
    from odibi_anchor._dispatcher._boot import _ENV
    from odibi_anchor._utils._session_state import _SESSION_STATE
    from odibi_anchor.planning._task_profile import normalize_task_profile
    monkeypatch.setitem(_ENV, "skills_dir", str(central_skills))
    _SESSION_STATE.active_task_profile = normalize_task_profile(
        legacy_mode="planning", execution_mode="read_only"
    )
    return anchor_fn


def test_real_skill_returns_complete_content_before_registration(anchor):
    res = anchor("skill_loaded", "writing-specs")
    assert res["registered"] == "writing-specs"
    assert res["char_count"] > 0
    assert len(res["content"]) == res["char_count"]
    assert len(res["content_sha256"]) == 64
    assert "preview" not in res


def test_include_content_true_remains_compatible(anchor):
    res = anchor("skill_loaded", "writing-specs", include_content=True)
    assert "content" in res and len(res["content"]) == res["char_count"]


def test_preview_only_request_neither_registers_nor_authorizes(anchor):
    from odibi_anchor._utils._session_state import _SESSION_STATE
    _SESSION_STATE.skills_loaded.clear()
    with pytest.raises(ValueError, match="always returns complete guidance"):
        anchor("skill_loaded", "writing-specs", include_content=False)
    assert _SESSION_STATE.skills_loaded == set()


def test_unknown_skill_loading_options_fail_before_registration(anchor):
    from odibi_anchor._utils._session_state import _SESSION_STATE
    _SESSION_STATE.skills_loaded.clear()
    with pytest.raises(ValueError, match="Unsupported skill_loaded argument"):
        anchor("skill_loaded", "writing-specs", preview=True)
    assert _SESSION_STATE.skills_loaded == set()


def test_extra_positional_skill_loading_option_fails_before_registration(anchor):
    from odibi_anchor._utils._session_state import _SESSION_STATE
    _SESSION_STATE.skills_loaded.clear()
    with pytest.raises(ValueError, match="exactly one positional skill name"):
        anchor("skill_loaded", "writing-specs", False)
    assert _SESSION_STATE.skills_loaded == set()


def test_post_dispatch_failure_does_not_register_skill(anchor, monkeypatch):
    from odibi_anchor._dispatcher import _post_dispatch
    from odibi_anchor._utils._session_state import _SESSION_STATE
    _SESSION_STATE.skills_loaded.clear()

    def fail_post_dispatch(*_args, **_kwargs):
        raise RuntimeError("injected post-dispatch failure")

    monkeypatch.setattr(_post_dispatch, "run_post_dispatch", fail_post_dispatch)
    with pytest.raises(RuntimeError, match="injected post-dispatch failure"):
        anchor("skill_loaded", "writing-specs")
    assert _SESSION_STATE.skills_loaded == set()


def test_skills_prefix_and_suffix_normalized(anchor):
    res = anchor("skill_loaded", "skills/writing-specs/SKILL.md")
    assert res["registered"] == "writing-specs"


def test_backslash_skill_path_is_normalized(anchor):
    res = anchor("skill_loaded", r"skills\writing-specs\SKILL.md")
    assert res["registered"] == "writing-specs"


def test_removed_name_neither_registers_nor_authorizes(anchor):
    from odibi_anchor._utils._session_state import _SESSION_STATE
    _SESSION_STATE.skills_loaded.clear()
    with pytest.raises(ValueError, match="available skills"):
        anchor("skill_loaded", "planning")
    assert _SESSION_STATE.skills_loaded == set()


def test_state_records_direct_native_name_only(anchor):
    from odibi_anchor._utils._session_state import _SESSION_STATE
    _SESSION_STATE.skills_loaded.clear()
    result = anchor("skill_loaded", "writing-specs")
    assert result["guidance_target"] == {"skill": "writing-specs"}
    assert result["resolved_paths"] == ["writing-specs/SKILL.md"]
    assert _SESSION_STATE.skills_loaded == {"writing-specs"}


def test_nested_reference_cannot_be_registered(anchor):
    with pytest.raises(ValueError, match="Unknown skill"):
        anchor("skill_loaded", "writing-specs/references/implementation-contract")


def test_nonexistent_skill_raises(anchor):
    with pytest.raises(ValueError, match="Unknown skill"):
        anchor("skill_loaded", "this-skill-does-not-exist")


def test_project_local_skill_is_not_used(anchor, tmp_path):
    local_skill = tmp_path / "project" / ".assistant" / "skills" / "local-only"
    local_skill.mkdir(parents=True)
    (local_skill / "SKILL.md").write_text("# Local only\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Unknown skill"):
        anchor("skill_loaded", "local-only")


def test_install_without_private_config_uses_empty_defaults(tmp_path, monkeypatch):
    project = tmp_path / "secondary-project"
    project.mkdir()
    monkeypatch.chdir(project)

    from odibi_anchor._dispatcher._boot import _load_config
    config = _load_config()

    assert config == {}


def test_empty_name_raises(anchor):
    with pytest.raises(ValueError, match="non-empty"):
        anchor("skill_loaded", "")


def test_preprofile_load_is_explicitly_blocked(anchor):
    from odibi_anchor._utils._session_state import _SESSION_STATE
    _SESSION_STATE.active_task_profile = None
    with pytest.raises(RuntimeError, match="active task profile"):
        anchor("skill_loaded", "writing-specs")
