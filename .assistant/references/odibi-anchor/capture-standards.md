# Truth-preserving capture standards

## Universal structure

Material records should make these dimensions explicit when applicable:

1. **Context** — task, environment, time window, and boundary.
2. **Observation** — what was directly seen, without inferred cause.
3. **Evidence** — retained source, exact method, result, and integrity/version.
4. **Interpretation** — what the observation may mean, labeled as analysis.
5. **Uncertainty** — missing, conflicting, unsupported, or unavailable evidence.
6. **Consequence** — why the fact matters and what it changes.
7. **Next authority** — who or what may authorize the next action.
8. **Provenance** — actor/tool, timestamp, project, revision, path/source, and lineage.

Omit inapplicable fields rather than inventing content. Never collapse observation and
interpretation into a causal claim unless the evidence establishes that cause.

## Record contracts

- **Task context:** outcome, scope/non-scope, authority, constraints, risks, resources,
  acceptance criteria, expected outputs, and stop conditions.
- **Journal:** event, status, actor/tool, task-window ID, time, affected object. Automatic;
  no chain-of-thought, credentials, raw private data, or unsupported conclusions.
- **Observation:** factual wording, context, provenance, uncertainty, recurrence signal,
  and why it may be reusable. Capture while working; assess at closure.
- **Evidence:** claim supported or contradicted, exact method/command, input identity,
  decisive result, status, timestamp, and retained location. Status vocabulary is only
  `passed`, `failed`, `skipped`, `blocked`, or `unavailable`.
- **Problem:** gap, known facts, issue tree, hypotheses, evidence, synthesis, material
  uncertainty, decision needed, and next action. Do not presuppose the solution.
- **Decision:** chosen option, owner, date, evidence, alternatives, rationale, consequences,
  reversal conditions, and linked Problem/Spec/Work Item.
- **Spec:** problem, required behavior, explicit non-goals, permissions, acceptance criteria,
  invariants, risks, compatibility, verification, and authority links.
- **Work Item:** outcome, scope/non-goals, owner/authority, dependencies, acceptance evidence,
  risks, status/disposition, revisions, and reopening triggers.
- **Memory candidate:** reusable insight, supporting evidence, project/trust scope,
  applicability, limits, confidence, provenance, bounded semantic-overlap result, canonical or
  related memory ID when one exists, and supersession relationship. Equivalent recurrence adds
  provenance and evidence to the canonical claim through the governed lifecycle instead of
  creating a competing candidate. Materially distinct or contradictory claims remain separate.
  Project-local versus cross-project applicability is independent of candidate/active/confirmed
  authority. Retrieval is not application; application is not validation; evaluation requires
  evidence.
- **Lesson:** supporting observations, recurrence or strong validation, applicability,
  limits, owner, and decision to publish. Promotion remains human-attested.
- **Trail:** trigger, ordered procedure, prerequisites, verification, failure modes,
  version, and provenance. A trail does not edit procedural guidance automatically.
- **Skill/reference:** audience, purpose, scope, canonical procedure, examples, failure
  modes, verification, provenance, and maintenance owner.
- **Source:** title, origin, author/publisher, retrieved date, version, license/terms,
  integrity hash when useful, and local relationship to the original. Preserve attribution.
- **Notebook:** purpose, inputs and versions, environment, deterministic steps, outputs,
  assertions, limitations, and links to governing evidence.
- **Verification result:** exact check, scope, status, method/command, decisive result,
  retained evidence, and unavailable portions. Invocation alone is not verification.
- **Snapshot:** timestamp, bounded scope, source identity/revision, captured state, integrity,
  limitations, and relationship to any live source.
- **Handoff:** current state and revision, governing authority, completed and remaining work,
  evidence, decisions, risks, unavailable context, exact next action, and stop conditions.
- **Terminal return:** completed/blocked/failed status; exact project/repository, branch and
  revision; outcome and changed artifacts; checks actually executed and retained evidence;
  review/gate/learning state; shared effects; commit/push/deploy/publication state;
  deviations, unavailable evidence, residual uncertainty, and next authority.

## Lifecycle

Capture observations during work. At closure, assess whether reusable learning exists. Before
creating a memory candidate, search bounded memory within the active project and trust domain and
compare operational claims, applicability, limits, freshness, and contradiction conditions.
Consolidate equivalent recurrence only through the governed lifecycle with explicit immutable
lineage; never merge across trust domains or infer broader applicability or authority. Review the
accepted task's bounded memory selections, record explicit applications, evaluate usefulness from
evidence, and govern stale or harmful records. Journals support forensic reconstruction, not exact
model or hidden-reasoning replay.
