# Vega-Lite Engineering Reference

**Researched:** 2026-08-14 | **Baseline:** 6.4.3  
**Evidence:** [D] docs; [S] v6 JSON schema; [U] target renderer and host

Vega-Lite is a high-level grammar that compiles to Vega. A unit specification combines data, mark,
encoding, transforms, parameters, projection, and configuration. Composition operators build layers,
facets, concatenations, and repeated views. The compiler chooses many low-level scales, guides,
signals, and marks. [D]

## Grammar and encodings

The required reasoning sequence is: analytical question → semantic fields → transformations → mark →
channels → interaction → composition. Do not start from visual decoration.

Channels include position (`x`, `y`, `x2`, `y2`), color, fill, stroke, opacity, size, shape, text,
tooltip, order, detail, row, column, facet, angle, radius, latitude, and longitude. Field definitions
declare `field`, semantic `type` (`quantitative`, `temporal`, `ordinal`, `nominal`, `geojson`), and
optional aggregate, bin, time unit, scale, axis, legend, sort, stack, title, and format. Correct type is
not cosmetic: it controls scales, aggregation, sorting, and guides.

```json
{"$schema":"https://vega.github.io/schema/vega-lite/v6.json","data":{"values":[{"day":"2026-08-01","n":4}]},
 "mark":{"type":"line","point":true},"encoding":{
   "x":{"field":"day","type":"temporal","title":"Day"},
   "y":{"field":"n","type":"quantitative","title":"Runs"},
   "tooltip":[{"field":"day","type":"temporal"},{"field":"n","type":"quantitative"}]}}
```

## Transform discipline

Transforms execute in array order. Important families are aggregate/joinaggregate, bin, calculate,
filter, flatten, fold, impute, lookup, pivot, density, quantile, regression, stack, timeUnit, and
window. Use Vega expressions in calculate/filter. Keep transformations in the spec when they must
react to parameters; move large static work upstream. Record whether aggregation occurred upstream or
inside Vega-Lite to avoid double aggregation.

## Composition

- `layer`: same plot region; resolve scales deliberately when units differ.
- `facet`/encoding `row` and `column`: small multiples over field values.
- `hconcat`, `vconcat`, `concat`: independent views arranged together.
- `repeat`: generate related views from repeated field definitions.

Shared scales improve comparison but can hide magnitude differences. Independent scales improve local
resolution but weaken cross-panel comparison. Make the choice explicit with `resolve`.

## Parameters and selection

Parameters hold literal/expression values or selection state. Point selections identify tuples;
interval selections identify ranges. Bind parameters to inputs or scales, define event behavior, and
consume them in filters, conditions, domains, or calculations. A selection alone does nothing visible:
it must drive a condition or transform.

Cross-filter and highlighting semantics differ. Filtering removes non-selected rows from downstream
dataflow; conditional opacity/color preserves context. Empty-selection behavior must be chosen and
tested. Host-provided cross-filtering is separate from internal Vega-Lite selection. [U]

## Configuration and portability

Prefer local mark/encoding properties for reusable components; use `config` for coherent defaults.
Themes and host configuration can alter fonts, colors, dimensions, and view strokes. Avoid assumptions
about external CSS. `autosize`, explicit width/height, and container sizing need host qualification.

Compile against the target's version. A spec valid under the latest online editor may fail in Deneb if
Deneb embeds an earlier compiler. Use only documented v6 properties and preserve `$schema`.

## Validation and accessibility

JSON Schema catches shape, enum, and union errors, not misleading aggregation or unreadable design.
Also compile and render with representative data. Add title/description, plain-language axes, useful
tooltips, sufficient contrast, restrained mark count, and a nonvisual summary. Avoid color as the only
carrier of status. Keyboard and screen-reader claims require host evidence. [U]

## Frequent failures

- Wrong semantic type or implicit aggregation.
- Referencing a field removed by aggregate/fold/pivot.
- Layered units with incompatible scales but no `resolve` choice.
- A selection parameter that never drives a condition/filter.
- Invalid dates, nulls, NaN, or empty domains.
- Thousands of marks and labels passed from unbounded data.
- Using Vega features directly inside a Vega-Lite schema.

## Sources and refresh

- https://vega.github.io/vega-lite/docs/
- https://vega.github.io/vega-lite/docs/encoding.html
- https://vega.github.io/vega-lite/docs/parameter.html
- https://vega.github.io/schema/vega-lite/v6.json

Refresh for an adopted minor/major, schema changes, selection-model changes, or target compiler drift.
