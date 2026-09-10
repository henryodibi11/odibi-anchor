# Anti-rationalization examples

> Lifecycle detail supporting the global operating contract; it is not a discoverable skill.

Agents don't skip steps because they're lazy. They skip steps because they construct
a plausible-sounding reason why THIS TIME it's okay to skip. This reference catalogs
known rationalization pattern and provides the hard counter.

**Rule:** If you catch yourself thinking any of these, STOP. The thought itself is the signal
that you're about to cut a corner.

## The Master List

### Planning Rationalizations

| What you're thinking | Why it's wrong | What to do instead |
|---|---|---|
| "This is a simple change, I don't need to plan" | You don't know it's simple until you plan. "Simple" changes that skip planning have the highest failure rate. | `anchor("task")` — always. RuntimeError if you skip it. |
| "I already know what to do" | If you already know, planning takes 30 seconds. If you're wrong, planning saves 30 minutes. | Plan anyway. The cost is trivial if you're right. |
| "Planning is overhead for this" | Unsupported edits are guessing. | Use the runtime-required write lifecycle with reasoning depth proportional to the task. |
| "I'll plan as I go" | Planning while coding is rationalization, not planning. You're justifying decisions after making them. | STOP. Plan now. Then code. |
| "The user said 'just do it'" | The user wants results fast. Planning makes results fast. Skipping planning makes results wrong. | Plan quickly but completely. |
| "I planned in my head" | Unwritten plans can't be reviewed, can't be critiqued, and shift as you code. | Write it down in `anchor("task")` kwargs. |
| "This is just a follow-up to what I already planned" | Each feature needs fresh planning. Previous planning doesn't cover new scope. | Fresh `anchor("task")` for each feature. RuntimeError enforces this. |

### Memory Rationalizations

| What you're thinking | Why it's wrong | What to do instead |
|---|---|---|
| "I don't need to review the task memory" | Accepted task memory is bounded context you have not assessed. | Before source edits, acknowledge its selected IDs and metadata—or explicitly none. |
| "I should scan all memory just in case" | Full-store scanning breaks the bounded project/trust contract and adds noise. | Review only the accepted task's `memory_context`. |
| "A selected memory must be right" | Retrieval is exposure, not authority or verification. | Apply only relevant advice, verify it, then disposition and evaluate truthfully. |

### Research Rationalizations

| What you're thinking | Why it's wrong | What to do instead |
|---|---|---|
| "I know this library well enough" | You know the 10% you've used. The other 90% may contain the method you need. | Read the authoritative docs and inspect installed signatures. |
| "I'll figure out the API as I go" | Trial-and-error with APIs wastes tokens and time. 5 minutes of docs saves 30 minutes of guessing. | Read docs → explore API → map capabilities → THEN code. |
| "The function probably takes these params" | "Probably" = guessing. Wrong params = TypeError = debug loop. | `anchor("lookup")` or `help(function)`. 3 seconds. |
| "I've used similar libraries" | Similar ≠ same. Every library has its own conventions and gotchas. | Read THIS library's docs. |

### File Reading Rationalizations

| What you're thinking | Why it's wrong | What to do instead |
|---|---|---|
| "I know what's in that file" | You don't. You have a stale mental model that may be wrong. | Read the file. Every time. |
| "I read it earlier in this session" | It may have changed (you or another agent edited it). Your memory of it may be wrong. | Read it again if you're about to modify it. |
| "I just need to add one line, I don't need to read the whole file" | The "one line" depends on context — imports, variable names, patterns. | Read at least the relevant section. |
| "I can infer the structure from the function name" | Names lie. `process_data()` could do anything. | Read the actual code. |

### Verification Rationalizations

| What you're thinking | Why it's wrong | What to do instead |
|---|---|---|
| "This edit is trivial, no need to verify" | Trivial edits have trivial verification cost. Skip it and you'll pay 10x when it cascades. | Verify every edit. 3-second read-back minimum. |
| "I'll verify everything at the end" | If edit #2 is wrong, edits #3-#7 are wasted. Verify as you go. | Edit → verify → proceed under the global invariant. |
| "The tests will catch it" | Tests catch WHAT broke, not WHERE you went wrong. Verification catches errors at the source. | Verify first, test second. Both are required. |
| "It compiled, so it's correct" | Compiles ≠ correct. Syntax is the lowest bar. Logic errors compile fine. | Read it back. Does it do what you INTENDED? |

### Scope Rationalizations

