# When to ask

> Lifecycle detail supporting the global operating contract; it is not a discoverable skill.

The two failure modes: **asking too much** (user loses patience, says "just do it") and
**asking too little** (agent makes wrong assumption, wastes 20 minutes, delivers wrong thing).
This reference expands the global rule for when to stop and ask.

## The Decision Rule

```
Can I VERIFY my assumption without asking?
├── YES (read file, run test, check docs) → Verify, don't ask
├── NO, but assumption is LOW RISK → State assumption, proceed, verify later
└── NO, and assumption is HIGH RISK → ASK before proceeding
```

## Risk Assessment Matrix

| Factor | Low risk | High risk |
|---|---|---|
| **Reversibility** | Easy to undo (one file, config change) | Hard to undo (schema change, data migration, multi-file) |
| **Blast radius** | Affects one function | Affects multiple modules, callers, or production data |
| **Ambiguity** | One obvious interpretation | Multiple valid interpretations |
| **User preference** | Technical choice (algorithm, data structure) | UX/behavior choice (what the feature does) |
| **Precedent** | Similar pattern exists in codebase | No precedent, novel design |
| **Cost of wrong guess** | 2 minutes to fix | 20+ minutes to redo |

## When to ALWAYS Ask

These are hard gates — never proceed without user input:

| Situation | Why |
|---|---|
| **Multiple valid approaches** with different trade-offs | You can't know which trade-off the user prefers |
| **Ambiguous requirements** — "make it better", "clean this up" | "Better" means different things to different people |
| **Architecture/design decisions** | These are expensive to change later |
| **Scope expansion** — task reveals more work than expected | User may want to defer, split, or reprioritize |
| **Destructive actions** — deleting files, dropping tables, overwriting data | Irreversible = always confirm |
| **User's domain knowledge needed** — business rules, data meaning | You can't derive business intent from code |
| **Contradictory evidence** — docs say X, code does Y | User knows which is correct |
| **Exceeding stated scope** — user asked for A, but A requires B and C | User may want just A with a TODO for B and C |

### How to Ask Well

```
I have a decision point:

**Question:** [specific, answerable question]

**Option A:** [approach] — [trade-off]
**Option B:** [approach] — [trade-off]

**My recommendation:** [which and why]

Which would you prefer?
```

**Rules for asking:**
- Present options, not open-ended questions ("how should I do this?" → bad)
- Include your recommendation — the user wants guidance, not just choices
- Be specific — "should I use approach A or B?" not "what do you think?"
- One question at a time — don't dump 5 decision points at once
- Show what you already know — don't ask things you could have checked

### Request input without requiring thread monitoring

If the question genuinely blocks safe progress and the Slack variables
`ANCHOR_SLACK_BOT_TOKEN`, `ANCHOR_SLACK_CHANNEL_ID`, and `ANCHOR_SLACK_USER_ID` are present, check
presence only and use Odibi Anchor's portable API:

```python
from odibi_anchor import HumanInputTimeout, request_human_input

try:
    response = request_human_input(
        message=(
            "Decision needed: <question>. "
            "Options: <bounded options and consequences>. "
            "Recommendation: <recommended option and reason>."
        ),
        timeout_minutes=60,
    )
except HumanInputTimeout:
    raise SystemExit("No human response received; stopping safely")
```

The caller must remain alive while waiting. Durable state prevents duplicate responses
and records timing, but cannot resume a terminated process. If Slack configuration is
unavailable, use the execution harness's normal communication boundary. Stop rather
than guessing if no safe response path exists. This transport is the initial adapter;
the agent-facing capability is not coupled to Slack or Amp.

## When to NEVER Ask

These waste the user's time — just do them:

