# Altair Engineering Reference

**Researched:** 2026-08-14 | **Baseline:** Altair 6.2.2; bundled Vega-Lite schema 6.4.3
**Evidence:** [D] docs; [S] API/source; [U] notebook/browser renderer

Altair is a declarative Python API that builds schema-backed Vega-Lite specifications. Chart objects
are immutable-style compositions: methods return charts rather than mutating a shared drawing canvas.
The durable artifact is the serialized Vega-Lite dictionary, not the Python object. [D]

## Construction crosswalk

```python
import altair as alt

chart = (
    alt.Chart(data)
    .mark_circle(size=70)
    .encode(
        x=alt.X("latency_ms:Q", title="Latency (ms)"),
        y=alt.Y("rows:Q", title="Rows"),
        color=alt.Color("status:N"),
        tooltip=["pipeline:N", "latency_ms:Q", "rows:Q"],
    )
    .properties(title="Pipeline runs", width=520, height=280)
)
spec = chart.to_dict(validate=True)
```

`alt.Chart` maps to a unit spec; `mark_*` to `mark`; `encode` to `encoding`; `transform_*` to ordered
transforms; `properties` to top-level properties. `+`, `|`, and `&` layer, horizontally concatenate,
and vertically concatenate. Prefer explicit `alt.X`, `alt.Color`, and typed shorthand (`field:Q`) at
public boundaries so inferred types cannot drift with data.

## Data transformers

Altair's transformer system decides how Python data becomes specification data. Inline data may be
bounded by a maximum-row safeguard. Disabling the safeguard does not solve scalability; aggregate,
sample under policy, or use an appropriate data transformer. A Spark adapter should select and
materialize a bounded pandas/Arrow result before Altair unless a supported transformer explicitly owns
Spark. Preserve row-count and sampling evidence.

Named datasets and external URLs affect portability. A standalone Power BI/Deneb spec generally needs
host-bound `dataset` data, not a Python-local file or notebook data server.

## Parameters and interaction

Create value parameters with `alt.param`; point/interval selections with selection helpers. Add them
using `.add_params(...)`, then reference them in `transform_filter`, conditional encodings, expressions,
or scale bindings. Selection behavior compiles to Vega signals; inspect emitted JSON when host
portability matters.

```python
brush = alt.selection_interval(encodings=["x"])
detail = chart.add_params(brush).encode(opacity=alt.condition(brush, alt.value(1), alt.value(.2)))
```

Do not equate an Altair selection with Power BI cross-filtering. Deneb host integration must map or
handle selection separately. [U]

## Serialization, validation, and export

- `to_dict(validate=True)`: preferred engine boundary for inspection and transformations.
- `to_json(...)`: text artifact; control formatting for deterministic diffs.
- `save(...)`: output support depends on suffix and installed rendering dependencies.
- `from_dict(...)`: reconstructs schema wrappers, but arbitrary unknown properties can fail validation.

Schema validation verifies the Altair-bundled schema, which can differ from a target host's compiler.
Validate twice when exporting: Altair authoring schema and exact target Vega-Lite schema/runtime.
`vl-convert-python` commonly provides PNG/SVG/PDF and compiled Vega export without Selenium. Fonts and
locale remain environment-dependent.

## Renderers and notebooks

Renderers determine display MIME/HTML, not chart semantics. Notebook frontend support differs across
Jupyter, VS Code, Databricks, and static docs. A successful `to_dict()` does not prove display. For
portable evidence, retain the JSON and an explicit static export or host test.

## Engine design guidance

Use Pydantic or dataclasses for your own intent contract, then compile to Altair or directly to a dict.
Do not expose the entire Altair object graph as the engine's stable public API. Keep an escape hatch for
validated raw Vega-Lite fragments, merge under a documented precedence policy, and return warnings for
features unsupported by Deneb.

## Failure modes

- Ambiguous shorthand or inferred types after an empty/changed DataFrame.
- MaxRows errors treated by disabling the guard rather than bounding data.
- Python objects, NumPy scalars, timestamps, or NaN values that do not serialize portably.
- Reusing field references after transforms changed schema.
- Depending on notebook display state as the artifact.
- Authoring against Altair's schema without checking the destination version.

## Sources and refresh

- https://altair-viz.github.io/user_guide/
- https://altair-viz.github.io/user_guide/data_transformers.html
- https://altair-viz.github.io/user_guide/interactions/parameters.html
- https://altair-viz.github.io/user_guide/saving_charts.html

Refresh when Altair or its bundled schema changes minor/major, transformer defaults change, or retained
renderer evidence contradicts this reference.
