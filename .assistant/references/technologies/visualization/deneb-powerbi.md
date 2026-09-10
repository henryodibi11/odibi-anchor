# Deneb and Power BI Engineering Reference

**Researched:** 2026-08-14 | **Baselines:** Deneb 1.9.1; Vega 6.2.0; Vega-Lite 6.4.1; Visuals API 5.11.0  
**Evidence:** [D] docs; [S] public package/source; [U] Power BI Desktop/Service host qualification

Deneb is a Power BI custom visual that renders Vega or Vega-Lite specifications. Power BI supplies a
dataset based on fields assigned to the visual. Deneb exposes that data under the named `dataset`
source and adds host integration for selection, highlighting, tooltips, formatting, themes, and visual
lifecycle. It is not a generic browser page and does not provide arbitrary Python execution. [D]

## Data contract

Users add measures and columns to Deneb's Values role. The emitted rows and field names depend on the
Power BI query, aggregation, categorical grouping, measures, and host-generated metadata. Start a spec
from `{"data":{"name":"dataset"}}`; do not inline production data. Keep generated specs aligned with
the exact field display names or use a deliberate mapping layer.

Power BI may aggregate before Deneb receives rows. Vega-Lite can aggregate again. Document which layer
owns grain and aggregation to prevent double counting. Blank values, formatted measures, highlight
fields, and identities need representative host inspection. [U]

## Templates and portability

Deneb templates package a specification plus metadata that helps map placeholder fields. A portable
engine should emit:

1. valid Vega-Lite/Vega JSON using `dataset`;
2. a field-role manifest with required type and aggregation;
3. Deneb import/paste instructions and version baseline;
4. optional template metadata only through a versioned adapter.

Keep the core spec usable outside Power BI by allowing the data source to be replaced for preview.
Do not pollute the core grammar with engine teaching text.

## Interaction layers

Internal Vega/Vega-Lite interactions include hover, parameters, brushes, filters, and signal-driven
state. Power BI interactions include selecting identities, cross-filtering/cross-highlighting other
visuals, report tooltips, context menus, drill behavior, bookmarks, and theme/high-contrast behavior.
These layers overlap but are not identical. A brush that filters marks inside Deneb does not prove it
filters another Power BI visual.

Deneb provides documented selection interactivity, but exact behavior depends on dataset identity and
the visual host. Qualify click/ctrl-click, multi-select, empty selection, highlights, cross-filter
direction, tooltip fields, and clearing selection in Desktop and Service. Label all unexecuted cases
[U], never “supported” solely because a Vega event works in the online editor.

## Version and security boundaries

Generate only syntax supported by Deneb's embedded Vega/Vega-Lite versions. Online editors may be
newer. Remote URL loading, custom expressions, external images, fonts, and browser APIs may be limited
by Power BI sandboxing, tenant policy, certification constraints, or service networking. Treat remote
resources as optional and security-reviewed; prefer inline static assets only when size and licensing
permit.

Certified-visual status and organizational approval are time-sensitive. Verify current marketplace,
tenant, export, and deployment policies rather than encoding a permanent compliance claim. [U]

## Qualification matrix

For each generated recipe retain:

- exact Deneb, embedded Vega/Vega-Lite, Power BI Desktop/Service and Visuals API versions;
- dataset fields, types, aggregation, representative row count, blanks and high cardinality;
- schema validation and Deneb parse result;
- default, filtered, highlighted, empty, and resize states;
- click/multi-select/cross-filter/tooltip outcomes;
- theme, high contrast, keyboard/screen reader, export and mobile behavior where required;
- unsupported features and a fallback visual or static export.

## Performance guidance

Reduce rows in the Power BI model/query where possible. Avoid one SVG text/shape per high-cardinality
row, repeated lookups, and expensive transforms on every signal update. Prefer Canvas for many marks
when supported, but test export/accessibility tradeoffs. Report payload and render timings rather than
assuming a valid spec is operationally usable.

## Frequent failures

- `data.values` works in preview but ignores Power BI `dataset`.
- Field names differ after Power BI aggregation or display-name edits.
- A newer Vega-Lite property is unsupported by Deneb's embedded compiler.
- Internal selection is mistaken for report cross-filtering.
- Remote fonts/images work locally but fail in Service or export.
- A template imports but maps semantically wrong fields.

## Sources and refresh

- https://deneb.guide/
- https://deneb.guide/docs/1.9/
- https://learn.microsoft.com/en-us/power-bi/developer/visuals/

Refresh on Deneb, embedded grammar, or Visuals API change and whenever actual Desktop/Service evidence
is retained. Host verification should upgrade only the exact tested behaviors.
