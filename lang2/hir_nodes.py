# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
High-level Intermediate Representation (HIR).

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
	"HCall", "HMethodCall", "HField", "HIndex", "HDVInit",
	"HUnary", "HBinary",
	"HBlock", "HExprStmt", "HLet", "HAssign", "HIf", "HLoop",
	"HBreak", "HContinue", "HReturn",
]
