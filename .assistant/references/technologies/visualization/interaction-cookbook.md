# Declarative Interaction Cookbook

**Researched:** 2026-08-14 | **Baselines:** Vega 6.3.1, Vega-Lite 6.4.3, Altair 6.2.2
**Evidence:** [D] documentation; [S] schema/source; [U] browser and Power BI host integration

Interactions are data contracts, not decorative callbacks. Define the selected values, empty-state
semantics, affected views, event source, and host effect before choosing an API.

## Interaction design checklist

For every interaction specify:

1. **State:** scalar value, tuple set, or continuous interval.
2. **Projection:** fields or positional encodings that identify selected data.
3. **Event:** click, pointerover, drag, wheel, keyboard-modified event, or widget change.
4. **Empty behavior:** all values or no values satisfy an empty selection.
5. **Effect:** condition, filter, scale domain, annotation, or external host action.
6. **Resolution:** one global state or union/intersection across repeated views.
7. **Clearing:** default double-click, explicit event, or disabled.
8. **Accessibility:** keyboard/non-pointer equivalent and visible state.
9. **Portability:** compiled browser behavior versus host-mediated behavior.

## Recipe 1: Hover highlight with nearest point

Use a point selection triggered by pointer movement, projected over the x field. `nearest=True` uses a
Voronoi-assisted nearest point for discrete marks. It is not directly supported on multi-element line
or area marks; layer transparent points over the line.

```python
hover = alt.selection_point(
    name="hover", fields=["timestamp"], nearest=True,
    on="pointerover", empty=False, clear="pointerout",
)

points = base.mark_point(opacity=0).add_params(hover)
rule = alt.Chart(data).mark_rule().encode(
    x="timestamp:T",
    opacity=alt.condition(hover, alt.value(1), alt.value(0)),
)
labels = base.mark_text(align="left", dx=5).encode(
    text=alt.condition(hover, "value:Q", alt.value("")),
)
chart = base + points + rule + labels
```

Use `empty=False` when an empty hover should not highlight everything.

## Recipe 2: Click selection and visual condition

```python
selected = alt.selection_point(name="selected", fields=["pipeline"], toggle=True)

chart = base.add_params(selected).encode(
    opacity=alt.condition(selected, alt.value(1), alt.value(0.2)),
    strokeWidth=alt.condition(selected, alt.value(2), alt.value(0)),
)
```

This preserves all rows and changes appearance. Use a filter only when non-selected rows should be
removed from downstream transforms or views.

## Recipe 3: Brush linked overview and detail

```python
brush = alt.selection_interval(name="brush", encodings=["x"])

overview = base.add_params(brush).encode(
    opacity=alt.condition(brush, alt.value(1), alt.value(0.15)),
)
detail = base.transform_filter(brush).properties(height=220)
chart = overview & detail
```

An interval projected over binned or time-unit encodings remains continuous. If conditional encoding
does not align with discretized bars, layer a selected mark rather than relying on one conditional
channel.

## Recipe 4: Pan and zoom by binding selection to scales

```python
zoom = alt.selection_interval(name="zoom", bind="scales", encodings=["x"])
chart = base.add_params(zoom)
```

Scale binding changes domains rather than filtering rows. Clearing a scale-bound selection resets the
initial domains. Disable or customize `translate` and `zoom` if gestures conflict with host scrolling.

## Recipe 5: Widget-driven threshold

```python
threshold = alt.param(
    name="threshold",
    value=100,
    bind=alt.binding_range(min=0, max=500, step=10, name="Threshold "),
)

chart = base.add_params(threshold).encode(
    color=alt.condition(
        alt.datum.latency_ms > threshold,
        alt.value("#d62728"),
        alt.value("#4c78a8"),
    )
)
```

Value parameters are appropriate when the state is not a selection query. Bound input widgets require
a host that renders Vega input bindings; qualify this separately in Deneb/Power BI.

## Recipe 6: Legend-bound filtering

```python
choose_status = alt.selection_point(
    name="status_choice", fields=["status"], bind="legend", empty=True,
)

chart = base.add_params(choose_status).encode(
    opacity=alt.condition(choose_status, alt.value(1), alt.value(0.12)),
)
```

