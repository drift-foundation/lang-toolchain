# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Local AST definitions for the lang refactor.

This mirrors the current parser AST but is colocated with the HIR/MIR rewrite
so we can evolve it without touching the production pipeline.

Pipeline placement:
  Surface syntax (AST, this file) → HIR → MIR → SSA → LLVM/obj
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Union

from lang.driftc.core.span import Span


# Base classes

class Node:
	"""Base class for all AST nodes (minimal)."""
	pass


class Expr(Node):
	"""Base class for expressions."""
	pass


class TraitExpr(Expr):
	"""Trait guard expression (type-level boolean)."""
	pass


class TraitSubject(Expr):
	"""Trait subject reference (Self or a type name)."""
	pass


@dataclass
class SelfRef(TraitSubject):
	loc: Span = field(default_factory=Span)

	def __hash__(self) -> int:
		return hash("Self")


@dataclass
class TypeNameRef(TraitSubject):
	name: str
	module_id: Optional[str] = None
	loc: Span = field(default_factory=Span)

	def __hash__(self) -> int:
		return hash((self.module_id, self.name))


@dataclass
class TraitIs(TraitExpr):
	subject: object
	trait: object
	loc: Span = field(default_factory=Span)


@dataclass
class TraitAnd(TraitExpr):
	left: TraitExpr
	right: TraitExpr
	loc: Span = field(default_factory=Span)


@dataclass
class TraitOr(TraitExpr):
	left: TraitExpr
	right: TraitExpr
	loc: Span = field(default_factory=Span)


@dataclass
class TraitNot(TraitExpr):
	expr: TraitExpr
	loc: Span = field(default_factory=Span)


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
class UintLiteral(Expr):
	"""Unsigned integer literal with `u` suffix (e.g. `42u`)."""
	value: int
	loc: Optional[object] = None


@dataclass
class Uint64Literal(Expr):
	"""Unsigned 64-bit integer literal with `u64` suffix (e.g. `42u64`)."""
	value: int
	loc: Optional[object] = None


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
class QualifiedMember(Expr):
	"""
	Type-level qualified member reference: `TypeRef::member`.

	This is a general expression form (not ctor-only). MVP semantics are
	restricted by the typed checker:
	- only variant constructors are supported as members, and
	- the qualified member must be called (`TypeRef::Ctor(...)`).

	`base_type_expr` is a parser type expression object (duck-typed on `name`,
	`args`, and optional `module_id`) so later phases can resolve it into a
	concrete `TypeId` without re-parsing.
	"""

	base_type_expr: object
	member: str
	loc: Span = field(default_factory=Span)


@dataclass
class Call(Expr):
	"""Function or method call prior to desugaring."""
	func: Expr
	args: List[Expr]
	kwargs: List["KwArg"]
	type_args: Optional[List[object]] = None
	loc: Optional[object] = None


@dataclass
class MacroCall(Expr):
	"""Macro invocation prior to expansion/desugaring."""
	func: Expr
	args: List[Expr]
	kwargs: List["KwArg"]
	loc: Optional[object] = None


@dataclass
class TypeApp(Expr):
	"""Explicit type application on a callable reference (no call)."""
	func: Expr
	type_args: List[object]
	loc: Optional[object] = None


@dataclass
class Cast(Expr):
	"""Explicit cast: cast<T>(expr)."""
	target_type: object
	expr: Expr
	loc: Optional[object] = None


@dataclass
class Lambda(Expr):
	"""Lambda expression: params + body (expr or block)."""
	params: List["Param"]
	ret_type: object | None = None
	captures: Optional[List["CaptureItem"]] = None
	body_expr: Expr | None = None
	body_block: "Block" | None = None
	declared_nothrow: bool = False
	loc: Optional[object] = None


@dataclass
class CaptureItem:
	"""Explicit capture list item for a lambda."""
	name: str
	kind: str  # "ref", "ref_mut", "copy", "move"
	loc: Optional[object] = None


@dataclass
class Block(Expr):
	"""Block expression: sequence of statements and an optional trailing expression."""
	statements: List[Stmt]
	loc: Optional[object] = None


