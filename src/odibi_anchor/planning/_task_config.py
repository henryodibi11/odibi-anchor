"""Static configuration data for task_execution_context."""

from __future__ import annotations

from typing import Any

_READINESS_GAP_QUESTIONS = {
    "Goal or desired outcome is missing.": [
        "What outcome should this task produce?",
        "What decision or action should be possible when the task is done?",
    ],
    "Background or current state is missing.": [
        "Why does this task exist now?",
        "What has already been tried or decided?",
    ],
    "Constraints are missing.": [
        "What must the executor avoid doing?",
        "What rules, standards, or environment constraints matter?",
    ],
    "In-scope or out-of-scope boundaries are missing.": [
        "What is explicitly included in this task?",
        "What should not be touched, even if it looks related?",
    ],
    "Artifacts, inputs, or dependencies are missing.": [
        "What files, tables, tickets, notebooks, or datasets should be used?",
        "What dependencies must be available before work starts?",
    ],
    "Acceptance criteria are missing.": [
        "How will you know the task is done?",
        "What checks should pass before this can be promoted or handed off?",
    ],
    "Risks or stop conditions are missing.": [
        "What could go wrong?",
        "When should the executor pause instead of continuing?",
    ],
    "Expected deliverables are missing.": [
        "What should the executor return at the end?",
        "Do you need a summary, files, tests, screenshots, recommendations, or a patch?",
    ],
}

