# Engineering Reference Library

This is Odibi Anchor's offline technical knowledge layer. Skills prescribe **how to work**;
references explain **the technology being worked on**. `registry.json` is the closed discovery
contract. Use `anchor("references")` to list, `anchor("references", "match", "query")` to find, and
`anchor("references", "load", "id")` to load full content. Task routing returns compact recommendations;
it never injects full documents or creates a compliance obligation.

For authoritative offline depth, search the licensed snapshots and load only the section needed:

```python
matches = anchor(
    "references", "search", "interval selection resolve union",
    reference_id="visualization.vega-lite", limit=5, output_format="dict",
)
section = anchor(
    "references", "load-section", matches["sections"][0]["section_id"],
    output_format="dict",
)
```

`search` returns bounded previews, source revision, path/lines, and file/section digests without full
content. `load-section` returns one exact section capped at 32 KiB/200 lines. Search can be global or
scoped to a registry ID. Both operate entirely from the distributed manifest and snapshots; they do
not access the network, execute retained source, or write a cache.

## Authored catalog

- **Stack and APIs:** `visualization.stack`, `visualization.api-crosswalk`
- **Interaction:** `visualization.interactions`, plus the Vega/Vega-Lite/Altair guides
- **Power BI:** `visualization.deneb-powerbi`, `visualization.powerbi-portability`
- **Rendering/export:** `visualization.vl-convert`
- **Typed contracts:** `python.pydantic-v2`, `python.pydantic-contracts`

Load an authored guide first for decisions and boundaries, then search/load source sections for exact
signatures or schema behavior. A search match is evidence discovery, not proof that the section was
read. Source inspection does not prove browser, Databricks, or Power BI host behavior.

## Evidence labels

- **[D] Documented:** supported by an authoritative public document.
- **[S] Source-inspected:** checked against public source or a published schema.
- **[V] Runtime-verified:** exercised at the stated version in a retained runtime.
- **[H] Host-verified:** exercised in the named external host.
- **[U] Unverified:** not exercised in the required runtime or host; treat as a qualification item.

Absence of [V] or [H] is deliberate. Documentation evidence does not prove runtime or host behavior.

## Offline source snapshots

Each registry entry links to licensed, pinned source documentation, schemas, or API files under
`snapshots/`. `snapshots/manifest.json` records source repository, immutable revision, archive hash,
file hashes, license, and any source that could not be copied safely. These snapshots are available to
agents with no network connection; web URLs remain provenance and refresh locations only. Run
`scripts/update_engineering_reference_snapshots.py` with network access to rebuild the snapshots after
reviewing a version change. Never update from a floating branch.

## Updating the library

Use the controlled path:

```text
observation → reference candidate → source/runtime verification → reviewed edit → published reference
```

Structured learning may identify a candidate but never edits global guidance automatically. Update a
reference when a registry refresh trigger fires, retain authoritative URLs and exact baselines, label
unavailable evidence, validate the registry, review the diff, and publish through the ordinary Anchor
lifecycle. Keep project-specific findings in managed project artifacts rather than this distribution.

Do not copy upstream documentation wholesale. Add original synthesis, decision boundaries, executable
micro-examples, failure modes, and explicit host limitations. Employer or proprietary material is
never eligible for this library.
