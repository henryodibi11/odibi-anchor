"""Accepted-task skill resolver contract."""

from odibi_anchor.planning._task_builders import required_skills_for_task
from odibi_anchor.planning._task_profile import normalize_task_profile


def profile(mode, **kwargs):
    return normalize_task_profile(legacy_mode=mode, **kwargs)


def test_mode_only_requirements_match_native_contract():
    assert required_skills_for_task(profile("documentation")) == ("documentation",)
    assert required_skills_for_task(profile("debugging")) == ("debugging",)
    assert required_skills_for_task(profile("implementation")) == ()
    assert required_skills_for_task(profile("greenfield")) == ()
    assert required_skills_for_task(profile("planning")) == ()
    assert required_skills_for_task(profile("testing")) == ()


def test_memory_governance_skill_requires_explicit_trait():
    assert required_skills_for_task(profile("implementation")) == ()
    assert required_skills_for_task(
        profile("implementation", traits=("memory-governance",)),
    ) == ("auditing-memory-governance",)


def test_memory_supply_skills_require_their_explicit_traits():
    assert required_skills_for_task(
        profile("implementation", traits=("memory-authoring",)),
    ) == ("authoring-governed-memories",)
    assert required_skills_for_task(
        profile("analysis", traits=("memory-packaging",)),
    ) == ("building-memory-packs",)
    assert required_skills_for_task(
        profile("implementation", traits=("memory-authoring", "memory-governance")),
    ) == ("auditing-memory-governance", "authoring-governed-memories")


def test_spec_and_profile_requirements_compose_deterministically():
    assert required_skills_for_task(
        profile("implementation"), specification_disposition="required",
    ) == ("writing-specs",)
    assert required_skills_for_task(
        profile("review", domains=("code",), traits=("pr",)),
    ) == ("code-comprehension", "cross-functional-pr")
    assert required_skills_for_task(
        profile("testing", traits=("test-repair",)),
    ) == ("writing-tests",)
