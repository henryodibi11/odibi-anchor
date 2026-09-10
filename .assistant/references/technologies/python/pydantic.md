# Pydantic v2 Engineering Reference

**Researched:** 2026-08-14 | **Baseline:** 2.13.4  
**Evidence:** [D] official docs; [S] public source/API; [U] project-specific performance

Pydantic v2 validates Python values against type annotations using `pydantic-core`, produces typed
models, serializes them, and generates JSON Schema. It is a boundary-validation tool, not a substitute
for domain design. Validate untrusted/external input; keep trusted internal objects simple when model
construction cost or assignment semantics do not add value. [D]

## Model contract

```python
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

class Encoding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    channel: Literal["x", "y", "color", "tooltip"]
    field: Annotated[str, Field(min_length=1)]
    semantic_type: Literal["quantitative", "temporal", "ordinal", "nominal"]

    @field_validator("field")
    @classmethod
    def normalized_field(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("field must not have surrounding whitespace")
        return value
```

`extra="forbid"` protects closed public contracts. `strict=True` prevents convenient but surprising
coercions; note that strict behavior can differ between Python and JSON input. `frozen=True` prevents
normal attribute assignment but does not recursively freeze mutable children. Use immutable child
types when deep immutability matters.

## Validation lifecycle

- `Field` and `Annotated` express local constraints and schema metadata.
- `field_validator` validates one or more fields in `before`, `after`, `plain`, or `wrap` mode.
- `model_validator` enforces cross-field invariants before/after the model.
- `ValidationInfo` exposes already validated data, context, field name, and mode; field order matters
  when reading sibling values from a field validator.

Prefer after validators for typed values. Use before/wrap only when normalization or controlled error
handling genuinely requires raw input. Validators must return the validated value/model. Avoid I/O,
global state, and hidden defaults inside validators so repeated validation is deterministic.

## Unions and polymorphism

Use discriminated unions for stable variant contracts:

```python
class Bar(BaseModel):
    kind: Literal["bar"]
    stacked: bool = False

class Line(BaseModel):
    kind: Literal["line"]
    interpolate: Literal["linear", "step"] = "linear"

Mark = Annotated[Bar | Line, Field(discriminator="kind")]
```

They validate efficiently, produce clearer errors, and emit usable `oneOf`/discriminator schemas.
Undiscriminated unions can choose branches unexpectedly when inputs overlap; branch order and smart
union behavior should not carry domain meaning.

## Serialization

Use `model_dump(mode="python"|"json", exclude_none=..., by_alias=...)` and `model_dump_json()`.
`field_serializer`, `model_serializer`, and computed fields customize output. Keep validation and
serialization symmetric unless the contract explicitly distinguishes input and output schemas.
Aliases need a documented policy: validation aliases, serialization aliases, and field names can
otherwise create non-round-tripping payloads.

Pydantic v2 serializes subclasses according to the annotated field type by default, reducing accidental
secret leakage. Use duck-typed serialization only deliberately. Never place credentials in model repr,
errors, generated examples, or schemas.

## TypeAdapter and non-model types

`TypeAdapter(T)` validates, serializes, and generates schemas for typed dictionaries, dataclasses,
lists, unions, and aliases without creating a wrapper model. Construct adapters once and reuse them;
schema construction has a cost. `RootModel[T]` is appropriate when the public payload itself is a
single list/map/value with model methods.

## JSON Schema

`model_json_schema()` and `TypeAdapter.json_schema()` emit JSON Schema. Input (`validation`) and output
(`serialization`) modes can differ. `$defs` and `$ref` are normal; consumers that demand fully inlined
schemas need a separate transformation and recursion policy. Pydantic JSON Schema is not automatically
the same as Vega-Lite JSON Schema or every OpenAPI consumer's accepted dialect.

For a visualization engine, Pydantic should validate the engine's intent/intermediate model. The final
Vega-Lite document should still be validated against the official Vega-Lite schema. Do not attempt to
re-model the entire upstream grammar unless owning that maintenance burden is intentional.

## Errors and context

Catch `ValidationError` at system boundaries and preserve structured `errors()` entries (`loc`, `type`,
message, input/context under a redaction policy). Convert them to actionable field paths. Do not catch
programmer `TypeError` as if it were user validation. Supply validation context explicitly when policy
depends on target host/version; avoid ambient globals.

## Migration from v1

Key changes include `model_validate`/`model_dump` naming, `ConfigDict`, validator/serializer APIs,
`RootModel`, changed equality and subclass serialization, and `pydantic-settings` separation. The
`pydantic.v1` compatibility namespace is a migration bridge, not the target design. Review optional vs
nullable semantics: `Optional[T]` permits `None` but does not necessarily imply a default.

## Performance and failure modes

Reuse `TypeAdapter`; avoid rebuilding dynamic models in loops; validate once at ingress; benchmark the
actual payload. Avoid `model_construct()` unless data is already trusted and the measured benefit
matters—it bypasses validation. Frequent defects are permissive extras, coercion hiding bad values,
mutable nested state in frozen models, validators depending on field order, unstable aliases, and JSON
Schema consumers assuming a different dialect.

## Sources and refresh

- https://docs.pydantic.dev/latest/concepts/models/
- https://docs.pydantic.dev/latest/concepts/validators/
- https://docs.pydantic.dev/latest/concepts/serialization/
- https://docs.pydantic.dev/latest/concepts/json_schema/
- https://docs.pydantic.dev/latest/migration/

Refresh on adopted minor/major changes, `pydantic-core` semantic changes, schema-output changes, or
project benchmarks that contradict this guidance.
