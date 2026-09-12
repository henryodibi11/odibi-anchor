"""Lint the native skill-file contract."""
import os
import tomllib
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


def test_managed_startup_guidance_has_exact_databricks_install_preflight():
    version = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    command = f'%pip install "odibi-anchor[databricks]=={version}"'
    instructions = Path(".assistant_instructions.md").read_text(encoding="utf-8")
    setup_skill = Path(
        ".assistant/skills/setting-up-odibi-anchor/SKILL.md"
    ).read_text(encoding="utf-8")

    assert command in instructions
    assert command in setup_skill
    assert "Never assume the Python package is installed" in instructions
    assert "Do not assume the package is installed" in setup_skill
    assert "dbutils.library.restartPython()" in instructions
    assert "dbutils.library.restartPython()" in setup_skill
    for text in (instructions, setup_skill):
        normalized = " ".join(text.split())
        assert "from odibi_anchor import prepare_portfolio_runtime" in text
        assert 'os.environ.update(prepared["environment"])' in text
        assert 'prepared["next_operation"]["arguments"]["script"]' in text
        assert "durable Volume through FUSE" in normalized