_MODE_HINTS: dict[str, dict[str, list[str]]] = {
    "planning": {
        "thinking_prompts": [
            "Write the desired outcome before listing tasks.",
            "Separate known facts from assumptions before asking for help.",
            "Name what is out of scope to prevent accidental expansion.",
        ],
        "prompting_hints": [
            "Ask the assistant to restate the task, gaps, and acceptance criteria before execution.",
            "Provide constraints and stop conditions before asking for a plan.",
        ],
        "execution_hints": [
            "Do not execute until readiness gaps are either resolved or acknowledged.",
            "Use the plan as a checklist, not as a rigid workflow engine.",
        ],
        "anti_patterns": [
            "Starting from implementation details before the outcome is clear.",
            "Treating assumptions as facts.",
        ],
    },
    "implementation": {
        "thinking_prompts": [
            "Define the public API and output contract before coding.",
            "List the smallest useful behavior that proves the tool works.",
            "Identify existing project conventions before proposing new files.",
        ],
        "prompting_hints": [
            "Ask for exact file paths, complete signatures, tests, and examples.",
            "State non-goals explicitly: no frameworks, no hidden state, no broad refactors.",
        ],
        "execution_hints": [
            "Build one vertical slice before broadening coverage.",
            "Run focused tests before adding docs or extra features.",
        ],
        "anti_patterns": [
            "Adding abstractions before the first implementation proves the pattern.",
            "Changing unrelated modules while implementing one tool.",
        ],
    },
    "testing": {
        "thinking_prompts": [
            "Decide what behavior must be proven before moving to the next phase.",
            "List happy path, edge cases, and stop conditions separately.",
            "Write expected output shape before running tests.",
        ],
        "prompting_hints": [
            "Tell the agent to inspect artifacts before applying them.",
            "Ask for a pass/fail table and exact failure messages.",
            "Explicitly block scope jumps such as moving from pandas to Spark too early.",
        ],
        "execution_hints": [
            "Start with an import check, then one smoke test, then edge cases.",
            "Record failures before editing code.",
        ],
        "anti_patterns": [
            "Testing against production data when synthetic cases are enough.",
            "Fixing while testing without recording the original failure.",
        ],
    },
    "debugging": {
        "thinking_prompts": [
            "Capture the exact error and expected behavior before hypothesizing.",
            "Create the smallest reproduction that still fails.",
            "Separate input, environment, dependency, and logic causes.",
        ],
        "prompting_hints": [
            "Give the agent the error, reproduction steps, expected behavior, and recent changes.",
            "Ask for likely causes ranked by evidence, not guesses.",
        ],
        "execution_hints": [
            "Change one variable at a time.",
            "Verify the fix against the minimal reproduction before broader tests.",
        ],
        "anti_patterns": [
            "Patching symptoms without reproducing the failure.",
            "Changing multiple layers at once.",
        ],
    },
    "review": {
        "thinking_prompts": [
            "Judge the work against its stated acceptance criteria.",
            "Separate blockers from suggestions.",
            "Check API shape before style details.",
        ],
        "prompting_hints": [
            "Ask for findings grouped by blocker, should-fix, and optional improvement.",
            "Provide the intended behavior and public API to anchor the review.",
        ],
        "execution_hints": [
            "Review behavior and tests before cosmetic cleanup.",
            "Make every requested change actionable.",
        ],
        "anti_patterns": [
            "Reviewing preference as if it were correctness.",
            "Leaving vague comments without suggested action.",
        ],
    },
    "migration": {
        "thinking_prompts": [
            "Map dependencies before moving files.",
            "Define the smallest safe migration unit.",
            "Write rollback or restore assumptions before touching target paths.",
        ],
        "prompting_hints": [
            "Ask the agent to list source paths, target paths, imports, and tests before moving anything.",
            "Require an import check immediately after migration.",
        ],
        "execution_hints": [
            "Move one standalone unit at a time.",
            "Run import checks before behavior tests.",
        ],
        "anti_patterns": [
            "Bundling unrelated cleanup into a migration.",
            "Moving files before dependency boundaries are understood.",
        ],
    },
    "handoff": {
        "thinking_prompts": [
            "Write the current state before the history.",
            "List artifacts and test results with locations.",
            "Make the next action concrete and testable.",
        ],
        "prompting_hints": [
            "Ask the recipient to confirm understanding of open issues before proceeding.",
            "Include decisions made and rejected ideas to reduce rework.",
        ],
        "execution_hints": [
            "Do not require chat history to continue.",
            "Make unresolved questions explicit.",
        ],
        "anti_patterns": [
            "Writing a diary instead of durable state.",
            "Hiding blockers inside narrative text.",
        ],
    },
    "decision": {
        "thinking_prompts": [
            "State the decision as a question.",
            "List options before arguing for one.",
            "Define criteria and reversibility before comparing tradeoffs.",
        ],
        "prompting_hints": [
            "Ask for a recommendation with rationale, tradeoffs, risks, and next action.",
            "Provide decision criteria so the agent does not optimize for the wrong thing.",
        ],
        "execution_hints": [
            "Prefer reversible next steps when uncertainty is high.",
            "Document what would change the recommendation.",
        ],
        "anti_patterns": [
            "Choosing from options before defining decision criteria.",
            "Treating all risks as equal.",
        ],
    },
    "analysis": {
        "thinking_prompts": [
            "State the analysis question before collecting data.",
            "Separate evidence from interpretation.",
            "Name confidence, caveats, and what would change the conclusion.",
        ],
        "prompting_hints": [
            "Ask for findings, caveats, confidence, and recommended next action.",
            "Provide evidence sources and known limitations up front.",
        ],
        "execution_hints": [
            "Validate inputs before interpreting results.",
            "Return signal, not raw observations only.",
        ],
        "anti_patterns": [
            "Starting analysis without a question.",
            "Mixing caveats into recommendations without labeling them.",
        ],
    },
    "greenfield": {
        "thinking_prompts": [
            "Define the directory structure and public API before writing code.",
            "Identify the closest existing tool to use as a reference pattern.",
            "List files to create and their single responsibility.",
        ],
        "prompting_hints": [
            "Provide the reference tool path so conventions are inherited, not invented.",
            "State acceptance criteria as runnable commands (CLI, tests).",
        ],
        "execution_hints": [
            "Create models first, then logic, then CLI, then tests.",
            "Run tests after each file, not after all files.",
        ],
        "anti_patterns": [
            "Adding enforcement ceremony for files that do not exist yet.",
            "Over-engineering before the first test passes.",
        ],
    },
    "retrospective": {
        "thinking_prompts": [
            "List all tools available and which were actually used.",
            "Identify systematic gaps between capability and usage.",
            "Separate outcome quality from process quality.",
            "Name what was skipped and why — habit, judgment, or no trigger.",
        ],
        "prompting_hints": [
            "Provide the full tool inventory and session timeline.",
            "Ask for concrete recommendations, not just observations.",
        ],
        "execution_hints": [
            "Rate each skip: harmless, minor cost, or significant cost.",
            "Identify patterns, not just individual incidents.",
            "Produce actionable fixes (workflow triggers, defaults, nudges).",
        ],
        "anti_patterns": [
            "Listing everything without judging significance.",
            "Blaming the agent without identifying systemic causes.",
            "Recommending changes without evidence of harm.",
        ],
    },
}