| Situation | Why |
|---|---|
| **Which file to read** | You can figure this out from `anchor("map")` |
| **Which anchor() tool to use** | That's your job — the skills tell you |
| **How to fix a syntax error** | You can see the error, fix it |
| **Import paths** | Use `anchor("lookup")` — don't ask the user |
| **Variable/function names** | Follow repository code standards |
| **Test structure** | Follow existing test patterns |
| **Whether to run tests** | Always run tests — don't ask |
| **Implementation details within approved plan** | User approved the plan — execute it |
| **Formatting, style, linting** | Follow repository standards — don't ask |
| **Whether to bootstrap** | Always bootstrap — don't ask |

## When to State-and-Proceed

For medium-risk situations, state your assumption and proceed. The user can correct you:

```
**Assumption:** I'm going to [approach] because [reasoning].
If you'd prefer a different approach, let me know and I'll adjust.

[proceed with implementation]
```

### Good candidates for state-and-proceed:

| Situation | Example |
|---|---|
| Technical implementation choice within approved plan | "Using window function for dedup (matches existing pattern)" |
| Error handling strategy | "Raising ValueError for invalid input (consistent with module)" |
| Test case selection | "Testing: happy path, empty input, null key — should cover main cases" |
| Default values for new parameters | "Defaulting to True for backward compatibility" |
| Naming choice with clear convention | "Naming it `_resolve_action()` — underscore prefix for module-private" |

## The 30-Second Rule

If you've been uncertain about something for more than 30 seconds:

1. **Can you verify it yourself?** (Read file, run test, check docs, `anchor("lookup")`)
   → YES: Verify now. Don't ask, don't guess.
2. **Is it a user-preference question?** (UX, scope, business logic)
   → YES: Ask now. Don't guess.
3. **Is it a technical question with a clear answer you just don't know?**
   → Research first (`web_search`, accepted-task memory context, read code). Ask only if research fails.

## Uncertainty Escalation

When you're uncertain, escalate through this sequence:

```
1. CHECK CODE     → Read the file, check existing patterns (5 sec)
2. CHECK MEMORY   → Review bounded accepted-task selections; verify relevant advice (5 sec)
3. CHECK DOCS     → anchor("lookup"), web_search for library docs (15 sec)
4. STATE + PROCEED → Low risk? State assumption, proceed (0 sec)
5. ASK USER       → High risk? Ask with options and recommendation
```

If the accepted task's bounded context is insufficient, use an explicit scoped memory query only
when it is material; do not widen this escalation into a routine full-store scan.

**Never jump to step 5.** Steps 1-3 resolve 80% of uncertainty without bothering the user.

## Common Rationalization Patterns

Agents rationalize both over-asking and under-asking:

### Under-asking rationalizations (dangerous):
| What the agent thinks | What's actually happening |
|---|---|
| "The user probably wants X" | Guessing user intent without evidence |
| "This is the obvious approach" | Obvious to you ≠ what the user wants |
| "I'll ask later if it's wrong" | Wrong work is wasted work |
| "Asking will slow things down" | Wrong assumptions slow things down more |

### Over-asking rationalizations (annoying):
| What the agent thinks | What's actually happening |
|---|---|
| "I should confirm everything" | You're offloading your decisions to the user |
| "The user knows best" | The user hired you to make technical decisions |
| "I don't want to get it wrong" | Use verification, not questions, for technical facts |
| "Let me check before proceeding" | Check the code/docs/memory, not the user |

## Integration with thread discipline

This reference complements [thread discipline](thread-discipline.md):
- Thread discipline defines WHEN to stop (after planning, after implementation, before commit)
- This reference explains how to decide whether to stop at other moments

## Anti-Patterns

| Don't | Do instead |
|---|---|
| Ask "how should I implement this?" | Present 2 options with trade-offs and your recommendation |
| Ask a question you could answer by reading code | Read the code first |
| Proceed silently on a high-risk assumption | Ask — 30 seconds of user time saves 30 minutes of wrong work |
| Dump 5 questions at once | Ask the most important one, proceed on the rest |
| Ask permission for things in the approved plan | The plan was approved — execute it |
| Guess at business rules | Always ask — you can't derive business intent from code |
