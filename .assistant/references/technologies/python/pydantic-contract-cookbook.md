# Pydantic v2 Contract Cookbook

**Researched:** 2026-08-14 | **Baseline:** Pydantic 2.13.4
**Evidence:** [D] official documentation; [S] public source/API; [U] project-specific performance

Use this guide to design stable application contracts around visualization intent. Pydantic validates
the engine's contract; it does not replace Vega-Lite schema validation or domain reasoning.

## API decision map

| Need | API | Why |
| --- | --- | --- |
| Named object with methods/invariants | `BaseModel` | Clear fields, validation, serialization, schema |
| Payload is one list/map/scalar | `RootModel[T]` | Avoid artificial wrapper field |
| Validate arbitrary type without model class | `TypeAdapter(T)` | Lists, unions, TypedDict, dataclass, aliases |
| Closed variants | discriminated union | Deterministic branch selection and clearer schema/errors |
| Local constraint/metadata | `Annotated[..., Field(...)]` | Reusable type-level contract |
| One-field normalization | `field_validator` | Typed or raw field-specific rule |
| Cross-field invariant | `model_validator` | Whole-object consistency |
| Output transformation | field/model serializer | Explicit serialization contract |
| Derived output | `computed_field` | Declared derived serialization/schema behavior |

## Strict immutable intent model

```python
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

NonEmpty = Annotated[str, Field(min_length=1, pattern=r".*\S.*")]

class FieldRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    name: NonEmpty
    semantic_type: Literal["quantitative", "temporal", "ordinal", "nominal"]

class Encoding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    channel: Literal["x", "y", "color", "size", "tooltip"]
    field: FieldRef
    aggregate: Literal["count", "sum", "mean", "min", "max"] | None = None

    @model_validator(mode="after")
    def aggregate_requires_quantitative_field(self) -> "Encoding":
        if self.aggregate not in {None, "count"} and self.field.semantic_type != "quantitative":
            raise ValueError("numeric aggregate requires a quantitative field")
        return self
```

`frozen=True` blocks model attribute assignment but does not recursively freeze mutable child values.
Use tuples, frozensets, and frozen child models where deep immutability is required.

## Discriminated mark union

```python
from typing import Annotated, Literal
from pydantic import BaseModel, Field

class BarMark(BaseModel):
    kind: Literal["bar"]
    stacked: bool = False

class LineMark(BaseModel):
    kind: Literal["line"]
    interpolate: Literal["linear", "step", "monotone"] = "linear"

Mark = Annotated[BarMark | LineMark, Field(discriminator="kind")]
```

Use a stable discriminator that appears in input and output. Avoid unions whose branches overlap and
depend on smart-union heuristics or ordering for domain meaning.

## Validation entry points

| Input | API | Notes |
| --- | --- | --- |
| Python object/dict | `Model.model_validate(value)` | Honors Python-mode strictness |
| JSON bytes/string | `Model.model_validate_json(value)` | JSON-mode coercion can differ under strict mode |
| String-key mapping | `Model.model_validate_strings(value)` | Useful for form/env-like nested strings |
| Existing model | normal constructor or `model_validate` | Revalidation behavior depends on config |
| Arbitrary typed payload | `adapter.validate_python/json(...)` | Reuse adapter instance |

Strict mode is not one universal no-coercion switch: accepted values can differ between Python and JSON
input. Test both paths if both are public APIs.

## Validator mode decision table

| Mode | Input | Use | Risk |
| --- | --- | --- | --- |
| `after` | validated typed value/model | Most invariants and normalization | Must return value/model |
| `before` | raw input | Controlled preprocessing of external shapes | Handles every possible raw type |
| `plain` | raw input, terminates pipeline | Fully custom field validation | Can bypass normal type validation |
| `wrap` | raw input plus handler | Error translation or conditional delegation | Easy to hide validation behavior |

Validators should be deterministic and side-effect free. Do not perform network calls, file reads,
database queries, or ambient policy lookup during model construction.

## Validation context

Use explicit context for target-version or host policy:

```python
from pydantic import ValidationInfo, field_validator

@field_validator("interaction")
@classmethod
def supported_by_target(cls, value, info: ValidationInfo):
    target = (info.context or {}).get("target")
    if target == "static" and value is not None:
        raise ValueError("interaction is unavailable for static target")
    return value

request = ChartIntent.model_validate(payload, context={"target": "static"})
```

Context is explicit call-time policy; it should not change the stable structural schema silently.

## Aliases and wire compatibility

Choose and document one policy:

- field name: internal Python vocabulary;
- validation alias: accepted input name(s);
- serialization alias: emitted wire name;
- alias generator: systematic casing policy.

Then test `model_validate`, `model_dump(by_alias=True)`, and round-trip behavior. Avoid accepting many
historical aliases indefinitely without deprecation evidence; ambiguous aliases can bind the wrong
field.

## Serialization map

| Need | API |
| --- | --- |
| Python-native dictionary | `model_dump(mode="python")` |
| JSON-compatible values | `model_dump(mode="json")` |
| JSON text | `model_dump_json()` |
| Exclude unset/default/None | corresponding `exclude_*` options |
| Wire aliases | `by_alias=True` |
| Custom field output | `field_serializer` |
| Whole-model output | `model_serializer` |
| Schema for accepted input | `model_json_schema(mode="validation")` |
| Schema for emitted output | `model_json_schema(mode="serialization")` |

