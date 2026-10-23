"""A small, strict JSON Schema subset for tool arguments.

Supported keywords: type (object, string, integer, number, boolean, array), enum,
const, minimum, maximum, minLength, maxLength, pattern (full match), items, minItems,
maxItems, properties, required, additionalProperties (only false is meaningful),
default (applied by `with_defaults`, not by `validate`). Booleans are never integers.
"""
from __future__ import annotations

import copy
import re

from ..core import ValidationError


class SchemaError(ValidationError):
    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path
        self.reason = message


_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    and v == v and v not in (float("inf"), float("-inf")),
}
_PATTERNS: dict[str, re.Pattern] = {}


def _show(v) -> str:
    s = repr(v)
    return s if len(s) <= 40 else s[:37] + "..."


def validate(schema: dict, value, path: str = "arguments") -> None:
    t = schema.get("type")
    if t is not None and not _TYPES[t](value):
        raise SchemaError(path, f"expected {t}")
    if "const" in schema and value != schema["const"]:
        raise SchemaError(path, f"must be {schema['const']!r}")
    if "enum" in schema and (isinstance(value, bool) and not any(
            isinstance(e, bool) for e in schema["enum"]) or value not in schema["enum"]):
        opts = ", ".join(str(e) for e in schema["enum"])
        raise SchemaError(path, f"{_show(value)} is not one of: {opts}")
    if t in ("integer", "number"):
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaError(path, f"{value} is below the minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaError(path, f"{value} is above the maximum {schema['maximum']}")
    elif t == "string":
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise SchemaError(path, f"shorter than {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise SchemaError(path, f"longer than {schema['maxLength']} characters")
        if "pattern" in schema:
            rx = _PATTERNS.get(schema["pattern"])
            if rx is None:
                rx = _PATTERNS[schema["pattern"]] = re.compile(schema["pattern"])
            if not rx.fullmatch(value):
                raise SchemaError(path, f"{_show(value)} does not match {schema['pattern']}")
    elif t == "array":
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise SchemaError(path, f"needs at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise SchemaError(path, f"allows at most {schema['maxItems']} items")
        if "items" in schema:
            for i, item in enumerate(value):
                validate(schema["items"], item, f"{path}[{i}]")
    elif t == "object":
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value:
                raise SchemaError(f"{path}.{k}", "is required")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(props))
            if extra:
                raise SchemaError(path, f"unknown argument {_show(extra[0])} (allowed: "
                                        f"{', '.join(props) or 'none'})")
        for k, v in value.items():
            if k in props:
                validate(props[k], v, f"{path}.{k}")


def with_defaults(schema: dict, value: dict) -> dict:
    """A copy of an (already validated) object with top-level defaults filled in."""
    out = copy.deepcopy(value)
    for k, sub in schema.get("properties", {}).items():
        if k not in out and "default" in sub:
            out[k] = copy.deepcopy(sub["default"])
    return out
