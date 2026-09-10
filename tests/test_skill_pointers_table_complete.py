"""Provider-neutral instruction and native-link hygiene contract."""

import importlib
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

RECIPE_LEDGER = {
    "code-quality/recipes/code-comprehension.md": "skills/code-comprehension/references/backbone.md",
    "code-quality/recipes/code-standards.md": "references/development/code-standards.md",
    "code-quality/recipes/code-standards-python.md": "references/development/code-standards-python.md",
    "code-quality/recipes/dependency-management.md": "skills/dependency-management/references/backbone.md",
    "code-quality/recipes/file-editing.md": "references/development/file-editing.md",
    "code-quality/recipes/schema-design.md": "skills/schema-design/references/backbone.md",
    "data-engineering/recipes/data-onboarding.md": "skills/data-onboarding/references/backbone.md",
    "data-engineering/recipes/data-onboarding-csv.md": "skills/data-onboarding/references/csv.md",
    "data-engineering/recipes/data-onboarding-excel.md": "skills/data-onboarding/references/excel.md",
    "data-engineering/recipes/data-onboarding-json.md": "skills/data-onboarding/references/json.md",
    "data-engineering/recipes/data-onboarding-quality.md": "skills/data-onboarding/references/quality.md",
    "data-engineering/recipes/data-onboarding-refresh.md": "skills/data-onboarding/references/refresh.md",
    "data-engineering/recipes/data-operations.md": "skills/data-operations/references/backbone.md",
    "data-engineering/recipes/planning-data-engineering.md": "skills/data-operations/references/planning.md",
    "data-engineering/recipes/planning-reconciliation.md": "skills/data-reconciliation/references/backbone.md",
    "data-engineering/recipes/table-profiler.md": "skills/data-onboarding/references/profiling.md",
    "debug-operations/recipes/debugging-playbook.md": "skills/debugging/references/backbone.md",
    "debug-operations/recipes/error-recovery.md": "skills/debugging/references/recovery.md",
    "debug-operations/recipes/incident-response.md": "skills/incident-response/references/backbone.md",
    "debug-operations/recipes/logging.md": "skills/debugging/references/logging.md",
    "debug-operations/recipes/performance-investigation.md": "skills/performance-investigation/references/backbone.md",
    "debug-operations/recipes/planning-debugging.md": "skills/debugging/references/planning.md",
    "operating-core/recipes/anti-rationalization.md": "references/lifecycle/anti-rationalization.md",
    "planning-delivery/recipes/documentation.md": "skills/documentation/references/backbone.md",
    "planning-delivery/recipes/memory-saving.md": "skills/work-item-management/references/memory-saving.md",
    "planning-delivery/recipes/multi-session-planning.md": "skills/work-item-management/references/multi-session.md",
    "planning-delivery/recipes/planning-greenfield.md": "skills/work-item-management/references/greenfield.md",
    "planning-delivery/recipes/planning-implementation.md": "skills/work-item-management/references/implementation.md",
    "planning-delivery/recipes/planning-integration.md": "skills/work-item-management/references/integration.md",
    "planning-delivery/recipes/planning-migration.md": "skills/work-item-management/references/migration.md",
    "planning-delivery/recipes/planning-refactoring.md": "skills/work-item-management/references/refactoring.md",
    "planning-delivery/recipes/planning.md": "skills/work-item-management/references/planning.md",
    "planning-delivery/recipes/thread-discipline.md": "references/lifecycle/thread-discipline.md",
    "planning-delivery/recipes/when-to-ask.md": "references/lifecycle/when-to-ask.md",
    "planning-delivery/recipes/work-item-management.md": "skills/work-item-management/references/backbone.md",
    "planning-delivery/recipes/writing-specs.md": "skills/writing-specs/references/backbone.md",
    "pr-readiness/recipes/cross-functional-pr.md": "skills/cross-functional-pr/references/backbone.md",
    "reference/recipes/action-reference.md": "references/odibi-anchor/actions.md",
    "reference/recipes/odibi-anchor.md": "references/odibi-anchor/workflow.md",
    "reference/recipes/quick-reference.md": "references/odibi-anchor/quick-reference.md",
    "testing-validation/recipes/compliance-gates.md": "references/lifecycle/compliance-gates.md",
    "testing-validation/recipes/dogfooding.md": "skills/writing-tests/references/scenarios.md",
    "testing-validation/recipes/planning-testing.md": "skills/writing-tests/references/planning.md",
    "testing-validation/recipes/self-review.md": "references/lifecycle/self-review.md",
    "testing-validation/recipes/testing-standards.md": "skills/writing-tests/references/backbone.md",
    "testing-validation/recipes/verify-every-edit.md": "references/lifecycle/verify-every-edit.md",
}

PACK_BODY_LEDGER = {
    "operating-core/SKILL.md": ".assistant_instructions.md",
    "planning-delivery/SKILL.md": ".assistant_instructions.md",
    "code-quality/SKILL.md": "references/development/code-standards.md",
    "testing-validation/SKILL.md": "references/lifecycle/compliance-gates.md",
    "data-engineering/SKILL.md": "skills/data-operations/SKILL.md",
    "debug-operations/SKILL.md": "skills/debugging/SKILL.md",
    "pr-readiness/SKILL.md": "skills/cross-functional-pr/SKILL.md",
    "reference/SKILL.md": "README.md",
}

# Every preservation entry resolves to one exact accountable destination heading.
# Representative semantic assertions below prove the highest-risk unique lessons;
# this table prevents a merely-existing file from satisfying the complete ledger.
DESTINATION_HEADINGS = {
    ".assistant_instructions.md": "# Odibi Anchor operating contract",
    "README.md": "# Native skill distribution",
    "references/odibi-anchor/actions.md": "# Odibi Anchor actions",
    "references/odibi-anchor/quick-reference.md": "# Odibi Anchor quick reference",
    "references/odibi-anchor/workflow.md": "# Odibi Anchor workflow",
    "references/development/code-standards-python.md": "# Anchor House Engineering Standard — Python Profile",
    "references/development/code-standards.md": "# Anchor House Engineering Standard",
    "references/development/file-editing.md": "# Safe file editing",
    "references/lifecycle/anti-rationalization.md": "# Anti-rationalization examples",
    "references/lifecycle/compliance-gates.md": "# Compliance gate protocol",
    "references/lifecycle/self-review.md": "# Self-review checklist",
    "references/lifecycle/thread-discipline.md": "# Thread discipline",
    "references/lifecycle/verify-every-edit.md": "# Verify Every Edit",
    "references/lifecycle/when-to-ask.md": "# When to ask",
    "skills/code-comprehension/references/backbone.md": "# Code comprehension workflow",
    "skills/cross-functional-pr/SKILL.md": "# Cross-functional PR",
    "skills/cross-functional-pr/references/backbone.md": "# Cross-functional PR workflow",
    "skills/data-onboarding/references/backbone.md": "# Data Onboarding Reference",
    "skills/data-onboarding/references/csv.md": "# Data Onboarding — CSV Sources",
    "skills/data-onboarding/references/excel.md": "# Data Onboarding — Excel Sources",
    "skills/data-onboarding/references/json.md": "# Data Onboarding — JSON / JSONL Sources",
    "skills/data-onboarding/references/profiling.md": "# Table Profiler Reference",
    "skills/data-onboarding/references/quality.md": "# Data Onboarding — Quality & Remediation",
    "skills/data-onboarding/references/refresh.md": "# Data Onboarding Refresh",
    "skills/data-operations/SKILL.md": "# Data operations",
    "skills/data-operations/references/backbone.md": "# Data Operations — Shift-Left Write Safety",
    "skills/data-operations/references/planning.md": "# Data-operations planning reference",
    "skills/data-reconciliation/references/backbone.md": "# Data reconciliation reference",
    "skills/debugging/SKILL.md": "# Debugging",
    "skills/debugging/references/backbone.md": "# Debugging techniques reference",
    "skills/debugging/references/logging.md": "# Evidence-oriented logging",
    "skills/debugging/references/planning.md": "# Debugging-planning reference",
    "skills/debugging/references/recovery.md": "# Error recovery workflow",
    "skills/dependency-management/references/backbone.md": "# Dependency decision workflow",
    "skills/documentation/references/backbone.md": "# Documentation workflow",
    "skills/incident-response/references/backbone.md": "# Incident response workflow",
    "skills/performance-investigation/references/backbone.md": "# Performance investigation workflow",
    "skills/schema-design/references/backbone.md": "# Schema design workflow",
    "skills/work-item-management/references/backbone.md": "# Work-item management",
    "skills/work-item-management/references/greenfield.md": "# Greenfield planning",
    "skills/work-item-management/references/implementation.md": "# Implementation planning reference",
    "skills/work-item-management/references/integration.md": "# Integration planning",
    "skills/work-item-management/references/memory-saving.md": "# Durable memory and learning",
    "skills/work-item-management/references/migration.md": "# Migration planning",
    "skills/work-item-management/references/multi-session.md": "# Multi-session planning",
    "skills/work-item-management/references/planning.md": "# Proportional planning",
    "skills/work-item-management/references/refactoring.md": "# Refactoring planning",
    "skills/writing-specs/references/backbone.md": "# Writing Specs",
    "skills/writing-tests/references/backbone.md": "# Test standards reference",
    "skills/writing-tests/references/planning.md": "# Test-planning reference",
    "skills/writing-tests/references/scenarios.md": "# Real-client test scenarios",
}

# Stable, representative hard-won lessons from each former ownership area. This
# proves semantic migration rather than merely proving that destination files
# exist, without freezing byte totals or resurrecting old discovery names.
SEMANTIC_PRESERVATION = {
    "skills/code-comprehension/references/backbone.md": ("entry points", "side effects"),
    "references/development/file-editing.md": ("known_bad", "immediately after each file write"),
    "skills/dependency-management/references/backbone.md": ("version conflict", "dependencies"),
    "skills/schema-design/references/backbone.md": ("grain", "must be not null"),
    "skills/data-onboarding/references/csv.md": ("delimiter", "encoding"),
    "skills/data-onboarding/references/excel.md": ("sheet", "formula"),
    "skills/data-onboarding/references/json.md": ("nested", "flatten"),
    "skills/data-onboarding/references/refresh.md": ("schema drift", "watermark"),
    "skills/data-reconciliation/references/backbone.md": ("profile", "both sides"),
    "skills/debugging/references/recovery.md": ("rollback", "cascading failures"),
    "skills/debugging/SKILL.md": ("spark_diagnose", "material uncertainty", "live provenance"),
    "skills/incident-response/references/backbone.md": ("incident_snapshot", "active problem record", "raw table"),
    "skills/performance-investigation/references/backbone.md": ("serverless", "query-profile", "classic stage apis"),
    "references/lifecycle/anti-rationalization.md": ("rationalization", "evidence"),
    "skills/work-item-management/references/multi-session.md": ("session", "handoff"),
    "skills/writing-specs/references/backbone.md": ("acceptance criteria", "rollback"),
    "skills/cross-functional-pr/references/backbone.md": ("reviewer", "teaching package"),
    "references/odibi-anchor/actions.md": ("anchor(", "action"),
    "references/lifecycle/compliance-gates.md": ("gate", "runtimeerror"),
    "skills/writing-tests/references/scenarios.md": ("scenario", "failure"),
    "references/lifecycle/verify-every-edit.md": ("verify", "edit"),
}