Keep validation and serialization symmetric unless the API intentionally distinguishes command input
from report output. Pydantic serializes subclasses according to annotated field types by default,
which helps prevent accidental secret-field leakage.

## JSON Schema boundary

Pydantic JSON Schema describes the engine intent contract. Vega-Lite JSON Schema describes the emitted
visualization. Validate both:

```text
untrusted payload
  → Pydantic validation schema
  → typed intent
  → compiler
  → Vega-Lite document
  → Vega-Lite target schema
```

`$defs` and `$ref` are normal. If a consumer requires inlining, define a recursion and identity policy;
blind recursive expansion can loop or create enormous schemas.

## Structured error response

```python
from pydantic import ValidationError

try:
    intent = ChartIntent.model_validate(payload)
except ValidationError as exc:
    issues = [
        {
            "path": ".".join(str(part) for part in item["loc"]),
            "code": item["type"],
            "message": item["msg"],
        }
        for item in exc.errors(include_input=False, include_url=False)
    ]
```

Exclude or redact input/context when they may contain data, credentials, expressions, or proprietary
field names. A programmer `TypeError` is not automatically a user validation error.

## Versioned contract pattern

```python
class ChartIntentV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    # ...

Intent = Annotated[ChartIntentV1 | ChartIntentV2, Field(discriminator="schema_version")]
intent_adapter = TypeAdapter(Intent)
```

Parse old versions explicitly, migrate to one internal representation, and serialize according to a
declared output version. Do not silently reinterpret old fields under new semantics.

## Raw Vega-Lite escape hatch

If the engine supports raw fragments, isolate and validate them:

```python
class RawVegaLite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    fragment: dict[str, object]
    precedence: Literal["engine", "fragment"] = "engine"
```

Pydantic only validates the wrapper shape unless you add a target-schema validator. The compiler must
apply a documented merge policy, validate the final Vega-Lite document, and report overwritten paths.

## TypeAdapter reuse

```python
Encodings = tuple[Encoding, ...]
encodings_adapter = TypeAdapter(Encodings)

def parse_encodings(value: object) -> tuple[Encoding, ...]:
    return encodings_adapter.validate_python(value)
```

Construct adapters once at module/service scope when safe. Rebuilding adapters or dynamic models in
loops repeats schema construction cost.

## Dataclass and TypedDict boundary

Use Pydantic dataclasses when dataclass interoperability matters but validation is required. Use
`TypedDict` plus `TypeAdapter` for dictionary-shaped boundaries that should remain dictionaries. Use a
plain dataclass internally when values are already trusted and Pydantic behavior adds no value.

## Settings are separate

Pydantic v2 moved settings behavior to `pydantic-settings`. Do not mix environment configuration with
portable visualization intent models. Settings can select defaults or target profiles before model
construction, but persisted intent should remain explicit.

## Migration map from v1

| v1 | v2 |
| --- | --- |
| `parse_obj` | `model_validate` |
| `parse_raw` | `model_validate_json` plus explicit loading policy |
| `dict` | `model_dump` |
| `json` | `model_dump_json` |
| `schema` | `model_json_schema` |
| inner `Config` | `model_config = ConfigDict(...)` |
| `@validator` | `@field_validator` |
| `@root_validator` | `@model_validator` |
| `__root__` | `RootModel` |
| ORM mode | `from_attributes=True` |

`pydantic.v1` is a migration bridge, not the target architecture.

## Contract test matrix

1. Minimal valid payload.
2. Full valid payload and deterministic serialization.
3. Unknown field rejected under `extra="forbid"`.
4. Strict mismatches rejected according to each input mode's conversion table; include JSON cases such
   as dates where strict mode deliberately still accepts a string representation.
5. Every discriminated-union variant and unknown discriminator.
6. Cross-field invariant failure with stable error path/code.
7. Alias input/output and round trip.
8. Validation versus serialization JSON Schema.
9. Redacted structured errors.
10. Old schema version migration and unsupported future version.
11. Frozen model plus nested mutability behavior.
12. Final Vega-Lite schema validation after compilation.

## Common failure modes

- `Optional[T]` permits `None` but does not necessarily provide a default.
- `frozen=True` mistaken for recursive immutability.
- Validators depending on field declaration order through `ValidationInfo.data`.
- `before` validators assuming a mapping and crashing on another raw type.
- Undiscriminated unions accepting an unintended branch.
- Alias policy producing payloads that do not round-trip.
- `model_construct()` used on untrusted input to “improve performance.”
- Pydantic schema mistaken for target Vega-Lite validation.
- Validation errors leaking raw input or proprietary expressions.

## Sources and refresh

- `snapshots/pydantic-2.13.4/docs/concepts/`
- `snapshots/pydantic-2.13.4/docs/api/`
- `snapshots/pydantic-2.13.4/docs/errors/`
- `snapshots/pydantic-2.13.4/docs/migration.md`

Refresh on an adopted Pydantic/pydantic-core minor or major release, schema-output changes, or retained
runtime evidence that contradicts a recipe.