@dataclass
class YieldExpr(Expr):
	"""Explicit value production inside a value block."""
	value: Expr
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
class Param:
	"""Function/lambda parameter (name + optional parsed type)."""
	name: str
	type_expr: object | None = None
	mutable: bool = False
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
class Move(Expr):
	"""
	Ownership transfer: `move <expr>`.

	This is a *surface* marker that explicitly requests moving a value out of an
	addressable place.

	Semantic intent (enforced later):
	- The operand must be an addressable place (not an rvalue).
	- The operand is consumed and becomes unusable until reinitialized.
	- Moving out of borrowed storage is rejected by the borrow checker.
	"""
	value: Expr
	loc: Optional[object] = None


@dataclass
class Copy(Expr):
	"""
	Explicit duplication: `copy <expr>`.

	The operand can be any expression; the type checker enforces that the
	operand's type is Copy.
	"""
	value: Expr
	loc: Optional[object] = None


@dataclass
class Share(Expr):
	"""
	Second-owner aliasing operation at expression position: `share <expr>`.

	Symmetric with `captures(share x)` in lambda capture lists.  v1
	restricts the operand to a NAME (local binding) at AST→HIR;
	non-NAME subjects emit `E-SHARE-EXPR-SUBJECT-NOT-LOCAL`.

	Lowered at AST→HIR to
	`HCall(HQualifiedMember(Share-trait, "share"), [HBorrow(<local>)])`
	with the call's `origin` field set to `"share_expr"` (NAME
	subject) or `"share_expr_non_local"` (non-NAME).  Type-check
	flows through the standard trait-dispatch pipeline; the
	source-form-keyed diagnostics (`E-SHARE-EXPR-NOT-SHARE`,
	`E-SHARE-EXPR-SUBJECT-NOT-LOCAL`) are emitted by the type
	checker dispatching on `origin` — `normalize.py`'s HCall rebuild
	preserves `origin` (a declared dataclass field) but drops
	dynamic attributes, so `origin` is the durable metadata
	channel.

	Borrow-check invariant: lowering must NOT mutate the binding.
	`share x` is a refcount bump on the owner; outstanding borrows
	into `*x` (e.g. `val r = x.get(); ... share x ...`) remain valid
	through the call AND the unwind path.  The `HBorrow(<local>)`
	desugaring guarantees this — no `MoveOut` / tombstone touches `x`.
	"""
	value: Expr
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
class MapEntry:
	"""Single `{ key: value }` map literal entry."""
	key: Expr
	value: Expr
	loc: Optional[object] = None


@dataclass
class MapLiteral(Expr):
	"""Map literal placeholder used in early AST; semantics refined later."""
	entries: List[MapEntry]
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
class MatchArm:
	"""
	Single `match` arm.

	Patterns in v1:
	- constructor pattern: `Ctor(b1, b2, ...)`
	- zero-field constructor: `Ctor`
	- default arm: `default`

	Arm bodies are blocks (statement lists). A value-producing arm is represented
	by a trailing `ExprStmt` in the block (from `value_block` parsing).
	"""

	ctor: Optional[str]  # None means default arm
	# Pattern argument form:
	# - "bare": `Ctor` (allowed only for zero-field constructors)
	# - "paren": `Ctor()` (tag-only match, ignores payload)
	# - "positional": `Ctor(a, b)` (binds fields by index, exact arity)
	# - "named": `Ctor(x = a, y = b)` (binds a subset of fields by name)
	pattern_arg_form: str
	binders: List[str]
	block: List[Stmt]
	# Field names for named binders, parallel to `binders`. Only meaningful when
	# `pattern_arg_form == "named"`.
	binder_fields: Optional[List[str]] = None
	# Qualified constructor base type (e.g., `pkg.Mod::Ctor`), if specified.
	ctor_base: Optional[TypeExpr] = None
	# Mutability flags for binders, parallel to `binders`.
	binder_is_mutable: Optional[List[bool]] = None
	# RAW scalar-literal pattern data (parser-owned), carried through to HIR for
	# the checker to validate.  `scalar_literal_kind` ∈ {"INT","UINT_LIT",
	# "UINT64_LIT","NEG_INT"} for integer-literal arms (else None);
	# `scalar_literal_magnitude` is the unsigned magnitude as written.
	scalar_literal_kind: Optional[str] = None
	scalar_literal_magnitude: Optional[int] = None
	loc: Span = field(default_factory=Span)


