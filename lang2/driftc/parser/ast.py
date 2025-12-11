from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence


@dataclass
class StructField:
    name: str
    type_expr: "TypeExpr"


@dataclass
class StructDef:
    name: str
    fields: List[StructField]
    loc: "Located"


@dataclass
class ExceptionArg:
    name: str
    type_expr: "TypeExpr"


@dataclass
class ExceptionDef:
    name: str
    args: List[ExceptionArg]
    loc: "Located"
    domain: Optional[str] = None


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
    capture: bool = False
    capture_alias: Optional[str] = None


@dataclass
class AssignStmt(Stmt):
    loc: Located
    target: "Expr"
    value: "Expr"


@dataclass
class IfStmt(Stmt):
    loc: Located
    condition: "Expr"
    then_block: "Block"
    else_block: Optional["Block"] = None


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
	orig_name: str
	params: Sequence[Param]
	return_type: TypeExpr
	body: Block
	loc: Located
	is_method: bool = False
	self_mode: str | None = None  # "value", "ref", "ref_mut"
	impl_target: TypeExpr | None = None


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
class Placeholder(Expr):
    loc: Located


@dataclass
class Attr(Expr):
    loc: Located
    value: Expr
    attr: str


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
class Index(Expr):
    loc: Located
    value: Expr
    index: Expr


@dataclass
class ArrayLiteral(Expr):
    loc: Located
    elements: List[Expr]


@dataclass
class Program:
	functions: List[FunctionDef] = field(default_factory=list)
	implements: List["ImplementDef"] = field(default_factory=list)
	statements: List[Stmt] = field(default_factory=list)
	structs: List[StructDef] = field(default_factory=list)
	exceptions: List[ExceptionDef] = field(default_factory=list)
	module: Optional[str] = None


@dataclass
class ImplementDef:
	target: TypeExpr
	loc: Located
	methods: List[FunctionDef] = field(default_factory=list)


@dataclass
class ImportStmt(Stmt):
    loc: Located
    path: List[str]
    alias: Optional[str] = None


@dataclass
class CatchClause:
    event: Optional[str]
    binder: Optional[str]
    block: Block
    event_code: Optional[int] = None
    arg_order: Optional[list[str]] = None


@dataclass
class TryStmt(Stmt):
    loc: Located
    body: Block
    catches: List[CatchClause]

@dataclass
class WhileStmt(Stmt):
    loc: Located
    condition: "Expr"
    body: Block


@dataclass
class ForStmt(Stmt):
    loc: Located
    var: str
    iter_expr: "Expr"
    body: Block

@dataclass
class BreakStmt(Stmt):
    loc: Located

@dataclass
class ContinueStmt(Stmt):
    loc: Located

@dataclass
class ThrowStmt(Stmt):
    loc: Located
    expr: "Expr"


@dataclass
class ExceptionCtor(Expr):
    """Semantic node representing an exception constructor application."""

    loc: Located
    name: str
    event_code: int
    fields: Dict[str, Expr]
    arg_order: Optional[list[str]] = None


@dataclass
class CatchExprArm:
    event: Optional[str]
    binder: Optional[str]
    block: Block


@dataclass
class TryCatchExpr(Expr):
    loc: Located
    attempt: Expr
    catch_arms: List[CatchExprArm]


@dataclass
class Ternary(Expr):
    loc: Located
    condition: Expr
    then_value: Expr
    else_value: Expr
