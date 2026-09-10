# Declarative Visualization API Crosswalk

**Researched:** 2026-08-14 | **Baselines:** Vega 6.3.1, Vega-Lite 6.4.3, Altair 6.2.2, vl-convert 1.9.0
**Evidence:** [D] documentation; [S] schemas, Python stubs, and public source; [U] browser and Power BI host behavior

Use this guide to decide which layer should own a feature and to translate intent between Python,
Vega-Lite JSON, Vega runtime primitives, static artifacts, and Deneb. Load exact source sections with
`anchor("references", "search", "query", reference_id="visualization.api-crosswalk")` when signatures or
schema details matter.

## Layer ownership decision table

| Need | Default owner | Why | Escalate when |
| --- | --- | --- | --- |
| Typed application intent | Pydantic/domain model | Stable product contract independent of a renderer | Never expose the entire upstream schema as the product API |
| Common statistical chart | Altair or Vega-Lite | Concise grammar with schema validation | The compiler cannot express required event/dataflow behavior |
| JSON artifact for portability | Vega-Lite | Language-neutral and accepted by multiple hosts | Target requires lower-level Vega |
| Custom reactive interaction | Vega | Signals, event streams, data mutation, View API | Vega-Lite parameters compile the behavior correctly |
| Python static export | vl-convert | Headless SVG/PNG/PDF without browser automation | Host-specific rendering must be qualified |
| Power BI custom visual | Deneb | Binds report data to Vega/Vega-Lite | Cross-visual behavior requires host testing |

Keep ownership at the highest layer that expresses the requirement without hidden post-processing.
Dropping to Vega is not inherently more capable engineering; it increases contract surface and test
cost. Conversely, repeatedly rewriting compiled Vega is fragile because compiler output is not the
authoring contract.

## Artifact crosswalk

| Artifact | Producer | Consumer | Durable use | Main risk |
| --- | --- | --- | --- | --- |
| Domain intent model | Application/Pydantic | Compiler adapter | Product API and saved configuration | Coupling intent to one backend |
| `alt.Chart` | Altair | Python caller | Authoring convenience | Python/runtime dependency |
| Vega-Lite `dict`/JSON | Altair or direct builder | Vega-Lite compiler, Deneb | Portable declarative artifact | Version mismatch |
| Vega JSON | Vega-Lite compiler or direct builder | Vega View/runtime | Exact reactive runtime graph | Larger and lower-level contract |
| SVG | Vega/vl-convert | Browser, docs, office tools | Diffable vector evidence | Fonts and external assets |
| PNG/JPEG | Vega/vl-convert | Any image consumer | Fixed raster evidence | Resolution and accessibility loss |
| PDF | vl-convert | Document workflows | Print/vector output | Font embedding and page assumptions |
| Bundled HTML | vl-convert | Browser | Offline interactive artifact | Embedded script/security policy |
| Deneb template/spec | Application/manual author | Power BI Deneb | Report-hosted visualization | Dataset names and host semantics |

## Altair to Vega-Lite map

| Altair | Vega-Lite output | Notes |
| --- | --- | --- |
| `alt.Chart(data)` | `data` plus unit-spec shell | Data transformer controls serialization |
| `.mark_bar(...)` | `mark: {type: "bar", ...}` | Mark properties are not encoding channels |
| `.encode(x=alt.X(...))` | `encoding.x` | Prefer explicit types at public boundaries |
| `.transform_filter(...)` | ordered `transform` entry | Transform order changes available fields |
| `.properties(...)` | top-level title/width/height/etc. | Width/height semantics vary in composition |
| `.add_params(param)` | top-level `params` | Selection helpers create selection parameters |
| `alt.condition(...)` | channel `condition` | Condition changes encoding, not row membership |
| `chart + other` | `layer` | Resolve scales explicitly when defaults are wrong |
| `chart | other` | `hconcat` | Child sizing and shared resolution require review |
| `chart & other` | `vconcat` | Same portability concerns as horizontal concat |
| `.facet(...)` | `facet` + `spec` | Facet selection resolution can be global/union/intersect |
| `.repeat(...)` | `repeat` + `spec` | Repeated field references compile into child specs |
| `.to_dict(validate=True)` | validated Python dictionary | Validation targets Altair's bundled schema |

