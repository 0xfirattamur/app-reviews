"""One converter from model values to JSON-safe values, shared by every ``to_dict``."""

from __future__ import annotations

from dataclasses import fields
from datetime import datetime
from enum import Enum
from typing import Any


def to_json_value(value: Any) -> Any:
    """Return ``value`` as something ``json.dumps`` accepts.

    Datetimes become ISO 8601 strings: ``str()`` would put a space where ISO
    wants a ``T``, which strict parsers reject. Nested models become their own
    ``to_dict()``, enums their value, and containers are converted element by
    element. Anything else, ``None`` included, is already JSON-safe and passes
    through unchanged.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: to_json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [to_json_value(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    return to_dict() if to_dict is not None else value


def fields_to_dict(
    instance: Any,
    *,
    exclude: tuple[str, ...] = (),
    by_reference: tuple[str, ...] = (),
    key_order: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Convert every dataclass field of ``instance`` except ``exclude``.

    Keys follow field declaration order unless ``key_order`` names every
    emitted key in the order consumers already rely on. Fields in
    ``by_reference`` are passed through uncopied, for payloads too large to
    convert. A field handled differently (one taking arguments) is excluded and
    added by the caller.
    """
    converted_fields = {
        model_field.name: _convert_field(instance, model_field.name, by_reference)
        for model_field in fields(instance)
        if model_field.name not in exclude
    }
    if not key_order:
        return converted_fields
    return {name: converted_fields[name] for name in key_order}


def _convert_field(instance: Any, name: str, by_reference: tuple[str, ...]) -> Any:
    field_value = getattr(instance, name)
    return field_value if name in by_reference else to_json_value(field_value)
