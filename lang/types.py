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
DISPLAYABLE = Type("<Displayable>")
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

_DISPLAYABLE_PRIMITIVES = frozenset({I64, F64, BOOL, STR, ERROR})


def is_displayable(ty: Type) -> bool:
    return ty in _DISPLAYABLE_PRIMITIVES


def resolve_type(type_expr: TypeExpr) -> Type:
    if type_expr.args:
        # Generics flow through as symbolic names for now
        inner = ", ".join(arg.name for arg in type_expr.args)
        name = f"{type_expr.name}[{inner}]"
        return Type(name)
    builtin = _PRIMITIVES.get(type_expr.name)
    if builtin:
        return builtin
    return Type(type_expr.name)


@dataclass(frozen=True)
class FunctionSignature:
    name: str
    params: tuple[Type, ...]
    return_type: Type
    effects: Optional[frozenset[str]]
    allowed_kwargs: frozenset[str] = frozenset()


class TypeSystemError(Exception):
    pass
