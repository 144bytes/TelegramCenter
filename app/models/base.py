"""Dataclass <-> dict conversion that tolerates unknown / missing keys.

Handles nesting: a field annotated with another dataclass, or with
`list[SomeDataclass]`, is rebuilt into real objects rather than left as dicts.
That is what lets Campaign.schedule and Campaign.results survive a round-trip
through JSON.
"""
from __future__ import annotations

import dataclasses
import types
import typing
from typing import Any, TypeVar

T = TypeVar("T", bound="Model")


def _strip_optional(tp: Any) -> Any:
    """`X | None` -> `X`; anything else is returned unchanged."""
    origin = typing.get_origin(tp)
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return tp


def _resolve_hints(cls: type) -> dict[str, Any]:
    """Evaluate string annotations (we use `from __future__ import annotations`)."""
    cached = cls.__dict__.get("_tc_hints")
    if cached is not None:
        return cached
    module = __import__(cls.__module__, fromlist=["__dict__"])
    ns = dict(vars(module))
    try:
        hints = typing.get_type_hints(cls, globalns=ns)
    except Exception:  # noqa: BLE001 — unresolvable forward ref: fall back to raw
        hints = {f.name: f.type for f in dataclasses.fields(cls)}
    setattr(cls, "_tc_hints", hints)
    return hints


def _coerce(value: Any, hint: Any) -> Any:
    hint = _strip_optional(hint)
    if dataclasses.is_dataclass(hint) and isinstance(value, dict):
        return hint.from_dict(value) if hasattr(hint, "from_dict") else hint(**value)
    if typing.get_origin(hint) in (list, typing.List) and isinstance(value, list):
        (inner,) = typing.get_args(hint) or (Any,)
        return [_coerce(v, inner) for v in value]
    return value


class Model:
    """Mixin for @dataclass models."""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls: type[T], data: dict[str, Any]) -> T:
        if not isinstance(data, dict):
            data = {}
        hints = _resolve_hints(cls)
        known: dict[str, Any] = {}
        for f in dataclasses.fields(cls):
            if f.name in data:
                known[f.name] = _coerce(data[f.name], hints.get(f.name, Any))
        return cls(**known)  # type: ignore[arg-type]
