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

from dataclasses import dataclass, field
from typing import List, Optional, Union

from lang2.driftc.core.span import Span


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
	kwargs: List["KwArg"]
	loc: Optional[object] = None


@dataclass
class KwArg:
	"""
	Keyword argument `name = value` (used by calls and exception constructors).

	We keep `loc` so later passes can point diagnostics at the keyword name
	(token) rather than at the value expression.
	"""
	name: str
	value: Expr
	loc: Span = field(default_factory=Span)


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
	Exception constructor application (throw-only in the surface language).

	Supports both positional and keyword arguments; positional arguments must
	precede keyword arguments (enforced by the parser).

	Semantics of mapping arguments to declared exception fields is handled later
	once exception schemas are available.
	"""
	name: str
	args: List[Expr]
	kwargs: List[KwArg]
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


@dataclass
class FStringHole:
	"""
	Single hole `{expr[:spec]}` inside an f-string.

	- `expr` is any expression.
	- `spec` is a compile-time string (MVP: opaque text; no nested `{}`).
	"""
	expr: Expr
	spec: str = ""
	loc: Optional[object] = None


@dataclass
class FString(Expr):
	"""
	f-string literal `f"..."`.

	Representation matches the lowering contract: `len(parts) == len(holes) + 1`.
	"""
	parts: list[str]
	holes: list[FStringHole]
	loc: Optional[object] = None


# Statements

@dataclass
class LetStmt(Stmt):
	"""
	Binding introduction (`val` / `var`).

	The parser-level AST distinguishes between immutable (`val`) and mutable
	(`var`) bindings. Stage0 preserves this as `mutable` so later phases can
	enforce MVP borrow rules (e.g., `&mut x` requires `x` to be mutable).
	"""
	name: str
	value: Expr
	type_expr: Optional[object] = None  # preserve parsed type annotation if present
	mutable: bool = False
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


@dataclass
class RethrowStmt(Stmt):
	"""Rethrow the currently caught error; only valid inside a catch."""
	loc: Span = field(default_factory=Span)


__all__ = [
	"Node", "Expr", "Stmt",
	"Literal", "Name", "Placeholder", "Attr", "Call", "Binary", "Unary",
	"Index", "ArrayLiteral", "ExceptionCtor", "CatchExprArm", "TryCatchExpr", "Ternary", "TryExpr",
	"LetStmt", "AssignStmt", "IfStmt", "ReturnStmt", "RaiseStmt", "ExprStmt", "ImportStmt",
	"TryStmt", "WhileStmt", "ForStmt", "BreakStmt", "ContinueStmt", "ThrowStmt", "RethrowStmt",
]
