"""Tests for odibi_anchor._utils._tool_registry module."""
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure Anchor source is importable
_CW_SRC = "/Workspace/Users/user@example.com/odibi_anchor/src"
if _CW_SRC not in sys.path:
    sys.path.insert(0, _CW_SRC)

from odibi_anchor._utils._tool_registry import (
    VALID_CATEGORIES,
    Registry,
    ToolLoadError,
    ToolSpec,
)


# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def valid_tool_json():
    """A valid tool.json dict."""
    return {
        "name": "test_tool",
        "version": "1.0.0",
        "description": "A test tool for unit tests",
        "category": "diagnostics",
        "entry_point": "test_module:test_func",
        "requires_planning": False,
        "accepts_output_format": True,
        "allowed_effects": ["read"],
        "inputs": {
            "required": ["message"],
            "optional": ["verbose"],
        },
        "outputs": {
            "contract": "StandardContract",
            "kind": "test_tool",
        },
        "dependencies": [],
        "tags": ["testing"],
    }


@pytest.fixture
def tools_dir(tmp_path, valid_tool_json):
    """Create a temporary tools directory with one valid tool."""
    tool_dir = tmp_path / "test_tool"
    tool_dir.mkdir()
    manifest = tool_dir / "tool.json"
    manifest.write_text(json.dumps(valid_tool_json))

    # Create a resolvable entry point
    module_file = tool_dir / "test_module.py"
    module_file.write_text(
        "def test_func(*args, **kwargs):\n"
        "    return {\'kind\': \'test_tool\', \'message\': kwargs.get(\'message\', \'\')}"
    )

    return tmp_path


@pytest.fixture
def registry():
    """A fresh Registry instance."""
    return Registry()


# ─── ToolSpec Tests ───────────────────────────────────────────────────────────


class TestToolSpec:
    def test_from_dict_basic(self, valid_tool_json):
        spec = ToolSpec.from_dict(valid_tool_json, source_path="/tmp/test")
        assert spec.name == "test_tool"
        assert spec.version == "1.0.0"
        assert spec.category == "diagnostics"
        assert spec.entry_point == "test_module:test_func"
        assert spec.requires_planning is False
        assert spec.accepts_output_format is True
        assert spec.inputs_required == ("message",)
        assert spec.inputs_optional == ("verbose",)
        assert spec.output_contract == "StandardContract"
        assert spec.output_kind == "test_tool"
        assert spec.source_path == "/tmp/test"

    def test_from_dict_defaults(self):
        minimal = {
            "name": "minimal",
            "version": "0.1.0",
            "description": "Minimal tool",
            "category": "code",
            "entry_point": "mod:fn",
            "allowed_effects": ["read"],
            "inputs": {"required": []},
            "outputs": {"contract": "StandardContract", "kind": "minimal"},
        }
        spec = ToolSpec.from_dict(minimal)
        assert spec.requires_planning is False
        assert spec.accepts_output_format is False
        assert spec.renderer is None
        assert spec.dependencies == ()
        assert spec.tags == ()
        assert spec.allowed_effects == frozenset({"read"})
        assert spec.pre_task_access == "task_required"
        assert spec.pre_task_access_declared is False

    def test_frozen(self, valid_tool_json):
        spec = ToolSpec.from_dict(valid_tool_json)
        with pytest.raises(Exception):  # FrozenInstanceError
            spec.name = "changed"


# ─── Validation Tests ─────────────────────────────────────────────────────────


