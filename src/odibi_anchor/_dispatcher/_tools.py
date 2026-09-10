"""_tools.py — Tool registry actions.

Extracted from agent_init.py Phase 5 (revamp spec).
Tool listing and registration dispatch functions.
"""
from odibi_anchor._utils.contract import build_base_context


def _tools_action(root, anchor_root, tool_registry, *args, **kwargs) -> dict | str:
    """List all available tools — both hardcoded and registered.

    Usage:
        anchor("tools")
        anchor("tools", category="data")
        anchor("tools", tags=["profiling"])
    """
    output_format = kwargs.pop("output_format", "markdown")
    _dispatch_keys = kwargs.pop("_dispatch_keys", [])
    action_contracts = kwargs.pop("_action_contracts", {})
    category = kwargs.get("category") or (args[0] if args else None)
    tags = kwargs.get("tags")

    from odibi_anchor._utils.contract import build_base_context

    # Collect hardcoded tool metadata
    hardcoded_tools = []
    for name in sorted(_dispatch_keys):
        contract = action_contracts[name]
        metadata = {
            "name": name,
            "category": "—",
            "source": "hardcoded",
            "description": "Built-in Anchor action",
            "pre_task_access_mode": (
                "fixed" if len(contract.allowed_pre_task_access) == 1 else "invocation"
            ),
            "allowed_pre_task_access": sorted(contract.allowed_pre_task_access),
        }
        if len(contract.allowed_pre_task_access) == 1:
            metadata["pre_task_access"] = next(iter(contract.allowed_pre_task_access))
        hardcoded_tools.append(metadata)

    # Collect registered tools (filtered)
    registered_tools = []
    for spec in tool_registry.list_tools(category=category, tags=tags):
        registered_tools.append({
            "name": spec.name,
            "category": spec.category,
            "source": spec.source_path.replace(str(anchor_root) + "/", "") if anchor_root in spec.source_path else spec.source_path,
            "description": spec.description,
            "version": spec.version,
            "pre_task_access": spec.pre_task_access,
            "pre_task_access_declared": spec.pre_task_access_declared,
        })

    all_tools = registered_tools  # Show registered tools (detailed info)
    total = len(hardcoded_tools) + len(registered_tools)

    ctx = build_base_context(
        kind="tools_inventory",
        subject="tool_registry",
        summary=f"{total} tools available: {len(hardcoded_tools)} hardcoded, {len(registered_tools)} registered",
        metrics={
            "total": total,
            "hardcoded": len(hardcoded_tools),
            "registered": len(registered_tools),
            "categories": sorted(set(t["category"] for t in registered_tools)) if registered_tools else [],
        },
        findings=[
            f"{len(registered_tools)} tool(s) loaded from registry" if registered_tools else "No registered tools found",
            f"Registry warnings: {len(tool_registry.warnings)}" if tool_registry.warnings else "",
        ],
        risks=[],
        samples={
            "hardcoded_tools": hardcoded_tools,
            "registered_tools": registered_tools,
        },
        suggested_next_actions=[
            "Use anchor('echo', message='hello') to test a registered tool.",
            "Use anchor('register_tool', path='...') to add a tool at runtime.",
        ],
        hardcoded_actions=sorted(_dispatch_keys),
    )

    if output_format == "markdown":
        lines = ["# Tool Registry\n"]
        lines.append(f"**{ctx['summary']}**\n")
        if registered_tools:
            lines.append("## Registered Tools\n")
            lines.append("| Name | Category | Version | Access | Source | Description |")
            lines.append("| --- | --- | --- | --- | --- | --- |")
            for t in registered_tools:
                lines.append(
                    f"| {t['name']} | {t['category']} | {t.get('version', '?')} | "
                    f"{t['pre_task_access']} | {t['source']} | {t['description']} |"
                )
        lines.append(f"\n## Hardcoded Actions ({len(hardcoded_tools)})\n")
        lines.append(", ".join(f"`{t['name']}`" for t in hardcoded_tools))
        if tool_registry.warnings:
            lines.append("\n## Discovery Warnings\n")
            for w in tool_registry.warnings:
                lines.append(f"- {w}")
        return "\n".join(lines)
    return ctx




def _register_tool_action(tool_registry, *args, **kwargs) -> dict | str:
    """Register a tool from an external path at runtime.

    Usage:
        anchor("register_tool", path="/absolute/path/to/tool_directory")
    """
    output_format = kwargs.pop("output_format", "markdown")
    path = kwargs.get("path") or (args[0] if args else None)

    if not path:
        raise ValueError(
            "anchor(\"register_tool\") requires path= argument.\n"
            "Usage: anchor(\"register_tool\", path=\"/path/to/tool_dir\")"
        )

    from odibi_anchor._utils.contract import build_base_context

    count = tool_registry.register_path(path)

    # Find the newly registered tool
    from pathlib import Path as _P
    tool_dir = _P(path)
    tool_name = None
    if (tool_dir / "tool.json").exists():
        import json
        with open(tool_dir / "tool.json", encoding="utf-8") as f:
            data = json.load(f)
            tool_name = data.get("name")

    spec = tool_registry.get_tool(tool_name) if tool_name else None

    # Dynamically add to planning gate if needed
    if spec and spec.requires_planning:
        global _PLANNING_REQUIRED_ACTIONS
        _PLANNING_REQUIRED_ACTIONS = _PLANNING_REQUIRED_ACTIONS | frozenset({spec.name})

    summary = f"Registered {count} tool(s) from {path}"
    ctx = build_base_context(
        kind="register_tool",
        subject=tool_name or str(path),
        summary=summary,
        metrics={"tools_registered": count, "path": str(path), "tool_name": tool_name},
        findings=[f"Tool '{tool_name}' is now available via anchor('{tool_name}', ...)" if tool_name else f"{count} tool(s) registered"],
        risks=[],
        samples={"spec": {"name": spec.name, "category": spec.category, "entry_point": spec.entry_point} if spec else {}},
        suggested_next_actions=[
            f"Run anchor('{tool_name}', ...) to use the registered tool." if tool_name else "Run anchor('tools') to see all tools.",
        ],
    )

    if output_format == "markdown":
        md = f"# Register Tool\n\n**{summary}**\n"
        if spec:
            md += f"\n- Name: `{spec.name}`\n- Category: {spec.category}\n- Entry: `{spec.entry_point}`\n"
            md += f"- Planning required: {spec.requires_planning}\n"
        return md
    return ctx


