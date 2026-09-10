"""Output cost-disclosure (AXI principle #9: contextual disclosure).

Centralized, future-proof mechanism that teaches the agent about cheaper output
forms AT POINT OF USE — when a response is large. Wired once into the anchor()
dispatcher, so every current AND future action inherits it with no per-builder
change. The agent doesn't need prior knowledge of `output_format="toon"` or
`detail="summary"`; it learns the affordance from the output it just received.

Two knobs are surfaced:
  - output_format="toon"  — universal (every contract action), suggested whenever
    the result isn't already TOON.
  - detail="summary"      — only for actions that accept it (DETAIL_CAPABLE_ACTIONS),
    and skipped when the result is already a summary (detected via metrics.detail).

Tunable via env var ANCHOR_OUTPUT_HINT_THRESHOLD (token count; 0 disables).
"""

from __future__ import annotations

import json
import os
from typing import Any

# Default: only nudge on genuinely large outputs (~32K chars). Override via env.
try:
    DEFAULT_HINT_THRESHOLD_TOKENS = int(os.environ.get("ANCHOR_OUTPUT_HINT_THRESHOLD", "8000"))
except ValueError:
    DEFAULT_HINT_THRESHOLD_TOKENS = 8000

# Actions that accept detail="summary" for a leaner payload. Extend as builders
# gain the option — that is the single place to update for new lean-capable actions.
DETAIL_CAPABLE_ACTIONS: frozenset[str] = frozenset({"map"})

_HINT_MARKER = "TIP: large output"


def estimate_tokens(obj: Any) -> int:
    """Rough token estimate (chars/4) for a dict or string output."""
    if isinstance(obj, str):
        chars = len(obj)
    else:
        try:
            chars = len(json.dumps(obj, default=str))
        except Exception:
            chars = len(str(obj))
    return chars // 4


def build_cost_hint(
    action: str,
    size_tokens: int,
    *,
    already_toon: bool,
    already_summary: bool,
    threshold: int = DEFAULT_HINT_THRESHOLD_TOKENS,
    detail_capable: frozenset[str] = DETAIL_CAPABLE_ACTIONS,
) -> str | None:
    """Return a parameterized cheaper-form hint, or None if not warranted.

    Pure/testable. Uses AXI's placeholder convention: it names the action with
    fixed disambiguating kwargs and never guesses runtime values.
    """
    if threshold <= 0 or size_tokens < threshold:
        return None
    knobs: list[str] = []
    if action in detail_capable and not already_summary:
        knobs.append("detail='summary'")
    if not already_toon:
        knobs.append("output_format='toon'")
    if not knobs:
        return None
    return (
        f"{_HINT_MARKER} (~{size_tokens // 1000}k tokens). "
        f"For a cheaper view: anchor('{action}', {', '.join(knobs)})."
    )


def apply_output_cost_hint(
    action: str,
    output: Any,
    *,
    already_toon: bool,
    threshold: int = DEFAULT_HINT_THRESHOLD_TOKENS,
    detail_capable: frozenset[str] = DETAIL_CAPABLE_ACTIONS,
) -> Any:
    """Append a cheaper-form hint to a large output, in place where possible.

    - dict: records `metrics.output_tokens_estimate` and appends the hint to
      `suggested_next_actions` (only when that key is already a list, i.e. a
      contract output — never fabricated on plain dicts).
    - str (markdown/toon): appends a trailing hint line (idempotent).
    Returns the (possibly modified) output.
    """
    size = estimate_tokens(output)
    already_summary = False
    if isinstance(output, dict):
        already_summary = (output.get("metrics") or {}).get("detail") == "summary"
    hint = build_cost_hint(
        action, size,
        already_toon=already_toon, already_summary=already_summary,
        threshold=threshold, detail_capable=detail_capable,
    )
    if isinstance(output, dict):
        metrics = output.get("metrics")
        if isinstance(metrics, dict):
            metrics.setdefault("output_tokens_estimate", size)
        if hint:
            sna = output.get("suggested_next_actions")
            if isinstance(sna, list) and not any(_HINT_MARKER in str(s) for s in sna):
                sna.append(hint)
        return output
    if isinstance(output, str) and hint and _HINT_MARKER not in output:
        return output + "\n\n" + hint
    return output
