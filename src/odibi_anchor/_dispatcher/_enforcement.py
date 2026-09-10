"""Pure enforcement decision functions — testable without boot sequence.

These functions extract the decision logic from agent_init.py's enforcement blocks
into pure, side-effect-free functions that can be unit tested independently.

Each function takes explicit state as parameters and returns a (should_block, message)
tuple. The calling code in agent_init.py retains the RuntimeError raises and global
state access — only the DECISION logic is extracted here.
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any


def closure_satisfied(timing: dict[str, Any]) -> bool:
    """Return whether one successful timing commits either supported closure route."""
    if timing.get("error") is not None or timing.get("passed", True) is not True:
        return False
    if timing.get("action") in {"learn", "learning"}:
        return timing.get("action") == "learn" or timing.get("learning_command") == "assess"
    return timing.get("action") == "checkpoint" and bool(
        timing.get("includes_closure") or timing.get("includes_learn")
    )


def should_block_edit_limit(
    timings: list[dict], max_ungated: int = 6
) -> tuple[bool, str]:
    """Determine if the ungated edit limit has been exceeded.

    Counts safe/semantic/touched actions since the last successful gate/checkpoint.
    Only counts actions where error is None (successful edits).

    Args:
        timings: Session timing records (list of dicts with 'action', 'error', 'passed' keys).
        max_ungated: Maximum allowed edits before gate is required. Default 6.

    Returns:
        (should_block, message) — True if limit exceeded.
    """
    # Find last successful gate or checkpoint
    last_gate_idx = None
    for i in range(len(timings) - 1, -1, -1):
        if (timings[i]["action"] in ("gate", "checkpoint")
                and timings[i].get("passed", False)):
            last_gate_idx = i
            break

    start = (last_gate_idx + 1) if last_gate_idx is not None else 0
    edits_since_gate = sum(
        1 for j in range(start, len(timings))
        if timings[j]["action"] in ("safe", "semantic", "touched")
        and timings[j].get("error") is None
    )

    if edits_since_gate >= max_ungated:
        return True, f"{edits_since_gate} file edits without quality gate."
    return False, ""


def should_block_checkpoint(
    files_changed: set[str],
    files_at_last_checkpoint: int,
    current_touch: str,
    threshold: int = 5,
    is_reconciliation: bool = False,
) -> tuple[bool, str]:
    """Determine if the checkpoint file threshold has been exceeded.

    Args:
        files_changed: Set of files changed this session.
        files_at_last_checkpoint: Count of files_changed at the last checkpoint.
        current_touch: The file about to be touched (may not be in files_changed yet).
        threshold: Maximum files between checkpoints. Default 5.
        is_reconciliation: If True, the touch is reconciling drift, not a new change.

    Returns:
        (should_block, message) — True if threshold exceeded.
    """
    files_since_cp = len(files_changed) - files_at_last_checkpoint
    # +1 if the current file isn't already registered
    if current_touch and current_touch not in files_changed:
        files_since_cp += 1

    if files_since_cp > threshold and not is_reconciliation:
        return True, f"{files_since_cp} files changed since last checkpoint."
    return False, ""


def should_block_gate_review(
    timings: list[dict], files_changed: set[str]
) -> tuple[bool, str]:
    """Determine if gate should block due to missing self-review.

    Checks that anchor("review") or anchor("diff") ran since the last successful gate.
    Only enforced when files were actually changed.

    Args:
        timings: Session timing records.
        files_changed: Set of files changed this session.

    Returns:
        (should_block, message) — True if review is required but missing.
    """
    if not files_changed:
        return False, ""

    # Find timings since last successful gate
    last_gate_idx = None
    for i in range(len(timings) - 1, -1, -1):
        if (timings[i]["action"] in ("gate", "checkpoint")
                and timings[i].get("passed", False)):
            last_gate_idx = i
            break

    start = (last_gate_idx + 1) if last_gate_idx is not None else 0
    review_ran = any(
        timings[j]["action"] in ("review", "diff") and timings[j].get("error") is None
        for j in range(start, len(timings))
    )
    if not review_ran:
        return True, 'Self-review required before gate. Run anchor("review") to review your changeset.'

    return False, ""


def should_block_gate_tests(
    timings: list[dict], files_changed: set[str]
) -> tuple[bool, str]:
    """Determine if gate should block due to missing or failed tests.

    Blocks when:
    - .py files were changed AND no test/test_focus action succeeded
    - .py files were changed AND the last test run failed

    Args:
        timings: Session timing records.
        files_changed: Set of files changed this session.

    Returns:
        (should_block, message) — True if tests are required but missing/failed.
    """
    py_changed = any(f.endswith(".py") for f in files_changed)
    if not py_changed:
        return False, ""

    test_entries = [
        t for t in timings
        if t["action"] == "test" and t.get("error") is None
    ]
    if not test_entries:
        return True, "Tests required for .py changes."

    last_test = test_entries[-1]
    if not last_test.get("passed", True):
        return True, "Last test run had failures."

    return False, ""


def should_block_gate_preflight(
    timings: list[dict], files_changed: set[str]
) -> tuple[bool, str]:
    """Determine if gate should block due to missing preflight on .py changes.

    Mirrors should_block_gate_tests: when .py files changed, a successful
    anchor("preflight") must have run since the last successful gate/checkpoint.
    Enforces instruction hard rule #11 ("NEVER skip anchor('preflight')... gate
    will FAIL"), which was previously only detected post-hoc by the compliance
    audit and never actually enforced at the gate.

    Args:
        timings: Session timing records.
        files_changed: Set of files changed this session.

    Returns:
        (should_block, message) — True if preflight is required but missing.
    """
    py_changed = any(f.endswith(".py") for f in files_changed)
    if not py_changed:
        return False, ""

    # Find last successful gate or checkpoint
    last_gate_idx = None
    for i in range(len(timings) - 1, -1, -1):
        if (timings[i]["action"] in ("gate", "checkpoint")
                and timings[i].get("passed", False)):
            last_gate_idx = i
            break

    start = (last_gate_idx + 1) if last_gate_idx is not None else 0
    preflight_ran = any(
        timings[j]["action"] == "preflight" and timings[j].get("error") is None
        for j in range(start, len(timings))
    )
    if not preflight_ran:
        return True, (
            "Preflight required for .py changes (hard rule #11). "
            'Run anchor("preflight", changed_files=[...]) before gate.'
        )
    return False, ""


def check_test_coverage(
    changed_py: list[str],
    test_target: str | list[str] | tuple[str, ...],
    test_map: dict[str, list[str]] | None = None,
) -> tuple[bool, list[str]]:
    """Check if test_target plausibly covers the changed .py files.

    Uses three strategies in order:
    1. Frame test_map lookup (explicit mapping)
    2. Stem matching (test_foo.py covers foo.py)
    3. Unmapped passthrough (files with no test_map entry are not blockable)

    Args:
        changed_py: List of changed .py file paths.
        test_target: The test file/directory that was run.
        test_map: Optional mapping from source file → list of test files.

    Returns:
        (is_covered, uncovered_files) — True if all files are covered.
    """
    targets = [test_target] if isinstance(test_target, str) else list(test_target)
    normalized_targets = [str(target).replace("\\", "/") for target in targets]
    normalized_test_map = {
        str(source).replace("\\", "/"): [str(test).replace("\\", "/") for test in tests]
        for source, tests in (test_map or {}).items()
    }
    uncovered = []
    for src in changed_py:
        normalized_src = src.replace("\\", "/")
        # Strategy 1: test_map
        if normalized_test_map:
            mapped_tests = normalized_test_map.get(normalized_src, [])
            if mapped_tests:
                if any(
                    target in mapped_tests or
                    (target.endswith("/") and any(test.startswith(target) for test in mapped_tests))
                    for target in normalized_targets
                ):
                    continue  # Covered by map
                # Target doesn't match mapped tests — truly uncovered
                uncovered.append(src)
                continue

        # Strategy 2: Stem matching (for files not in test_map)
        src_stem = PurePosixPath(normalized_src).stem
        if any(
            src_stem == PurePosixPath(target).stem.removeprefix("test_") or
            src_stem in target or
            PurePosixPath(target).stem.removeprefix("test_") in src_stem
            for target in normalized_targets
        ):
            continue  # Stem match

        # Strategy 3: Unmapped = not blockable (no test_map entry, no stem match)
        # (e.g., agent_init.py which is exec'd and has no direct test file)

    return len(uncovered) == 0, uncovered



def should_block_learn_debt(prior_session_learn_debt: bool) -> tuple[bool, str]:
    """Determine if task should be blocked due to prior session learn debt.

    Args:
        prior_session_learn_debt: Whether the previous session ended without anchor("learn").

    Returns:
        (should_block, message) — True if learn debt exists.
    """
    if prior_session_learn_debt:
        return True, "Previous session has unresolved learn debt."
    return False, ""


def should_block_config_mutation(
    timings: list[dict], mutation_kwargs: set[str]
) -> tuple[bool, str]:
    """Determine if config mutation should be blocked without planning.

    Args:
        timings: Session timing records.
        mutation_kwargs: Set of mutation kwarg names present in the config call.

    Returns:
        (should_block, message) — True if mutation attempted without planning.
    """
    _config_mutation_keys = {
        "suppress_category", "unsuppress_category",
        "suppress_id", "unsuppress_id",
        "file_override", "remove_override",
    }
    if not (_config_mutation_keys & mutation_kwargs):
        return False, ""

    task_ran = any(
        t["action"] == "task" and t["error"] is None and t.get("passed", True)
        for t in timings
    )
    if not task_ran:
        return True, "Config mutations require planning first."
    return False, ""


def required_task_prerequisite(
    timings: list[dict], *, allow_inline_continuation: bool = False,
) -> str | None:
    """Return the exact next startup action, without parsing diagnostics."""
    checks = (
        ("status", lambda t: t["action"] == "status"),
        ("audit_history", lambda t: t["action"] == "audit_history"),
        ("new_session", lambda t: (
            allow_inline_continuation or t["action"] == "new_session"
        )),
    )
    for action, predicate in checks:
        if not any(predicate(t) and t.get("error") is None for t in timings):
            return action
    return None


def should_block_task_prerequisites(
    timings: list[dict], *, allow_inline_continuation: bool = False,
) -> tuple[bool, str]:
    """Determine if task should be blocked due to missing prerequisite actions.

    Checks that status and audit_history have run successfully and a session has
    been created before task can proceed. Bounded memory retrieval belongs to the
    accepted task result, not to this pre-task sequence.

    Args:
        timings: Session timing records.

    Returns:
        (should_block, message) — True if prerequisites are missing.
    """
    status_ran = any(
        t["action"] == "status" and t["error"] is None
        for t in timings
    )
    if not status_ran:
        return True, 'anchor("task") requires orientation first. Run anchor("status") before planning.'

    audit_reviewed = any(
        t["action"] == "audit_history" and t["error"] is None
        for t in timings
    )
    if not audit_reviewed:
        return True, 'anchor("task") requires compliance audit review first. Run anchor("audit_history") before planning.'

    session_created = allow_inline_continuation or any(
        t["action"] == "new_session" and t["error"] is None
        for t in timings
    )
    if not session_created:
        return True, 'anchor("task") requires a session notebook. Run anchor("new_session", name="...") before planning.'

    return False, ""


def should_block_task_inputs(description: str, goal: str) -> tuple[bool, str]:
    """Determine if task inputs are too short or missing.

    Args:
        description: The task description (first positional arg to anchor("task")).
        goal: The goal kwarg value.

    Returns:
        (should_block, message) — True if inputs are invalid.
    """
    if not description or len(description.strip()) < 10:
        return True, f'anchor("task") requires a meaningful description (>=10 chars). Got: {description!r}'

    if not goal or len(str(goal).strip()) < 10:
        return True, f'anchor("task") requires a meaningful goal= kwarg (>=10 chars). Got: goal={goal!r}'

    return False, ""


def should_block_known_bad(
    action: str, target: str, timings: list[dict]
) -> tuple[bool, str]:
    """Determine if a .py file edit should be blocked without known_bad check.

    Args:
        action: The anchor() action being performed (safe, semantic, touched).
        target: The file path being targeted.
        timings: Session timing records.

    Returns:
        (should_block, message) — True if known_bad hasn't been run for .py targets.
    """
    if not target or not target.endswith(".py"):
        return False, ""

    known_bad_ran = any(
        t["action"] == "known_bad" and t["error"] is None
        for t in timings
    )
    if not known_bad_ran:
        return True, (
            f'anchor("{action}") on .py files requires known-bad check first. '
            f'Run anchor("known_bad", changed_files=["{target}"])'
        )
    return False, ""


def should_block_planning_gate(
    action: str,
    timings: list[dict],
    planning_required_actions: set[str] | frozenset[str],
    *,
    task_context_present: bool | None = None,
) -> tuple[bool, str]:
    """Determine if a file-modifying action should be blocked without planning.

    Checks:
    1. status must have been called
    2. task must have been called (with passed=True) after the last successful gate/checkpoint

    Args:
        action: The anchor() action being performed.
        timings: Session timing records.
        planning_required_actions: Set of actions that require planning.
        task_context_present: Whether this process currently holds an accepted task profile.
            ``None`` preserves the timing-only compatibility contract for direct callers.

    Returns:
        (should_block, message) — True if planning gate not satisfied.
    """
    if action not in planning_required_actions:
        return False, ""

    # Check 1: status must have run
    status_ran = any(
        t["action"] == "status" and t["error"] is None
        for t in timings
    )
    if not status_ran:
        return True, (
            f'anchor("{action}") requires orientation first. '
            'Run anchor("status") before any file-modifying action.'
        )

    if task_context_present is False:
        return True, (
            f'anchor("{action}") requires a currently accepted task context. '
            'Run anchor("task", "describe intended work", goal="...", mode="...") in this '
            "process before any file-modifying action. Historical task timings do not restore "
            "task authority."
        )

    # Check 2: task must have run after last successful gate/checkpoint
    last_gate_idx = None
    for i in range(len(timings) - 1, -1, -1):
        if (timings[i]["action"] in ("gate", "checkpoint")
                and timings[i].get("passed", False)):
            last_gate_idx = i
            break

    if last_gate_idx is None:
        # No prior gate — just check if task ran at all with passed=True
        task_ran = any(
            t["action"] == "task" and t["error"] is None and t.get("passed", True)
            for t in timings
        )
    else:
        # Prior gate exists — task must have run AFTER it
        task_ran = any(
            timings[j]["action"] == "task"
            and timings[j]["error"] is None
            and timings[j].get("passed", True)
            for j in range(last_gate_idx + 1, len(timings))
        )
        if action == "gate" and any(
            closure_satisfied(timing) for timing in timings[last_gate_idx + 1:]
        ):
            # One accepted task may contain multiple verified gate→closure
            # cycles. Closure preserves that task window; it does not imply a
            # new feature or require replacement planning by itself.
            task_ran = any(
                timing["action"] == "task"
                and timing["error"] is None
                and timing.get("passed", True)
                for timing in timings[:last_gate_idx]
            )

    if not task_ran:
        feature_msg = (
            " Previous feature was delivered via gate/checkpoint —"
            " new feature requires fresh planning."
            if last_gate_idx is not None else ""
        )
        return True, (
            f'anchor("{action}") requires planning first.{feature_msg} '
            'Run anchor("task", goal="...", mode="...") before any file-modifying action.'
        )

    return False, ""


def should_block_gate_learn(timings: list[dict]) -> tuple[bool, str]:
    """Determine if gate should be blocked due to missing learn after previous gate.

    If a prior gate/checkpoint passed, learn must have run after it before
    the next gate can proceed.

    Args:
        timings: Session timing records.

    Returns:
        (should_block, message) — True if learn is missing after prior gate.
    """
    # Find the most recent successful gate/checkpoint
    prev_gate_idx = None
    for i in range(len(timings) - 1, -1, -1):
        if (timings[i]["action"] in ("gate", "checkpoint")
                and timings[i].get("passed", False)):
            prev_gate_idx = i
            break

    if prev_gate_idx is None:
        return False, ""  # No prior gate — no learn requirement

    if closure_satisfied(timings[prev_gate_idx]):
        return False, ""  # Successful checkpoints own their committed closure.

    learn_after_prev_gate = any(
        closure_satisfied(timings[j])
        for j in range(prev_gate_idx + 1, len(timings))
    )
    if not learn_after_prev_gate:
        return True, (
            'anchor("gate") requires a committed structured learning assessment '
            'after the previous gate.'
        )

    return False, ""


def should_block_mode_mismatch(
    timings: list[dict], files_changed: set[str],
) -> tuple[bool, str]:
    """Reject file delivery from the latest read-only task mode."""
    if not files_changed:
        return False, ""
    last_task = next(
        (
            timing for timing in reversed(timings)
            if timing["action"] == "task" and timing.get("error") is None
        ),
        None,
    )
    planned_mode = last_task.get("task_mode", "planning") if last_task else None
    readonly_modes = {"analysis", "review", "retrospective", "planning", "decision"}
    if planned_mode not in readonly_modes:
        return False, ""
    return True, (
        f"Mode mismatch — planned as '{planned_mode}' but files were modified.\n"
        f"Changed files: {sorted(files_changed)[:5]}\n"
        "Re-run anchor(\"task\", goal=\"...\", mode=\"implementation\") with a file-modifying mode,\n"
        "then re-run anchor(\"gate\")."
    )


def should_block_documentation_paths(
    active_task_mode: str | None,
    paths: list[str] | set[str] | tuple[str, ...],
) -> tuple[bool, str]:
    """Restrict documentation mode to Markdown artifacts only."""
    if active_task_mode != "documentation":
        return False, ""
    invalid = sorted(
        str(path) for path in paths
        if not str(path).lower().endswith((".md", ".markdown"))
    )
    if not invalid:
        return False, ""
    return True, (
        "Documentation mode permits only .md and .markdown files.\n"
        f"Rejected paths: {invalid[:8]}"
    )


def should_block_spec_gate(
    action: str,
    active_task_mode: str | None,
    spec_persisted: bool,
    spec_required_modes: frozenset[str],
    linked_spec: str | None = None,
    persisted_spec_name: str | None = None,
) -> tuple[bool, str]:
    """Determine if a file-modifying action should be blocked without a persisted spec.

    For modes in spec_required_modes, anchor("spec", "persist") must have been
    called before any file-modifying action (touched, safe, semantic) proceeds.

    Args:
        action: The anchor() action being performed.
        active_task_mode: The mode from the last anchor("task") call.
        spec_persisted: Whether anchor("spec", "persist") succeeded this session.
        spec_required_modes: Set of modes that require a persisted spec.

    Returns:
        (should_block, message) — True if spec is required but not persisted.
    """
    if action not in ("touched", "safe", "semantic"):
        return False, ""
    if active_task_mode is None:
        return False, ""  # No task planned yet — planning gate handles this
    if active_task_mode not in spec_required_modes:
        return False, ""
    # ``spec_persisted`` is a compatibility projection only.  Authority comes
    # from an exact canonical identity binding, never from the boolean alone.
    persistence_matches = bool(linked_spec and persisted_spec_name == linked_spec)
    if persistence_matches:
        return False, ""
    return True, (
        f'Mode "{active_task_mode}" requires a persisted spec before file modifications.\n'
        f'Run anchor("task", goal="...", mode="{active_task_mode}") then '
        f'anchor("spec", "persist", task_result) before calling anchor("{action}").'
    )


def should_block_skill_gate(
    action: str,
    active_task_mode: str | None,
    skills_loaded: set[str],
    mode_skill_map: dict[str, list[str]],
    exempt_actions: "frozenset[str] | set[str] | None" = None,
) -> tuple[bool, str]:
    """Determine if a substantive work action should be blocked without required skills.

    Once a task mode is active, every action NOT in exempt_actions (the
    orientation / planning / compliance / meta machinery) requires the mode's
    skills to be loaded via anchor("skill_loaded", "name"). This gives the pre-action
    skill layer the same teeth as file edits, closing the read-only/analysis hole
    where planning / anti-rationalization could otherwise be skipped entirely.

    Args:
        action: The anchor() action being performed.
        active_task_mode: The mode from the last anchor("task") call.
        skills_loaded: Set of skill names loaded this session.
        mode_skill_map: Mapping of mode → list of required skill names.
        exempt_actions: Actions that never require skills. When None, falls back
            to the legacy file-edit-only set so existing direct callers keep their
            original (narrower) behavior.

    Returns:
        (should_block, message) — True if required skills are missing.
    """
    if exempt_actions is None:
        # Legacy fallback: gate only file-modifying actions.
        if action not in ("touched", "safe", "semantic"):
            return False, ""
    elif action in exempt_actions:
        return False, ""
    if active_task_mode is None:
        return False, ""
    required = set(mode_skill_map.get(active_task_mode, []))
    if not required:
        return False, ""
    missing = sorted(required - skills_loaded)
    if not missing:
        return False, ""
    load_cmds = ", ".join(f'anchor("skill_loaded", "{s}")' for s in missing)
    return True, (
        f'Mode "{active_task_mode}" requires skills not yet loaded: {", ".join(missing)}.\n'
        f'Load the Odibi Anchor skills, then register: {load_cmds}'
    )


def should_block_spec_review_gate(
    action: str,
    active_task_mode: str | None,
    spec_persisted: bool,
    spec_review_rating: str | None,
    spec_required_modes: frozenset[str],
    linked_spec: str | None = None,
    persisted_spec_name: str | None = None,
    reviewed_spec_name: str | None = None,
) -> tuple[bool, str]:
    """Determine if execution should be blocked due to spec review not passing.

    For compatibility callers that supply a required-mode set, after persist the
    exact Spec must be reviewed to "good" or "excellent" before edits proceed.
    Production task policy no longer derives Spec requirements from mode names.

    Args:
        action: The anchor() action being performed.
        active_task_mode: The mode from the last anchor("task") call.
        spec_persisted: Whether anchor("spec", "persist") succeeded this session.
        spec_review_rating: Rating from anchor("spec", "review"), or None if not reviewed.
        spec_required_modes: Set of modes that require spec review.

    Returns:
        (should_block, message) — True if spec review hasn't passed.
    """
    if action not in ("touched", "safe", "semantic"):
        return False, ""
    if active_task_mode is None or active_task_mode not in spec_required_modes:
        return False, ""
    # ``spec_persisted`` is intentionally non-authoritative (compatibility only).
    persistence_matches = bool(linked_spec and persisted_spec_name == linked_spec)
    if not persistence_matches:
        return True, (
            f'Mode "{active_task_mode}" requires an exact persisted linked spec '
            "before review evidence can authorize file modifications."
        )
    review_matches = bool(linked_spec and reviewed_spec_name == linked_spec)
    if review_matches and spec_review_rating in ("excellent", "good"):
        return False, ""
    if spec_review_rating is None:
        return True, (
            f'Mode "{active_task_mode}" requires spec review before file modifications.\n'
            f'Run anchor("spec", "review", "SPEC_NAME") — rating must be "good" or "excellent".'
        )
    return True, (
        f'Spec review rating "{spec_review_rating}" is insufficient (need "good" or "excellent").\n'
        f'Fix the issues identified by anchor("spec", "review") and re-run until rating passes.'
    )


def should_block_data_write_unchecked(timings: list[dict]) -> tuple[bool, str]:
    """Block an executing Delta write that was not preceded by a quality check (D-001).

    Called only for apply_sql(mode="table") — the single anchor-owned Delta-write
    surface (apply_transform_context.py: CREATE OR REPLACE TABLE). Blocks unless a
    quality-class action ran successfully since the last passing gate/checkpoint.

    Quality-class actions: quality, pre_merge, validate, duplicate. The composed
    workflows investigate/evolve run validate internally, so they count too
    (data-tool spec constraint #4 — composed workflows satisfy individual
    requirements).

    Args:
        timings: Session timing records ({"action", "error", "passed", ...}).

    Returns:
        (should_block, message). message is empty when not blocking.
    """
    quality_actions = ("quality", "pre_merge", "validate", "duplicate")
    workflow_actions = ("investigate", "evolve")

    last_gate_idx = None
    for i in range(len(timings) - 1, -1, -1):
        if (timings[i]["action"] in ("gate", "checkpoint")
                and timings[i].get("passed", False)):
            last_gate_idx = i
            break

    start = (last_gate_idx + 1) if last_gate_idx is not None else 0
    checked = any(
        timings[j]["action"] in (quality_actions + workflow_actions)
        and timings[j].get("error") is None
        for j in range(start, len(timings))
    )
    if not checked:
        return True, (
            "Delta write requires a quality check first. Run "
            'anchor("quality", df, subject="<name>", keys=[...]) or '
            'anchor("pre_merge", source_df, "<target>", keys=[...]) before the write.'
        )
    return False, ""


def should_block_spec_persist_evidence(
    active_task_mode: str | None,
    timings: list[dict],
    data_spec_modes: frozenset[str],
) -> tuple[bool, str]:
    """Block persisting a data spec until source evidence was actually gathered (S-2).

    For data modes, a spec must not be persisted on *claimed* understanding — the
    agent must have actually run profile_table / contract / investigate on the
    source this session. Converts self-asserted evidence into earned evidence.

    Args:
        active_task_mode: Mode from the last anchor("task").
        timings: Session timing records.
        data_spec_modes: Modes that require proven source evidence.

    Returns:
        (should_block, message).
    """
    if active_task_mode not in data_spec_modes:
        return False, ""
    profiled = any(
        t["action"] in ("profile_table", "contract", "investigate")
        and t.get("error") is None
        for t in timings
    )
    if not profiled:
        return True, (
            "Data spec requires source evidence first. Run "
            'anchor("profile_table", df, subject="<source>") (or anchor("investigate")) '
            "before persisting the spec — prove you understand the data, don't assert it."
        )
    return False, ""


def should_block_gate_outcome_evidence(
    active_task_mode: str | None,
    timings: list[dict],
    data_modes: frozenset[str] | set[str],
) -> tuple[bool, str]:
    """Block closing a data-mode unit of work with no output verification (D-010).

    The gate is the delivery checkpoint. For data modes, reaching it implies you
    produced or changed data — so at least one verification-class action must have
    run *after the most recent successful task* (and since the last passing
    gate/checkpoint). This converts a self-reported "pipeline built / tables
    produced" into earned evidence: you must have actually profiled or
    quality-checked an output through anchor, not merely asserted success.

    It closes the hole where a build executed entirely outside anchor's write
    surfaces (e.g. an external framework or Databricks pipeline run) could be marked done with
    zero data verification — exactly the Pattern-1 "validates clean, never run,
    never profiled" failure. Source profiling done during planning runs *before*
    anchor("task"), so anchoring the window
    after the task forces verification of what was actually built, not the
    upfront source read.

    Verification-class actions: profile_table, contract, quality, validate,
    pre_merge, duplicate, investigate, evolve.

    Args:
        active_task_mode: Mode from the last anchor("task").
        timings: Session timing records.
        data_modes: Modes for which output evidence is required to close.

    Returns:
        (should_block, message). message is empty when not blocking.
    """
    if active_task_mode not in data_modes:
        return False, ""

    verification_actions = (
        "profile_table", "contract", "quality", "validate",
        "pre_merge", "duplicate", "investigate", "evolve",
    )

    # Window start: after the most recent successful gate/checkpoint...
    start = 0
    for i in range(len(timings) - 1, -1, -1):
        if (timings[i]["action"] in ("gate", "checkpoint")
                and timings[i].get("passed", False)):
            start = i + 1
            break

    # ...then advance to after the most recent successful task within that window
    # (planning-phase source profiling runs before anchor("task"); requiring evidence
    # after the task forces verification of what was built, not the source read).
    for j in range(len(timings) - 1, start - 1, -1):
        if timings[j]["action"] == "task" and timings[j].get("error") is None:
            start = j + 1
            break

    verified = any(
        timings[j]["action"] in verification_actions
        and timings[j].get("error") is None
        for j in range(start, len(timings))
    )
    if verified:
        return False, ""
    return True, (
        f'Mode "{active_task_mode}" cannot close without verifying an output. '
        "No profile/quality/validate/contract ran after planning this session — a "
        'pipeline that "validates clean" but was never run and never profiled is '
        'NOT done. Run anchor("profile_table", output_df, subject="<catalog.schema.table>") '
        '(or anchor("quality", ...)) on the output you produced, then re-run anchor("gate").'
    )


def data_quality_posture(timings: list[dict]) -> str:
    """Return a soft risk note if data was transformed but never quality-checked (D-003).

    Non-blocking signal for gate time. Fires only when an executing data mutation
    ran (apply_transform / apply_sql / coerce_fix without error) but no
    quality-class action did. Returns "" when posture is clean.

    Args:
        timings: Session timing records.

    Returns:
        A risk-note string, or "" when no warning is warranted.
    """
    mutated = any(
        t["action"] in ("apply_transform", "apply_sql", "coerce_fix")
        and t.get("error") is None
        for t in timings
    )
    if not mutated:
        return ""
    checked = any(
        t["action"] in ("quality", "validate", "pre_merge", "duplicate",
                        "investigate", "evolve")
        and t.get("error") is None
        for t in timings
    )
    if checked:
        return ""
    return ("Data was transformed this session but no quality/validate check ran — "
            "structural issues (dup keys, null grain, schema drift) may be undetected.")


def classify_problem_rigor(
    task: str,
    *,
    mode: str = "planning",
    priority: str | None = None,
) -> dict:
    """Advisory classification for proportionate Problem Record rigor.

    Level 0 bypasses a record, level 1 suggests a compact record, and level 2
    suggests the complete seven-stage workflow. This deliberately uses observable
    task signals; it does not claim to score reasoning quality.
    """
    text = f"{task} {mode} {priority or ''}".lower()
    level_two_signals = (
        "architecture", "migration", "multi-session", "cross-system", "cross system",
        "data correctness", "production", "security", "high impact", "high-impact",
        "multiple specs", "regulatory",
    )
    level_one_signals = (
        "investigate", "root cause", "why ", "compare", "choose", "decision",
        "analyze", "analysis", "debug", "design", "plan ",
    )
    if priority in {"high", "critical"} or mode == "migration" or any(
        signal in text for signal in level_two_signals
    ):
        return {
            "level": 2,
            "label": "full",
            "reason": "High-impact, cross-system, migration, or multi-session signal detected.",
            "record_recommended": True,
        }
    if mode in {"analysis", "debugging", "decision"} or any(
        signal in text for signal in level_one_signals
    ):
        return {
            "level": 1,
            "label": "compact",
            "reason": "The work includes localized ambiguity, analysis, debugging, or a decision.",
            "record_recommended": True,
        }
    return {
        "level": 0,
        "label": "direct",
        "reason": "No substantial ambiguity or high-impact signal detected.",
        "record_recommended": False,
    }