Legend binding works for point selections projected over one field. Decide whether empty means “show
all” (`empty=True`) or “show none” (`empty=False`).

## Recipe 7: Multi-view selection resolution

For selections inside facets or repeats:

- `global`: one selection; starting a new brush clears the old one.
- `union`: each cell has state; a datum satisfies any cell's selection.
- `intersect`: a datum must satisfy every cell's selection.

Avoid accidental intersect behavior with empty cells. Test clearing and newly created facets as data
changes.

## Recipe 8: Custom events and modifier keys

Vega event-stream selectors can control `on`, `clear`, interval `translate`, and interval `zoom`:

```json
{
  "name": "brush",
  "select": {
    "type": "interval",
    "translate": "[pointerdown[event.shiftKey], window:pointerup] > window:pointermove!",
    "zoom": "wheel![event.shiftKey]",
    "clear": "dblclick"
  }
}
```

Event filters are expressions evaluated by the Vega runtime. Treat user-authored expressions as code
within the visualization sandbox and do not splice untrusted text into them.

## Recipe 9: Direct Vega signal integration

Use direct Vega when an application must read/write interaction state:

```javascript
const runtime = vega.parse(spec);
const view = await new vega.View(runtime, {
  container: "#chart",
  renderer: "svg",
  hover: true
}).runAsync();

const handler = (name, value) => publishSelection(value);
view.addSignalListener("selected_pipeline", handler);

await view.signal("threshold", 250).runAsync();

// On component teardown:
view.removeSignalListener("selected_pipeline", handler);
view.finalize();
```

Do not call synchronous `run()` recursively inside a signal listener. A listener runs during dataflow
evaluation, before every other signal/transform is guaranteed current.

## Recipe 10: Persist and restore interaction state

`view.getState()` returns signal state and modified datasets; `view.setState(state)` restores them.
This is a Vega runtime state contract, not a portable Vega-Lite artifact. Version and validate saved
state with the exact specification revision because renamed signals/datasets can invalidate it.

## Filter versus condition versus domain

| Effect | Use | Consequence |
| --- | --- | --- |
| Dim non-selected marks | conditional opacity/color | All data remains in transforms and scales |
| Remove rows from linked view | selection/parameter filter | Downstream aggregates and domains change |
| Pan/zoom axis | scale binding or parameterized domain | Rows remain, visible domain changes |
| Show selected details | filtered child view | Child computes only selected rows |
| Annotate selected value | conditional text/rule | Preserve base view while exposing state |

## Empty-state traps

- The default empty selection may make every datum satisfy a predicate.
- A condition can therefore display its “selected” branch before any interaction.
- A filtered view can initially show all rows when the desired behavior was no rows.
- Scale-bound clearing resets domains rather than merely removing a brush rectangle.
- In composed views, empty local selections interact with union/intersect resolution.

Write an explicit assertion for initial, selected, multi-selected, and cleared states.

## Power BI/Deneb boundary

A Vega-Lite selection controls the visualization's internal dataflow. Power BI cross-selection,
cross-filtering, context menus, report-page tooltips, and highlight payloads are host APIs mediated by
Deneb. Do not infer those effects from a working browser selection. The Deneb source declares support
for selection, highlight, multi-visual selection, context-menu settings, and enhanced tooltips, but the
exact spec convention and report behavior require host qualification. [S][U]

## Interaction verification matrix

| State | Minimum check |
| --- | --- |
| Initial | no accidental all-selected/none-selected behavior |
| Hover/focus | visible response and no stuck state after pointerout/blur |
| Single select | correct tuple and linked-view effect |
| Multi-select | toggle modifier and deterministic membership |
| Clear | returns to documented empty state |
| Facet/repeat | intended global/union/intersect resolution |
| Data refresh | stale selected values handled safely |
| Keyboard/accessibility | equivalent operation or explicit limitation |
| Static export | useful non-interactive representation remains |
| Deneb host | report effects verified in Desktop/Service as applicable |

## Sources and refresh

- `snapshots/vega-lite-6.4.3/site/docs/parameter/`
- `snapshots/altair-6.2.2/doc/user_guide/interactions/`
- `snapshots/vega-6.3.1/docs/docs/api/view.md`

Refresh when selection schema, event-stream behavior, Altair parameter APIs, or target-host interaction
contracts change.