Do not infer Power BI support from successful Altair serialization. Before Deneb export, remove or
replace Python-local data, validate against the target Vega-Lite version, and qualify interactions in
the host.

## Mark selection map

| Analytical question | Typical mark | Required encodings | Common failure |
| --- | --- | --- | --- |
| Compare magnitudes | bar | categorical axis + quantitative axis | Truncated scale exaggerates differences |
| Trend over ordered time | line | temporal/ordinal x + quantitative y | Unsorted or missing time values connect incorrectly |
| Relationship/distribution | point/circle | quantitative x/y | Overplotting hides density |
| Range over continuous domain | area | x plus y/y2 | Baseline implies meaning that data lacks |
| Interval/range | rule/bar | x/x2 or y/y2 | Confusing interval with point estimate |
| Matrix/intensity | rect | x/y categories + color | Color scale lacks perceptual ordering |
| Part-to-whole | arc | theta + color | Too many categories and poor comparison accuracy |
| Spatial geometry | geoshape | shape plus projection | Invalid topology/CRS or excessive geometry |
| Annotation | text/rule | datum/value or field | Annotation participates in scale unexpectedly |

## Encoding channel map

- `x`, `y`, `x2`, `y2`: position and ranges; field type and scale determine semantics.
- `color`, `fill`, `stroke`: grouping or magnitude; distinguish categorical palettes from continuous
  ramps and preserve contrast.
- `size`, `strokeWidth`: magnitude; area perception means symbol radius is not direct magnitude.
- `shape`, `strokeDash`: categorical redundancy useful for accessibility, bounded to few categories.
- `opacity`: emphasis, uncertainty, or selection state; low values can disappear on export.
- `detail`: grouping without a visible channel, especially for separate lines.
- `order`: stack/line order; do not rely on source row order.
- `tooltip`: display values only; it is not a semantic encoding or accessibility substitute.
- `href`: browser navigation; host security and click handling may override it.
- `facet`, `row`, `column`: composition; cardinality directly controls view count and cost.

## Transform output-field crosswalk

| Transform | Row effect | New fields | Design consequence |
| --- | --- | --- | --- |
| `filter` | removes rows | none | Later domains and aggregates see only survivors |
| `calculate` | preserves rows | named expression result | Expression type is runtime-derived |
| `bin` | usually preserves rows | start/end bin fields | Encode bin semantics consistently |
| `timeUnit` | preserves rows | derived temporal unit | Timezone and week/year semantics matter |
| `aggregate` | collapses groups | aliases for measures | Original row fields disappear unless grouped |
| `joinaggregate` | preserves rows | group aggregate aliases | Useful for percent-of-total and comparisons |
| `window` | preserves rows | rank/lag/running aggregate aliases | Sort and frame are part of correctness |
| `fold` | multiplies rows | key/value fields | Original wide measure names become values |
| `flatten` | multiplies rows | flattened array values | Parallel arrays can create nulls/misalignment |
| `pivot` | widens rows | data-dependent columns | Output schema may be unstable |
| `lookup` | preserves or enriches | selected lookup fields | Missing matches and duplicate keys need policy |
| `density`/`regression`/`loess` | generates rows | transform-specific outputs | Derived data is not original evidence |
| `stack` | preserves groups | start/end fields | Order, offset, and grouping control meaning |

Any engine that references fields after transforms should maintain an explicit field-lineage model or
validate emitted field references after each compilation phase.

## Vega-Lite parameter to Vega runtime map

Vega-Lite parameters compile to Vega signals and, for selections, supporting data stores and event
streams. Treat the compiled names as implementation details unless a target host explicitly requires
them. Prefer authoring with:

