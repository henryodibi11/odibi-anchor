# Native skill distribution

Every `SKILL.md` must contain, in frontmatter and body:

- Frontmatter: `name:` (matches directory) and `description:` (one line, used by `anchor("skills")`).
- `## When to load` — a specific trigger (user request or symptom).
- `## When NOT to load` — at least one explicit exclusion.
- `## Enforcement` — one of: `System hook` (a gate enforces this),
  `Self-enforced` (discipline), or `Advisory`.
- Cross-references to related skills via `[[skill-name]]` or `skills/<name>/SKILL.md`.

The `skills/` directory contains exactly the native skill packages. Deterministic
Odibi Anchor documentation, universal development guidance, and lifecycle detail
live under `references/`; they are global distribution resources rather than skills.

`references/registry.json` additionally indexes the Engineering Reference Library.
Use `anchor("references")` for compact discovery, `anchor("references", "match", "query")`
for deterministic routing, and `anchor("references", "load", "id")` to load one complete
authored reference. Use `anchor("references", "search", "query", reference_id="id")` to search
licensed offline snapshots and `anchor("references", "load-section", "sec-v1-...")` to load one
exact bounded source section. References are advisory technical knowledge, not skills or gate obligations.
Registry entries include pinned licensed offline source snapshots; external URLs are provenance and
refresh locations, not a runtime dependency.
