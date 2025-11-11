from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Optional

from .ast import TypeExpr


@dataclass(frozen=True)
class Type:
    name: str

    def __str__(self) -> str:  # pragma: no cover - trivial repr
        return self.name


I64 = Type("Int64")
F64 = Type("Float64")
BOOL = Type("Bool")
STR = Type("String")
UNIT = Type("Void")
ERROR = Type("Error")
CONSOLE_OUT = Type("ConsoleOut")
BOTTOM = Type("⊥")

_PRIMITIVES: Dict[str, Type] = {
    "Int64": I64,
    "i64": I64,
    "Float64": F64,
    "f64": F64,
    "Bool": BOOL,
    "bool": BOOL,
    "String": STR,
    "str": STR,
    "Void": UNIT,
    "unit": UNIT,
    "Error": ERROR,
    "error": ERROR,
    "ConsoleOut": CONSOLE_OUT,
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