REMOVED_NATIVE_NAMES = {
    "action-reference", "anti-rationalization", "code-quality", "code-standards",
    "code-standards-python", "compliance-gates", "odibi-anchor", "data-engineering",
    "data-onboarding-csv", "data-onboarding-excel", "data-onboarding-json",
    "data-onboarding-quality", "data-onboarding-refresh", "debug-operations",
    "debugging-playbook", "dogfooding", "error-recovery", "file-editing", "logging",
    "memory-saving", "multi-session-planning", "operating-core", "planning",
    "planning-data-engineering", "planning-debugging", "planning-delivery",
    "planning-greenfield", "planning-implementation", "planning-integration",
    "planning-migration", "planning-reconciliation", "planning-refactoring",
    "planning-testing", "pr-readiness", "quick-reference", "reference", "self-review",
    "table-profiler", "testing-standards", "testing-validation", "thread-discipline",
    "verify-every-edit", "when-to-ask",
}


def test_instructions_are_concise_provider_neutral_routing_contract():
    text = (ROOT / ".assistant_instructions.md").read_text(encoding="utf-8")
    lowered = text.casefold()
    assert len(text.encode()) < 26_000
    assert all(word in lowered for word in ("outcome", "preserve", "proportion", "evidence", "stop"))
    prohibited = ("aliases.json", "runtime alias", "mcp_cw_", "%pip", "/workspace/users/", "databricks")
    assert not any(fragment in lowered for fragment in prohibited)


def test_host_entrypoints_route_applicable_repository_guidance_to_canonical_contract():
    instructions = (ROOT / ".assistant_instructions.md").read_text(encoding="utf-8")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")

    for name in (".assistant_instructions.md", "AGENTS.md", "CLAUDE.md"):
        assert f"`{name}`" in instructions
    assert "selected target root through each target path" in instructions
    assert "More-specific nested guidance" in instructions
    assert "canonical `.assistant_instructions.md` contract" in agents
    assert "every applicable\n`AGENTS.md`" in claude
    assert "host adapter" in claude


def test_instructions_enforce_explicit_bootstrap_project_and_delivery_contract():
    text = (ROOT / ".assistant_instructions.md").read_text(encoding="utf-8")
    mandatory = text.split("## ⛔ STOP", 1)[1].split("## Route native skills", 1)[0]
    required = (
        "## ⛔ STOP — mandatory agent execution contract",
        "Odibi Anchor use is mandatory",
        'namespace = runpy.run_path(bootstrap_path)',
        'assert bootstrap["success"] is True',
        'project_state = anchor("project", "status", output_format="dict")',
        'anchor("project", "use", "project-id"',
        'session = anchor("new_session"',
        'task = anchor(',
        'skill_result = anchor("skill_loaded"',
        'known_bad_result = anchor("known_bad"',
        'touch_result = anchor("touched"',
        'preflight_result = anchor("preflight"',
        'test_result = anchor("test"',
        'review_result = anchor("review"',
        'gate_result = anchor("gate"',
        'learning_result = anchor("learning", "assess"',
        "`scope` is one prose string",
        "Do not start deferred work",
        "durable session notebooks",
        "Do not edit after a successful gate",
        "NEVER use `exec`",
        "NEVER retry",
        "Issue exactly one Anchor call at a time",
        "NEVER bundle,\nparallelize, or concurrently execute Anchor calls",
        "independent non-Anchor reads may run in parallel",
    )
    for phrase in required:
        assert phrase in text
    assert text.index("## ⛔ STOP") < text.index("## Route native skills")
    assert text.index("runpy.run_path(bootstrap_path)") < text.index('anchor("new_session"')
    assert mandatory.index('selection = anchor("project", "use"') < mandatory.index(
        'session = anchor("new_session"'
    )
    assert "same persistent Python process" in text
    assert "artifact_root" in text
    assert "target_root" in text
    lifecycle = (
        'known_bad_result = anchor("known_bad"',
        "Make only the task-authorized edit",
        'touch_result = anchor("touched"',
        'preflight_result = anchor("preflight"',
        'test_result = anchor("test"',
        'review_result = anchor("review"',
        'gate_result = anchor("gate"',
        'learning_result = anchor("learning", "assess"',
        "Terminal return required:",
        "Status: completed, blocked, or failed",
        "Checks actually executed and retained evidence",
        "External/shared effects performed",
        "Decision or next authority required from me",
        "Do not describe an unexecuted, skipped, unavailable, or failing check as passing.",
    )
    assert [mandatory.index(step) for step in lifecycle] == sorted(
        mandatory.index(step) for step in lifecycle
    )
    post_gate = mandatory.split("Do not edit after a successful gate", 1)[1]
    assert post_gate.index("close that gate's\nlearning obligation") < post_gate.index(
        "fresh session/task"
    )
    assert "code, notebooks, tests" not in mandatory
    assert "project code" not in mandatory.split("`artifact_root`", 1)[0]


def test_routine_memory_review_is_bounded_advisory_and_pre_edit():
    instructions = (ROOT / ".assistant_instructions.md").read_text(encoding="utf-8")
    policy = instructions.split("For routine substantive tasks", 1)[1].split(
        "### 4. Read and apply", 1
    )[0]
    normalized = " ".join(policy.split())

    for phrase in (
        "immediately after task acceptance and before source edits",
        "bounded, active-project/trust-scoped `memory_context`",
        "selected memory IDs with their status, source/provenance, scope, and match reasons",
        "no memories were selected",
        "Do not scan the full store",
        "candidate as authority or verification",
        "Dispose every selection",
        "application and evidence-backed evaluation",
        "follow the existing fail-closed/degraded policy without inventing evidence",
        "do not manufacture this ceremony or a new lesson",
        "Pre-edit acknowledgement is an instruction-level mandate",
        "blocks learning closure until every selection has a disposition",
        "every application has an evidence-backed evaluation",
    ):
        assert phrase in normalized


def test_planning_guidance_orders_pre_task_evidence_before_accepted_task_memory():
    planning = (
        ROOT / ".assistant" / "skills" / "work-item-management" / "references" / "planning.md"
    ).read_text(encoding="utf-8")
    implementation = (
        ROOT / ".assistant" / "skills" / "work-item-management" / "references" /
        "implementation.md"
    ).read_text(encoding="utf-8")
    multi_session = (
        ROOT / ".assistant" / "skills" / "work-item-management" / "references" /
        "multi-session.md"
    ).read_text(encoding="utf-8")
    quick_reference = (
        ROOT / ".assistant" / "references" / "odibi-anchor" / "quick-reference.md"
    ).read_text(encoding="utf-8")
    memory_docs = (ROOT / "docs" / "tools" / "memory_context.md").read_text(encoding="utf-8")

    assert planning.index("Think — Understand Before Planning") < planning.index(
        "Call `anchor(\"task\")` with Relevant Context"
    ) < planning.index("Once `anchor(\"task\")` returns an accepted task")
    assert implementation.index("Define Acceptance Criteria") < implementation.index(
        "Post-Acceptance Memory Review"
    )
    think_section = multi_session.split("## Step 2: PLAN", 1)[0]
    assert "accepted task memory_context" not in think_section
    assert "After task acceptance and before writing code" in multi_session
    assert "step 4 must also pass" in quick_reference
    assert "step 6 must also pass" not in quick_reference
    assert "At the start of every session" not in memory_docs
    assert "After a substantive task is accepted" in memory_docs

    pre_task_references = (
        ROOT / ".assistant" / "skills" / "debugging" / "references" / "planning.md",
        ROOT / ".assistant" / "skills" / "work-item-management" / "references" /
        "integration.md",
        ROOT / ".assistant" / "skills" / "data-operations" / "references" / "planning.md",
        ROOT / ".assistant" / "skills" / "data-reconciliation" / "references" / "backbone.md",
        ROOT / ".assistant" / "skills" / "work-item-management" / "references" /
        "refactoring.md",
        ROOT / ".assistant" / "skills" / "work-item-management" / "references" /
        "migration.md",
        ROOT / ".assistant" / "skills" / "writing-tests" / "references" / "planning.md",
    )
    for path in pre_task_references:
        text = path.read_text(encoding="utf-8")
        assert 'anchor("memory_tags"' not in text
        assert "bounded `memory_context`" in text or "bounded task `memory_context`" in text
    assert "Don't re-derive" not in pre_task_references[0].read_text(encoding="utf-8")

    runtime_guidance = (
        ROOT / "src" / "odibi_anchor" / "_dispatcher" / "_frame.py",
        ROOT / "src" / "odibi_anchor" / "codebase" / "safe_change_context.py",
        ROOT / "src" / "odibi_anchor" / "_utils" / "_session_state.py",
        ROOT / "src" / "odibi_anchor" / "_dispatcher" / "_compliance.py",
    )
    for path in runtime_guidance:
        assert "anchor('memory_tags'" not in path.read_text(encoding="utf-8")

    workflow = (
        ROOT / ".assistant" / "references" / "odibi-anchor" / "workflow.md"
    ).read_text(encoding="utf-8")
    planner_suggestions = workflow.split("## Task Planner Mode-Aware Suggestions", 1)[1].split(
        "## Enforcement Mechanisms", 1
    )[0]
    assert 'anchor("memory")' not in planner_suggestions
    assert 'task_result["memory_context"]' in planner_suggestions

    memory_saving = (
        ROOT / ".assistant" / "skills" / "work-item-management" / "references" /
        "memory-saving.md"
    ).read_text(encoding="utf-8")
    assert 'anchor("memory", task_type="general")' not in memory_saving
    assert 'anchor("memory_tags"' not in memory_saving
    assert 'task_result["memory_context"]["selections"]' in memory_saving

    notebook_path = ROOT / "agent_workspace.ipynb"
    if notebook_path.exists():
        notebook = json.loads(notebook_path.read_text(encoding="utf-8"))
        notebook_source = "".join(
            "".join(cell.get("source", [])) for cell in notebook["cells"]
        )
        bootstrap_source = "".join(notebook["cells"][1].get("source", []))
        assert 'anchor("memory")' not in notebook_source
        assert "runpy.run_path(bootstrap_path)" in bootstrap_source
        assert "sys.path.insert" not in bootstrap_source
        assert "odibi_anchor.bootstrap import init" not in bootstrap_source
        assert notebook_source.index('anchor("status")') < notebook_source.index(
            'anchor("audit_history")'
        ) < notebook_source.index('anchor("new_session"') < notebook_source.index('anchor(\n    "task"')
        assert 'result["memory_context"]' in notebook_source

    memory_runtime = (
        ROOT / "src" / "odibi_anchor" / "codebase" / "memory_context.py"
    ).read_text(encoding="utf-8")
    learn_runtime = (
        ROOT / "src" / "odibi_anchor" / "codebase" / "learn_context.py"
    ).read_text(encoding="utf-8")
    assert "MUST: If you solve a novel problem" not in memory_runtime
    assert "MUST: Call memory_context at next session start" not in learn_runtime

    boot_runtime = (
        ROOT / "src" / "odibi_anchor" / "_dispatcher" / "_boot.py"
    ).read_text(encoding="utf-8")
    assert "_db_query_memories" not in boot_runtime
    assert "Active memories:" not in boot_runtime
    assert "bounded retrieval deferred to task acceptance" in boot_runtime

    spec_runtime = (
        ROOT / "src" / "odibi_anchor" / "_dispatcher" / "_spec.py"
    ).read_text(encoding="utf-8")
    assert "memory_context(" not in spec_runtime
    assert "prior_learnings" not in spec_runtime

    forced_capture_runtime = (
        ROOT / "src" / "odibi_anchor" / "codebase" /
        "known_bad_change_context.py",
        ROOT / "src" / "odibi_anchor" / "debugging" /
        "failure_pattern_context.py",
        ROOT / "src" / "odibi_anchor" / "codebase" /
        "workflow_gate_context.py",
        ROOT / "src" / "odibi_anchor" / "codebase" /
        "framework_lookup_context.py",
        ROOT / "src" / "odibi_anchor" / "planning" / "handoff_context.py",
    )
    for path in forced_capture_runtime:
        text = path.read_text(encoding="utf-8")
        assert "MUST: Run anchor('save'" not in text
        assert 'SHOULD: Run anchor("save"' not in text

    forced_capture_guidance = (
        ROOT / ".assistant" / "skills" / "dependency-management" / "references" /
        "backbone.md",
        ROOT / ".assistant" / "skills" / "debugging" / "references" / "recovery.md",
        ROOT / ".assistant" / "skills" / "performance-investigation" / "references" /
        "backbone.md",
        ROOT / ".assistant" / "skills" / "incident-response" / "references" /
        "backbone.md",
        ROOT / ".assistant" / "skills" / "schema-design" / "references" / "backbone.md",
    )
    for path in forced_capture_guidance:
        assert 'anchor("save"' not in path.read_text(encoding="utf-8")