class TestValidation:
    def test_valid_spec(self, valid_tool_json):
        errors = Registry.validate_spec(valid_tool_json)
        assert errors == []

    def test_missing_required_field(self, valid_tool_json):
        del valid_tool_json["name"]
        errors = Registry.validate_spec(valid_tool_json)
        assert any("name" in e for e in errors)

    def test_invalid_name_format(self, valid_tool_json):
        valid_tool_json["name"] = "Invalid-Name"
        errors = Registry.validate_spec(valid_tool_json)
        assert any("Invalid name" in e for e in errors)

    def test_invalid_category(self, valid_tool_json):
        valid_tool_json["category"] = "nonexistent"
        errors = Registry.validate_spec(valid_tool_json)
        assert any("Invalid category" in e for e in errors)

    def test_invalid_entry_point_no_colon(self, valid_tool_json):
        valid_tool_json["entry_point"] = "no_colon_here"
        errors = Registry.validate_spec(valid_tool_json)
        assert any("entry_point" in e for e in errors)

    def test_missing_inputs_required(self, valid_tool_json):
        valid_tool_json["inputs"] = {"optional": ["x"]}
        errors = Registry.validate_spec(valid_tool_json)
        assert any("inputs.required" in e for e in errors)

    def test_missing_outputs_fields(self, valid_tool_json):
        valid_tool_json["outputs"] = {}
        errors = Registry.validate_spec(valid_tool_json)
        assert any("outputs.contract" in e for e in errors)
        assert any("outputs.kind" in e for e in errors)

    def test_invalid_version(self, valid_tool_json):
        valid_tool_json["version"] = "not-semver"
        errors = Registry.validate_spec(valid_tool_json)
        assert any("version" in e for e in errors)

    @pytest.mark.parametrize("effects", [None, [], ["unknown"], ["read", "data_write"]])
    def test_invalid_effect_declarations_fail_closed(self, valid_tool_json, effects):
        if effects is None:
            del valid_tool_json["allowed_effects"]
        else:
            valid_tool_json["allowed_effects"] = effects
        errors = Registry.validate_spec(valid_tool_json)
        assert any("allowed_effects" in error for error in errors)

    @pytest.mark.parametrize("value", [None, True, [], "safe", "unknown"])
    def test_invalid_explicit_pre_task_access_is_rejected(self, valid_tool_json, value):
        valid_tool_json["pre_task_access"] = value
        assert any("pre_task_access" in error for error in Registry.validate_spec(valid_tool_json))


def test_exact_bundled_manifest_access_inventory():
    root = Path(__file__).resolve().parents[2]
    expected = {
        "coerce_fix_tool/tool.json": ("coerce_fix", frozenset({"data_write"})),
        "delta_diff_tool/tool.json": ("delta_diff", frozenset({"read"})),
        "diagnose_empty_tool/tool.json": ("diagnose_empty", frozenset({"read"})),
        "echo_tool/tool.json": ("echo", frozenset({"read"})),
        "explain_row_tool/tool.json": ("explain_row", frozenset({"read"})),
        "partition_check_tool/tool.json": ("partition_check", frozenset({"read"})),
        "pre_join_tool/tool.json": ("pre_join", frozenset({"read"})),
        "pre_merge_tool/tool.json": ("pre_merge", frozenset({"read"})),
        "schema_migrate_tool/tool.json": ("schema_migrate", frozenset({"read"})),
        "suggest_rules_tool/tool.json": ("suggest_rules", frozenset({"read"})),
        "watermark_tool/tool.json": ("watermark", frozenset({"read"})),
    }
    manifests = sorted((root / "tools").glob("*/tool.json"))
    actual = {}
    for path in manifests:
        payload = json.loads(path.read_text(encoding="utf-8"))
        actual[str(path.relative_to(root / "tools"))] = (
            payload["name"], frozenset(payload["allowed_effects"]),
        )
        assert payload.get("pre_task_access") == "task_required"
    assert actual == expected
    registry = Registry()
    assert registry.discover_tools([str(root / "tools")]) == 11
    discovered = {spec.name: spec for spec in registry.list_tools()}
    assert set(discovered) == {name for name, _effects in expected.values()}
    assert all(spec.pre_task_access_declared for spec in discovered.values())
    assert all(spec.pre_task_access == "task_required" for spec in discovered.values())


# ─── Discovery Tests ──────────────────────────────────────────────────────────


class TestDiscovery:
    def test_discover_valid_tool(self, registry, tools_dir):
        count = registry.discover_tools([str(tools_dir)])
        assert count == 1
        assert registry.tool_count == 1
        spec = registry.get_tool("test_tool")
        assert spec is not None
        assert spec.name == "test_tool"

    def test_discover_empty_directory(self, registry, tmp_path):
        count = registry.discover_tools([str(tmp_path)])
        assert count == 0

    def test_discover_nonexistent_path(self, registry):
        count = registry.discover_tools(["/nonexistent/path"])
        assert count == 0
        assert registry.warnings == []

    def test_discover_invalid_json(self, registry, tmp_path):
        tool_dir = tmp_path / "bad_tool"
        tool_dir.mkdir()
        (tool_dir / "tool.json").write_text("not valid json {{{")
        count = registry.discover_tools([str(tmp_path)])
        assert count == 0
        assert len(registry.warnings) == 1
        assert "parse error" in registry.warnings[0]

    def test_discover_invalid_spec(self, registry, tmp_path):
        tool_dir = tmp_path / "incomplete_tool"
        tool_dir.mkdir()
        (tool_dir / "tool.json").write_text(json.dumps({"name": "incomplete"}))
        count = registry.discover_tools([str(tmp_path)])
        assert count == 0
        assert len(registry.warnings) == 1
        assert "validation failed" in registry.warnings[0]

    def test_discover_duplicate_name(self, registry, tmp_path, valid_tool_json):
        # Create two directories with same tool name
        dir1 = tmp_path / "path1" / "test_tool"
        dir1.mkdir(parents=True)
        (dir1 / "tool.json").write_text(json.dumps(valid_tool_json))

        dir2 = tmp_path / "path2" / "test_tool"
        dir2.mkdir(parents=True)
        (dir2 / "tool.json").write_text(json.dumps(valid_tool_json))

        # First path wins
        count = registry.discover_tools([str(tmp_path / "path1"), str(tmp_path / "path2")])
        assert count == 1  # Only first registered
        assert "Duplicate" in registry.warnings[0]


