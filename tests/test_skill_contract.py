"""Lint the native skill-file contract."""
import os
from pathlib import Path

SKILLS_DIR = os.path.join(".assistant", "skills")

def _all_skills():
    return sorted(
        d for d in os.listdir(SKILLS_DIR)
        if os.path.isfile(os.path.join(SKILLS_DIR, d, "SKILL.md"))
    )


def test_new_skills_declare_when_not_to_load_and_enforcement():
    failures = []
    for name in _all_skills():
        with open(os.path.join(SKILLS_DIR, name, "SKILL.md"), encoding="utf-8") as f:
            text = f.read().lower()
        if "when not to load" not in text:
            failures.append(f"{name}: missing 'When NOT to load'")
        if "## enforcement" not in text and "enforcement:" not in text:
            failures.append(f"{name}: missing 'Enforcement' label")
    assert not failures, "\n".join(failures)


def test_managed_startup_guidance_uses_launcher_resolved_install_preflight():
    instructions = Path(".assistant_instructions.md").read_text(encoding="utf-8")
    setup_skill = Path(
        ".assistant/skills/setting-up-odibi-anchor/SKILL.md"
    ).read_text(encoding="utf-8")
    launcher = Path(".assistant/agent_bootstrap.py").read_text(encoding="utf-8")

    for text in (instructions, setup_skill):
        assert "latest" in text
        assert "agent_bootstrap.py" in text
        assert "ANCHOR_PROJECT_ID" in text
    assert "https://pypi.org/pypi/odibi-anchor/json" in launcher
    assert 'odibi-anchor[databricks]=={_latest}' in launcher
    assert "dbutils.library.restartPython()" in launcher
    assert "dbutils.library.restartPython()" in setup_skill
    assert "from odibi_anchor import prepare_portfolio_runtime" in setup_skill
    assert 'os.environ.update(prepared["environment"])' in setup_skill
    assert 'prepared["next_operation"]["arguments"]["script"]' in setup_skill
    assert "only to recover or diagnose" in setup_skill
