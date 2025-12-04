# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04

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
	NEG = auto()      # numeric negation: -x
	NOT = auto()      # logical not: !x
	BIT_NOT = auto()  # bitwise not: ~x


class BinaryOp(Enum):
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
	name: str


@dataclass
class HLiteralInt(HExpr):
	value: int


@dataclass
class HLiteralString(HExpr):
	value: str


@dataclass
class HLiteralBool(HExpr):
	value: bool


@dataclass
class HCall(HExpr):
	fn: HExpr
	args: List[HExpr]


@dataclass
class HMethodCall(HExpr):
	receiver: HExpr
	method_name: str
	args: List[HExpr]


@dataclass
class HField(HExpr):
	subject: HExpr
	name: str


@dataclass
class HIndex(HExpr):
	subject: HExpr
	index: HExpr


@dataclass
class HDVInit(HExpr):
	"""Desugared DiagnosticValue constructor call."""
	dv_type_name: str
	args: List[HExpr]


@dataclass
class HUnary(HExpr):
	op: UnaryOp
	expr: HExpr


@dataclass
class HBinary(HExpr):
	op: BinaryOp
	left: HExpr
	right: HExpr


# Statements

@dataclass
class HBlock(HStmt):
	statements: List[HStmt]


@dataclass
class HLet(HStmt):
	name: str
	value: HExpr


@dataclass
class HAssign(HStmt):
	target: HExpr  # usually HVar, HField, HIndex
	value: HExpr


@dataclass
class HIf(HStmt):
	cond: HExpr
	then_block: HBlock
	else_block: Optional[HBlock]


@dataclass
class HLoop(HStmt):
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
	"HBlock", "HLet", "HAssign", "HIf", "HLoop",
	"HBreak", "HContinue", "HReturn",
]