@dataclass
class MatchExpr(Expr):
	"""Expression-form `match` (expression-only in v1)."""

	scrutinee: Expr
	arms: List[MatchArm]
	loc: Optional[object] = None


@dataclass
class Ternary(Expr):
	"""Conditional expression: cond ? then_expr : else_expr."""
	cond: Expr
	then_expr: Expr
	else_expr: Expr
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
	capture: bool = False
	capture_alias: Optional[str] = None
	loc: Optional[object] = None


@dataclass
class LocalConstStmt(Stmt):
	"""Block-scope constant declaration.

	Syntax: const NAME: Type = <literal>;

	Semantics: compile-time literal alias with no storage. Each use site
	re-materializes the value as a MIR literal.
	"""
	name: str
	type_expr: object  # TypeExpr
	value: Expr
	loc: Optional[object] = None


@dataclass
class AssignStmt(Stmt):
	"""Assignment to an expression target."""
	target: Expr
	value: Expr
	loc: Optional[object] = None


@dataclass
class AugAssignStmt(Stmt):
	"""
	Augmented assignment statement.

	MVP supports:
	`+=`, `-=`, `*=`, `/=`, `%=`, `&=`, `|=`, `^=`, `<<=`, `>>=`.

	This node exists to preserve correct evaluation semantics for complex
	lvalues. Lowering should evaluate the target address once and perform a
	read-modify-write sequence rather than desugaring to `x = x + y` too early.
	"""

	target: Expr
	op: str
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
class AssertStmt(Stmt):
	"""Assert statement: assert(cond[, msg])."""
	cond: Expr
	msg: Optional[Expr]
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
	iter_var_mutable: bool = False
	iter_var_type: Optional[TypeExpr] = None
	loc: Optional[object] = None


@dataclass
class ForCountStmt(Stmt):
	"""Counted loop:
	  for init; cond; step { body }
	  for (init?; cond?; step?) { body }    // C-style, all optional
	"""
	init_name: Optional[str]
	init_value: Optional[Expr]
	cond: Optional[Expr]
	step: Optional[Stmt]
	body: List[Stmt]
	init_mutable: bool = False
	init_type: Optional[TypeExpr] = None
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
class BlockStmt(Stmt):
	"""Bare block statement: { ... } — explicit lexical scope."""
	body: List[Stmt]
	loc: Optional[object] = None


@dataclass
class UnsafeBlockStmt(Stmt):
	"""Unsafe block statement: unsafe { ... }."""
	body: List[Stmt]
	loc: Optional[object] = None


@dataclass
class UnsafeExpr(Expr):
	"""Expression-form unsafe block: unsafe { stmts; expr }."""
	body: List[Stmt]
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
	"TraitExpr", "TraitSubject", "SelfRef", "TypeNameRef", "TraitIs", "TraitAnd", "TraitOr", "TraitNot",
	"Literal", "UintLiteral", "Name", "Placeholder", "Attr", "QualifiedMember",
	"Param", "KwArg", "Call", "TypeApp", "Lambda", "Block",
	"Binary", "Unary", "Move", "Index", "ArrayLiteral", "MapEntry", "MapLiteral", "ExceptionCtor", "CatchExprArm", "TryCatchExpr", "UnsafeExpr", "Ternary", "YieldExpr",
	"LetStmt", "AssignStmt", "AugAssignStmt", "IfStmt", "ReturnStmt", "RaiseStmt", "ExprStmt", "ImportStmt",
	"AssertStmt", "TryStmt", "WhileStmt", "ForStmt", "ForCountStmt", "BreakStmt", "ContinueStmt", "BlockStmt", "UnsafeBlockStmt", "ThrowStmt", "RethrowStmt",
]
