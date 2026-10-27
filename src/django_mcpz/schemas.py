"""
JSON Schema generation for tool parameters and results, from msgspec-supported
types and the annotations of tool functions.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

import msgspec
import msgspec.json
from django.core.exceptions import ImproperlyConfigured


def is_struct_type(obj: Any) -> bool:
    try:
        return issubclass(obj, msgspec.Struct)
    except TypeError:
        return False


def inspect_types(func: Callable[..., Any], tool_name: str) -> tuple[Any, Any]:
    """
    Read a tool function's input type from the annotation of its params
    argument, the second parameter, plus its return annotation. A function
    taking only the request has no input type.
    """
    parameters = list(inspect.signature(func).parameters.values())
    if len(parameters) not in (1, 2):
        raise ImproperlyConfigured(
            f"Tool function for {tool_name!r} must accept the request,"
            " optionally followed by one params argument."
        )
    try:
        # On Python 3.14+, this resolves annotations with annotationlib.
        annotations = inspect.get_annotations(func, eval_str=True)
    except NameError as exc:
        raise ImproperlyConfigured(
            f"Could not resolve the type annotations of the tool function for"
            f" {tool_name!r} ({exc}). Ensure the types are resolvable at"
            " module level."
        ) from exc
    input_type = None
    if len(parameters) == 2:
        parameter = parameters[1]
        if parameter.kind not in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            raise ImproperlyConfigured(
                f"Tool function for {tool_name!r} must accept its params"
                f" argument positionally, unlike {parameter.name!r}."
            )
        if parameter.name not in annotations:
            raise ImproperlyConfigured(
                f"Tool function for {tool_name!r} params argument"
                f" {parameter.name!r} requires a type annotation, or pass"
                " input_schema."
            )
        input_type = annotations[parameter.name]
    return input_type, annotations.get("return")


def type_schema(type_: Any) -> dict[str, Any]:
    """
    Generate a JSON Schema for a msgspec-supported type, inlining the
    top-level $ref so that the properties sit at the schema root.
    """
    schema = msgspec.json.schema(type_)
    ref = schema.get("$ref", "")
    defs = schema.get("$defs")
    if not (
        isinstance(ref, str) and ref.startswith("#/$defs/") and isinstance(defs, dict)
    ):
        return schema
    name = ref.removeprefix("#/$defs/")
    # A self-referencing (recursive) definition cannot be inlined.
    if f'"#/$defs/{name}"'.encode() in msgspec.json.encode(defs):
        return schema
    root = dict(defs[name])
    remaining = {key: value for key, value in defs.items() if key != name}
    if remaining:
        root["$defs"] = remaining
    return root


def reject_header_annotations(schema: object) -> None:
    """
    Raise ImproperlyConfigured if a schema uses the x-mcp-header extension.

    Declaring it would oblige this server to validate the mirrored
    Mcp-Param-* headers on every call, which django-mcpz does not implement.
    """
    if isinstance(schema, dict):
        if "x-mcp-header" in schema:
            raise ImproperlyConfigured(
                "The x-mcp-header schema extension is not supported by"
                " django-mcpz. Remove it from the input schema."
            )
        for value in schema.values():
            reject_header_annotations(value)
    elif isinstance(schema, list):
        for item in schema:
            reject_header_annotations(item)


_ELICITATION_PRIMITIVE_TYPES = frozenset({"boolean", "integer", "number", "string"})


def elicitation_schema(type_: type) -> dict[str, Any]:
    """
    Generate the requestedSchema for an elicitation from a msgspec-supported
    type, restricted to the flat object of primitive properties that form mode
    allows, so that every client can render it as a form.

    Enum and Literal fields are resolved to inline string enums, since msgspec
    writes them as a $defs reference or a bare enum, where the specification's
    enum schema carries its type.
    """
    schema = type_schema(type_)
    properties = schema.get("properties")
    if schema.get("type") != "object" or not isinstance(properties, dict):
        raise ImproperlyConfigured(
            "An elicitation schema must be an object with properties, such as"
            f" a msgspec.Struct subclass, not {type_!r}."
        )
    defs = schema.get("$defs", {})
    requested: dict[str, Any] = {
        "type": "object",
        "properties": {
            name: _primitive_schema(property_, defs, name)
            for name, property_ in properties.items()
        },
    }
    required = schema.get("required")
    if required:
        requested["required"] = list(required)
    return requested


def _primitive_schema(
    schema: dict[str, Any], defs: dict[str, Any], field: str
) -> dict[str, Any]:
    """
    Check one property of an elicitation schema, returning it in the form the
    specification defines for that kind of field.
    """
    schema = _inlined(schema, defs)
    if "enum" in schema:
        return _string_enum(schema, field)
    type_ = schema.get("type")
    if type_ in _ELICITATION_PRIMITIVE_TYPES:
        return schema
    if type_ == "array":
        items = _inlined(schema.get("items", {}), defs)
        if "enum" not in items:
            raise ImproperlyConfigured(
                f"Elicitation field {field!r} is a list of values that are not"
                " a fixed set of options. Form mode allows a list only as a"
                " multiple choice, so annotate its items with a Literal or an"
                " Enum."
            )
        return {**schema, "items": _string_enum(items, field)}
    if "anyOf" in schema:
        raise ImproperlyConfigured(
            f"Elicitation field {field!r} is a union of types, such as an"
            " optional field typed with None. Form mode allows one primitive"
            " type per field, so give the field a default instead."
        )
    raise ImproperlyConfigured(
        f"Elicitation field {field!r} is not one of the types form mode"
        " allows: a string, number, integer, boolean, a Literal or Enum of"
        " strings, or a list of a Literal or Enum of strings, as a multiple"
        " choice."
    )


def _inlined(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """
    Replace a $ref with the definition it names, keeping the sibling keys that
    msgspec writes alongside it, such as a field's default. The definition's
    title and description describe the type, not the field, so are dropped.

    Every reference resolves, since msgspec writes the definitions of the
    same schema it generated the reference in.
    """
    ref = schema.get("$ref")
    if ref is None:
        return schema
    definition = defs[str(ref).removeprefix("#/$defs/")]
    options = {
        key: value
        for key, value in definition.items()
        if key not in ("title", "description")
    }
    siblings = {key: value for key, value in schema.items() if key != "$ref"}
    return {**options, **siblings}


def _string_enum(schema: dict[str, Any], field: str) -> dict[str, Any]:
    """
    Declare an enum schema's type, which the specification requires and
    msgspec leaves out, rejecting options that are not strings.
    """
    if not all(isinstance(value, str) for value in schema["enum"]):
        raise ImproperlyConfigured(
            f"Elicitation field {field!r} offers options that are not all"
            " strings. Form mode allows a choice between strings only, so use"
            " a str-valued Enum or a Literal of strings."
        )
    return {**schema, "type": "string"}