_MODE_DEFAULTS: dict[str, dict[str, list[Any]]] = {
    "planning": {
        "plan": [
            {
                "phase": "orient",
                "action": "State the task and intended outcome in plain language.",
                "why": "A clear task statement prevents downstream ambiguity.",
                "done_when": "The task, goal, and desired outcome are understandable without extra context.",
            },
            {
                "phase": "gather",
                "action": "Identify available artifacts, inputs, dependencies, and current state.",
                "why": "Planning quality depends on knowing what evidence and materials are available.",
                "done_when": "Required resources are listed or missing resources are called out.",
            },
            {
                "phase": "separate",
                "action": "Separate known facts, assumptions, open questions, and decisions needed.",
                "why": "Mixing facts and assumptions causes weak plans and poor handoffs.",
                "done_when": "Uncertainty is explicit instead of hidden in the task description.",
            },
            {
                "phase": "scope",
                "action": "Define what is in scope and out of scope.",
                "why": "Clear boundaries prevent accidental expansion during execution.",
                "done_when": "The executor knows what to do and what not to do.",
            },
            {
                "phase": "verify",
                "action": "Define acceptance criteria, stop conditions, and expected deliverables.",
                "why": "The task needs an objective definition of done before work starts.",
                "done_when": "The executor can decide whether to continue, stop, or hand off.",
            },
            {
                "phase": "handoff",
                "action": "Prepare a concise brief for yourself, a teammate, or an agent.",
                "why": "A good brief reduces back-and-forth and improves execution quality.",
                "done_when": "The brief can be pasted into a ticket, notebook, chat, or agent prompt.",
            },
        ],
        "critique_checks": [
            "Is the desired outcome clear enough to execute?",
            "Are facts, assumptions, and open questions separated?",
            "Are scope boundaries explicit?",
            "Are acceptance criteria testable?",
            "Would a teammate understand the task without a meeting?",
        ],
        "verification_steps": [
            "Review readiness gaps.",
            "Resolve or acknowledge open questions.",
            "Confirm acceptance criteria before execution.",
            "Confirm the handoff brief matches the intended audience.",
        ],
        "suggested_next_actions": [
            "Fill missing goal, scope, constraints, and done criteria.",
            "MUST: Run anchor('map') to orient on the codebase before execution.",
            "MUST: Review the accepted task's bounded memory_context before execution.",
            "Use the plan as the execution checklist.",
        ],
    },
    "implementation": {
        "plan": [
            {
                "phase": "orient",
                "action": "Confirm goal, constraints, expected files, and existing project patterns.",
                "why": "Implementation should fit the current codebase instead of inventing a new style.",
                "done_when": "Relevant conventions and target paths are known.",
            },
            {
                "phase": "design",
                "action": "Define the smallest useful public API and output contract.",
                "why": "A narrow API keeps the implementation maintainable.",
                "done_when": "Inputs, outputs, and non-goals are explicit.",
            },
            {
                "phase": "build",
                "action": "Implement the standalone function and only the helpers needed for readability.",
                "why": "Small functions reduce framework creep and make review easier.",
                "done_when": "The core behavior works on a realistic small example.",
            },
            {
                "phase": "test",
                "action": "Add deterministic tests for happy path, edge cases, and output contract.",
                "why": "Tests prove the utility and protect future changes.",
                "done_when": "Focused tests pass locally or in the target environment.",
            },
            {
                "phase": "document",
                "action": "Add usage examples, guardrails, and handoff notes.",
                "why": "A teammate should be able to use the tool without reading all source code.",
                "done_when": "Docs explain when to use it, how to call it, and what it returns.",
            },
        ],
        "critique_checks": [
            "Is this still a standalone function rather than a framework?",
            "Are inputs and outputs explicit?",
            "Is the public API smaller than the implementation details?",
            "Are helpers justified by readability?",
            "Are tests synthetic, deterministic, and focused?",
        ],
        "verification_steps": [
            "Run the targeted test file.",
            "Verify the output contract keys.",
            "Run at least one realistic usage example.",
            "Confirm no unrelated files or abstractions were added.",
        ],
        "suggested_next_actions": [
            "MUST: Run anchor('map') to understand codebase structure.",
            "MUST: Run anchor('lookup', 'relevant_function') before implementing.",
            "MUST: Run anchor('convention', action='new_function') for naming guidance.",
            "Implement the smallest useful version first.",
            "MUST: Run anchor('touched', 'file.py') after each file edit.",
            "MUST: Run anchor('preflight', changed_files=[...]) then anchor('test', changed_files=[...]).",
            "MUST: Run anchor('gate', actions_taken=[...]) before delivering.",
        ],
    },
    "testing": {
        "plan": [
            {
                "phase": "orient",
                "action": "Confirm the artifact, target paths, environment, and test goal.",
                "why": "Testing the wrong artifact or environment creates false confidence.",
                "done_when": "The item under test and success criteria are explicit.",
            },
            {
                "phase": "inspect",
                "action": "Inspect files and imports before moving or modifying production code.",
                "why": "Early inspection catches packaging and path issues safely.",
                "done_when": "Expected files, imports, and dependencies are understood.",
            },
            {
                "phase": "prepare",
                "action": "Create small synthetic test cases for happy path and edge cases.",
                "why": "Synthetic data keeps the test safe, deterministic, and easy to debug.",
                "done_when": "The test cases cover normal behavior and likely failure modes.",
            },
            {
                "phase": "execute",
                "action": "Run the smallest smoke test before broader tests.",
                "why": "A smoke test quickly separates import/setup issues from logic issues.",
                "done_when": "The function or artifact runs once and returns the expected shape.",
            },
            {
                "phase": "verify",
                "action": "Run edge cases and verify the output contract.",
                "why": "Output shape stability matters as much as individual values.",
                "done_when": "Required keys, caps, and error behavior are confirmed.",
            },
            {
                "phase": "summarize",
                "action": "Record pass/fail results, issues found, and recommended next step.",
                "why": "Testing should end with a clear promote, fix, or defer decision.",
                "done_when": "The result is easy to paste into a handoff or PR comment.",
            },
        ],
        "critique_checks": [
            "Did we inspect before modifying files?",
            "Are tests using synthetic data only?",
            "Did we test the intended implementation, not stale code?",
            "Did we validate the output contract, not only the happy path?",
            "Did we stop before expanding into unrelated work?",
        ],
        "verification_steps": [
            "Run an import check.",
            "Run a happy-path synthetic test.",
            "Run edge-case tests.",
            "Check required output keys and sample/list caps.",
            "Capture results and failures in a test summary.",
        ],
        "suggested_next_actions": [
            "Inspect the artifact or implementation before applying it.",
            "MUST: Run anchor('profile_table', df, subject='...') to understand data shape before testing.",
            "Run the smallest smoke test first.",
            "MUST: Run anchor('validate', df, rules=[...]) for semantic checks.",
            "Record failures before making changes.",
            "MUST: Run anchor('gate', actions_taken=[...]) after test changes.",
        ],
    },
    "debugging": {
        "plan": [
            {
                "phase": "capture",
                "action": "Capture the exact error, failing step, inputs, and expected behavior.",
                "why": "Precise failure context prevents guessing.",
                "done_when": "The error and expected behavior are written down.",
            },
            {
                "phase": "reproduce",
                "action": "Reproduce the issue with the smallest possible example.",
                "why": "A small reproduction separates root cause from surrounding noise.",
                "done_when": "The failure can be triggered consistently.",
            },
            {
                "phase": "isolate",
                "action": "Separate input, environment, dependency, and logic causes.",
                "why": "Fixing the wrong layer creates churn and regressions.",
                "done_when": "The likely cause category is identified.",
            },
            {
                "phase": "fix",
                "action": "Test one targeted fix at a time.",
                "why": "Small fixes are easier to verify and roll back.",
                "done_when": "The reproduction passes without unrelated changes.",
            },
            {
                "phase": "verify",
                "action": "Run regression checks and summarize root cause.",
                "why": "A fix is incomplete until regression risk is checked.",
                "done_when": "The root cause, fix, and tests are documented.",
            },
        ],
        "critique_checks": [
            "Are we guessing instead of reproducing?",
            "Did we isolate the smallest failing case?",
            "Did we confuse symptom with root cause?",
            "Are we changing one thing at a time?",
            "Did we verify the fix against regression risk?",
        ],
        "verification_steps": [
            "Confirm the failure reproduces.",
            "Run the targeted fix test.",
            "Run nearby regression tests.",
            "Document root cause and remaining risk.",
        ],
        "suggested_next_actions": [
            "MUST: Run anchor('trace', 'error text') to parse the traceback.",
            "MUST: Run anchor('known_error', 'error text') to check known failure patterns.",
            "MUST: Run anchor('map') to locate the failing module.",
            "Create the smallest failing example.",
            "Test one fix before broad cleanup.",
            "MUST: Run anchor('touched') + anchor('preflight') + anchor('test') + anchor('gate') after fix.",
            "SKILL: Load skills/debugging/SKILL.md for evidence-oriented diagnostics.",
        ],
    },
    "review": {
        "plan": [
            {
                "phase": "orient",
                "action": "Confirm the intent, changed artifacts, and review criteria.",
                "why": "A review should judge the work against its goal, not personal preference.",
                "done_when": "Intent and expected behavior are clear.",
            },
            {
                "phase": "api",
                "action": "Inspect public API, input/output contract, and backward compatibility.",
                "why": "API shape determines maintainability and adoption.",
                "done_when": "API risks and contract gaps are identified.",
            },
            {
                "phase": "behavior",
                "action": "Check core behavior, edge cases, and test coverage.",
                "why": "Correct behavior matters more than surface structure.",
                "done_when": "Happy path and failure modes are covered or gaps are explicit.",
            },
            {
                "phase": "maintainability",
                "action": "Check simplicity, dependencies, side effects, and team readability.",
                "why": "A good tool should be easy for another engineer to maintain.",
                "done_when": "Maintainability concerns are listed with severity.",
            },
            {
                "phase": "decision",
                "action": "Recommend approve, request changes, or block with reasons.",
                "why": "The review should produce an actionable outcome.",
                "done_when": "The next action is clear to the author.",
            },
        ],
        "critique_checks": [
            "Is the API too broad or unclear?",
            "Are there hidden side effects or global state?",
            "Are tests meaningful enough for the risk?",
            "Is this easy for a teammate to maintain?",
            "Are review comments actionable?",
        ],
        "verification_steps": [
            "Compare changes against acceptance criteria.",
            "Run or inspect targeted tests.",
            "Check for scope creep and unnecessary abstraction.",
            "Record approve/change/block decision.",
        ],
        "suggested_next_actions": [
            "MUST: Run anchor('map') to see full module structure.",
            "MUST: Run anchor('consistency') to check export alignment.",
            "MUST: Run anchor('impact', target='file.py') for downstream risk.",
            "Check test coverage before style concerns.",
            "Separate blockers from suggestions.",
            "SKILL: Load skills/code-comprehension/SKILL.md when understanding the change is material.",
        ],
    },
    "migration": {
        "plan": [
            {
                "phase": "map",
                "action": "Identify source paths, target paths, dependencies, and migration unit.",
                "why": "Safe migration starts with knowing what moves together.",
                "done_when": "Dependency and path constraints are explicit.",
            },
            {
                "phase": "isolate",
                "action": "Move the smallest safe standalone unit first.",
                "why": "Smaller migrations reduce blast radius and simplify rollback.",
                "done_when": "The target files exist without unrelated movement.",
            },
            {
                "phase": "import",
                "action": "Run import checks in the target environment.",
                "why": "Import failures catch dependency and packaging gaps early.",
                "done_when": "The migrated unit imports from the intended path.",
            },
            {
                "phase": "test",
                "action": "Run focused tests for the migrated unit.",
                "why": "Migration success means behavior still works, not only files moved.",
                "done_when": "Targeted tests pass in the destination environment.",
            },
            {
                "phase": "document",
                "action": "Document compatibility issues, deferrals, and next migration step.",
                "why": "Migration threads need durable handoff notes.",
                "done_when": "Open issues and follow-up work are explicit.",
            },
        ],
        "critique_checks": [
            "Are dependencies understood before moving files?",
            "Are we moving too much at once?",
            "Did imports still work after migration?",
            "Did tests prove behavior, not just file presence?",
            "Did we defer unrelated cleanup?",
        ],
        "verification_steps": [
            "Check target paths exist.",
            "Run import checks.",
            "Run focused tests.",
            "Record compatibility issues and follow-up items.",
        ],
        "suggested_next_actions": [
            "Map dependencies before copying files.",
            "Move the smallest standalone unit.",
            "Run imports before behavior tests.",
        ],
    },
    "handoff": {
        "plan": [
            {
                "phase": "summarize",
                "action": "Summarize current state, goal, and what changed.",
                "why": "The next person needs orientation before details.",
                "done_when": "The handoff can be understood in one minute.",
            },
            {
                "phase": "inventory",
                "action": "List artifacts, files, tests, outputs, and decisions.",
                "why": "Handoffs fail when artifacts and decisions are scattered.",
                "done_when": "All relevant materials are discoverable.",
            },
            {
                "phase": "status",
                "action": "Separate completed work, open issues, risks, and blockers.",
                "why": "The next executor needs to know where to resume safely.",
                "done_when": "Known gaps and blockers are explicit.",
            },
            {
                "phase": "next",
                "action": "Provide concrete next actions and acceptance criteria.",
                "why": "A good handoff reduces restart cost.",
                "done_when": "The next action is clear and testable.",
            },
        ],
        "critique_checks": [
            "Can another engineer continue without asking for chat history?",
            "Are decisions and rejected ideas documented?",
            "Are unresolved items explicit?",
            "Are artifacts easy to locate?",
            "Is the next action specific enough?",
        ],
        "verification_steps": [
            "Check artifact inventory.",
            "Check completed vs open work.",
            "Check next action and done criteria.",
            "Confirm the handoff brief is concise.",
        ],
        "suggested_next_actions": [
            "Summarize state before details.",
            "List artifacts and test results.",
            "Make the next action explicit.",
        ],
    },
    "decision": {
        "plan": [
            {
                "phase": "frame",
                "action": "State the decision and why it matters.",
                "why": "A clear decision frame prevents comparing the wrong options.",
                "done_when": "The decision can be written as a question or choice.",
            },
            {
                "phase": "options",
                "action": "List viable options and any non-options.",
                "why": "Explicit options make tradeoffs visible.",
                "done_when": "Each viable option is named and briefly described.",
            },
            {
                "phase": "criteria",
                "action": "Define decision criteria and constraints.",
                "why": "Criteria prevent preference-driven decisions.",
                "done_when": "Options can be compared against the same criteria.",
            },
            {
                "phase": "tradeoffs",
                "action": "Compare tradeoffs across benefits, costs, risks, and reversibility.",
                "why": "Good decisions account for operational consequences.",
                "done_when": "Tradeoffs are explicit enough to defend the recommendation.",
            },
            {
                "phase": "recommend",
                "action": "Recommend a path and define the next action.",
                "why": "Decision work should end with movement, not just analysis.",
                "done_when": "The recommendation, rationale, and next step are clear.",
            },
        ],
        "critique_checks": [
            "Is the decision framed clearly?",
            "Are options compared against the same criteria?",
            "Are risks and reversibility considered?",
            "Is the recommendation actionable?",
            "Are deferred decisions documented?",
        ],
        "verification_steps": [
            "Confirm decision question.",
            "Confirm options and criteria.",
            "Check tradeoffs and risks.",
            "Record recommendation and next action.",
        ],
        "suggested_next_actions": [
            "State the decision as a clear question.",
            "List viable options and criteria.",
            "Choose the next reversible step where possible.",
        ],
    },
    "analysis": {
        "plan": [
            {
                "phase": "frame",
                "action": "Define the question, subject, and expected output.",
                "why": "Analysis needs a target question to avoid wandering.",
                "done_when": "The analysis question and output format are clear.",
            },
            {
                "phase": "gather",
                "action": "Identify inputs, evidence, assumptions, and data quality constraints.",
                "why": "Analysis is only as strong as its evidence and assumptions.",
                "done_when": "Inputs and caveats are listed.",
            },
            {
                "phase": "inspect",
                "action": "Check the data or evidence before drawing conclusions.",
                "why": "Premature conclusions lead to rework and bad recommendations.",
                "done_when": "Basic sanity checks and limitations are known.",
            },
            {
                "phase": "summarize",
                "action": "Summarize findings, confidence, risks, and recommended action.",
                "why": "Analysis should produce usable signal, not just raw observations.",
                "done_when": "The output supports a decision or next action.",
            },
        ],
        "critique_checks": [
            "Is the analysis question specific?",
            "Are assumptions and limitations explicit?",
            "Is evidence separated from interpretation?",
            "Are confidence and risks stated?",
            "Does the analysis support a next action?",
        ],
        "verification_steps": [
            "Confirm the analysis question.",
            "Check input quality and assumptions.",
            "Validate key calculations or observations.",
            "Summarize findings with confidence and caveats.",
        ],
        "suggested_next_actions": [
            "Clarify the analysis question.",
            "List evidence and assumptions.",
            "Produce findings with caveats and next actions.",
        ],
    },

    "retrospective": {
        "plan": [
            {
                "phase": "inventory",
                "action": "List tools available and tools actually used during the session.",
                "why": "A complete inventory makes gaps visible without guessing.",
                "done_when": "Complete tool usage inventory with timestamps/order.",
            },
            {
                "phase": "gaps",
                "action": "Identify tools that SHOULD have been used but weren't, and why.",
                "why": "Understanding skip reasons reveals systemic vs one-off issues.",
                "done_when": "Each skipped tool has a reason (forgot, felt unnecessary, no trigger).",
            },
            {
                "phase": "outcomes",
                "action": "Assess outcome quality: did skipping tools cause problems?",
                "why": "Not every skip is harmful — focus on those with actual cost.",
                "done_when": "Each skip is rated: harmless, minor cost, significant cost.",
            },
            {
                "phase": "patterns",
                "action": "Identify systemic patterns (always skip X, always over-use Y).",
                "why": "Patterns are more actionable than individual incidents.",
                "done_when": "Actionable patterns identified with fix recommendations.",
            },
            {
                "phase": "recommendations",
                "action": "Produce concrete tool/process improvements.",
                "why": "Retrospectives only matter if they produce changes.",
                "done_when": "Each recommendation is specific enough to implement.",
            },
        ],
        "critique_checks": [
            "Did we consider all available tools, not just favorites?",
            "Did we separate process quality from outcome quality?",
            "Are recommendations specific enough to implement?",
            "Did we identify root causes, not just symptoms?",
            "Did we rate severity honestly (not all gaps are critical)?",
        ],
        "verification_steps": [
            "Confirm tool inventory is complete.",
            "Check each gap has a reason and cost rating.",
            "Verify patterns are supported by multiple instances.",
            "Confirm recommendations are actionable.",
        ],
        "suggested_next_actions": [
            "Complete the tool usage inventory.",
            "Rate each skip by actual cost.",
            "Identify systemic patterns across sessions.",
            "Implement top recommendation before next session.",
        ],
    },

    "greenfield": {
        "plan": [
            {
                "phase": "orient",
                "action": "Define directory structure, public API, and reference tool for conventions.",
                "why": "New tools should inherit patterns from existing ones, not invent new ones.",
                "done_when": "Directory layout, file list, and reference tool are documented.",
            },
            {
                "phase": "create",
                "action": "Create files in dependency order: models, logic, CLI, tests.",
                "why": "Bottom-up creation ensures each file can import its dependencies.",
                "done_when": "All files exist and parse without import errors.",
            },
            {
                "phase": "test",
                "action": "Run tests after each file, not after all files.",
                "why": "Incremental testing catches issues before they compound.",
                "done_when": "All tests pass.",
            },
            {
                "phase": "verify",
                "action": "Run CLI end-to-end on a realistic input.",
                "why": "Integration test proves the tool works as a unit.",
                "done_when": "CLI produces correct output for at least one real input.",
            },
        ],
        "critique_checks": [
            "Does the directory structure match the reference tool?",
            "Are imports relative and consistent with existing tools?",
            "Is the public API minimal?",
            "Do tests use tmp_path fixtures (no external dependencies)?",
        ],
        "verification_steps": [
            "All files parse (ast.parse).",
            "All tests pass.",
            "CLI runs end-to-end.",
        ],
        "suggested_next_actions": [
            "Identify the reference tool and read its structure.",
            "Create files in dependency order.",
            "Run tests after each file.",
            "MUST: Run anchor('touched', 'file.py') after each file edit.",
            "MUST: Run anchor('gate', actions_taken=[...]) before delivering.",
        ],
    },
}


