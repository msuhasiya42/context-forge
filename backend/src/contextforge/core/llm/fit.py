"""Fit a model's structured answer to its schema before validation.

Models slip on strict schemas in ways that lose no meaning: a field the schema does not have
(Gemini, live 13 Nov 2026: ``conclusions[0].staged``, ``sessionId``, ``nativeElement``), a list
longer than its cap (11 facts for 6), a reason that thinks out loud past its length limit. Any
one of these used to fail the whole answer. ``fit_to_schema`` drops unknown fields, keeps the
first items of an over-long list, and cuts an over-long string, guided by the schema itself.
Everything else (a missing required field, a wrong type, a bad enum value) is left for
validation to reject.
"""

import types
from typing import Any, Union, get_args, get_origin

from annotated_types import MaxLen
from pydantic import BaseModel
from pydantic.fields import FieldInfo


def fit_to_schema(schema: type[BaseModel], answer: Any) -> tuple[Any, list[str]]:
    """``answer`` fitted to ``schema``, and a note per change, naming its path."""
    fixes: list[str] = []
    return _fit_model(schema, answer, "", fixes), fixes


def _fit_model(schema: type[BaseModel], value: Any, path: str, fixes: list[str]) -> Any:
    if not isinstance(value, dict):
        return value
    by_key: dict[str, FieldInfo] = {}
    for name, info in schema.model_fields.items():
        by_key[name] = info
        if info.alias:
            by_key[info.alias] = info
    forbids_extra = schema.model_config.get("extra") == "forbid"
    fitted: dict[str, Any] = {}
    for key, item in value.items():
        field = by_key.get(key)
        if field is None:
            if forbids_extra:
                fixes.append(f"dropped unknown field {path}{key}")
                continue
            fitted[key] = item
            continue
        fitted[key] = _fit_value(field.annotation, _max_length(field), item, f"{path}{key}", fixes)
    return fitted


def _fit_value(
    annotation: Any, max_length: int | None, value: Any, path: str, fixes: list[str]
) -> Any:
    if isinstance(value, str) and max_length is not None and len(value) > max_length:
        fixes.append(f"cut {path} to {max_length} characters")
        return value[: max_length - 1].rstrip() + "…"
    if isinstance(value, list):
        if max_length is not None and len(value) > max_length:
            fixes.append(f"kept the first {max_length} of {len(value)} {path}")
            value = value[:max_length]
        item_type = _list_item_type(annotation)
        return [
            _fit_value(item_type, None, item, f"{path}.{i}", fixes) for i, item in enumerate(value)
        ]
    model = _model_in(annotation)
    if model is not None:
        return _fit_model(model, value, f"{path}.", fixes)
    return value


def _max_length(field: FieldInfo) -> int | None:
    return next((m.max_length for m in field.metadata if isinstance(m, MaxLen)), None)


def _list_item_type(annotation: Any) -> Any:
    for candidate in _union_members(annotation):
        if get_origin(candidate) is list and get_args(candidate):
            return get_args(candidate)[0]
    return None


def _model_in(annotation: Any) -> type[BaseModel] | None:
    """The model class ``annotation`` names, alone or as the one model in a union with None."""
    models = [
        member
        for member in _union_members(annotation)
        if isinstance(member, type) and issubclass(member, BaseModel)
    ]
    return models[0] if len(models) == 1 else None


def _union_members(annotation: Any) -> tuple[Any, ...]:
    if get_origin(annotation) in (Union, types.UnionType):
        return get_args(annotation)
    return (annotation,)
