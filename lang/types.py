from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Optional

from .ast import TypeExpr


@dataclass(frozen=True)
class Type:
    name: str

    def __str__(self) -> str:  # pragma: no cover - trivial repr
        return self.name


I64 = Type("i64")
F64 = Type("f64")
BOOL = Type("bool")
STR = Type("str")
UNIT = Type("unit")
ERROR = Type("error")
BOTTOM = Type("⊥")

_PRIMITIVES: Dict[str, Type] = {
    t.name: t
    for t in (I64, F64, BOOL, STR, UNIT, ERROR)
}


def resolve_type(type_expr: TypeExpr) -> Type:
    if type_expr.args:
        raise TypeSystemError(
            f"Generic type '{type_expr.name}' is not supported yet."
        )
    try:
        return _PRIMITIVES[type_expr.name]
    except KeyError as exc:  # pragma: no cover - defensive
        raise TypeSystemError(f"Unknown type '{type_expr.name}'") from exc


@dataclass(frozen=True)
class FunctionSignature:
    name: str
    params: tuple[Type, ...]
    return_type: Type
    effects: Optional[frozenset[str]]
    allowed_kwargs: frozenset[str] = frozenset()


class TypeSystemError(Exception):
    pass