# ─── list_tools Tests ─────────────────────────────────────────────────────────


class TestListTools:
    def test_list_all(self, registry, tools_dir):
        registry.discover_tools([str(tools_dir)])
        results = registry.list_tools()
        assert len(results) == 1
        assert results[0].name == "test_tool"

    def test_list_by_category(self, registry, tools_dir):
        registry.discover_tools([str(tools_dir)])
        results = registry.list_tools(category="diagnostics")
        assert len(results) == 1
        results = registry.list_tools(category="code")
        assert len(results) == 0

    def test_list_by_tags(self, registry, tools_dir):
        registry.discover_tools([str(tools_dir)])
        results = registry.list_tools(tags=["testing"])
        assert len(results) == 1
        results = registry.list_tools(tags=["nonexistent"])
        assert len(results) == 0


# ─── register_path Tests ──────────────────────────────────────────────────────


class TestRegisterPath:
    def test_register_single_tool_dir(self, registry, tools_dir, valid_tool_json):
        tool_dir = tools_dir / "test_tool"
        count = registry.register_path(str(tool_dir))
        assert count == 1
        assert registry.get_tool("test_tool") is not None

    def test_register_nonexistent_path(self, registry, tmp_path):
        with pytest.raises(ValueError, match="does not exist"):
            registry.register_path(str(tmp_path / "nonexistent_tool_dir"))

    def test_register_duplicate_raises(self, registry, tools_dir, valid_tool_json):
        tool_dir = tools_dir / "test_tool"
        registry.register_path(str(tool_dir))
        with pytest.raises(ValueError, match="already registered"):
            registry.register_path(str(tool_dir))


# ─── resolve_callable Tests ───────────────────────────────────────────────────


class TestResolveCallable:
    def test_resolve_valid(self, registry, tools_dir):
        registry.discover_tools([str(tools_dir)])
        spec = registry.get_tool("test_tool")
        fn = registry.resolve_callable(spec)
        assert callable(fn)
        # Call it to verify
        result = fn(message="hello")
        assert result["kind"] == "test_tool"
        assert result["message"] == "hello"

    def test_resolve_caches(self, registry, tools_dir):
        registry.discover_tools([str(tools_dir)])
        spec = registry.get_tool("test_tool")
        fn1 = registry.resolve_callable(spec)
        fn2 = registry.resolve_callable(spec)
        assert fn1 is fn2  # Same object — cached

    def test_resolve_bad_module(self, registry):
        spec = ToolSpec(
            name="bad",
            version="1.0.0",
            description="Bad tool",
            category="code",
            entry_point="nonexistent_module_xyz:func",
            source_path="/tmp",
        )
        registry._specs["bad"] = spec
        with pytest.raises(ToolLoadError, match="Cannot import"):
            registry.resolve_callable(spec)

    def test_resolve_bad_callable(self, registry, tmp_path):
        # Create module without the expected function
        mod_dir = tmp_path / "mod_dir"
        mod_dir.mkdir()
        (mod_dir / "mymod.py").write_text("def other_func(): pass")

        spec = ToolSpec(
            name="missing_fn",
            version="1.0.0",
            description="Missing fn",
            category="code",
            entry_point="mymod:missing_func",
            source_path=str(mod_dir),
        )
        registry._specs["missing_fn"] = spec
        with pytest.raises(ToolLoadError, match="no attribute"):
            registry.resolve_callable(spec)


# ─── Clear Tests ──────────────────────────────────────────────────────────────


class TestClear:
    def test_clear_resets_everything(self, registry, tools_dir):
        registry.discover_tools([str(tools_dir)])
        assert registry.tool_count == 1
        registry.clear()
        assert registry.tool_count == 0
        assert registry.warnings == []
