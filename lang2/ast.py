# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Local AST definitions for the lang2 refactor.

This mirrors the current parser AST but is colocated with the HIR/MIR rewrite
so we can evolve it without touching the production pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Union


# Base classes

class Node:
	"""Base class for all AST nodes (minimal)."""
	pass


class Expr(Node):
	"""Base class for expressions."""
	pass


class Stmt(Node):
	"""Base class for statements."""
	pass


# Expressions

@dataclass
class Literal(Expr):
	value: Union[int, str, bool]
	loc: Optional[object] = None  # placeholder for source location


@dataclass
class Name(Expr):
	ident: str
	loc: Optional[object] = None


@dataclass
class Placeholder(Expr):
	loc: Optional[object] = None


@dataclass
class Attr(Expr):
	value: Expr
	attr: str
	loc: Optional[object] = None


@dataclass
class Call(Expr):
	func: Expr
	args: List[Expr]
	kwargs: List[object]  # keep shape compatible; specifics not needed yet
	loc: Optional[object] = None


@dataclass
class Binary(Expr):
	op: str
	left: Expr
	right: Expr
	loc: Optional[object] = None


@dataclass
class Unary(Expr):
	op: str
	operand: Expr
	loc: Optional[object] = None


@dataclass
class Index(Expr):
	value: Expr
	index: Expr
	loc: Optional[object] = None


@dataclass
class ArrayLiteral(Expr):
	elems: List[Expr]
	loc: Optional[object] = None


@dataclass
class ExceptionCtor(Expr):
	name: str
	fields: List[object]  # placeholder; HIR lowering will handle later
	loc: Optional[object] = None


@dataclass
class CatchExprArm:
	event: Optional[str]
	binder: Optional[str]
	block: List[Stmt]
	loc: Optional[object] = None


@dataclass
class TryCatchExpr(Expr):
	attempt: Expr
	catch_arms: List[CatchExprArm]
	loc: Optional[object] = None


@dataclass
class Ternary(Expr):
	cond: Expr
	then_expr: Expr
	else_expr: Expr
	loc: Optional[object] = None


# Statements

@dataclass
class LetStmt(Stmt):
	name: str
	value: Expr
	loc: Optional[object] = None


@dataclass
class AssignStmt(Stmt):
	target: Expr
	value: Expr
	loc: Optional[object] = None


@dataclass
class IfStmt(Stmt):
	cond: Expr
	then_block: List[Stmt]
	else_block: List[Stmt]
	loc: Optional[object] = None


@dataclass
class ReturnStmt(Stmt):
	value: Optional[Expr]
	loc: Optional[object] = None


@dataclass
class RaiseStmt(Stmt):
	value: Expr
	loc: Optional[object] = None


@dataclass
class ExprStmt(Stmt):
	expr: Expr
	loc: Optional[object] = None


@dataclass
class ImportStmt(Stmt):
	path: str
	loc: Optional[object] = None


@dataclass
class TryStmt(Stmt):
	body: List[Stmt]
	catches: List[CatchExprArm]
	loc: Optional[object] = None


@dataclass
class WhileStmt(Stmt):
	cond: Expr
	body: List[Stmt]
	loc: Optional[object] = None


@dataclass
class ForStmt(Stmt):
	iter_var: str
	iterable: Expr
	body: List[Stmt]
	loc: Optional[object] = None


@dataclass
class BreakStmt(Stmt):
	loc: Optional[object] = None


@dataclass
class ContinueStmt(Stmt):
	loc: Optional[object] = None


@dataclass
class ThrowStmt(Stmt):
	value: Expr
	loc: Optional[object] = None


__all__ = [
	"Node", "Expr", "Stmt",
	"Literal", "Name", "Placeholder", "Attr", "Call", "Binary", "Unary",
	"Index", "ArrayLiteral", "ExceptionCtor", "CatchExprArm", "TryCatchExpr", "Ternary",
	"LetStmt", "AssignStmt", "IfStmt", "ReturnStmt", "RaiseStmt", "ExprStmt", "ImportStmt",
	"TryStmt", "WhileStmt", "ForStmt", "BreakStmt", "ContinueStmt", "ThrowStmt",
]
