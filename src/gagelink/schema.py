"""A validator for the subset of JSON Schema this package declares.

The tool schemas are a contract with a model: they say what an argument may be and what
comes back. Declaring a contract and not checking it is worse than not declaring one,
because both sides then believe something that nothing enforces. This checks it, on the
way in for arguments and in the tests for results.

It is a subset on purpose. What is implemented is what the tool schemas use, which is
`type`, `properties`, `required`, `items`, `enum`, `additionalProperties`, and `$ref`
against `$defs`. Anything else in a schema is ignored rather than rejected, so an
unimplemented keyword weakens the check instead of failing a call that was fine.

A full validator is a dependency, and this package takes dependencies only where the
alternative is reimplementing something subtle. Three hundred lines of draft 2020-12
keyword semantics would be subtle; this is not.
"""

from __future__ import annotations

from typing import Any, Mapping

#: JSON type names against the Python types that satisfy them. `bool` is checked before
#: `int` everywhere below, since in Python a bool is an int and a schema saying integer
#: should not accept true.
_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "null": (type(None),),
}


def _type_matches(value: Any, name: str) -> bool:
    if name == "boolean":
        return isinstance(value, bool)
    if name in {"integer", "number"} and isinstance(value, bool):
        return False
    expected = _TYPES.get(name)
    if expected is None:
        return True  # an unknown type name constrains nothing
    return isinstance(value, expected)


def _describe(value: Any) -> str:
    """The JSON type name for a value, for a message a model can act on."""
    for name in ("null", "boolean", "integer", "number", "string", "array", "object"):
        if _type_matches(value, name) and isinstance(value, _TYPES[name]):
            return name
    return type(value).__name__


def _resolve(schema: Mapping[str, Any], root: Mapping[str, Any]) -> Mapping[str, Any]:
    """Follow a local `$ref` into `$defs`, which is the only form used here."""
    ref = schema.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
        return schema
    target = (root.get("$defs") or {}).get(ref.removeprefix("#/$defs/"))
    return target if isinstance(target, dict) else schema


def validate(
    value: Any,
    schema: Mapping[str, Any],
    root: Mapping[str, Any] | None = None,
    path: str = "",
) -> list[str]:
    """Every way `value` fails `schema`, as sentences naming the place that failed.

    Returns all the failures rather than the first, because a model correcting two
    arguments in one turn is cheaper than correcting one and being told about the next.
    """
    root = root if root is not None else schema
    schema = _resolve(schema, root)
    where = path or "the argument"
    errors: list[str] = []

    declared = schema.get("type")
    if isinstance(declared, str):
        allowed = [declared]
    elif isinstance(declared, list):
        allowed = [t for t in declared if isinstance(t, str)]
    else:
        allowed = []
    if allowed and not any(_type_matches(value, t) for t in allowed):
        wanted = " or ".join(allowed)
        return [f"{where} should be {wanted}, and is {_describe(value)}"]

    choices = schema.get("enum")
    if isinstance(choices, list) and value not in choices:
        listed = ", ".join(repr(c) for c in choices)
        errors.append(f"{where} should be one of {listed}, and is {value!r}")

    if isinstance(value, dict):
        errors.extend(_object_errors(value, schema, root, path))
    elif isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                errors.extend(validate(item, items, root, f"{path}[{index}]" if path else f"item {index}"))

    return errors


def _object_errors(
    value: Mapping[str, Any],
    schema: Mapping[str, Any],
    root: Mapping[str, Any],
    path: str,
) -> list[str]:
    errors: list[str] = []
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}

    required = schema.get("required")
    if isinstance(required, list):
        for name in required:
            if name not in value:
                errors.append(f"{name} is required and was not given")

    if schema.get("additionalProperties") is False:
        unknown = sorted(set(value) - set(properties))
        for name in unknown:
            known = ", ".join(sorted(properties)) or "none"
            errors.append(f"{name} is not an argument of this tool; the arguments are {known}")

    for name, subschema in properties.items():
        if name in value and isinstance(subschema, dict):
            errors.extend(validate(value[name], subschema, root, f"{path}.{name}" if path else name))

    return errors