_MODE_HINTS["documentation"] = {
    "thinking_prompts": ["Name the document and the reader-facing outcome."],
    "prompting_hints": ["Keep the scope to Markdown documentation."],
    "execution_hints": ["Review the rendered prose and links before delivery."],
    "anti_patterns": ["Using documentation mode for source or configuration changes."],
}

_MODE_DEFAULTS["documentation"] = {
    "plan": [
        {
            "phase": "draft",
            "action": "Update the named Markdown documentation within the stated scope.",
            "why": "A bounded documentation task should remain proportionate.",
            "done_when": "The named document communicates the intended outcome clearly.",
        },
        {
            "phase": "review",
            "action": "Review prose, links, and final changed paths.",
            "why": "Documentation still requires accurate delivery evidence.",
            "done_when": "Only Markdown files changed and the content is ready to deliver.",
        },
    ],
    "critique_checks": ["Is the content accurate and scoped to the named document?"],
    "verification_steps": ["Review the final Markdown diff and links."],
    "suggested_next_actions": ["Load the documentation skill before editing."],
}

_MODE_EVIDENCE_GAPS: dict[str, list[str]] = {
    "documentation": ["Named Markdown document or documentation scope and intended audience outcome."],
    "planning": [
        "Task goal, scope boundaries, constraints, acceptance criteria, and relevant artifacts.",
    ],
    "implementation": [
        "Existing code patterns, target file paths, public API, output contract, and tests expected for the MVP.",
    ],
    "testing": [
        "Artifact under test, expected import path, synthetic test cases, and expected output contract.",
    ],
    "debugging": [
        "Exact error, minimal reproduction, expected behavior, recent changes, and environment details.",
    ],
    "review": [
        "Changed files, intended behavior, tests run, acceptance criteria, and known tradeoffs.",
    ],
    "migration": [
        "Source paths, target paths, dependency boundaries, import checks, rollback assumptions, and focused tests.",
    ],
    "handoff": [
        "Current state, artifacts, decisions made, tests run, open issues, blockers, and next action.",
    ],
    "decision": [
        "Decision question, viable options, criteria, tradeoffs, risks, and reversibility.",
    ],
    "analysis": [
        "Analysis question, evidence sources, input quality checks, assumptions, caveats, and confidence level.",
    ],
    "greenfield": [
        "Goal, directory structure, reference tool for conventions, files to create, and acceptance criteria.",
    ],
}
