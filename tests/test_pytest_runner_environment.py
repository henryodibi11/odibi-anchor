"""`anchor("test")` must not hand the live runtime's routing to the suite.

Issue #17: `child_environment` copied `os.environ` wholesale, so a test run started
from inside a bound runtime inherited ANCHOR_HOME, ANCHOR_MEMORY_DB,
ANCHOR_PROJECT_ID and ANCHOR_PROJECT_ROOT. Tests that resolve Anchor state from the
environment then bound to the operator's real project rather than their own
fixtures — writing foreign task windows into the operator's memory database and
failing in ways that do not reproduce under plain pytest.
"""
from odibi_anchor.pytest_runner import SUMMARY_ENV, child_environment

# Pinned as a literal so these assert behaviour rather than agreeing with whatever
# the module happens to define.
ANCHOR_ENV_PREFIX = "ANCHOR_"

ROUTING_VARIABLES = {
    "ANCHOR_HOME": "/real/operator/home",
    "ANCHOR_MEMORY_DB": "/real/operator/home/.agent_memory.db",
    "ANCHOR_PROJECT_ID": "operator-project",
    "ANCHOR_PROJECT_ROOT": "/real/operator/project",
    "ANCHOR_AUTHORITY_ID": "operator",
    "ANCHOR_TRUST_DOMAIN": "work",
}


def test_routing_variables_are_stripped():
    env = child_environment({**ROUTING_VARIABLES, "PATH": "/usr/bin"})
    leaked = sorted(name for name in env if name.startswith(ANCHOR_ENV_PREFIX))
    assert not leaked, f"routing variables reached the pytest child: {leaked}"


def test_unrelated_variables_are_preserved():
    """Only Anchor's own routing is removed; the rest of the environment stands."""
    env = child_environment({**ROUTING_VARIABLES, "PATH": "/usr/bin", "HOME": "/root"})
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/root"


def test_summary_handoff_variable_survives_the_strip():
    """SUMMARY_ENV starts with ODIBI_ANCHOR_, so the ANCHOR_ prefix must not match it."""
    assert not SUMMARY_ENV.startswith(ANCHOR_ENV_PREFIX)
    env = child_environment({**ROUTING_VARIABLES, SUMMARY_ENV: "/tmp/summary.json"})
    assert env[SUMMARY_ENV] == "/tmp/summary.json"


def test_git_identity_is_still_configured():
    """Negative control: the isolation must not drop the runner's own child settings."""
    env = child_environment({**ROUTING_VARIABLES})
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["GIT_AUTHOR_NAME"] == "Odibi Anchor Tests"
    assert int(env["GIT_CONFIG_COUNT"]) >= 2


def test_caller_environment_is_not_mutated():
    """The strip applies to the child copy only."""
    base = dict(ROUTING_VARIABLES)
    child_environment(base)
    assert base == ROUTING_VARIABLES