def test_instruction_routing_matrix_covers_and_traces_native_ownership():
    instructions = (ROOT / ".assistant_instructions.md").read_text(encoding="utf-8")
    match = re.search(
        r"^## Route native skills by material intent\s*$\n(.*?)(?=^##\s)",
        instructions,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match, "bounded native routing section is missing"
    section = match.group(1)
    table_lines = [line for line in section.splitlines() if line.startswith("|")]
    assert len(table_lines) == 19
    assert table_lines[0] == "| Native skill | Apply for this material intent | Do not apply for |"
    assert re.fullmatch(r"\|(?:\s*---\s*\|){3}", table_lines[1])

    rows = {}
    for line in table_lines[2:]:
        name, positive, negative = [
            cell.strip().strip("`") for cell in line.strip("|").split("|")
        ]
        assert name not in rows
        rows[name] = (positive, negative)

    skill_descriptions = {}
    for skill_file in sorted((ROOT / ".assistant" / "skills").glob("*/SKILL.md")):
        text = skill_file.read_text(encoding="utf-8")
        frontmatter = text.split("---\n", 2)[1]
        fields = dict(
            line.split(":", 1) for line in frontmatter.splitlines() if ":" in line
        )
        skill_descriptions[fields["name"].strip()] = fields["description"].strip()
    assert set(rows) == set(skill_descriptions)
    assert len(rows) == 17

    stopwords = {
        "a", "an", "and", "as", "at", "be", "do", "for", "from", "in",
        "is", "it", "merely", "not", "of", "on", "or", "the", "this", "to",
        "use", "when", "with", "without",
    }

    def concepts(value):
        return {
            token for token in re.findall(r"[a-z][a-z-]+", value.casefold())
            if token not in stopwords and len(token) > 2
        }

    for name, (positive, negative) in rows.items():
        clauses = re.split(
            r";\s*do not (?:use|trigger)\b", skill_descriptions[name], maxsplit=1,
            flags=re.IGNORECASE,
        )
        assert len(clauses) == 2, f"{name} frontmatter lacks a negative clause"
        positive_concepts, negative_concepts = concepts(positive), concepts(negative)
        assert len(positive_concepts) >= 3 and len(negative_concepts) >= 3
        assert positive_concepts & concepts(clauses[0]), name
        assert negative_concepts & concepts(clauses[1]), name

    lowered = section.casefold()
    assert "multiple skills only when each owns a material outcome" in lowered
    assert "select and apply through the host" in lowered
    assert 'anchor("skill_loaded", "<name>")' in section
    for concept in ("complete resolved content", "before registering", "not comprehension"):
        assert concept in lowered


def test_independent_pr_review_uses_host_adapter_or_portable_reference():
    instructions = " ".join(
        (ROOT / ".assistant_instructions.md").read_text(encoding="utf-8").casefold().split()
    )
    reference_path = (
        ROOT / ".assistant" / "references" / "reviewing" / "independent-pr-review.md"
    )
    reference = " ".join(reference_path.read_text(encoding="utf-8").casefold().split())
    cross_functional = " ".join((
        ROOT / ".assistant" / "skills" / "cross-functional-pr" / "SKILL.md"
    ).read_text(encoding="utf-8").casefold().split())

    assert "independent pr review uses universal guidance outside the native skill registry" in instructions
    assert "fallback routing is explicit" in instructions
    assert "`reviewing-pull-requests`" in instructions
    assert "otherwise load and follow `.assistant/references/reviewing/independent-pr-review.md`" in instructions
    assert "never route independent review to `cross-functional-pr`" in instructions
    assert "keep the native distribution aligned with the canonical registry" in instructions
    assert "review intent first and implementation second" in reference
    assert "`work-pr-reviews`" in reference and "`personal-pr-reviews`" in reference
    assert "exact target and source commit shas" in reference
    assert "passing tests, mergeability, or a context workbench gate does not establish" in reference
    assert "apply the data-engineering lens" in reference
    assert "prepare an explicit pr" in cross_functional
    assert "do not use for independent pr review" in cross_functional
    assert not (ROOT / ".assistant" / "skills" / "reviewing-pull-requests").exists()
    assert len(list((ROOT / ".assistant" / "skills").glob("*/SKILL.md"))) == 17


def test_asana_task_authoring_is_a_bounded_work_item_reference_not_a_new_skill():
    skill_path = ROOT / ".assistant" / "skills" / "work-item-management" / "SKILL.md"
    reference_path = skill_path.parent / "references" / "asana-task-authoring.md"
    skill = skill_path.read_text(encoding="utf-8")
    reference = reference_path.read_text(encoding="utf-8")
    normalized_reference = " ".join(reference.split())

    assert "you MUST read and apply [Asana task authoring]" in skill
    assert "before drafting, creating, commenting on, or materially updating" in skill
    assert reference.startswith("# Asana task authoring\n")
    assert "Search before creation" in reference
    assert "## Definition of ready" in reference
    assert "## Definition of done" in reference
    assert "Post-write re-read result" in reference
    assert "organization-specific field mappings and examples" in reference
    assert "does not create granular Asana OAuth scopes" in normalized_reference
    assert "requests access to all tools available" in normalized_reference
    assert "report that limitation rather than claiming exact verification" in normalized_reference

    tool_section = reference.split("## Recommended 15-tool MCP baseline", 1)[1].split(
        "## Write authority matrix", 1
    )[0]
    read_section, write_section = tool_section.split("### Controlled write tools", 1)
    read_tools = set(re.findall(r"^\d+\. `([^`]+)`$", read_section, flags=re.MULTILINE))
    write_tools = set(re.findall(r"^\d+\. `([^`]+)`$", write_section, flags=re.MULTILINE))
    assert read_tools == {
        "search_objects", "get_task", "get_tasks", "get_my_tasks", "search_tasks",
        "get_project", "get_projects", "get_status_overview", "get_attachments",
        "get_me", "get_users",
    }
    assert write_tools == {
        "create_tasks", "update_tasks", "add_comment", "create_project_status_update",
    }
    assert "delete_task" not in read_tools | write_tools
    assert "create_project" not in read_tools | write_tools

    matrix = reference.split("## Write authority matrix", 1)[1].split(
        "## Required authoring workflow", 1
    )[0]
    for action in (
        "Create one or more tasks", "Change title or description", "Complete, reopen",
        "Add a comment", "Post a project status update", "Bulk create or update",
    ):
        row = next(line for line in matrix.splitlines() if action in line)
        assert "Blocked" in row
    assert len(list((ROOT / ".assistant" / "skills").glob("*/SKILL.md"))) == 17


def test_memory_governance_owner_is_narrow_complete_and_packaged():
    from odibi_anchor._dispatcher._guidance import NATIVE_SKILLS

    name = "auditing-memory-governance"
    skill = (ROOT / ".assistant" / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    normalized = " ".join(skill.split())
    instructions = (ROOT / ".assistant_instructions.md").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert name in NATIVE_SKILLS
    assert f"| `{name}` |" in instructions
    for phrase in (
        "reviewed seeds", "cross-thread harvest", "candidate-only advisory authority",
        "Never scan the entire store by default", "never mutate SQLite directly",
        "employer content", "installed/public path", "nothing_reusable_learned",
        "unavailable", "Ordinary substantive tasks",
    ):
        assert phrase in normalized
    assert 'only-include = ["src/odibi_anchor", ".assistant", ".assistant_instructions.md", "tools"]' in pyproject


def test_memory_supply_owners_are_complete_distinct_and_packaged():
    from odibi_anchor._dispatcher._guidance import NATIVE_SKILLS

    skills_root = ROOT / ".assistant" / "skills"
    instructions = (ROOT / ".assistant_instructions.md").read_text(encoding="utf-8")
    expected = {
        "authoring-governed-memories": (
            "one atomic, falsifiable claim", "candidate-only", "retrieval cues",
            "typed verifier", "Supersede", "Never store secrets",
        ),
        "building-memory-packs": (
            "complete, reviewable evidence artifact", "10 to 30 high-signal candidates",
            "three representative real tasks", "trust domain", "Deduplicate semantically",
            "auditing-memory-governance",
        ),
    }
    for name, phrases in expected.items():
        skill = (skills_root / name / "SKILL.md").read_text(encoding="utf-8")
        normalized = " ".join(skill.split())
        assert name in NATIVE_SKILLS
        assert f"| `{name}` |" in instructions
        for phrase in phrases:
            assert phrase in normalized


def test_full_tree_has_no_removed_native_links_or_migration_language():
    assistant = ROOT / ".assistant"
    forbidden_paths = ("aliases.json", ".crc", "__pycache__")
    forbidden_language = ("legacy skill", "redirect stub", "canonical pack")
    removed_roots = ("planning-delivery/", "data-engineering/", "debug-operations/", "testing-validation/")
    for path in assistant.rglob("*"):
        assert not any(part in forbidden_paths or part.endswith(".crc") for part in path.parts)
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8").casefold()
        assert not any(term in text for term in forbidden_language), path
        assert not any(f"skills/{root}" in text for root in removed_roots), path


def test_all_recipe_and_pack_body_lessons_have_named_destination_headings():
    assert len(RECIPE_LEDGER) == 46
    assert len(PACK_BODY_LEDGER) == 8
    assistant = ROOT / ".assistant"
    destinations = set(RECIPE_LEDGER.values()) | set(PACK_BODY_LEDGER.values())
    assert set(DESTINATION_HEADINGS) == destinations
    for source, destination in {**RECIPE_LEDGER, **PACK_BODY_LEDGER}.items():
        path = ROOT / destination if destination == ".assistant_instructions.md" else assistant / destination
        assert path.is_file(), f"{source} destination is missing: {destination}"
        headings = [line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("#")]
        expected_heading = DESTINATION_HEADINGS[destination]
        assert expected_heading in headings, (
            f"{source} lost accountable destination heading "
            f"{expected_heading!r} in {destination}"
        )


def test_representative_unique_lessons_survive_in_accountable_destinations():
    assistant = ROOT / ".assistant"
    for destination, phrases in SEMANTIC_PRESERVATION.items():
        text = (assistant / destination).read_text(encoding="utf-8").casefold()
        for phrase in phrases:
            assert phrase.casefold() in text, f"{destination} lost lesson: {phrase}"


def test_removed_names_cannot_survive_as_discovery_or_registration_targets():
    assistant = ROOT / ".assistant"
    immediate = {path.name for path in (assistant / "skills").iterdir()}
    assert not immediate & REMOVED_NATIVE_NAMES
    removed = "|".join(re.escape(name) for name in sorted(REMOVED_NATIVE_NAMES, key=len, reverse=True))
    active_pattern = re.compile(
        rf"skills/(?:{removed})(?:/SKILL\.md)?|"
        rf"skill_loaded[\"']?\s*,\s*[\"'](?:{removed})[\"']|"
        rf"\[\[(?:{removed})\]\]|"
        rf"\bload\s+[`'\"]?(?:{removed})[`'\"]?(?:\s+skill)?|"
        rf"\buse\s+(?:the\s+)?[`'\"]?(?:{removed})[`'\"]?\s+skill",
        re.IGNORECASE,
    )
    for path in assistant.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        assert not active_pattern.search(text), path
        if path.name != "SKILL.md":
            assert not text.startswith("---\nname:"), path


def test_portable_onboarding_references_have_no_removed_private_profiler_contracts():
    onboarding = ROOT / ".assistant" / "skills" / "data-onboarding" / "references"
    excel = (onboarding / "excel.md").read_text(encoding="utf-8")
    json_text = (onboarding / "json.md").read_text(encoding="utf-8")
    backbone = (onboarding / "backbone.md").read_text(encoding="utf-8")
    combined = "\n".join((excel, json_text, backbone))
    for private_contract in (
        "extract_excel_structure", "extract_json_structure", "excel_profiler",
        "max_records", "from lib.",
    ):
        assert private_contract not in combined
    assert excel.count("expected_rows = len(pdf)") == 2
    assert "sample_limit = 1000" in json_text
    assert "for line_number, line in enumerate(stream, start=1)" in json_text
    assert "records = [json.loads" not in json_text
    assert 'profile = {' in json_text
    assert 'profile["record_count"]' in json_text


def test_json_onboarding_profiler_example_executes_for_json_and_jsonl(tmp_path):
    reference = (
        ROOT / ".assistant" / "skills" / "data-onboarding" / "references" / "json.md"
    ).read_text(encoding="utf-8")
    snippet = reference.split("```python", 1)[1].split("```", 1)[0]

    for suffix, content in (
        (".json", '[{"id": 1, "address": {"city": "A"}}, {"id": 2, "name": "two"}]'),
        (".jsonl", '{"id": 1, "address": {"city": "A"}}\n\n{"id": 2, "name": "two"}\n'),
    ):
        source = tmp_path / f"source{suffix}"
        source.write_text(content, encoding="utf-8")
        executable = snippet.replace(
            'path = Path("/path/to/file.json")', f"path = Path({str(source)!r})",
        )
        namespace = {}
        exec(compile(executable, "json-onboarding-example", "exec"), namespace)
        assert namespace["profile"] == {
            "record_count": 2,
            "sample_count": 2,
            "keys": ["address", "id", "name"],
            "schema_consistency_percent": 50.0,
            "types_by_key": {"address": ["dict"], "id": ["int"], "name": ["str"]},
        }


def test_json_onboarding_nested_read_preserves_struct_and_verifies_count(tmp_path):
    pytest.importorskip("pyspark")
    from pyspark.sql import SparkSession
    from pyspark.sql.types import StructType

    source = tmp_path / "nested.json"
    source.write_text(
        '[{"id": 1, "address": {"city": "A"}}, {"id": 2, "address": {"city": "B"}}]',
        encoding="utf-8",
    )
    try:
        spark = (
            SparkSession.builder.master("local[1]")
            .appName("anchor-json-onboarding-reference")
            .config("spark.ui.enabled", "false")
            .config("spark.sql.shuffle.partitions", "1")
            .getOrCreate()
        )
    except Exception as exc:
        pytest.skip(f"Local SparkSession unavailable: {exc}")
    try:
        df = spark.read.option("multiLine", True).json(str(source))
        assert df.count() == 2
        assert isinstance(df.schema["address"].dataType, StructType)
        assert df.columns == ["address", "id"]
    finally:
        spark.stop()


def test_active_debugging_calls_and_enforcement_language_match_runtime_contracts():
    assistant = ROOT / ".assistant"
    debugging = (assistant / "skills" / "debugging" / "references" / "backbone.md").read_text(
        encoding="utf-8",
    )
    incident = (
        assistant / "skills" / "incident-response" / "references" / "backbone.md"
    ).read_text(encoding="utf-8")
    active = debugging + incident
    for stale in (
        "diagnose-error", 'known_bad", symptom=', 'known_error", error=',
        'anchor("touched", "catalog.',
    ):
        assert stale not in active
    assert 'anchor("known_bad", error_text=' in active
    assert 'anchor("known_error", "target has 50% fewer rows' in debugging
    assert 'anchor("observe_table", source_profile, subject=' in debugging

    references = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (assistant / "references").rglob("*.md")
    )
    for stale in (
        ">3 SKILL: hints", "source-type sub-skills", "language-specific sub-skills",
        "/Workspace/Repos/eaai-common-resources/eaai-utilities",
        "Skills REQUIRED per mode", "before file-modifying actions proceed",
    ):
        assert stale not in references
    assert "clean unregistered changes are auto-registered" in references
    assert "non-exempt substantive action" in references


def _write_fake_bootstrap_checkout(checkout: Path) -> None:
    package = checkout / "src" / "odibi_anchor"
    package.mkdir(parents=True)
    dispatcher = package / "_dispatcher"
    dispatcher.mkdir()
    (checkout / "agent_bootstrap.py").write_bytes((ROOT / "agent_bootstrap.py").read_bytes())
    (package / "__init__.py").write_text("", encoding="utf-8")
    (dispatcher / "__init__.py").write_text("", encoding="utf-8")
    (dispatcher / "_project.py").write_text(
        textwrap.dedent(
            """
            def resolve_active_project(anchor_home, project):
                return None

            def resolve_route_binding(*args, **kwargs):
                return None
            """
        ),
        encoding="utf-8",
    )
    (dispatcher / "_boot.py").write_text(
        textwrap.dedent(
            """
            import os
            from pathlib import Path
            from types import SimpleNamespace

            home = Path(os.environ.get("ANCHOR_HOME", Path(__file__).parents[3])).resolve()
            def resolve_boot_environment(project_root=None):
                home = Path(os.environ.get("ANCHOR_HOME", Path(__file__).parents[3])).resolve()
                return {
                    "runtime_paths": SimpleNamespace(anchor_home=home),
                    "memory_db": os.environ.get("ANCHOR_MEMORY_DB", str(home / ".agent_memory.db")),
                }
            _ENV = resolve_boot_environment()
            """
        ),
        encoding="utf-8",
    )
    (package / "_repository_snapshot.py").write_text(
        "def canonical_local_git_available(target):\n    return False\n",
        encoding="utf-8",
    )
    (package / "bootstrap.py").write_text(
        textwrap.dedent(
            """
            import sys

            def init(root=None, **kwargs):
                sys.path.insert(0, root)
                def anchor(action, **call_kwargs):
                    if action == "orient":
                        return {
                            "kind": "orientation",
                            "status": {"kind": "status"},
                            "memory": {"kind": "memory"},
                            "audit_history": {"kind": "audit_history"},
                        }
                    return {"kind": action, "same_process": True}
                return anchor, root, {"kind": "manifest", "init_kwargs": kwargs}
            """
        ),
        encoding="utf-8",
    )


def _run_fake_checkout(checkout: Path, cwd: Path, environment: dict[str, str]) -> dict:
    script = textwrap.dedent(
        """
        import json
        import os
        import runpy
        import sys
        from pathlib import Path

        source = str(Path(sys.argv[1]).parent / "src")
        sys.path.extend([source, source])
        namespace = runpy.run_path(sys.argv[1])
        continued = namespace["anchor"]("status", output_format="dict")
        print(json.dumps({
            "bootstrap": namespace["BOOTSTRAP"],
            "continued": continued,
            "anchor_home": os.environ.get("ANCHOR_HOME"),
            "memory_db": os.environ.get("ANCHOR_MEMORY_DB"),
            "src_first": sys.path[0],
            "src_count": sys.path.count(source),
        }))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(checkout / "agent_bootstrap.py")],
        cwd=cwd,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_agent_bootstrap_runs_from_arbitrary_cwd_and_preserves_process_state(tmp_path):
    checkout = tmp_path / "git-folder" / "odibi_anchor"
    elsewhere = tmp_path / "compute-cwd"
    elsewhere.mkdir()
    _write_fake_bootstrap_checkout(checkout)
    preimage = {
        path.relative_to(checkout).as_posix(): path.read_bytes()
        for path in checkout.rglob("*")
        if path.is_file()
    }
    environment = dict(os.environ)
    for name in ("PYTHONPATH", "ANCHOR_HOME", "ANCHOR_MEMORY_DB"):
        environment.pop(name, None)

    result = _run_fake_checkout(checkout, elsewhere, environment)

    expected_home = checkout
    assert result["bootstrap"] == {
        "success": True,
        "kind": "agent_bootstrap",
        "repository": str(checkout),
        "state_home": str(expected_home),
        "memory_db": str(expected_home / ".agent_memory.db"),
        "requested_target": str(checkout),
        "effective_root": str(checkout),
        "orientation_kind": "orientation",
        "repository_evidence": {
            "kind": "unavailable",
            "provider_id": None,
            "acquisition_outcome": "unavailable",
            "reason": "target_is_not_a_canonical_local_git_worktree",
            "capabilities": {
                "host_repository_identity": "unavailable",
                "local_worktree_status": "unavailable",
                "git_changed_paths_and_diff": "unavailable",
                "merge_base_and_history": "unavailable",
                "task_scoped_content_diff": "unavailable",
                "task_scoped_write_tracking": "unavailable",
                "pr_readiness": "unavailable",
            },
        },
    }
    assert result["continued"] == {"kind": "status", "same_process": True}
    assert result["src_first"] == str(checkout / "src")
    assert result["src_count"] == 1
    assert result["anchor_home"] is None
    assert result["memory_db"] is None
    assert {
        path.relative_to(checkout).as_posix(): path.read_bytes()
        for path in checkout.rglob("*")
        if path.is_file()
    } == preimage
    assert not list(checkout.rglob("__pycache__"))


def test_agent_bootstrap_honors_database_only_override(tmp_path):
    checkout = tmp_path / "git-folder" / "odibi_anchor"
    elsewhere = tmp_path / "compute-cwd"
    elsewhere.mkdir()
    _write_fake_bootstrap_checkout(checkout)
    explicit_db = tmp_path / "other-state" / "memory.sqlite"
    environment = dict(os.environ)
    for name in ("PYTHONPATH", "ANCHOR_HOME"):
        environment.pop(name, None)
    environment["ANCHOR_MEMORY_DB"] = str(explicit_db)

    result = _run_fake_checkout(checkout, elsewhere, environment)

    expected_home = checkout
    assert result["anchor_home"] is None
    assert result["memory_db"] == str(explicit_db)
    assert result["bootstrap"]["memory_db"] == str(explicit_db)
    assert expected_home.is_dir()


def test_source_entrypoints_share_fresh_clone_routing_across_processes(tmp_path):
    checkout = tmp_path / "fresh-checkout"
    shutil.copytree(
        ROOT,
        checkout,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", "*.pyc", ".agent_memory.db",
            ".anchor_manifest.json", ".anchor_session_state.json", ".session_snapshot.json",
            ".patch_log.jsonl", "workspace",
        ),
    )
    selector = checkout / "workspace" / ".active_project"
    selector.unlink(missing_ok=True)
    target_override = tmp_path / "target-override"
    target_override.mkdir()
    external_home = tmp_path / "external-state"
    external_home.mkdir()
    default_state = tmp_path / "default-state"
    default_state.mkdir()
    environment = dict(os.environ)
    for name in ("ANCHOR_HOME", "ANCHOR_MEMORY_DB", "ANCHOR_PROFILE", "PYTHONPATH", "DATABRICKS_RUNTIME_VERSION"):
        environment.pop(name, None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    # Provide ANCHOR_HOME for calls that need a valid state directory but are not
    # testing profile-based routing.
    env_with_state = {**environment, "ANCHOR_HOME": str(default_state),
                      "ANCHOR_MEMORY_DB": str(default_state / ".agent_memory.db")}
    python = sys.executable

    script = textwrap.dedent(
        """
        import json, os, runpy, sys
        from odibi_anchor.bootstrap import init
        mode, checkout, override = sys.argv[1:]
        if mode == "launcher":
            namespace = runpy.run_path(checkout + "/agent_bootstrap.py")
            anchor, root = namespace["anchor"], namespace["ROOT"]
            state_home = namespace["BOOTSTRAP"]["state_home"]
            memory_db = namespace["BOOTSTRAP"]["memory_db"]
        else:
            anchor, root, _ = init(root=override or None, output_format="dict")
            state_home = None
            from odibi_anchor._dispatcher._boot import _ENV
            memory_db = _ENV["memory_db"]
        projects = anchor("project", "list", output_format="dict")
        state_home = state_home or projects["anchor_home"]
        status = anchor("status", output_format="dict")
        print("RESULT=" + json.dumps({
            "root": root, "state_home": state_home,
            "memory_db": memory_db,
            "active": status["runtime"]["active_project"],
            "projects": [item["id"] for item in projects["projects"]],
            "environment": {name: os.environ.get(name) for name in ("ANCHOR_HOME", "ANCHOR_MEMORY_DB")},
        }))
        """
    )

    def run(mode="direct", *, override="", env=env_with_state):
        result = subprocess.run(
            [python, "-c", script, mode, str(checkout), override], cwd=checkout,
            env={**env, "PYTHONPATH": str(checkout / "src")}, check=True,
            capture_output=True, text=True,
        )
        return json.loads(result.stdout.rsplit("RESULT=", 1)[1])

    no_selector = run()
    # After source/state separation, workspace/ is not in the repo so
    # a fresh clone has no discoverable projects.
    assert no_selector["active"] is None
    overridden = run(override=str(target_override))
    assert overridden["root"] == str(target_override)
    assert overridden["state_home"] == str(default_state)

    # After source/state separation, workspace/ is no longer tracked in the
    # repo, so the review-response-lifecycle project is not in a fresh clone.
    # Guard the CLI-batch project-selection phase accordingly.
    if (checkout / "workspace" / "projects" / "review-response-lifecycle").exists():
        cli = subprocess.run(
            [python, "-c", "from odibi_anchor.cli import main; main()", "--root", str(checkout),
             "batch", "-"],
            cwd=checkout, env={**environment, "PYTHONPATH": str(checkout / "src")},
            input=json.dumps([
                {"action": "status"}, {"action": "audit_history"},
                {"action": "new_session", "kwargs": {"name": "fresh-setup"}},
                {"action": "task", "args": ["Restore tracked project selector"], "kwargs": {
                    "goal": "Select the tracked project", "mode": "implementation",
                    "work_type": "operate", "execution_mode": "artifact_only",
                    "acceptance_criteria": ["Selector persists across processes"],
                }},
                {"action": "project", "args": ["use", "review-response-lifecycle"]},
            ]), check=True, capture_output=True, text=True,
        )
        assert cli.returncode == 0
        direct = run()
        launcher = run("launcher")
        assert direct == launcher
        assert direct["active"] == "review-response-lifecycle"
        assert direct["root"] == str(checkout)

    explicit_environment = {**environment, "ANCHOR_HOME": str(external_home)}
    external = run("launcher", env=explicit_environment)
    assert external["state_home"] == str(external_home)
    assert external["root"] == str(checkout)
    assert external["active"] is None

    profile_home = tmp_path / "profile-state"
    profile_memory = tmp_path / "profile-memory" / "knowledge.sqlite"
    profile_home.mkdir()
    (checkout / ".anchor_config.json").write_text(json.dumps({"environment": {"profiles": {
        "selected": {"platform": "test", "anchor_root": str(profile_home),
                     "memory_db": str(profile_memory)},
        "databricks": {"platform": "databricks", "anchor_root": str(profile_home),
                       "memory_db": str(profile_memory)},
    }}}), encoding="utf-8")
    profile_environment = {**environment, "ANCHOR_PROFILE": "selected"}
    selected_direct = run(override=str(checkout), env=profile_environment)
    selected_launcher = run("launcher", env=profile_environment)
    assert selected_direct == selected_launcher
    assert selected_direct["state_home"] == str(profile_home)
    assert selected_direct["memory_db"] == str(profile_memory)
    assert selected_direct["root"] == str(checkout)
    assert selected_direct["environment"] == {"ANCHOR_HOME": None, "ANCHOR_MEMORY_DB": None}
    profile_cli = subprocess.run(
        [python, "-c", "from odibi_anchor.cli import main; main()", "--root", str(checkout),
         "batch", "-"],
        cwd=checkout, env={**profile_environment, "PYTHONPATH": str(checkout / "src")},
        input=json.dumps([{"action": "project", "args": ["list"]}, {"action": "status"}]),
        check=True, capture_output=True, text=True,
    )
    assert str(profile_home) in profile_cli.stdout
    assert str(checkout) in profile_cli.stdout

    auto_environment = {**environment, "DATABRICKS_RUNTIME_VERSION": "test-runtime"}
    auto_direct = run(override=str(checkout), env=auto_environment)
    auto_launcher = run("launcher", env=auto_environment)
    assert auto_direct == auto_launcher
    assert auto_direct["state_home"] == str(profile_home)
    assert auto_direct["memory_db"] == str(profile_memory)


def test_agent_bootstrap_restores_active_managed_project_target(tmp_path):
    state = tmp_path / "state"
    target = tmp_path / "managed-target"
    target.mkdir()
    script = textwrap.dedent(
        """
        import json
        import os
        import runpy
        import sys

        entrypoint, target = sys.argv[1:]
        first = runpy.run_path(entrypoint)
        from odibi_anchor._dispatcher._project import project_action
        project_action(
            os.environ["ANCHOR_HOME"],
            "create",
            name="managed-project",
            target=target,
            output_format="dict",
        )
        second = runpy.run_path(entrypoint)
        project = second["anchor"]("project", "status", output_format="dict")
        print("RESULT=" + json.dumps({
            "root": second["ROOT"],
            "requested_target": second["BOOTSTRAP"]["requested_target"],
            "active_project": project["active_project"],
            "reinitialize_required": project["reinitialize_required"],
        }))
        """
    )
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["ANCHOR_HOME"] = str(state)
    environment["ANCHOR_MEMORY_DB"] = str(state / ".agent_memory.db")
    result = subprocess.run(
        [sys.executable, "-c", script, str(ROOT / "agent_bootstrap.py"), str(target)],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout.rsplit("RESULT=", 1)[1])
    assert payload == {
        "root": str(target),
        "requested_target": None,
        "active_project": "managed-project",
        "reinitialize_required": False,
    }


def test_agent_bootstrap_explicit_project_uses_registered_external_target(tmp_path):
    state = tmp_path / "state"
    target = tmp_path / "referenced-target"
    target.mkdir()
    script = textwrap.dedent(
        """
        import json
        import os
        import runpy
        import sys

        entrypoint, target = sys.argv[1:]
        first = runpy.run_path(entrypoint)
        from odibi_anchor._dispatcher._project import project_action
        project_action(
            os.environ["ANCHOR_HOME"],
            "create",
            name="referenced-project",
            target=target,
            output_format="dict",
        )
        os.environ["ANCHOR_PROJECT_ID"] = "referenced-project"
        second = runpy.run_path(entrypoint)
        binding = second["_route_binding"]
        print("RESULT=" + json.dumps({
            "root": second["ROOT"],
            "project_id": binding.project_id,
            "target_root": binding.target_root,
            "binding_source": binding.binding_source,
        }))
        """
    )
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["ANCHOR_HOME"] = str(state)
    environment["ANCHOR_MEMORY_DB"] = str(state / ".agent_memory.db")
    result = subprocess.run(
        [sys.executable, "-c", script, str(ROOT / "agent_bootstrap.py"), str(target)],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout.rsplit("RESULT=", 1)[1])
    assert payload == {
        "root": str(target),
        "project_id": "referenced-project",
        "target_root": str(target),
        "binding_source": "explicit",
    }


def test_agent_bootstrap_recomputes_profile_after_boot_module_was_cached(tmp_path):
    checkout = tmp_path / "checkout"
    shutil.copytree(
        ROOT,
        checkout,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", "*.pyc", ".agent_memory.db",
            ".anchor_manifest.json", ".anchor_session_state.json", ".session_snapshot.json",
            ".patch_log.jsonl",
        ),
    )
    homes = {name: tmp_path / f"state-{name}" for name in ("a", "b")}
    memories = {name: tmp_path / f"memory-{name}.sqlite" for name in ("a", "b")}
    targets = {name: tmp_path / f"target-{name}" for name in ("a", "b")}
    for home in homes.values():
        home.mkdir()
    for target in targets.values():
        target.mkdir()
    (checkout / ".anchor_config.json").write_text(json.dumps({"environment": {"profiles": {
        name: {"anchor_root": str(homes[name]), "memory_db": str(memories[name])}
        for name in homes
    }}}), encoding="utf-8")
    script = textwrap.dedent(
        """
        import json, os, runpy, sys
        entrypoint, target_a, target_b = sys.argv[1:]
        os.environ["ANCHOR_PROFILE"] = "a"
        import odibi_anchor._dispatcher._boot as cached_boot
        assert str(cached_boot._ENV["runtime_paths"].anchor_home).endswith("state-a")
        from odibi_anchor._dispatcher._project import project_action
        project_action(cached_boot._ENV["runtime_paths"].anchor_home, "create",
                       name="project-a", target=target_a, output_format="dict")
        os.environ["ANCHOR_PROFILE"] = "b"
        current_home = cached_boot.resolve_boot_environment()["runtime_paths"].anchor_home
        project_action(current_home, "create", name="project-b", target=target_b,
                       output_format="dict")
        results = []
        for profile in ("b", "a"):
            os.environ["ANCHOR_PROFILE"] = profile
            namespace = runpy.run_path(entrypoint)
            status = namespace["anchor"]("status", output_format="dict")
            results.append({
                "profile": profile,
                "state_home": namespace["BOOTSTRAP"]["state_home"],
                "memory_db": namespace["BOOTSTRAP"]["memory_db"],
                "requested_target": namespace["BOOTSTRAP"]["requested_target"],
                "effective_root": namespace["BOOTSTRAP"]["effective_root"],
                "returned_root": namespace["ROOT"],
                "active": status["runtime"]["active_project"],
            })
        print("RESULT=" + json.dumps(results))
        """
    )
    environment = dict(os.environ)
    for name in ("ANCHOR_HOME", "ANCHOR_MEMORY_DB", "ANCHOR_PROFILE", "DATABRICKS_RUNTIME_VERSION"):
        environment.pop(name, None)
    environment.update({"PYTHONPATH": str(checkout / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
    result = subprocess.run(
        [sys.executable, "-c", script, str(checkout / "agent_bootstrap.py"),
         str(targets["a"]), str(targets["b"])],
        cwd=checkout, env=environment, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.rsplit("RESULT=", 1)[1])
    for item in payload:
        profile = item.pop("profile")
        assert item == {
            "state_home": str(homes[profile]),
            "memory_db": str(memories[profile]),
            "requested_target": None,
            "effective_root": str(targets[profile]),
            "returned_root": str(targets[profile]),
            "active": f"project-{profile}",
        }


@pytest.mark.parametrize("root_matches_target", [True, False])
def test_agent_bootstrap_auto_configures_active_databricks_target(
    monkeypatch, tmp_path, root_matches_target
):
    import odibi_anchor._dispatcher._project as project_module
    import odibi_anchor.bootstrap as bootstrap
    import odibi_anchor.operational._databricks as databricks_module

    target = Path("/Workspace/Users/test@example.invalid/Python-Mastery")
    provider = type(
        "ActiveTargetProvider",
        (),
        {"provider_id": "test.active-target", "capture_identity": lambda *_args: None},
    )()
    evidence = {
        "kind": "databricks_git_folder",
        "provider_id": provider.provider_id,
        "acquisition_outcome": "available",
        "reason": None,
        "capabilities": {"local_worktree_status": "unavailable"},
        "identity": {"head_sha": "a" * 40, "branch": "main"},
    }
    factory_targets = []
    observed = {}

    monkeypatch.setattr(
        project_module,
        "resolve_active_project",
        lambda *_args: {
            "project_id": "python-mastery",
            "project_root": str(tmp_path / "state" / "workspace" / "projects" / "python-mastery"),
            "artifact_root": str(tmp_path / "state" / "workspace" / "projects" / "python-mastery"),
            "target_root": str(target),
            "project_type": "referenced",
        },
    )

    def fake_autoconfigure(candidate):
        factory_targets.append(Path(candidate))
        return provider, evidence

    def fake_init(*, root, output_format, repository_provider):
        observed.update(
            root=root,
            output_format=output_format,
            repository_provider=repository_provider,
        )

        def anchor(action, **_kwargs):
            assert action == "orient"
            return {
                "kind": "orientation",
                "status": {"kind": "status"},
                "memory": {"kind": "memory"},
                "audit_history": {"kind": "audit_history"},
            }

        effective_root = target if root_matches_target else target.parent / "Other-Project"
        return anchor, str(effective_root), {"kind": "manifest"}

    monkeypatch.setattr(databricks_module, "autoconfigure_databricks_git_folder_repository", fake_autoconfigure)
    monkeypatch.setattr(bootstrap, "init", fake_init)
    monkeypatch.setenv("ANCHOR_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(tmp_path / "state" / "memory.db"))
    original_path = list(sys.path)
    original_bytecode = sys.dont_write_bytecode
    try:
        namespace = runpy.run_path(str(ROOT / "agent_bootstrap.py"))
    finally:
        sys.path[:] = original_path
        sys.dont_write_bytecode = original_bytecode

    assert factory_targets == [target]
    assert observed == {
        "root": None,
        "output_format": "dict",
        "repository_provider": provider,
    }
    if root_matches_target:
        assert namespace["ROOT"] == str(target)
        assert namespace["REPOSITORY_EVIDENCE"] is evidence
        assert namespace["BOOTSTRAP"]["repository_evidence"] is evidence
    else:
        assert namespace["ROOT"] == str(target.parent / "Other-Project")
        assert namespace["REPOSITORY_EVIDENCE"]["kind"] == "unavailable"
        assert namespace["REPOSITORY_EVIDENCE"]["reason"] == (
            "effective_target_differs_from_attested_databricks_checkout"
        )


@pytest.mark.parametrize("root_matches_target", [True, False])
def test_agent_bootstrap_scopes_unavailable_active_databricks_target_evidence(
    monkeypatch, tmp_path, root_matches_target
):
    import odibi_anchor._dispatcher._project as project_module
    import odibi_anchor.bootstrap as bootstrap
    import odibi_anchor.operational._databricks as databricks_module

    target = Path("/Workspace/Users/test@example.invalid/Unavailable-Project")
    evidence = {
        "kind": "unavailable",
        "provider_id": None,
        "acquisition_outcome": "denied",
        "reason": "databricks_sdk_client_denied",
        "capabilities": {},
    }
    monkeypatch.setattr(
        project_module,
        "resolve_active_project",
        lambda *_args: {"project_id": "unavailable", "target_root": str(target)},
    )
    monkeypatch.setattr(
        databricks_module,
        "autoconfigure_databricks_git_folder_repository",
        lambda candidate: (None, evidence) if Path(candidate) == target else None,
    )

    def fake_init(**kwargs):
        assert kwargs == {"root": None, "output_format": "dict"}

        def anchor(action, **_kwargs):
            assert action == "orient"
            return {
                "kind": "orientation",
                "status": {"kind": "status"},
                "memory": {"kind": "memory"},
                "audit_history": {"kind": "audit_history"},
            }

        effective_root = target if root_matches_target else target.parent / "Other-Project"
        return anchor, str(effective_root), {"kind": "manifest"}

    monkeypatch.setattr(bootstrap, "init", fake_init)
    monkeypatch.setenv("ANCHOR_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(tmp_path / "state" / "memory.db"))
    original_path = list(sys.path)
    original_bytecode = sys.dont_write_bytecode
    try:
        namespace = runpy.run_path(str(ROOT / "agent_bootstrap.py"))
    finally:
        sys.path[:] = original_path
        sys.dont_write_bytecode = original_bytecode

    assert namespace["BOOTSTRAP"]["success"] is True
    if root_matches_target:
        assert namespace["BOOTSTRAP"]["repository_evidence"] is evidence
    else:
        assert namespace["BOOTSTRAP"]["repository_evidence"]["reason"] == (
            "effective_target_differs_from_attested_databricks_checkout"
        )


@pytest.mark.parametrize(
    "orientation",
    [
        {"kind": "not-orientation"},
        {
            "kind": "orientation",
            "status": {"kind": "status"},
            "memory": {"error": "unavailable"},
            "audit_history": {"kind": "audit_history"},
        },
    ],
)
def test_agent_bootstrap_never_claims_success_for_invalid_orientation(
    monkeypatch, tmp_path, orientation
):
    import odibi_anchor._dispatcher._project as project_module
    import odibi_anchor.bootstrap as bootstrap

    def fake_init(*, root, output_format):
        assert root == str(ROOT)
        assert output_format == "dict"
        return lambda action, **kwargs: orientation, root, {"kind": "manifest"}

    monkeypatch.setattr(bootstrap, "init", fake_init)
    monkeypatch.setattr(project_module, "resolve_active_project", lambda *_args: None)
    monkeypatch.setenv("ANCHOR_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(tmp_path / "state" / "memory.db"))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", os.environ.get("PYTHONDONTWRITEBYTECODE", ""))
    original_path = list(sys.path)
    original_bytecode = sys.dont_write_bytecode
    try:
        with pytest.raises(RuntimeError, match="orientation"):
            runpy.run_path(str(ROOT / "agent_bootstrap.py"))
    finally:
        sys.path[:] = original_path
        sys.dont_write_bytecode = original_bytecode


def test_agent_bootstrap_propagates_init_failure(monkeypatch, tmp_path):
    import odibi_anchor.bootstrap as bootstrap

    def failed_init(*, root, output_format):
        raise ValueError(f"init failed for {root} as {output_format}")

    monkeypatch.setattr(bootstrap, "init", failed_init)
    monkeypatch.setenv("ANCHOR_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(tmp_path / "state" / "memory.db"))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", os.environ.get("PYTHONDONTWRITEBYTECODE", ""))
    original_path = list(sys.path)
    original_bytecode = sys.dont_write_bytecode
    try:
        with pytest.raises(ValueError, match="init failed"):
            runpy.run_path(str(ROOT / "agent_bootstrap.py"))
    finally:
        sys.path[:] = original_path
        sys.dont_write_bytecode = original_bytecode


def test_agent_bootstrap_explicit_repository_provider_wins(monkeypatch, tmp_path):
    import odibi_anchor._dispatcher._project as project_module
    import odibi_anchor.bootstrap as bootstrap

    provider = type(
        "ExplicitProvider",
        (),
        {"provider_id": "test.explicit", "capture_identity": lambda *_args: None},
    )()
    observed: dict[str, object] = {}

    def fake_init(*, root, output_format, repository_provider):
        observed.update(
            root=root,
            output_format=output_format,
            repository_provider=repository_provider,
        )

        def anchor(action, **_kwargs):
            assert action == "orient"
            return {
                "kind": "orientation",
                "status": {"kind": "status"},
                "memory": {"kind": "memory"},
                "audit_history": {"kind": "audit_history"},
            }

        return anchor, root, {"kind": "manifest"}

    monkeypatch.setattr(bootstrap, "init", fake_init)
    monkeypatch.setattr(project_module, "resolve_active_project", lambda *_args: None)
    monkeypatch.setenv("ANCHOR_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(tmp_path / "state" / "memory.db"))
    original_path = list(sys.path)
    original_bytecode = sys.dont_write_bytecode
    try:
        namespace = runpy.run_path(
            str(ROOT / "agent_bootstrap.py"),
            init_globals={"ANCHOR_REPOSITORY_PROVIDER": provider},
        )
    finally:
        sys.path[:] = original_path
        sys.dont_write_bytecode = original_bytecode

    assert observed == {
        "root": str(ROOT),
        "output_format": "dict",
        "repository_provider": provider,
    }
    assert namespace["REPOSITORY_EVIDENCE"] == {
        "kind": "databricks_git_folder",
        "provider_id": "test.explicit",
        "acquisition_outcome": "deferred",
        "reason": "caller_supplied_provider_not_attested_during_bootstrap",
        "capabilities": {
            "host_repository_identity": "unavailable",
            "local_worktree_status": "unavailable",
            "git_changed_paths_and_diff": "unavailable",
            "merge_base_and_history": "unavailable",
            "task_scoped_content_diff": "unavailable",
            "task_scoped_write_tracking": "unavailable",
            "pr_readiness": "unavailable",
        },
    }


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ANCHOR_HOME", "relative-state"),
        ("ANCHOR_HOME", "~/state"),
        ("ANCHOR_MEMORY_DB", "relative-memory.db"),
        ("ANCHOR_MEMORY_DB", "~/memory.db"),
    ],
)
def test_agent_bootstrap_rejects_non_absolute_state_without_side_effects(
    monkeypatch, tmp_path, name, value
):
    safe_home = tmp_path / "safe-state"
    monkeypatch.setenv("ANCHOR_HOME", str(safe_home))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(safe_home / "memory.db"))
    monkeypatch.setenv(name, value)
    before = {key: os.environ.get(key) for key in ("ANCHOR_HOME", "ANCHOR_MEMORY_DB", "PYTHONDONTWRITEBYTECODE")}
    with pytest.raises(RuntimeError, match="absolute, non-tilde"):
        runpy.run_path(str(ROOT / "agent_bootstrap.py"))
    assert {key: os.environ.get(key) for key in before} == before
    assert not safe_home.exists()


def test_agent_bootstrap_rejects_empty_overlap_and_foreign_module(monkeypatch, tmp_path):
    original_path = list(sys.path)
    try:
        monkeypatch.setenv("ANCHOR_HOME", "")
        monkeypatch.delenv("ANCHOR_MEMORY_DB", raising=False)
        with pytest.raises(RuntimeError, match="ANCHOR_HOME is set but empty"):
            runpy.run_path(str(ROOT / "agent_bootstrap.py"))

        monkeypatch.setenv("ANCHOR_HOME", str(ROOT / "unsafe-state"))
        with pytest.raises(RuntimeError, match="ANCHOR_HOME must be outside"):
            runpy.run_path(str(ROOT / "agent_bootstrap.py"))

        foreign = type(sys)("odibi_anchor")
        foreign.__file__ = str(tmp_path / "foreign" / "odibi_anchor" / "__init__.py")
        monkeypatch.setitem(sys.modules, "odibi_anchor", foreign)
        monkeypatch.setenv("ANCHOR_HOME", str(tmp_path / "safe-state"))
        monkeypatch.setenv("ANCHOR_MEMORY_DB", str(tmp_path / "safe-state" / "memory.db"))
        before = {
            key: os.environ.get(key)
            for key in ("ANCHOR_HOME", "ANCHOR_MEMORY_DB", "PYTHONDONTWRITEBYTECODE")
        }
        with pytest.raises(RuntimeError, match="different Odibi Anchor"):
            runpy.run_path(str(ROOT / "agent_bootstrap.py"))
        assert {key: os.environ.get(key) for key in before} == before
        assert not (tmp_path / "safe-state").exists()
    finally:
        sys.path[:] = original_path


def test_agent_bootstrap_rejects_foreign_import_before_creating_state(monkeypatch, tmp_path):
    safe_home = tmp_path / "safe-state"
    foreign = type(
        "ForeignBootstrap",
        (),
        {"__file__": str(tmp_path / "foreign" / "odibi_anchor" / "bootstrap.py")},
    )()
    real_import_module = importlib.import_module

    def import_module(name, package=None):
        if name == "odibi_anchor.bootstrap":
            return foreign
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", import_module)
    monkeypatch.setenv("ANCHOR_HOME", str(safe_home))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(safe_home / "memory.db"))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", os.environ.get("PYTHONDONTWRITEBYTECODE", ""))
    original_path = list(sys.path)
    original_bytecode = sys.dont_write_bytecode
    try:
        with pytest.raises(RuntimeError, match="unexpected source"):
            runpy.run_path(str(ROOT / "agent_bootstrap.py"))
        assert not safe_home.exists()
    finally:
        sys.path[:] = original_path
        sys.dont_write_bytecode = original_bytecode


def _write_trampoline_target(
    checkout: Path,
    *,
    success: bool = True,
    repository: Path | None = None,
    omit: str | None = None,
    delegated_error: str | None = None,
    sentinel: Path | None = None,
) -> None:
    package = checkout / "src" / "odibi_anchor"
    package.mkdir(parents=True)
    (package / "bootstrap.py").write_text("# checkout marker\n", encoding="utf-8")
    statements = [
        "import os",
        "from pathlib import Path",
        "DELEGATED_NAME = __name__",
        "DELEGATED_GLOBALS = {",
        "    'provider': globals().get('ANCHOR_REPOSITORY_PROVIDER'),",
        "    'unrelated': globals().get('UNRELATED'),",
        "}",
    ]
    if sentinel is not None:
        statements.append(f"Path({str(sentinel)!r}).write_text('delegated', encoding='utf-8')")
    if delegated_error is not None:
        statements.append(f"raise ValueError({delegated_error!r})")
    values = {
        "anchor": (
            "lambda action, **kwargs: "
            "{'kind': action, 'pid': os.getpid(), 'kwargs': kwargs}"
        ),
        "ROOT": repr(str(checkout / "external-target")),
        "MANIFEST": "{'kind': 'manifest'}",
        "ORIENTATION": "{'kind': 'orientation'}",
        "BOOTSTRAP": repr({
            "success": success,
            "repository": str(repository or checkout),
        }),
    }
    statements.extend(
        f"{name} = {value}" for name, value in values.items() if name != omit
    )
    (checkout / "agent_bootstrap.py").write_text(
        "\n".join(statements) + "\n",
        encoding="utf-8",
    )


def _copy_assistant_launcher(destination: Path) -> Path:
    launcher = destination / ".assistant" / "agent_bootstrap.py"
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes((ROOT / ".assistant" / "agent_bootstrap.py").read_bytes())
    return launcher


def test_assistant_launcher_actual_source_exports_live_bootstrap_state(tmp_path):
    checkout = tmp_path / "explicit-checkout"
    _write_trampoline_target(checkout)
    provider = object()

    namespace = runpy.run_path(
        str(ROOT / ".assistant" / "agent_bootstrap.py"),
        init_globals={
            "ANCHOR_SOURCE_CHECKOUT": str(checkout),
            "ANCHOR_REPOSITORY_PROVIDER": provider,
            "UNRELATED": "must-not-propagate",
        },
    )

    delegated = namespace["_namespace"]
    for name in ("anchor", "ROOT", "MANIFEST", "ORIENTATION", "BOOTSTRAP"):
        assert namespace[name] is delegated[name]
    assert namespace["anchor"]("status", output_format="dict") == {
        "kind": "status",
        "pid": os.getpid(),
        "kwargs": {"output_format": "dict"},
    }
    assert delegated["DELEGATED_NAME"] == "__odibi_anchor_checkout_bootstrap__"
    assert delegated["DELEGATED_GLOBALS"] == {
        "provider": provider,
        "unrelated": None,
    }


@pytest.mark.parametrize("copied_topology", [False, True])
def test_assistant_launcher_resolves_only_supported_topologies_from_arbitrary_cwd(
    monkeypatch, tmp_path, copied_topology
):
    host = tmp_path / "host"
    checkout = host / "odibi_anchor" if copied_topology else host
    launcher = _copy_assistant_launcher(host)
    _write_trampoline_target(checkout)
    elsewhere = tmp_path / "unrelated-cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("ANCHOR_SOURCE_CHECKOUT", raising=False)

    namespace = runpy.run_path(str(launcher))

    assert namespace["BOOTSTRAP"]["repository"] == str(checkout)
    assert namespace["anchor"]("status")["pid"] == os.getpid()


@pytest.mark.parametrize("explicit_source", ["init_global", "environment"])
def test_assistant_launcher_explicit_override_wins_over_source_topology(
    monkeypatch, tmp_path, explicit_source
):
    topology_checkout = tmp_path / "topology"
    explicit_checkout = tmp_path / "explicit"
    launcher = _copy_assistant_launcher(topology_checkout)
    _write_trampoline_target(topology_checkout)
    _write_trampoline_target(explicit_checkout)
    monkeypatch.delenv("ANCHOR_SOURCE_CHECKOUT", raising=False)
    init_globals = None
    if explicit_source == "init_global":
        init_globals = {"ANCHOR_SOURCE_CHECKOUT": explicit_checkout}
    else:
        monkeypatch.setenv("ANCHOR_SOURCE_CHECKOUT", str(explicit_checkout))

    namespace = runpy.run_path(
        str(launcher),
        init_globals=init_globals,
    )

    assert namespace["BOOTSTRAP"]["repository"] == str(explicit_checkout)


@pytest.mark.parametrize("explicit_source", ["init_global", "environment"])
def test_assistant_launcher_invalid_explicit_path_never_falls_back_or_delegates(
    monkeypatch, tmp_path, explicit_source
):
    checkout = tmp_path / "source-checkout"
    sentinel = tmp_path / "delegate-ran"
    launcher = _copy_assistant_launcher(checkout)
    _write_trampoline_target(checkout, sentinel=sentinel)
    monkeypatch.delenv("ANCHOR_SOURCE_CHECKOUT", raising=False)
    init_globals = None
    if explicit_source == "init_global":
        monkeypatch.setenv("ANCHOR_SOURCE_CHECKOUT", str(checkout))
        init_globals = {"ANCHOR_SOURCE_CHECKOUT": "relative-checkout"}
    else:
        monkeypatch.setenv("ANCHOR_SOURCE_CHECKOUT", "~/odibi_anchor")

    with pytest.raises(RuntimeError, match="absolute, non-tilde"):
        runpy.run_path(str(launcher), init_globals=init_globals)

    assert not sentinel.exists()


def test_assistant_launcher_invalid_fixed_sibling_does_not_scan(tmp_path, monkeypatch):
    host = tmp_path / "host"
    launcher = _copy_assistant_launcher(host)
    (host / "odibi_anchor").mkdir()
    undiscoverable = host / "other" / "odibi_anchor"
    sentinel = tmp_path / "delegate-ran"
    _write_trampoline_target(undiscoverable, sentinel=sentinel)
    monkeypatch.delenv("ANCHOR_SOURCE_CHECKOUT", raising=False)

    with pytest.raises(FileNotFoundError, match="No managed project target matches"):
        runpy.run_path(str(launcher))

    assert not sentinel.exists()


def test_complete_copied_assistant_delegates_to_fixed_sibling_from_arbitrary_cwd(
    monkeypatch, tmp_path
):
    host = tmp_path / "workspace-root"
    shutil.copytree(ROOT / ".assistant", host / ".assistant")
    checkout = host / "odibi_anchor"
    _write_trampoline_target(checkout)
    elsewhere = tmp_path / "arbitrary-cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("ANCHOR_SOURCE_CHECKOUT", raising=False)

    namespace = runpy.run_path(str(host / ".assistant" / "agent_bootstrap.py"))

    assert namespace["BOOTSTRAP"]["repository"] == str(checkout)
    assert namespace["anchor"]("status")["pid"] == os.getpid()


def test_assistant_launcher_rejects_exec_without_file_before_delegating(tmp_path):
    checkout = tmp_path / "checkout"
    sentinel = tmp_path / "delegate-ran"
    _write_trampoline_target(checkout, sentinel=sentinel)
    source = (ROOT / ".assistant" / "agent_bootstrap.py").read_text(encoding="utf-8")

    with pytest.raises(RuntimeError, match=r"requires __file__.*runpy\.run_path"):
        exec(compile(source, "<agent_bootstrap.py>", "exec"), {
            "ANCHOR_SOURCE_CHECKOUT": str(checkout),
        })

    assert not sentinel.exists()


def test_assistant_launcher_propagates_delegated_error(tmp_path):
    checkout = tmp_path / "checkout"
    launcher = _copy_assistant_launcher(checkout)
    _write_trampoline_target(checkout, delegated_error="delegated bootstrap failed")

    with pytest.raises(ValueError, match="delegated bootstrap failed"):
        runpy.run_path(str(launcher))


@pytest.mark.parametrize(
    ("success", "repository_name", "omit", "message"),
    [
        (False, None, None, "did not report success"),
        (True, "other-checkout", None, "does not match the selected checkout"),
        (True, None, "ORIENTATION", "omitted required values"),
    ],
)
def test_assistant_launcher_rejects_invalid_delegated_result(
    tmp_path, success, repository_name, omit, message
):
    checkout = tmp_path / "checkout"
    launcher = _copy_assistant_launcher(checkout)
    repository = tmp_path / repository_name if repository_name else None
    if repository is not None:
        repository.mkdir()
    _write_trampoline_target(
        checkout,
        success=success,
        repository=repository,
        omit=omit,
    )

    with pytest.raises(RuntimeError, match=message):
        runpy.run_path(str(launcher))


def test_bootstrap_instructions_and_entrypoint_cannot_drift():
    entrypoint = ROOT / "agent_bootstrap.py"
    launcher = ROOT / ".assistant" / "agent_bootstrap.py"
    instructions = (ROOT / ".assistant_instructions.md").read_text(encoding="utf-8")
    workflow = (
        ROOT / ".assistant" / "references" / "odibi-anchor" / "workflow.md"
    ).read_text(encoding="utf-8")
    thread = (
        ROOT / ".assistant" / "references" / "lifecycle" / "thread-discipline.md"
    ).read_text(encoding="utf-8")
    quick = (
        ROOT / ".assistant" / "references" / "odibi-anchor" / "quick-reference.md"
    ).read_text(encoding="utf-8")
    public_contract = "\n".join(
        (
            instructions,
            workflow,
            thread,
            quick,
            (ROOT / "README.md").read_text(),
            (ROOT / "docs" / "guides" / "quickstart.md").read_text(),
        )
    )

    assert entrypoint.is_file()
    assert launcher.is_file()
    assert "agent_bootstrap.py" in instructions
    assert 'bootstrap_path = "<instruction root>/.assistant/agent_bootstrap.py"' in workflow
    assert workflow.count("runpy.run_path(bootstrap_path)") == 2
    assert "re-run `init()`" not in workflow
    assert "same persistent Python process" in instructions
    assert "If deterministic resolution fails" in instructions
    assert "Locating source or a failed init is not successful bootstrap" in instructions
    assert "This is host-owned setup" in instructions
    assert "Never require the user to mention `agent_bootstrap.py`" in instructions
    assert "This is an automatic host obligation, not user-prompt boilerplate" in workflow
    assert "existing external `target=` directory is valid and expected" in workflow
    assert "fixed sibling `odibi_anchor`" in workflow
    assert "host troubleshooting/fallback equivalent" in public_contract
    for stale in (
        'Path.cwd() / "src"',
        "user@example.com",
        "anchor = init()",
    ):
        assert stale not in public_contract
    source = entrypoint.read_text(encoding="utf-8")
    assert "ANCHOR_REPO = Path(__file__).resolve().parent" in source
    assert "_route_binding = _project.resolve_route_binding(" in source
    assert 'project=_explicit_project' in source
    assert '_init_kwargs["route_binding"] = _route_binding' in source
    assert 'anchor, ROOT, MANIFEST = _bootstrap.init(**_init_kwargs)' in source
    assert '_init_kwargs["repository_provider"] = _repository_provider' in source
    assert 'ORIENTATION = anchor("orient", output_format="dict")' in source
    assert "subprocess" not in source
    launcher_source = launcher.read_text(encoding="utf-8")
    assert "runpy.run_path(" in launcher_source
    assert 'run_name="__odibi_anchor_checkout_bootstrap__"' in launcher_source
    assert "sys.path" not in launcher_source
    assert "WorkspaceClient" not in launcher_source
    assert "init(" not in launcher_source


def test_assistant_launcher_is_not_a_native_skill_registration():
    from odibi_anchor._dispatcher._guidance import NATIVE_SKILLS

    launcher = ROOT / ".assistant" / "agent_bootstrap.py"
    assert launcher.is_file()
    assert launcher.parent == ROOT / ".assistant"
    assert "agent-bootstrap" not in NATIVE_SKILLS
    assert "agent_bootstrap" not in NATIVE_SKILLS


def test_quick_reference_matches_startup_and_action_access_contracts():
    from odibi_anchor._dispatcher._protocol import (
        PRE_DELIVERY_SEQUENCE,
        STARTUP_SEQUENCE,
        protocol_invocation,
    )

    quick = (
        ROOT / ".assistant" / "references" / "odibi-anchor" / "quick-reference.md"
    ).read_text(encoding="utf-8")
    assert STARTUP_SEQUENCE[:2] == ("status", "audit_history")
    assert "structured orientation → inspect returned status and prior gate evidence" in quick
    positions = [
        re.search(rf'anchor\("{action}"(?:[,\)])', quick).start()
        for action in STARTUP_SEQUENCE[2:]
    ]
    assert positions == sorted(positions)
    assert 'anchor("task", "describe intended work", goal=' in quick
    for step in STARTUP_SEQUENCE:
        invocation = protocol_invocation(step)
        assert invocation.startswith('anchor("')
        if step == "task":
            assert 'goal="' in invocation
            assert 'acceptance_criteria=[' in invocation
    for action in ("preflight", "test", "review", "gate"):
        assert re.search(rf'anchor\("{action}"(?:[,\)])', quick)
    assert PRE_DELIVERY_SEQUENCE == (
        "preflight (for Python changes)", "test (for Python changes)",
        "review", "gate",
        "learning assess",
    )
    for nonexistent in ("handoff", "explore", "profile", "error"):
        assert f'anchor("{nonexistent}")' not in quick
    for task_required in ("trace", "trace_row", "evolve", "reconcile", "profile_table"):
        assert task_required in quick.split("Substantive actions", 1)[1]


def test_handoff_examples_compile_and_use_only_snapshot_contract_fields():
    assistant = ROOT / ".assistant"
    sources = (
        (
            assistant / "references" / "lifecycle" / "thread-discipline.md",
            "# 3. Hand off with detailed state",
        ),
        (
            assistant / "skills" / "work-item-management" / "references" / "memory-saving.md",
            '### Use `anchor("snapshot", mode="handoff")` with Detailed State',
        ),
    )
    allowed = {
        "mode", "summary", "state", "subject", "decisions", "rejected_alternatives",
        "open_questions", "next_steps", "artifacts", "evidence_chain", "output_format",
    }
    calls = []

    def fake_cw(action, *args, **kwargs):
        assert action == "snapshot"
        assert not args
        assert set(kwargs) <= allowed
        calls.append(kwargs)

    for path, marker in sources:
        text = path.read_text(encoding="utf-8").split(marker, 1)[1]
        snippet = (
            text.split("```python", 1)[1].split("```", 1)[0]
            if text.lstrip().startswith("```python")
            else text.split("```", 1)[0]
        )
        exec(compile(snippet, str(path), "exec"), {"anchor": fake_cw})
    assert len(calls) == 2
    assert all(call["mode"] == "handoff" and call["summary"] for call in calls)

    all_guidance = "\n".join(
        path.read_text(encoding="utf-8") for path in assistant.rglob("*.md")
    )
    assert not re.search(r'mode="handoff",\s*"', all_guidance)
    assert "files_modified=" not in all_guidance
    assert "compiled_details=" not in all_guidance


def test_task_skill_enforcement_message_matches_substantive_action_gate():
    from odibi_anchor.planning.task_execution_context import _build_required_skills

    profile = __import__(
        "odibi_anchor.planning._task_profile", fromlist=["normalize_task_profile"],
    ).normalize_task_profile(legacy_mode="debugging")
    required = _build_required_skills(profile)
    assert required[0]["enforcement"] == (
        "MUST load before each non-exempt substantive action — RuntimeError if skipped"
    )
