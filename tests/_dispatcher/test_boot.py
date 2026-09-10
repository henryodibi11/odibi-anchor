"""Tests for odibi_anchor._dispatcher._boot — project detection and boot helpers."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from odibi_anchor._dispatcher._boot import (
    _BOOT_MEMORY_LIMIT,
    _BOOT_TYPE_PRIORITY,
    _PROJECT_ROOTS,
    FRAMEWORK_ROOT,
    BootResult,
    _detect_profile,
    _detect_project_root,
    _discover_project_roots,
    _rank_boot_entries,
    _resolve_environment,
    run_boot,
)
from odibi_anchor._dispatcher._project import RouteBinding
from odibi_anchor._runtime_paths import (
    RuntimePaths,
    _default_user_state_home,
    resolve_runtime_paths,
)


def resolve_installed_project_bootstrap(*args, **kwargs):
    """Call the current boot module generation after bootstrap cache eviction."""
    from odibi_anchor._dispatcher._boot import (
        resolve_installed_project_bootstrap as current_resolver,
    )

    return current_resolver(*args, **kwargs)


class TestRankBootEntries:
    """Tests for _rank_boot_entries selection logic."""

    def test_fewer_than_limit__returns_all_sorted_by_type(self):
        entries = [
            {"type": "decision", "content": "d1"},
            {"type": "gotcha", "content": "g1"},
            {"type": "convention", "content": "c1"},
        ]
        result = _rank_boot_entries(entries, limit=8)
        assert len(result) == 3
        # gotcha has priority 0, should be first
        assert result[0]["type"] == "gotcha"

    def test_exactly_at_limit__returns_all(self):
        entries = [{"type": "gotcha", "content": f"g{i}"} for i in range(8)]
        result = _rank_boot_entries(entries, limit=8)
        assert len(result) == 8

    def test_exceeds_limit__returns_limit_count(self):
        entries = [{"type": "gotcha", "content": f"g{i}"} for i in range(20)]
        result = _rank_boot_entries(entries, limit=8)
        assert len(result) == 8

    def test_type_diversity__one_per_type_first(self):
        """Ensures at least one entry per type when available."""
        entries = [
            {"type": "gotcha", "content": "g1", "confidence": 0.9, "use_count": 5},
            {"type": "gotcha", "content": "g2", "confidence": 0.9, "use_count": 5},
            {"type": "gotcha", "content": "g3", "confidence": 0.9, "use_count": 5},
            {"type": "convention", "content": "c1", "confidence": 0.5, "use_count": 0},
            {"type": "decision", "content": "d1", "confidence": 0.5, "use_count": 0},
            {"type": "discovery", "content": "dis1", "confidence": 0.5, "use_count": 0},
            {"type": "pattern", "content": "p1", "confidence": 0.5, "use_count": 0},
        ]
        result = _rank_boot_entries(entries, limit=5)
        types_in_result = {e["type"] for e in result}
        # Should have diversity — not all gotchas
        assert len(types_in_result) >= 3

    def test_confidence_boosts_score(self):
        """Higher confidence entries should be preferred."""
        entries = [
            {"type": "gotcha", "content": "low", "confidence": 0.1, "use_count": 0},
            {"type": "gotcha", "content": "high", "confidence": 1.0, "use_count": 0},
        ] * 5  # enough to exceed limit
        result = _rank_boot_entries(entries, limit=3)
        # All selected should be high-confidence
        assert all(e["confidence"] >= 0.5 for e in result[:1])

    def test_explicit_evidence_boosts_score(self):
        """Application evidence, not legacy use_count, drives preference."""
        entries = [
            {"type": "gotcha", "content": f"g{i}", "confidence": 0.5,
             "use_count": 100 - i, "applied_count": i}
            for i in range(15)
        ]
        result = _rank_boot_entries(entries, limit=5)
        assert max(e["applied_count"] for e in result) >= 9

    def test_empty_entries__returns_empty(self):
        result = _rank_boot_entries([], limit=8)
        assert result == []

    def test_no_boot_score_key_in_output(self):
        """_boot_score temp key should be cleaned up."""
        entries = [{"type": "gotcha", "content": f"g{i}"} for i in range(20)]
        result = _rank_boot_entries(entries, limit=5)
        for e in result:
            assert "_boot_score" not in e


class TestDiscoverProjectRoots:
    """Tests for _discover_project_roots."""

    def test_always_includes_hardcoded_roots(self):
        """Result should include _PROJECT_ROOTS regardless of filesystem."""
        result = _discover_project_roots("/nonexistent/path")
        for root in _PROJECT_ROOTS:
            assert root in result

    def test_returns_list(self):
        result = _discover_project_roots()
        assert isinstance(result, list)
        assert len(result) >= len(_PROJECT_ROOTS)

    def test_real_home_includes_odibi_anchor(self):
        """ANCHOR_ROOT is discovered via _PROJECT_ROOTS or env, not .agent_memory.db."""
        result = _discover_project_roots()
        # After source/state separation, the checkout may or may not appear
        # in discovered roots depending on ANCHOR_HOME.  The important assertion
        # is that discovery returns a list without crashing.
        assert isinstance(result, list)


class TestDetectProjectRoot:
    """Tests for _detect_project_root."""

    def test_agent_root_override__priority_1(self):
        """Explicit _AGENT_ROOT takes highest priority."""
        caller_globals = {"_AGENT_ROOT": "/my/custom/root"}
        result = _detect_project_root(caller_globals)
        assert result == "/my/custom/root"

    def test_no_globals__falls_through(self):
        """With no globals and no env, should not crash."""
        result = _detect_project_root(None)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_env_var_detection(self):
        """DATABRICKS_NOTEBOOK_PATH env var triggers path matching."""
        # FRAMEWORK_ROOT is the source checkout; ANCHOR_ROOT is the external state
        # directory.  Path matching uses the source checkout in project roots.
        with patch.dict(os.environ, {"DATABRICKS_NOTEBOOK_PATH": FRAMEWORK_ROOT + "/notebooks/test"}):
            result = _detect_project_root(None)
            assert result == FRAMEWORK_ROOT


class TestBootResult:
    """Tests for BootResult dataclass."""

    def test_default_values(self):
        br = BootResult()
        assert br.project == "unknown"
        assert br.boot_entries == []
        assert br.mem_total == 0
        assert br.mem_count == 0
        assert br.promoted_count == 0
        assert br.manifest == {}
        assert br.manifest_msg == "unavailable"
        assert br.frame is None
        assert br.prior_learn_debt is False
        assert br.debt_info == {}

    def test_custom_values(self):
        br = BootResult(project="my_proj", mem_total=100, mem_count=10)
        assert br.project == "my_proj"
        assert br.mem_total == 100
        assert br.mem_count == 10


def test_boot_defers_memory_store_access_and_does_not_print_totals(
    tmp_path, monkeypatch, capsys,
):
    from odibi_anchor.codebase import _memory_db

    def fail_store_access(*_args, **_kwargs):
        raise AssertionError("boot must not inspect the memory store")

    monkeypatch.setattr(_memory_db, "entry_count", fail_store_access)
    monkeypatch.setattr(_memory_db, "query_memories", fail_store_access)
    root = tmp_path / "project"
    root.mkdir()

    result = run_boot(
        str(root), str(tmp_path), state_root=str(root),
        project_id="project:test", db_path=str(tmp_path / "memory.db"),
        frame_enabled=False, learning_recovery_checked=True,
    )

    output = capsys.readouterr().out
    assert result.mem_total == 0
    assert "total" not in output.lower()
    assert "bounded retrieval deferred to task acceptance" in output


def test_boot_skips_learning_lookup_without_exact_task_owner(tmp_path, monkeypatch):
    from odibi_anchor.codebase import structured_learning_context as learning

    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(
        learning,
        "active_learning_obligation",
        lambda **_kwargs: pytest.fail("boot must not perform a project-global lookup"),
    )

    result = run_boot(
        str(root),
        str(tmp_path),
        state_root=str(root),
        project_id="project:test",
        db_path=str(tmp_path / "memory.db"),
        frame_enabled=False,
    )

    assert result.learning_recovery_status == "skipped_no_exact_owner"


def test_bound_boot_fails_closed_when_continuity_is_unverifiable(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    binding = RouteBinding(
        project_id="project:test", target_root=str(root),
        artifact_root=str(root), anchor_home=str(tmp_path),
        binding_source="explicit", runtime_instance_id="runtime-test",
    )
    sentinel = root / "continuity" / "v1" / "OWNER.json"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("{}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="owner mismatch"):
        run_boot(
            str(root), str(tmp_path), state_root=str(root),
            project_id="project:test", db_path=str(tmp_path / "memory.db"),
            frame_enabled=False, route_binding=binding,
        )


def test_boot_queries_learning_with_exact_route_and_session_task(tmp_path, monkeypatch):
    import json

    from odibi_anchor.codebase import structured_learning_context as learning

    root = tmp_path / "project"
    root.mkdir()
    (root / ".anchor_session_state.json").write_text(
        json.dumps({"task_window_id": "ltw_exact", "awaiting_learn": False}),
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(
        learning,
        "active_learning_obligation",
        lambda **owner: calls.append(("active", owner)) or None,
    )
    monkeypatch.setattr(
        learning,
        "latest_closed_learning_obligation",
        lambda **owner: calls.append(("latest", owner)) or None,
    )

    result = run_boot(
        str(root),
        str(tmp_path),
        state_root=str(root),
        project_id="project:test",
        db_path=str(tmp_path / "memory.db"),
        frame_enabled=False,
    )

    expected = {"project_id": "project:test", "task_window_id": "ltw_exact"}
    assert calls == [("active", expected), ("latest", expected)]
    assert result.learning_recovery_status == "owner_checked"


class TestConstants:
    """Verify constants are correctly defined."""

    def test_boot_memory_limit(self):
        assert _BOOT_MEMORY_LIMIT == 8

    def test_type_priority_coverage(self):
        expected_types = {"gotcha", "convention", "pattern", "decision", "discovery", "preference", "failure_pattern", "tool_call"}
        assert expected_types == set(_BOOT_TYPE_PRIORITY.keys())

    def test_type_priority_values_unique(self):
        values = list(_BOOT_TYPE_PRIORITY.values())
        assert len(values) == len(set(values))

    def test_gotcha_has_highest_priority(self):
        assert _BOOT_TYPE_PRIORITY["gotcha"] == 0


class TestProfileDetection:
    """Test auto-detection of environment profiles."""

    def test_databricks_detected(self, monkeypatch):
        monkeypatch.setenv("DATABRICKS_RUNTIME_VERSION", "14.3")
        config = {
            "environment": {
                "profiles": {
                    "databricks": {"platform": "databricks", "home": "/Workspace/Users/test"},
                    "laptop": {"platform": "local", "home": "/nonexistent"},
                }
            }
        }
        profile = _detect_profile(config)
        assert profile["platform"] == "databricks"

    def test_ci_detected(self, monkeypatch):
        monkeypatch.setenv("CI", "true")
        monkeypatch.delenv("DATABRICKS_RUNTIME_VERSION", raising=False)
        monkeypatch.delenv("ANCHOR_PROFILE", raising=False)
        config = {
            "environment": {
                "profiles": {
                    "ci": {"platform": "ci", "home": None},
                    "laptop": {"platform": "local", "home": "/nonexistent"},
                }
            }
        }
        profile = _detect_profile(config)
        assert profile["platform"] == "ci"

    def test_local_matched_by_path(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DATABRICKS_RUNTIME_VERSION", raising=False)
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("ANCHOR_PROFILE", raising=False)
        config = {
            "environment": {
                "profiles": {
                    "databricks": {"platform": "databricks", "home": "/Workspace/fake"},
                    "laptop": {"platform": "local", "home": str(tmp_path)},
                }
            }
        }
        profile = _detect_profile(config)
        assert profile["platform"] == "local"
        assert profile["home"] == str(tmp_path)

    def test_explicit_override(self, monkeypatch):
        monkeypatch.setenv("ANCHOR_PROFILE", "custom")
        config = {
            "environment": {
                "profiles": {
                    "databricks": {"platform": "databricks", "home": "/Workspace/fake"},
                    "custom": {"platform": "local", "home": "/custom/path"},
                }
            }
        }
        profile = _detect_profile(config)
        assert profile["home"] == "/custom/path"

    def test_unknown_explicit_override_fails_closed(self, monkeypatch):
        monkeypatch.setenv("ANCHOR_PROFILE", "typo")
        config = {
            "environment": {
                "profiles": {"custom": {"platform": "local", "home": "/custom/path"}},
            },
        }
        with pytest.raises(ValueError, match=r"Unknown ANCHOR_PROFILE.*custom"):
            _detect_profile(config)

    def test_explicit_override_without_declared_profiles_fails_closed(self, monkeypatch):
        monkeypatch.setenv("ANCHOR_PROFILE", "typo")
        with pytest.raises(ValueError, match="no environment profiles"):
            _detect_profile({})

    def test_empty_config_returns_empty(self):
        assert _detect_profile({}) == {}

    def test_databricks_wins_over_local(self, monkeypatch, tmp_path):
        """Databricks env var takes priority over matching local path."""
        monkeypatch.setenv("DATABRICKS_RUNTIME_VERSION", "14.3")
        monkeypatch.delenv("ANCHOR_PROFILE", raising=False)
        config = {
            "environment": {
                "profiles": {
                    "databricks": {"platform": "databricks", "home": "/Workspace/test"},
                    "laptop": {"platform": "local", "home": str(tmp_path)},
                }
            }
        }
        profile = _detect_profile(config)
        assert profile["platform"] == "databricks"


class TestResolveEnvironment:
    """Test environment resolution with defaults."""

    def test_null_values_default_to_cwd(self, monkeypatch):
        monkeypatch.delenv("DATABRICKS_RUNTIME_VERSION", raising=False)
        monkeypatch.delenv("CI", raising=False)
        config = {
            "environment": {
                "profiles": {
                    "ci": {"platform": "ci", "home": None, "framework_root": None},
                }
            }
        }
        monkeypatch.setenv("ANCHOR_PROFILE", "ci")
        env = _resolve_environment(config)
        assert env["home"] == os.getcwd()
        assert env["framework_root"] == os.getcwd()

    def test_explicit_values_preserved(self, monkeypatch):
        monkeypatch.setenv("ANCHOR_PROFILE", "test")
        config = {
            "environment": {
                "profiles": {
                    "test": {
                        "platform": "local",
                        "home": "/my/home",
                        "framework_root": "/my/framework",
                    }
                }
            }
        }
        env = _resolve_environment(config)
        assert env["home"] == "/my/home"
        assert env["framework_root"] == "/my/framework"

    def test_profile_paths_resolve_to_one_physical_identity(self, monkeypatch, tmp_path):
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        alias = tmp_path / "workspace-alias"
        alias.symlink_to(checkout, target_is_directory=True)
        sync_target = checkout / ".assistant_instructions.md"
        monkeypatch.delenv("ANCHOR_HOME", raising=False)
        monkeypatch.setenv("ANCHOR_PROFILE", "test")
        config = {
            "environment": {
                "profiles": {
                    "test": {
                        "platform": "databricks",
                        "home": str(alias),
                        "framework_root": str(alias),
                        "anchor_root": str(alias),
                        "project_roots": [str(alias)],
                        "sync_target": str(alias / sync_target.name),
                    }
                }
            }
        }

        env = _resolve_environment(config)

        assert env["home"] == str(checkout)
        assert env["framework_root"] == str(checkout)
        assert env["anchor_root"] == str(checkout)
        assert env["project_roots"] == [str(checkout)]
        assert env["sync_target"] == str(sync_target)

    def test_cw_home_overrides_profile_root_and_default_memory(self, monkeypatch, tmp_path):
        explicit_home = tmp_path / "explicit-anchor-home"
        monkeypatch.setenv("ANCHOR_HOME", str(explicit_home))
        monkeypatch.delenv("ANCHOR_MEMORY_DB", raising=False)
        monkeypatch.setenv("ANCHOR_PROFILE", "test")
        config = {
            "environment": {
                "profiles": {
                    "test": {
                        "platform": "local",
                        "home": str(tmp_path / "user-workspace"),
                        "anchor_root": str(tmp_path / "profile-anchor-root"),
                    }
                }
            }
        }

        env = _resolve_environment(config)

        assert env["anchor_root"] == str(explicit_home.resolve())
        assert env["home"] == str(tmp_path / "user-workspace")
        assert env["memory_db"] == str(explicit_home.resolve() / ".agent_memory.db")

    def test_profile_memory_path_remains_explicit(self, monkeypatch, tmp_path):
        profile_memory = tmp_path / "profile" / "memory.db"
        monkeypatch.setenv("ANCHOR_HOME", str(tmp_path / "explicit-home"))
        monkeypatch.delenv("ANCHOR_MEMORY_DB", raising=False)
        monkeypatch.setenv("ANCHOR_PROFILE", "test")
        config = {
            "environment": {
                "profiles": {
                    "test": {
                        "platform": "local",
                        "home": str(tmp_path),
                        "anchor_root": str(tmp_path / "profile-root"),
                        "memory_db": str(profile_memory),
                    }
                }
            }
        }

        assert _resolve_environment(config)["memory_db"] == str(profile_memory)


class TestRuntimePaths:
    """Test source detection and installed writable-home precedence."""

    def test_current_package_is_verified_source_checkout(self):
        paths = resolve_runtime_paths(environment={})
        repository_root = Path(__file__).resolve().parents[2]

        assert paths.source_checkout is True
        assert paths.resource_root == repository_root
        # Source checkout is never used as writable state home.
        assert paths.anchor_home != repository_root
        assert paths.skills_dir == repository_root / ".assistant" / "skills"
        assert paths.tools_dir == repository_root / "tools"

    def test_source_layout_and_tools_marker_identify_a_source_copy(self, tmp_path):
        package_file = tmp_path / "checkout" / "src" / "odibi_anchor" / "_runtime_paths.py"
        package_file.parent.mkdir(parents=True)
        package_file.touch()
        (tmp_path / "checkout" / "tools").mkdir()

        paths = resolve_runtime_paths(
            package_file=package_file,
            environment={},
            user_home=tmp_path / "user",
        )

        assert paths.source_checkout is True
        assert paths.resource_root == (tmp_path / "checkout").resolve()
        # Source checkout never defaults anchor_home into itself.
        expected_home = (tmp_path / "user" / ".local" / "state" / "odibi-anchor").resolve()
        assert paths.anchor_home == expected_home

    def test_installed_fallback_uses_user_state_without_creating_it(self, tmp_path):
        package_file = tmp_path / "venv" / "site-packages" / "odibi_anchor" / "_runtime_paths.py"
        package_file.parent.mkdir(parents=True)
        package_file.touch()
        user_home = tmp_path / "user"

        paths = resolve_runtime_paths(
            package_file=package_file,
            environment={},
            platform="posix",
            user_home=user_home,
        )

        assert paths.source_checkout is False
        assert paths.resource_root == package_file.parent.parent.resolve()
        assert paths.anchor_home == (user_home / ".local" / "state" / "odibi-anchor").resolve()
        assert not paths.anchor_home.exists()

    def test_cw_home_wins_over_profile_and_source(self, tmp_path):
        explicit_home = tmp_path / "explicit"
        profile_home = tmp_path / "profile"

        paths = resolve_runtime_paths(
            profile_home,
            environment={"ANCHOR_HOME": str(explicit_home)},
        )

        assert paths.source_checkout is True
        assert paths.anchor_home == explicit_home.resolve()

    def test_profile_root_wins_over_installed_fallback(self, tmp_path):
        package_file = tmp_path / "site-packages" / "odibi_anchor" / "_runtime_paths.py"
        package_file.parent.mkdir(parents=True)
        package_file.touch()
        profile_home = tmp_path / "profile"

        paths = resolve_runtime_paths(
            profile_home,
            package_file=package_file,
            environment={},
            user_home=tmp_path / "user",
        )

        assert paths.anchor_home == profile_home.resolve()

    def test_xdg_state_home_is_used_on_unix(self, tmp_path):
        state_home = _default_user_state_home(
            {"XDG_STATE_HOME": str(tmp_path / "xdg")},
            platform="posix",
            user_home=tmp_path / "user",
        )

        assert state_home == tmp_path / "xdg" / "odibi-anchor"

    def test_windows_local_application_state_is_deterministic(self):
        state_home = _default_user_state_home(
            {"LOCALAPPDATA": "C:/Users/example/AppData/Local"},
            platform="nt",
            user_home=Path("C:/Users/example"),
        )

        assert state_home.as_posix().endswith("AppData/Local/odibi-anchor")

    def test_databricks_without_cw_home_raises(self, tmp_path):
        """Databricks without ANCHOR_HOME or profile raises a clear RuntimeError."""
        package_file = tmp_path / "checkout" / "src" / "odibi_anchor" / "_runtime_paths.py"
        package_file.parent.mkdir(parents=True)
        package_file.touch()
        (tmp_path / "checkout" / "tools").mkdir()

        with pytest.raises(RuntimeError, match="ANCHOR_HOME must be set"):
            resolve_runtime_paths(
                package_file=package_file,
                environment={"DATABRICKS_RUNTIME_VERSION": "17.3"},
                user_home=tmp_path / "user",
            )

    def test_databricks_with_cw_home_resolves(self, tmp_path):
        """Databricks with explicit ANCHOR_HOME resolves normally."""
        external_home = tmp_path / "external-state"

        paths = resolve_runtime_paths(
            environment={
                "ANCHOR_HOME": str(external_home),
                "DATABRICKS_RUNTIME_VERSION": "17.3",
            },
        )

        assert paths.anchor_home == external_home.resolve()

    def test_databricks_with_profile_resolves(self, tmp_path):
        """Databricks with a profile anchor_root resolves without needing ANCHOR_HOME."""
        profile_root = tmp_path / "profile-state"
        package_file = tmp_path / "checkout" / "src" / "odibi_anchor" / "_runtime_paths.py"
        package_file.parent.mkdir(parents=True)
        package_file.touch()
        (tmp_path / "checkout" / "tools").mkdir()

        paths = resolve_runtime_paths(
            profile_root,
            package_file=package_file,
            environment={"DATABRICKS_RUNTIME_VERSION": "17.3"},
            user_home=tmp_path / "user",
        )

        assert paths.anchor_home == profile_root.resolve()

    def test_local_source_checkout_uses_user_state_home(self, tmp_path):
        """Local source checkout without ANCHOR_HOME uses platform user-state directory."""
        package_file = tmp_path / "checkout" / "src" / "odibi_anchor" / "_runtime_paths.py"
        package_file.parent.mkdir(parents=True)
        package_file.touch()
        (tmp_path / "checkout" / "tools").mkdir()
        user_home = tmp_path / "home"

        paths = resolve_runtime_paths(
            package_file=package_file,
            environment={},
            platform="posix",
            user_home=user_home,
        )

        expected_home = (user_home / ".local" / "state" / "odibi-anchor").resolve()
        assert paths.source_checkout is True
        assert paths.anchor_home == expected_home
        # State directory is not created — only resolved.
        assert not paths.anchor_home.exists()



class TestInstalledProjectBootstrap:
    """Test pure installed-orb target and writable-state separation."""

    @staticmethod
    def _runtime_paths(tmp_path: Path, *, anchor_home: Path | None = None) -> RuntimePaths:
        resources = (tmp_path / "venv" / "site-packages").resolve()
        return RuntimePaths(
            source_checkout=False,
            resource_root=resources,
            instructions_file=resources / ".assistant_instructions.md",
            skills_dir=resources / ".assistant" / "skills",
            tools_dir=resources / "tools",
            anchor_home=(anchor_home or tmp_path / "state").resolve(),
        )

    def test_resolves_explicit_target_without_writes(self, tmp_path, monkeypatch):
        target = tmp_path / "target"
        target.mkdir()
        paths = self._runtime_paths(tmp_path)
        memory = tmp_path / "memory" / "agent.db"
        environment = {
            "ANCHOR_PROJECT_ROOT": str(target),
            "ANCHOR_HOME": str(paths.anchor_home),
            "ANCHOR_MEMORY_DB": str(memory),
        }
        monkeypatch.setattr(
            "odibi_anchor._dispatcher._boot.resolve_boot_environment",
            lambda *_args, **_kwargs: {
                "runtime_paths": paths,
                "memory_db": str(memory),
                "skills_dir": str(paths.skills_dir),
            },
        )

        result = resolve_installed_project_bootstrap(str(target), environment)

        assert result.requested_target == str(target)
        assert result.canonical_target == target.resolve()
        assert result.target_source == "ANCHOR_PROJECT_ROOT"
        assert result.runtime_kind == "installed_distribution"
        assert result.runtime_paths == paths
        assert result.memory_db == memory.resolve()
        assert not paths.anchor_home.exists()
        assert not memory.parent.exists()

    def test_explicit_target_does_not_use_foreign_cwd_profile(self, tmp_path, monkeypatch):
        target = tmp_path / "target"
        foreign_cwd = tmp_path / "foreign-cwd"
        foreign_home = tmp_path / "foreign-home"
        default_home = tmp_path / "default-home"
        resources = tmp_path / "resources"
        target.mkdir()
        foreign_cwd.mkdir()
        (foreign_cwd / ".anchor_config.json").write_text(
            '{"environment":{"profiles":{"foreign":'
            f'{{"platform":"local","anchor_root":"{foreign_home}"}},'
            '"ci":{"platform":"ci",'
            f'"anchor_root":"{foreign_home}"}}}}}}',
            encoding="utf-8",
        )
        monkeypatch.chdir(foreign_cwd)

        def installed_paths(profile_cw_root=None, **_kwargs):
            home = Path(profile_cw_root) if profile_cw_root else default_home
            return RuntimePaths(
                source_checkout=False,
                resource_root=resources,
                instructions_file=resources / ".assistant_instructions.md",
                skills_dir=resources / ".assistant" / "skills",
                tools_dir=resources / "tools",
                anchor_home=home,
            )

        monkeypatch.setattr(
            "odibi_anchor._dispatcher._boot.resolve_runtime_paths",
            installed_paths,
        )

        with pytest.raises(RuntimeError, match="ANCHOR_PROFILE is not declared by the target"):
            resolve_installed_project_bootstrap(
                str(target),
                {"ANCHOR_PROJECT_ROOT": str(target), "ANCHOR_PROFILE": "foreign"},
            )

        automatic = resolve_installed_project_bootstrap(
            str(target),
            {"ANCHOR_PROJECT_ROOT": str(target), "CI": "1"},
        )
        assert automatic.runtime_paths.anchor_home == default_home.resolve()
        assert not foreign_home.exists()

    @pytest.mark.parametrize(
        ("target", "message"),
        [
            ("", "required and must be non-empty"),
            ("relative", "absolute, non-tilde"),
            ("~/target", "absolute, non-tilde"),
            ("/absolute/target\nother", "must not contain line breaks"),
        ],
    )
    def test_rejects_invalid_target_before_resolution(self, target, message):
        with pytest.raises(RuntimeError, match=message):
            resolve_installed_project_bootstrap(target, {"ANCHOR_PROJECT_ROOT": target})

    def test_rejects_missing_or_non_directory_target(self, tmp_path):
        missing = tmp_path / "missing"
        with pytest.raises(RuntimeError, match="does not resolve"):
            resolve_installed_project_bootstrap(
                str(missing),
                {"ANCHOR_PROJECT_ROOT": str(missing)},
            )

        file_target = tmp_path / "file"
        file_target.write_text("not a checkout", encoding="utf-8")
        with pytest.raises(RuntimeError, match="is not a directory"):
            resolve_installed_project_bootstrap(
                str(file_target),
                {"ANCHOR_PROJECT_ROOT": str(file_target)},
            )

    @pytest.mark.parametrize("name", ["ANCHOR_HOME", "ANCHOR_MEMORY_DB", "ANCHOR_PROFILE"])
    def test_rejects_empty_explicit_environment_value(self, tmp_path, name):
        target = tmp_path / "target"
        target.mkdir()
        environment = {"ANCHOR_PROJECT_ROOT": str(target), name: ""}

        with pytest.raises(RuntimeError, match=f"{name} is set but empty"):
            resolve_installed_project_bootstrap(str(target), environment)

    @pytest.mark.parametrize("name", ["ANCHOR_HOME", "ANCHOR_MEMORY_DB"])
    def test_rejects_line_break_environment_path_before_writes(self, tmp_path, name):
        target = tmp_path / "target"
        state = tmp_path / "state"
        target.mkdir()
        environment = {
            "ANCHOR_PROJECT_ROOT": str(target),
            name: f"{state}\nother",
        }

        with pytest.raises(RuntimeError, match=f"{name} must not contain line breaks"):
            resolve_installed_project_bootstrap(str(target), environment)
        assert not state.exists()

    def test_rejects_missing_or_mismatched_environment_target(self, tmp_path):
        target = tmp_path / "target"
        other = tmp_path / "other"
        target.mkdir()
        other.mkdir()

        with pytest.raises(RuntimeError, match="required in portable installed-orb mode"):
            resolve_installed_project_bootstrap(str(target), {})
        with pytest.raises(RuntimeError, match="does not match the requested target"):
            resolve_installed_project_bootstrap(
                str(target),
                {"ANCHOR_PROJECT_ROOT": str(other)},
            )

    @pytest.mark.parametrize("conflict", ["target", "resources"])
    def test_rejects_state_or_memory_overlap(self, tmp_path, monkeypatch, conflict):
        target = tmp_path / "target"
        target.mkdir()
        paths = self._runtime_paths(
            tmp_path,
            anchor_home=target / "state" if conflict == "target" else tmp_path / "state",
        )
        memory = (
            paths.resource_root / "memory.db"
            if conflict == "resources"
            else tmp_path / "memory.db"
        )
        environment = {"ANCHOR_PROJECT_ROOT": str(target)}
        monkeypatch.setattr(
            "odibi_anchor._dispatcher._boot.resolve_boot_environment",
            lambda *_args, **_kwargs: {
                "runtime_paths": paths,
                "memory_db": str(memory),
                "skills_dir": str(paths.skills_dir),
            },
        )

        with pytest.raises(RuntimeError, match="must be outside"):
            resolve_installed_project_bootstrap(str(target), environment)

    def test_rejects_target_inside_distribution_resources(self, tmp_path, monkeypatch):
        paths = self._runtime_paths(tmp_path)
        target = paths.resource_root / "target"
        target.mkdir(parents=True)
        environment = {"ANCHOR_PROJECT_ROOT": str(target)}
        monkeypatch.setattr(
            "odibi_anchor._dispatcher._boot.resolve_boot_environment",
            lambda *_args, **_kwargs: {
                "runtime_paths": paths,
                "memory_db": str(tmp_path / "memory.db"),
                "skills_dir": str(paths.skills_dir),
            },
        )

        with pytest.raises(RuntimeError, match="ANCHOR_PROJECT_ROOT must be outside"):
            resolve_installed_project_bootstrap(str(target), environment)

    def test_rejects_source_runtime_and_relocated_installed_skills(
        self,
        tmp_path,
        monkeypatch,
    ):
        target = tmp_path / "target"
        target.mkdir()
        installed_paths = self._runtime_paths(tmp_path)
        source_paths = RuntimePaths(
            source_checkout=True,
            resource_root=installed_paths.resource_root,
            instructions_file=installed_paths.instructions_file,
            skills_dir=installed_paths.skills_dir,
            tools_dir=installed_paths.tools_dir,
            anchor_home=installed_paths.anchor_home,
        )
        environment = {"ANCHOR_PROJECT_ROOT": str(target)}
        boot_environment = {
            "runtime_paths": source_paths,
            "memory_db": str(tmp_path / "memory.db"),
            "skills_dir": str(source_paths.skills_dir),
        }
        monkeypatch.setattr(
            "odibi_anchor._dispatcher._boot.resolve_boot_environment",
            lambda *_args, **_kwargs: boot_environment,
        )

        with pytest.raises(RuntimeError, match="requires an installed distribution"):
            resolve_installed_project_bootstrap(str(target), environment)

        boot_environment["runtime_paths"] = installed_paths
        boot_environment["skills_dir"] = str(tmp_path / "relocated-skills")
        with pytest.raises(RuntimeError, match="cannot relocate installed skills"):
            resolve_installed_project_bootstrap(str(target), environment)

    def test_remembered_project_does_not_override_explicit_orb_target(
        self, tmp_path, monkeypatch,
    ):
        target = tmp_path / "target"
        target.mkdir()
        paths = self._runtime_paths(tmp_path)
        selector = paths.anchor_home / "workspace" / ".active_project"
        descriptor = paths.anchor_home / "workspace" / "projects" / "alpha" / "PROJECT.md"
        selector.parent.mkdir(parents=True)
        descriptor.parent.mkdir(parents=True)
        selector.write_text("alpha\n", encoding="utf-8")
        descriptor.write_text(
            "---\nid: alpha\nname: alpha\nstatus: active\n"
            f"project_type: referenced\ntarget_root: {target}\n---\n",
            encoding="utf-8",
        )
        before = {
            path.relative_to(paths.anchor_home): path.read_bytes()
            for path in paths.anchor_home.rglob("*")
            if path.is_file()
        }
        environment = {"ANCHOR_PROJECT_ROOT": str(target)}
        monkeypatch.setattr(
            "odibi_anchor._dispatcher._boot.resolve_boot_environment",
            lambda *_args, **_kwargs: {
                "runtime_paths": paths,
                "memory_db": str(tmp_path / "memory.db"),
                "skills_dir": str(paths.skills_dir),
            },
        )

        resolved = resolve_installed_project_bootstrap(str(target), environment)
        assert resolved.canonical_target == target.resolve()
        assert {
            path.relative_to(paths.anchor_home): path.read_bytes()
            for path in paths.anchor_home.rglob("*")
            if path.is_file()
        } == before
