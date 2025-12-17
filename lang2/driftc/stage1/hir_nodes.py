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

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Optional

from lang2.driftc.core.span import Span

# Stable identifiers for bindings (locals/params). Populated by the typed
# checker; optional today to preserve existing HIR construction.
BindingId = int


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
	DEREF = auto()    # pointer dereference: *p (MVP: refs only)


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
	binding_id: Optional[BindingId] = None


@dataclass(frozen=True)
class HPlaceProj(HNode):
	"""
	Projection inside an addressable place.

	HIR distinguishes between:
	  - value expressions (rvalues), and
	  - *places* (lvalues): an addressable storage location plus projections.

	A place is represented as `HPlaceExpr(base, projections)`, where `base` is a
	binding (`HVar`) and projections describe how to reach a sub-location.

	This structure is intentionally minimal and purely syntactic. Semantic rules
	(like "index must be Int") are enforced by the typed checker and borrow
	checker, not here.
	"""


@dataclass(frozen=True)
class HPlaceField(HPlaceProj):
	"""Field projection: `.name`."""

	name: str


@dataclass(frozen=True)
class HPlaceIndex(HPlaceProj):
	"""Index projection: `[index]` (index is a value expression)."""

	index: "HExpr"


@dataclass(frozen=True)
class HPlaceDeref(HPlaceProj):
	"""
	Deref projection: `*p`.

	We model deref as a projection so nested places like `(*p).x` are represented
	as a single place with projections: `Deref`, `Field("x")`.
	"""


@dataclass
class HPlaceExpr(HExpr):
	"""
	Addressable place expression (lvalue).

	This is the stage1→stage2 *boundary* representation for lvalues: later phases
	should not "re-derive" place-ness from arbitrary expression trees. Instead,
	normalization converts assign targets and borrow operands into this canonical
	shape.

	Example source shapes and their canonical places:

	  x                -> base=x, projections=[]
	  x.field          -> base=x, projections=[Field("field")]
	  x[i]             -> base=x, projections=[Index(i)]
	  (*p).field[i]    -> base=p, projections=[Deref, Field("field"), Index(i)]

	Notes:
	  - `base` is always a binding (`HVar`), not an arbitrary expression. This is
	    deliberate: borrowing an rvalue is handled by temporary materialization.
	  - `loc` is a best-effort span for diagnostics; many HIR nodes do not yet
	    carry precise spans, so `Span()` acts as an explicit "unknown location"
	    sentinel.
	"""

	base: HVar
	projections: list[HPlaceProj] = field(default_factory=list)
	loc: Span = field(default_factory=Span)


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
class HLiteralFloat(HExpr):
	"""
	Float literal (as parsed).

	Surface `Float` in lang2 v1 is IEEE-754 double precision (`f64` / LLVM `double`).
	"""
	value: float


@dataclass
class HFStringHole(HNode):
	"""
	Single `{expr[:spec]}` hole inside an f-string.

	We keep the hole's expression as HIR (not preformatted) so later stages can:
	- type-check the hole expression,
	- validate/interpret the `spec` string, and
	- lower the formatting operation appropriately.

	MVP: `spec` is opaque text (no nested `{}`), and only a small set of hole
	value types are supported (Bool/Int/Uint/Float/String).
	"""
	expr: HExpr
	spec: str = ""
	loc: Span = field(default_factory=Span)


@dataclass
class HFString(HExpr):
	"""
	f-string literal: `f"..."`.

	Representation matches the lowering contract: `len(parts) == len(holes) + 1`.
	Text parts are already unescaped (normal string escape rules + `{{`/`}}`).

	Lowering: stage2 expands this into explicit String concatenations and runtime
	formatting calls. We *do not* desugar in stage1 because we want to use type
	information to decide how each hole expression is formatted.
	"""
	parts: list[str]
	holes: list[HFStringHole]
	loc: Span = field(default_factory=Span)


@dataclass
class HCall(HExpr):
	"""Plain function call: fn(args...)."""
	fn: HExpr
	args: List[HExpr]
	kwargs: List["HKwArg"] = field(default_factory=list)


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
	kwargs: List["HKwArg"] = field(default_factory=list)


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
class HTryExprArm:
	"""
	Single arm in an expression-form try/catch.

	The block must ultimately produce a value (checked later); control-flow
	statements are not allowed in expression-form catch arms.
	"""

	event_fqn: Optional[str]
	binder: Optional[str]
	block: "HBlock"
	result: Optional[HExpr]
	loc: Span = field(default_factory=Span)


@dataclass
class HTryExpr(HExpr):
	"""
	Expression-form try/catch.

	attempt: must be a call expression in v1.
	arms: list of catch arms that each produce a value of the same type as the attempt.
	"""

	attempt: HExpr
	arms: List[HTryExprArm]
	loc: Span = field(default_factory=Span)


@dataclass
class HMatchArm(HNode):
	"""
	Single arm in a `match` expression.

	MVP patterns:
	- constructor pattern: `Ctor(b1, b2, ...)`
	- zero-field constructor: `Ctor`
	- default arm: `default`

	Arm bodies are blocks. A value-producing arm has `result` set to the final
	expression; the remaining statements are in `block`.
	"""

	ctor: Optional[str]  # None means default arm
	binders: list[str]
	block: "HBlock"
	result: Optional[HExpr]
	loc: Span = field(default_factory=Span)


@dataclass
class HMatchExpr(HExpr):
	"""
	Expression-form `match` (expression-only in MVP).

	The typed checker enforces:
	- duplicate constructors are rejected,
	- `default` (if present) is last and appears at most once,
	- without `default`, matches must be exhaustive for known variants, and
	- when the match result is used, all arms must yield values of the same type.
	"""

	scrutinee: HExpr
	arms: list[HMatchArm]
	loc: Span = field(default_factory=Span)