| What you're thinking | Why it's wrong | What to do instead |
|---|---|---|
| "While I'm in here, I'll also fix..." | Scope creep is the #1 cause of cascading failures and long threads. | Hand off the bounded finding; retain it only if evidence shows it is reusable. One task per thread. |
| "This related issue is quick to fix" | It's never quick. And if it breaks, now you have two problems. | Hand it off and focus on the original task. |
| "It would be irresponsible to leave this broken" | It would be irresponsible to break the current task by scope-creeping. | Report the finding; capture a learning Observation only when it meets the learning firewall. |
| "The user would want me to fix this too" | The user wants the thing they asked for. Ask before expanding scope. | Ask: "I found X. Should I fix it now or defer?" |
| "I'm almost done, just one more thing" | "One more thing" is how 1-hour tasks become 3-hour tasks. | Checkpoint current work. Assess if thread has capacity. |

### Gate/Learn Rationalizations

| What you're thinking | Why it's wrong | What to do instead |
|---|---|---|
| "This change is too small for a gate" | Ungated source changes accumulate delivery debt. | The runtime allows at most 12 ungated edits; close the applicable delivery boundary before the 13th. |
| "I'll gate at the end of the session" | Gate checks for drift, test failures, and compliance. Unrelated batching defeats the purpose. | Gate at the coherent runtime or delivery boundary. |
| "Learn isn't necessary, nothing interesting happened" | A post-gate assessment is still required, but fabricated content is harmful. | Assess real Observation IDs or choose `nothing_reusable_learned`; the global operating contract owns the firewall. |
| "I'll remember this reusable finding" | Unrecorded genuine findings are unavailable to a future session. | If the finding is reusable, record a bounded Observation; otherwise assess no reusable learning. |
| "The assessment needs some content" | Fabricated content is not evidence. | Use truthful closure choices and the global learning firewall. |

### Testing Rationalizations

| What you're thinking | Why it's wrong | What to do instead |
|---|---|---|
| "Gate can't match test files by name, let me run the full suite" | Running everything is lazy — it doesn't prove you KNOW which tests cover your changes. Full suite passing doesn't mean YOUR changes are tested. | State the mapping explicitly: "tests/test_extractor.py covers lib/extractor.py". Then run the targeted test. Full suite is a bonus, not a substitute. |
| "All tests pass so my change is correct" | Tests pass ≠ change is correct. Tests cover existing behavior. Your NEW behavior may have zero coverage. | Write or identify a test that specifically exercises your change. |
| "I'll write the tests later" | Code without tests is unverified code. "Later" means never. | Write tests alongside the code, or immediately after. Gate checks for this. |
| "The existing tests are sufficient" | Sufficient for the OLD behavior. Your change introduced NEW behavior or modified existing behavior. | Check: does any test actually exercise the code path you changed? If not, add one. |

### Speed Rationalizations

| What you're thinking | Why it's wrong | What to do instead |
|---|---|---|
| "This workflow is slowing me down" | The workflow prevents the failures that ACTUALLY slow you down. | Trust the process. It exists because skipping it failed repeatedly. |
| "I can be faster if I skip X" | You're optimizing for speed of the current minute, not speed of the current task. | Measure total task time, not step time. |
| "The user is waiting, I should skip steps" | Delivering wrong work fast is slower than delivering right work at normal speed. | Fast AND right. Not fast OR right. |
| "I've done this before, I can shortcut" | Every instance is different. "I've done this before" is how bugs get copy-pasted. | Follow the process. Same steps, every time. |

### Recovery Rationalizations

| What you're thinking | Why it's wrong | What to do instead |
|---|---|---|
| "I can fix this with one more edit" | If your last 3 edits didn't fix it, your 4th won't either. You're in a fix loop. | Stop, preserve evidence, roll back safely, and re-plan. |
| "I'm almost there, just need to tweak..." | "Almost there" after 10 minutes of tweaking = lost. | Rollback to last known good state. |
| "The approach is fine, just this one part is tricky" | If "one part" has taken 3+ attempts, the approach may be wrong. | Step back. Is the approach actually right? |
| "Rolling back would waste all my progress" | Building on a broken foundation wastes MORE progress. | Rollback. The work you lose is work that was wrong. |

## How to use these examples

1. **Keep the global operating contract active** — before you start thinking about the task
2. **When you catch yourself thinking any of these** → the thought is the red flag
3. **Do the opposite of what the rationalization suggests** — every time
4. **If in doubt, do the step** — the cost of doing an unnecessary step is 5 seconds. The cost of skipping a necessary step is 5-30 minutes.

## The Meta-Rule

```
If you're constructing a reason why it's okay to skip a step,
that's proof you need to do the step.

The effort of rationalization > the effort of compliance.

NEVER skip bootstrap — "just a quick fix" is the #1 source of ungated edits.
NEVER skip `anchor("task")` — "I already know what to do" always leads to scope creep.
```

After a successful gate, close the generated obligation with explicit Observation IDs or
`outcome="nothing_reusable_learned"`. Never invent an Observation. The complete non-authority rules live in
the global operating contract.
