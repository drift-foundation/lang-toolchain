from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from .ast import TypeExpr


@dataclass(frozen=True)
class Type:
    name: str
    args: Tuple["Type", ...] = ()

    def __str__(self) -> str:  # pragma: no cover - trivial repr
        if not self.args:
            return self.name
        inner = ", ".join(str(a) for a in self.args)
        return f"{self.name}[{inner}]"


@dataclass(frozen=True)
class ReferenceType(Type):
    mutable: bool = False

    def __str__(self) -> str:  # pragma: no cover - trivial repr
        prefix = "&mut " if self.mutable else "&"
        return f"{prefix}{self.args[0]}"


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
    "Float64": F64,
    "Bool": BOOL,
    "String": STR,
    "Void": UNIT,
    "Error": ERROR,
    "ConsoleOut": CONSOLE_OUT,
}

_ALIAS_HINTS = {
    "i64": "Int64",
    "f64": "Float64",
    "bool": "Bool",
    "str": "String",
    "unit": "Void",
    "error": "Error",
}

_DISPLAYABLE_PRIMITIVES = frozenset({I64, F64, BOOL, STR, ERROR})


def is_displayable(ty: Type) -> bool:
    return ty in _DISPLAYABLE_PRIMITIVES


def resolve_type(type_expr: TypeExpr) -> Type:
    if type_expr.name == "&":
        inner = resolve_type(type_expr.args[0])
        return ReferenceType(name="&", args=(inner,), mutable=False)
    if type_expr.name == "&mut":
        inner = resolve_type(type_expr.args[0])
        return ReferenceType(name="&mut", args=(inner,), mutable=True)
    if type_expr.args:
        # Generics flow through as symbolic names for now
        inner = ", ".join(str(arg) for arg in type_expr.args)
        name = f"{type_expr.name}[{inner}]"
        return Type(name)
    alias_hint = _ALIAS_HINTS.get(type_expr.name)
    if alias_hint:
        raise TypeSystemError(
            f"Type '{type_expr.name}' is not defined. Use '{alias_hint}' instead."
        )
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


def array_of(inner: Type) -> Type:
    return Type(f"Array[{inner}]")


def array_element_type(array_type: Type) -> Optional[Type]:
    name = array_type.name
    prefix = "Array["
    if name.startswith(prefix) and name.endswith("]"):
        inner = name[len(prefix) : -1]
        return Type(inner)
    return None


def ref_of(inner: Type, mutable: bool = False) -> ReferenceType:
    return ReferenceType(name="&mut" if mutable else "&", args=(inner,), mutable=mutable)
