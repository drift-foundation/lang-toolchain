# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Local AST definitions for the lang2 refactor.

This mirrors the current parser AST but is colocated with the HIR/MIR rewrite
so we can evolve it without touching the production pipeline.

Pipeline placement:
  Surface syntax (AST, this file) → HIR → MIR → SSA → LLVM/obj
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
	"""Literal value (int, string, or bool)."""
	value: Union[int, str, bool]
	loc: Optional[object] = None  # placeholder for source location


@dataclass
class Name(Expr):
	"""Identifier reference."""
	ident: str
	loc: Optional[object] = None


@dataclass
class Placeholder(Expr):
	"""Receiver placeholder (dot-shortcut) before desugaring."""
	loc: Optional[object] = None


@dataclass
class Attr(Expr):
	"""Attribute access: value.attr."""
	value: Expr
	attr: str
	loc: Optional[object] = None


@dataclass
class Call(Expr):
	"""Function or method call prior to desugaring."""
	func: Expr
	args: List[Expr]
	kwargs: List[object]  # keep shape compatible; specifics not needed yet
	loc: Optional[object] = None


@dataclass
class Binary(Expr):
	"""Binary operator expression."""
	op: str
	left: Expr
	right: Expr
	loc: Optional[object] = None


@dataclass
class Unary(Expr):
	"""Unary operator expression."""
	op: str
	operand: Expr
	loc: Optional[object] = None


@dataclass
class Index(Expr):
	"""Indexing expression: value[index]."""
	value: Expr
	index: Expr
	loc: Optional[object] = None


@dataclass
class ArrayLiteral(Expr):
	"""Array literal placeholder used in early AST; semantics refined later."""
	elements: List[Expr]
	loc: Optional[object] = None


@dataclass
class ExceptionCtor(Expr):
	"""
	Exception constructor application. Fields map parameter name -> expression.
	arg_order is omitted for now; HIR lowering can impose ordering if needed.
	"""
	name: str
	fields: dict  # mapping str -> Expr; kept loose for the rewrite
	loc: Optional[object] = None


@dataclass
class CatchExprArm:
	"""Single catch arm in a try/catch expression."""
	event: Optional[str]
	binder: Optional[str]
	block: List[Stmt]
	loc: Optional[object] = None


@dataclass
class TryCatchExpr(Expr):
	"""Expression-form try/catch (lowered later)."""
	attempt: Expr
	catch_arms: List[CatchExprArm]
	loc: Optional[object] = None


@dataclass
class Ternary(Expr):
	"""Conditional expression: cond ? then_expr : else_expr."""
	cond: Expr
	then_expr: Expr
	else_expr: Expr
	loc: Optional[object] = None


@dataclass
class TryExpr(Expr):
	"""
	Result-driven try sugar (e.g., expr? / try expr).

	This is a syntactic marker that will be desugared in a later HIR rewrite
	pass once types are known. Semantics: treat an Err from expr as a throw.
	"""
	expr: Expr
	loc: Optional[object] = None


# Statements

@dataclass
class LetStmt(Stmt):
	"""Let-binding statement: let name = value."""
	name: str
	value: Expr
	type_expr: Optional[object] = None  # preserve parsed type annotation if present
	loc: Optional[object] = None


@dataclass
class AssignStmt(Stmt):
	"""Assignment to an expression target."""
	target: Expr
	value: Expr
	loc: Optional[object] = None


@dataclass
class IfStmt(Stmt):
	"""If/else statement with explicit blocks."""
	cond: Expr
	then_block: List[Stmt]
	else_block: List[Stmt]
	loc: Optional[object] = None


@dataclass
class ReturnStmt(Stmt):
	"""Function return with optional value."""
	value: Optional[Expr]
	loc: Optional[object] = None


@dataclass
class RaiseStmt(Stmt):
	"""Raise expression value as an error (placeholder)."""
	value: Expr
	loc: Optional[object] = None


@dataclass
class ExprStmt(Stmt):
	"""Expression used for side effects as a statement."""
	expr: Expr
	loc: Optional[object] = None


@dataclass
class ImportStmt(Stmt):
	"""Import statement placeholder (path-only for now)."""
	path: str
	loc: Optional[object] = None


@dataclass
class TryStmt(Stmt):
	"""Statement-form try/catch placeholder."""
	body: List[Stmt]
	catches: List[CatchExprArm]
	loc: Optional[object] = None


@dataclass
class WhileStmt(Stmt):
	"""While loop: while cond { body }."""
	cond: Expr
	body: List[Stmt]
	loc: Optional[object] = None


@dataclass
class ForStmt(Stmt):
	"""Foreach loop: for iter_var in iterable { body }."""
	iter_var: str
	iterable: Expr
	body: List[Stmt]
	loc: Optional[object] = None


@dataclass
class BreakStmt(Stmt):
	"""Loop break."""
	loc: Optional[object] = None


@dataclass
class ContinueStmt(Stmt):
	"""Loop continue."""
	loc: Optional[object] = None


@dataclass
class ThrowStmt(Stmt):
	"""Throw statement placeholder."""
	value: Expr
	loc: Optional[object] = None


__all__ = [
	"Node", "Expr", "Stmt",
	"Literal", "Name", "Placeholder", "Attr", "Call", "Binary", "Unary",
	"Index", "ArrayLiteral", "ExceptionCtor", "CatchExprArm", "TryCatchExpr", "Ternary", "TryExpr",
	"LetStmt", "AssignStmt", "IfStmt", "ReturnStmt", "RaiseStmt", "ExprStmt", "ImportStmt",
	"TryStmt", "WhileStmt", "ForStmt", "BreakStmt", "ContinueStmt", "ThrowStmt",
]
