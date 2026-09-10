"""_protocol.py — Single source of truth for the anchor() mandatory protocol.

All surfaces (boot banner, pre_dispatch, help, skill docs, tests) should
reference these constants instead of hardcoding the sequence.
"""
from __future__ import annotations

# Mandatory startup sequence — each must complete (no error) before anchor("task")
STARTUP_SEQUENCE: tuple[str, ...] = (
    "status",
    "audit_history",
    "new_session",
    "task",
)

# Pre-delivery sequence — required before session can be considered complete
PRE_DELIVERY_SEQUENCE: tuple[str, ...] = (
    "preflight (for Python changes)",
    "test (for Python changes)",
    "review",
    "gate",
    "learning assess",
)


def protocol_invocation(step: str) -> str:
    """Render an executable or explicitly fillable invocation for protocol guidance."""
    if step == "new_session":
        return 'anchor("new_session", name="feature_name", inline=True)'
    if step == "task":
        return (
            'anchor("task", "describe intended work", '
            'goal="state intended outcome", mode="implementation", '
            'acceptance_criteria=["state how completion will be verified"])'
        )
    if step == "preflight (for Python changes)":
        return 'anchor("preflight")  # required for Python changes'
    if step == "test (for Python changes)":
        return 'anchor("test")  # required for Python changes'
    if step == "learning assess":
        return 'anchor("learning", "assess", outcome="...")'
    return f'anchor("{step}")'


# Actions that accept NO arguments (agent should not fabricate kwargs)
NO_ARG_ACTIONS: frozenset[str] = frozenset({"gate"})

# Disabled actions (raise RuntimeError immediately)
DISABLED_ACTIONS: frozenset[str] = frozenset()  # All legacy actions fully removed from dispatch
