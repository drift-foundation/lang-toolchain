# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
High-level Intermediate Representation (HIR).

Pipeline placement:
  AST (lang2/stage0/ast.py) → HIR (this file) → MIR → SSA → LLVM/obj

The HIR is a *sugar-free* tree that sits between the parsed AST and MIR.
All surface sugar (dot placeholders, method-call sugar, index sugar, DV ctor
shorthands) must be removed before reaching this layer. That keeps the later
passes focused on semantics rather than syntax.

Guiding rules:
- Nodes are purely syntactic; no type or symbol resolution is embedded here.
- Method calls carry an explicit receiver.
- Blocks are explicit statements (`HBlock`), used for `HIf`/`HLoop` bodies.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import List, Optional


# Base node kinds

class HNode:
	"""Base class for all HIR nodes."""
	pass


class HExpr(HNode):
	"""Base class for all HIR expressions."""
	pass


class HStmt(HNode):
	"""Base class for all HIR statements."""
	pass


# Operator enums

class UnaryOp(Enum):
	"""Unary operators preserved in HIR."""
	NEG = auto()      # numeric negation: -x
	NOT = auto()      # logical not: !x
	BIT_NOT = auto()  # bitwise not: ~x


class BinaryOp(Enum):
	"""Binary operators preserved in HIR (no short-circuit lowering yet)."""
	ADD = auto()
	SUB = auto()
	MUL = auto()
	DIV = auto()
	MOD = auto()

	BIT_AND = auto()
	BIT_OR = auto()
	BIT_XOR = auto()
	SHL = auto()
	SHR = auto()

	EQ = auto()
	NE = auto()
	LT = auto()
	LE = auto()
	GT = auto()
	GE = auto()

	AND = auto()  # logical and (&&)
	OR = auto()   # logical or (||)


# Expressions

@dataclass
class HVar(HExpr):
	"""Reference to a local/binding (resolved later)."""
	name: str


@dataclass
class HLiteralInt(HExpr):
	"""Integer literal (as parsed)."""
	value: int


@dataclass
class HLiteralString(HExpr):
	"""String literal (UTF-8 bytes; normalization happens later)."""
	value: str


@dataclass
class HLiteralBool(HExpr):
	"""Boolean literal."""
	value: bool


@dataclass
class HCall(HExpr):
	"""Plain function call: fn(args...)."""
	fn: HExpr
	args: List[HExpr]


@dataclass
class HMethodCall(HExpr):
	"""
	Method call with explicit receiver.

	Example: lowering `obj.foo(1, 2)` becomes:
	    HMethodCall(receiver=HVar("obj"), method_name="foo", args=[HLiteralInt(1), HLiteralInt(2)])
	"""
	receiver: HExpr
	method_name: str
	args: List[HExpr]


@dataclass
class HTernary(HExpr):
	"""Conditional expression: cond ? then_expr : else_expr."""
	cond: HExpr
	then_expr: HExpr
	else_expr: HExpr


@dataclass
class HTryResult(HExpr):
	"""
	Result-driven try sugar marker (e.g., expr?).

	This remains in HIR until a rewrite pass desugars it into an explicit
	`if is_err { throw unwrap_err() } else { unwrap() }` pattern. Keeping it
	syntactic here avoids entangling lowering with sugar.
	"""
	expr: HExpr


@dataclass
class HResultOk(HExpr):
	"""
	Wrap a value into FnResult.Ok for explicit can-throw returns.

	This is a lightweight node so tests/pipeline code can express returning
	a FnResult without hand-coding MIR ops. Lowering turns this into
	ConstructResultOk(dest, value).
	"""

	value: HExpr


@dataclass
class HThrow(HStmt):
	"""Throw an error/exception value. Semantically returns Err from this function."""
	value: HExpr


@dataclass
class HTry(HStmt):
	"""
	Statement-level try/catch with multiple arms.

	  try { body }
	  catch EventName(e) { handler_for_event }
	  catch (e) { catch_all_with_binder }
	  catch { catch_all_no_binder }

	Arms are evaluated in source order; the first matching event_name (or the
	catch-all) is taken. If no arm matches, the error is rethrown.
	"""
	body: "HBlock"
	catches: List["HCatchArm"]


@dataclass
class HCatchArm(HStmt):
	"""
	Single catch arm inside an HTry.

	event_name: name of the exception/event to match (None = catch-all)
	binder: local name to bind the Error to inside the arm (None = no binder)
	block: handler body
	"""

	event_name: Optional[str]
	binder: Optional[str]
	block: "HBlock"


@dataclass
class HField(HExpr):
	"""Field access: subject.name"""
	subject: HExpr
	name: str


@dataclass
class HIndex(HExpr):
	"""Indexing: subject[index]"""
	subject: HExpr
	index: HExpr


@dataclass
class HDVInit(HExpr):
	"""Desugared DiagnosticValue constructor call."""
	dv_type_name: str
	args: List[HExpr]


@dataclass
class HUnary(HExpr):
	"""Unary operation."""
	op: UnaryOp
	expr: HExpr


@dataclass
class HBinary(HExpr):
	"""Binary operation (short-circuit not lowered yet)."""
	op: BinaryOp
	left: HExpr
	right: HExpr


@dataclass
class HArrayLiteral(HExpr):
	"""Array literal with element expressions."""
	elements: List[HExpr]


# Statements

@dataclass
class HBlock(HStmt):
	"""Ordered list of statements; used for bodies of control-flow constructs."""
	statements: List[HStmt]


@dataclass
class HExprStmt(HStmt):
	"""Expression used as a statement (value discarded)."""
	expr: HExpr


@dataclass
class HLet(HStmt):
	"""Immutable binding introduction."""
	name: str
	value: HExpr


@dataclass
class HAssign(HStmt):
	target: HExpr  # usually HVar, HField, HIndex
	value: HExpr


@dataclass
class HIf(HStmt):
	"""Conditional with explicit then/else blocks (else may be None)."""
	cond: HExpr
	then_block: HBlock
	else_block: Optional[HBlock]


@dataclass
class HLoop(HStmt):
	"""Loop with a single body block; lowering decides while/for semantics."""
	body: HBlock


@dataclass
class HBreak(HStmt):
	pass


@dataclass
class HContinue(HStmt):
	pass


@dataclass
class HReturn(HStmt):
	value: Optional[HExpr]


__all__ = [
	"HNode", "HExpr", "HStmt",
	"UnaryOp", "BinaryOp",
	"HVar", "HLiteralInt", "HLiteralString", "HLiteralBool",
	"HCall", "HMethodCall", "HTernary", "HTryResult", "HField", "HIndex", "HDVInit",
	"HUnary", "HBinary",
	"HBlock", "HExprStmt", "HLet", "HAssign", "HIf", "HLoop",
	"HBreak", "HContinue", "HReturn", "HThrow", "HTry", "HCatchArm",
]
