"""Data requirements derive from accepted TaskProfile characteristics."""

from odibi_anchor.planning._task_builders import required_skills_for_task
from odibi_anchor.planning._task_profile import normalize_task_profile


def profile(mode, **kwargs):
    return normalize_task_profile(legacy_mode=mode, **kwargs)


def test_data_change_and_reconciliation_compose():
    assert required_skills_for_task(profile(
        "implementation", execution_mode="data_change", traits=("reconciliation",),
    )) == ("data-operations", "data-reconciliation")


def test_read_only_data_does_not_imply_mutation_skill():
    assert required_skills_for_task(profile("analysis", domains=("data",))) == ()


def test_explicit_data_intents_route_directly():
    assert required_skills_for_task(profile(
        "implementation", traits=("ingestion",),
    )) == ("data-onboarding",)
    assert required_skills_for_task(profile(
        "reconciliation", traits=("reconciliation",),
    )) == ("data-reconciliation",)
