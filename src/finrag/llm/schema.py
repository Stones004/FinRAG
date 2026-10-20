"""A small JSON-schema checker for the schemas FinRAG sends (no extra dependency).

Supports: type (string, number, integer, boolean, object, array, null), enum, required, properties,
additionalProperties (false only), items.
"""

from __future__ import annotations

from typing import Any

_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
    "null": (type(None),),
}


def schema_errors(data: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Data and schema -> list of problems (empty = valid)."""
    errors: list[str] = []
    wanted = schema.get("type")
    if wanted is not None:
        kinds = wanted if isinstance(wanted, list) else [wanted]
        ok = any(
            isinstance(data, _TYPES[k])
            and not (k in ("number", "integer") and isinstance(data, bool))
            for k in kinds
        )
        if not ok:
            return [f"{path}: expected {wanted}, got {type(data).__name__}"]
    if "enum" in schema and data not in schema["enum"]:
        errors.append(f"{path}: {data!r} is not one of {schema['enum']}")
    if isinstance(data, dict):
        properties = schema.get("properties", {})
        errors += [f"{path}: missing '{k}'" for k in schema.get("required", []) if k not in data]
        if schema.get("additionalProperties") is False:
            errors += [f"{path}: unexpected '{k}'" for k in data if k not in properties]
        for key, sub in properties.items():
            if key in data:
                errors += schema_errors(data[key], sub, f"{path}.{key}")
    if isinstance(data, list) and "items" in schema:
        for i, item in enumerate(data):
            errors += schema_errors(item, schema["items"], f"{path}[{i}]")
    return errors
