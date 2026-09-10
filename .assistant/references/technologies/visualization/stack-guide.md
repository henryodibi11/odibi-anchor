# Declarative Visualization Stack

**Researched:** 2026-08-14  
**Evidence:** [D] architecture and APIs; [S] public schemas/source; [U] current browser and Power BI host behavior  
**Baselines:** Vega 6.3.1; Vega-Lite 6.4.3; Altair 6.2.2; Deneb 1.9.1

## Mental model

The stack is a set of boundaries, not interchangeable chart libraries:

```text
Python objects/data ──Altair──▶ Vega-Lite JSON ──compiler──▶ Vega JSON
                                                           │
                                         Vega runtime ◀────┘
                                           │         │
                                  browser canvas/SVG  static export

Power BI fields ──Deneb dataset──▶ Vega-Lite or Vega JSON ──Power BI visual host
```

Altair is a Python authoring API and schema wrapper. Vega-Lite is the concise declarative grammar.
Vega is the lower-level reactive grammar and runtime contract. `vl-convert` compiles or renders
without requiring a separately installed browser. Deneb supplies Power BI data binding and host
integration; it does not make every web interaction available in Power BI. [D]

## Selection guide

Use **Altair** when Python is the authoring environment, typed construction and composition improve
maintainability, and Vega-Lite can express the design. Serialize with `chart.to_dict()` or
`chart.to_json()` at the portability boundary.

Use **Vega-Lite JSON** when the artifact must move between Python, JavaScript, Deneb, registries, or
other hosts. It is the best default interchange contract because it is declarative and schema-backed.

Use **Vega** only when the requirement needs explicit signals, event streams, dataflow plumbing,
custom mark encode sets, or interactions the Vega-Lite compiler cannot express. The extra power costs
more specification volume and lower portability.

Use **vl-convert** for deterministic compilation and static export. Use **Vega-Embed** for browser
embedding and action menus. Use **Deneb** only after qualifying the generated spec against its exact
embedded Vega/Vega-Lite versions and Power BI interaction contract.

## Portable engine boundary

A durable engine should keep these layers separate:

1. **Intent model:** chart purpose, fields, semantics, interaction, accessibility, output target.
2. **Validated intermediate representation:** host-neutral typed model with explicit defaults.
3. **Compiler:** deterministic emission of Vega-Lite first, Vega only for an explicit escape hatch.
4. **Adapters:** pandas/Spark summarize data; Deneb binds Power BI fields; exporters render files.
5. **Evidence:** emitted schema version, compiler version, warnings, unsupported host features.

Do not pass a Spark DataFrame into a chart renderer and hope it remains scalable. Aggregate or sample
under an explicit policy, then materialize a bounded interchange dataset. Never silently sample.

## Self-teaching output

An engine should return the spec plus: why this chart fits the analytical question, field/channel
mapping, interaction instructions, accessibility notes, portability status, generated code snippets,
and limitations. Keep teaching metadata separate from valid Vega-Lite JSON so downstream tools can
consume the specification unchanged.

## Cross-host qualification checklist

- Validate the JSON against the exact target schema.
- Confirm bundled Vega/Vega-Lite versions, not only authoring versions.
- Bound row count and payload size; test representative high-cardinality data.
- Verify selections, cross-filtering, highlighting, tooltips, keyboard behavior, themes, and export in
  the actual host. Browser success is not Power BI success. [U]
- Confirm fonts and locale. Static exporters and hosts may resolve them differently.
- Preserve field names deliberately; Power BI and transformations can reshape or rename data.
- Treat URL loading and expression execution as a security boundary.

## Failure modes

- Emitting implementation-specific Python instead of a portable spec.
- Assuming all Vega features round-trip through Vega-Lite or Deneb.
- Mixing host metadata into the visualization grammar.
- Allowing unbounded raw records into the renderer.
- Claiming interaction support from schema validation alone.
- Pinning Altair but not recording its bundled Vega-Lite schema.

## Authoritative sources

- Vega project: https://vega.github.io/
- Vega-Lite docs: https://vega.github.io/vega-lite/docs/
- Altair docs: https://altair-viz.github.io/
- Deneb docs: https://deneb.guide/

## Refresh triggers

Refresh when any stack component changes major version, an adopted minor changes schemas or host
behavior, or retained browser/Power BI qualification contradicts this guide.