- value parameter → reusable value/expression input;
- point selection → discrete tuples selected by click/pointer events;
- interval selection → continuous x/y extents selected by drag/zoom;
- `condition` → visual response without filtering rows;
- `transform.filter` → data subset driven by parameter/selection;
- `bind` → widget, legend, or scale interaction;
- `resolve` → global, union, or intersect behavior in repeated/faceted views.

Escalate to direct Vega when you need arbitrary signal update expressions, custom event-stream
composition, direct runtime dataset mutation, signal listeners, state capture/restoration, or a View
integration contract not expressible through parameters.

## Vega runtime API map

| Task | API | Required follow-up |
| --- | --- | --- |
| Parse authoring JSON | `vega.parse(spec)` | Handle parse errors before creating a View |
| Instantiate | `new vega.View(runtime, options)` | Configure loader before construction if URLs load immediately |
| Attach/headless initialize | `view.initialize(container?)` | No container is valid for static export |
| Evaluate/render | `await view.runAsync()` | Await before reading final state or rerunning |
| Read/write signal | `view.signal(name[, value])` | Call `runAsync()` after writes |
| Observe signal | `addSignalListener` / `removeSignalListener` | Avoid recursive synchronous runs inside listeners |
| Read/mutate data | `view.data`, `view.change`, `view.insert/remove` | Run dataflow after mutation |
| Inspect state | `getState`, `scenegraph`, `scale` | Internal state filters are expert APIs |
| Export | `toSVG`, `toCanvas`, `toImageURL` | Wait for dataflow and assets first |
| Dispose | `view.finalize()` | Required to remove timers/listeners |

Call `hover()` only once. Changing renderer, loader, or tooltip can reset rendering state and requires a
subsequent run. External data loading may begin during View construction, so late loader replacement is
not a security boundary.

## Export decision matrix

| Requirement | Recommended call | Return |
| --- | --- | --- |
| Vega-Lite → Vega | `vl_convert.vegalite_to_vega(spec, vl_version=...)` | dictionary |
| Vega-Lite → SVG | `vl_convert.vegalite_to_svg(spec, ...)` | string |
| Vega-Lite → PNG | `vl_convert.vegalite_to_png(spec, scale=..., ppi=...)` | bytes |
| Vega-Lite → JPEG | `vl_convert.vegalite_to_jpeg(spec, scale=..., quality=...)` | bytes |
| Vega-Lite → PDF | `vl_convert.vegalite_to_pdf(spec, scale=...)` | bytes |
| Vega-Lite → offline HTML | `vl_convert.vegalite_to_html(spec, bundle=True, ...)` | string |
| Vega → static output | corresponding `vega_to_*` call | string/bytes/dict |
| Existing SVG → raster/PDF | `svg_to_png/jpeg/pdf` | bytes |

Pin `vl_version` where reproducibility matters. On render calls that expose `allowed_base_urls` (such
as SVG, PNG, JPEG, and PDF conversion), pass an explicit policy for inputs that may contain external
data or images; compilation and `vegalite_to_html` do not expose this parameter. Register fonts before
conversion and test font availability; schema validity does not guarantee deterministic text metrics.

## Compatibility checklist

1. Record authoring, compiler, renderer, and host versions separately.
2. Validate the intent model before compiling.
3. Validate emitted Vega-Lite against the exact target schema.
4. Compile with the intended Vega-Lite version and retain warnings.
5. Ensure data references are portable for the destination.
6. Check transformed field lineage and composition resolution.
7. Render at least one static artifact in the target runtime.
8. Exercise interactions in the actual host when host effects matter.
9. Preserve spec, versions, input attestation, warnings, and rendered evidence together.

## Sources and refresh

- Offline Vega View API: `snapshots/vega-6.3.1/docs/docs/api/view.md`
- Offline Vega-Lite schema/docs: `snapshots/vega-lite-6.4.3/`
- Offline Altair API and guide: `snapshots/altair-6.2.2/`
- Offline vl-convert stubs: `snapshots/vl-convert-1.9.0/vl-convert-python/vl_convert.pyi`

Refresh when any adopted component changes minor/major version or target-host evidence contradicts a
crosswalk. Re-run representative compile/export tests after refresh.