@dataclass
class HThrow(HStmt):
	"""Throw an error/exception value. Semantically returns Err from this function."""
	value: HExpr


@dataclass
class HRethrow(HStmt):
	"""Rethrow the currently caught Error; only valid inside a catch arm."""
	loc: Span = field(default_factory=Span)


@dataclass
class HTry(HStmt):
	"""
	Statement-level try/catch with multiple arms.

	  try { body }
	  catch EventFqn(e) { handler_for_event }
	  catch (e) { catch_all_with_binder }
	  catch { catch_all_no_binder }

	Arms are evaluated in source order; the first matching event_fqn (or the
	catch-all) is taken. If no arm matches, the error is rethrown.
	"""
	body: "HBlock"
	catches: List["HCatchArm"]


@dataclass
class HCatchArm(HStmt):
	"""
	Single catch arm inside an HTry.

	event_fqn: canonical FQN of the exception/event to match (None = catch-all)
	binder: local name to bind the Error to inside the arm (None = no binder)
	block: handler body
	"""

	event_fqn: Optional[str]
	binder: Optional[str]
	block: "HBlock"
	loc: Span = field(default_factory=Span)


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
class HBorrow(HExpr):
	"""Borrow an lvalue: &subject or &mut subject."""

	# Canonical place operand (stage1→stage2 boundary).
	#
	# Note: the compiler still permits ill-formed programs to be represented in
	# early HIR for diagnostics, but well-formed pipelines must run
	# `normalize_hir` which canonicalizes place contexts to `HPlaceExpr`.
	subject: "HPlaceExpr"
	is_mut: bool


@dataclass
class HMove(HExpr):
	"""
	Explicit move of a value out of an addressable place: `move <place>`.

	The operand is required to be an addressable place. Stage1 normalization
	canonicalizes it to `HPlaceExpr` so later phases can operate on a single
	representation of places.
	"""
	# Canonical place operand (stage1→stage2 boundary).
	subject: "HPlaceExpr"
	loc: Span = field(default_factory=Span)


@dataclass
class HDVInit(HExpr):
	"""Desugared DiagnosticValue constructor call."""
	dv_type_name: str
	args: List[HExpr]


@dataclass
class HExceptionInit(HExpr):
	"""
	Structured exception/event constructor.

	event_fqn: str  # canonical fully-qualified name (<module>.<submodules>:<event>)
	pos_args: positional arguments in source order (already lowered to HIR)
	kw_args: keyword arguments in source order (already lowered to HIR)

	Semantics note:
	  The mapping of (positional + keyword) arguments to declared exception
	  fields is validated once exception schemas are known (checker / lowering).
	"""
	event_fqn: str
	pos_args: List[HExpr]
	kw_args: List["HKwArg"]
	loc: Span = field(default_factory=Span)


@dataclass
class HKwArg:
	"""
	Keyword argument in HIR (used for exception constructors and call lowering).

	We preserve a dedicated `loc` for the keyword name token so diagnostics can
	highlight `name =` precisely instead of pointing at the value expression.
	"""

	name: str
	value: HExpr
	loc: Span = field(default_factory=Span)


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
	"""
	Binding introduction (`val` / `var`).

	`is_mutable` tracks whether the binding was introduced via `var` (mutable)
	or `val` (immutable). Borrowing/type-checking uses this to reject `&mut` of
	immutable bindings in MVP.
	"""
	name: str
	value: HExpr
	declared_type_expr: Optional[object] = None
	binding_id: Optional[BindingId] = None
	is_mutable: bool = False


@dataclass
class HAssign(HStmt):
	"""
	Assignment to an addressable place.

	Stage1 normalization canonicalizes assignment targets to `HPlaceExpr` so
	stage2 lowering does not need to re-derive lvalue structure from arbitrary
	expression trees.
	"""
	# Canonical assignment target (stage1→stage2 boundary).
	target: "HPlaceExpr"
	value: HExpr


@dataclass
class HAugAssign(HStmt):
	"""
	Augmented assignment to an addressable place.

	Surface syntax (MVP):
	  <place> += <expr>

	Why this exists:
	- Desugaring `x += y` to `x = x + y` too early duplicates evaluation of `x`,
	  which is incorrect for complex places like `arr[i]` (index expression
	  evaluated twice).
	- Instead, later lowering evaluates the address once and performs a
	  read-modify-write sequence: `addr = &place; old = load addr; new = old + y;
	  store addr, new`.

	Stage1 normalization canonicalizes `target` to `HPlaceExpr` so stage2 does not
	re-derive lvalue structure from arbitrary expression trees.
	"""

	target: "HPlaceExpr"
	op: str  # currently "+="
	value: HExpr
	loc: Span = field(default_factory=Span)


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
	"HVar", "HLiteralInt", "HLiteralString", "HLiteralBool", "HLiteralFloat",
	"HFString", "HFStringHole",
	"HCall", "HMethodCall", "HTernary", "HTryResult", "HResultOk",
	"HPlaceExpr", "HPlaceProj", "HPlaceField", "HPlaceIndex", "HPlaceDeref",
	"HField", "HIndex", "HBorrow", "HDVInit",
	"HMove",
	"HKwArg",
	"HExceptionInit",
	"HUnary", "HBinary", "HArrayLiteral",
	"HBlock", "HExprStmt", "HLet", "HAssign", "HAugAssign", "HIf", "HLoop",
	"HBreak", "HContinue", "HReturn",
	"HThrow", "HRethrow",
	"HTry", "HCatchArm",
	"HTryExpr", "HTryExprArm",
	"HMatchExpr", "HMatchArm",
]
