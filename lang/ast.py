from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence


@dataclass(frozen=True)
class Located:
    line: int
    column: int


@dataclass
class TypeExpr:
    name: str
    args: List["TypeExpr"] = field(default_factory=list)


@dataclass
class Param:
    name: str
    type_expr: TypeExpr


@dataclass
class Block:
    statements: List["Stmt"]


class Stmt:
    loc: Located


@dataclass
class LetStmt(Stmt):
    loc: Located
    name: str
    type_expr: Optional[TypeExpr]
    value: "Expr"
    mutable: bool = False


@dataclass
class AssignStmt(Stmt):
    loc: Located
    name: str
    value: "Expr"


@dataclass
class ReturnStmt(Stmt):
    loc: Located
    value: Optional["Expr"]


@dataclass
class RaiseStmt(Stmt):
    loc: Located
    value: "Expr"
    domain: Optional[str]


@dataclass
class ExprStmt(Stmt):
    loc: Located
    value: "Expr"


@dataclass
class FunctionDef:
    name: str
    params: Sequence[Param]
    return_type: TypeExpr
    body: Block
    thrown_domains: Optional[Sequence[str]]
    loc: Located


class Expr:
    loc: Located


@dataclass
class Literal(Expr):
    loc: Located
    value: object


@dataclass
class Name(Expr):
    loc: Located
    ident: str


@dataclass
class KwArg:
    name: str
    value: Expr


@dataclass
class Call(Expr):
    loc: Located
    func: Expr
    args: List[Expr]
    kwargs: List[KwArg]


@dataclass
class Binary(Expr):
    loc: Located
    op: str
    left: Expr
    right: Expr


@dataclass
class Unary(Expr):
    loc: Located
    op: str
    operand: Expr


@dataclass
class Move(Expr):
    loc: Located
    value: Expr


@dataclass
class Program:
    functions: List[FunctionDef]
    statements: List[Stmt]
