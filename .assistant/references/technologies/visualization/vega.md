# Vega Engineering Reference

**Researched:** 2026-08-14 | **Baseline:** 6.3.1  
**Evidence:** [D] public docs; [S] public source; [U] browser/runtime behavior in the target host

Vega is a declarative JSON grammar plus a reactive runtime. A specification defines data sources,
transforms, scales, projections, axes, legends, signals, marks, and configuration. The parser builds a
dataflow; changes propagate through operators and update a scenegraph rendered to Canvas or SVG. [D]

## Core anatomy

- `data`: named datasets, inline values, URLs, source derivation, transforms, and update triggers.
- `signals`: reactive scalar/object state. Signals may have `value`, `init`, `update`, `on`, or `bind`.
- `scales`/`projections`: map data domains to visual ranges or geographic coordinates.
- `marks`: scenegraph items with `from`, `transform`, and encode sets (`enter`, `update`, `hover`,
  `exit`). Group marks create nested coordinate systems and scopes.
- `axes`/`legends`/`title`: guides driven by scales and signals.

Minimal structure:

```json
{"$schema":"https://vega.github.io/schema/vega/v6.json","width":400,"height":220,
 "data":[{"name":"table","values":[{"x":"A","y":3}]}],
 "scales":[{"name":"x","type":"band","domain":{"data":"table","field":"x"},"range":"width"}],
 "marks":[{"type":"rect","from":{"data":"table"},"encode":{"enter":{
   "x":{"scale":"x","field":"x"},"width":{"scale":"x","band":1},
   "y":{"scale":"y","field":"y"},"y2":{"scale":"y","value":0}}}}]}
```

The abbreviated example still requires a declared `y` scale to validate and render. That omission is
useful: schema shape and semantic completeness are separate checks.

## Reactive interaction

Event streams describe sources such as `click`, `pointermove`, `window:keydown`, filtered events,
between streams (`[mousedown, window:mouseup] > window:mousemove`), throttling, and consumption.
Signal handlers update state from event values, item data, scale inversion, or expressions. Signals
are the explicit state machine: name state meaningfully and keep interaction state separate from raw
data where possible.

Expressions are not arbitrary JavaScript. Use Vega's expression language and registered functions.
Do not emit unsanitized user text as an expression. Treat expressions and remote URLs as executable or
network-capable inputs requiring a trust policy.

## Data and transforms

Common transforms include aggregate, bin, collect, extent, filter, formula, fold, identifier, join
aggregate, lookup, pivot, stack, window, and geographic transforms. Order matters because each
transform sees the previous output. Keep stable identifiers before transforms when interactions need
to trace marks back to source records.

Use dataflow transforms for interaction-responsive calculations. Prefer upstream Spark/pandas work for
large, static preparation. Recomputing a large aggregate on every pointer event is a design defect.

## Runtime and embedding

Typical JavaScript flow is `vega.parse(spec)`, construct a `vega.View(runtime)`, select a renderer,
initialize a DOM element, and `runAsync()`. A view exposes signal/data changes, listeners, resizing,
state, and image/SVG export. Use async completion before capturing output. Finalize discarded views to
release handlers. Vega-Embed wraps parsing, container setup, themes, loaders, and action menus. [D]

## When Vega is warranted

- Independent event streams or signal state cannot be represented by Vega-Lite parameters.
- Custom dataflow updates, mark lifecycle encode sets, or nested group scopes are required.
- A target contract explicitly accepts Vega and the team owns lower-level maintenance.

Do not drop to Vega merely to alter a tooltip, axis, conditional color, selection, or layered layout;
Vega-Lite usually expresses those with less code.

## Performance and accessibility

Canvas typically favors many marks; SVG favors DOM inspectability and vector output. Neither removes
the need to bound marks. Filter early, avoid repeated expensive transforms, debounce high-frequency
events, and test resize behavior. Supply descriptions, meaningful titles, sufficient contrast, and
alternate tabular/text summaries. Host keyboard and screen-reader behavior requires runtime testing.

## Validation and failure modes

Validate against the exact Vega v6 schema, then instantiate in the exact runtime. Watch for undefined
signals, scope errors, absent scales, invalid field references, transform ordering, NaN/invalid dates,
network loader restrictions, font drift, and Canvas/SVG differences. A valid spec may still be empty
because its dataflow filters all rows.

## Sources and refresh

- https://vega.github.io/vega/docs/
- https://vega.github.io/vega/docs/signals/
- https://vega.github.io/vega/docs/event-streams/
- https://github.com/vega/vega

Refresh for a new adopted Vega minor/major, expression or event-stream changes, or contradictory
runtime evidence.
