# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
High-level Intermediate Representation (HIR).

Pipeline placement:
  AST (lang/stage0/ast.py) → HIR (this file) → MIR → SSA → LLVM/obj

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

from lang.driftc.core.function_id import FunctionRefId
from lang.driftc.core.span import Span

# Stable identifiers for bindings (locals/params). Populated by the typed
# checker; optional today to preserve existing HIR construction.
BindingId = int
# Stable identifiers for HIR nodes (used by typed side tables).
NodeId = int


# Base node kinds

class HNode:
	"""Base class for all HIR nodes."""
	node_id: NodeId = 0


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
	"""Binary operators preserved in HIR."""
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
	module_id: Optional[str] = None
	loc: Span = field(default_factory=Span)


class HTraitExpr(HExpr):
	"""Trait guard expression (type-level boolean)."""
	pass


class HTraitSubject(HExpr):
	"""Trait subject reference (Self or a type name)."""
	pass


@dataclass
class HSelfRef(HTraitSubject):
	loc: Span = field(default_factory=Span)


@dataclass
class HTypeNameRef(HTraitSubject):
	name: str
	# Optional module qualifier — set when the source-side
	# `s0.TypeNameRef` carried a `module_id` (e.g. a fully-qualified
	# trait subject `std.foo::Type is Trait`).  Preserved through
	# AST→HIR so the trait-resolution machinery can disambiguate
	# subjects across modules.  Defaults to None for unqualified
	# subjects (the only shape the current parser produces).
	module_id: Optional[str] = None
	loc: Span = field(default_factory=Span)


@dataclass
class HTraitIs(HTraitExpr):
	subject: object
	trait: object
	loc: Span = field(default_factory=Span)


@dataclass
class HTraitAnd(HTraitExpr):
	left: HTraitExpr
	right: HTraitExpr
	loc: Span = field(default_factory=Span)


@dataclass
class HTraitOr(HTraitExpr):
	left: HTraitExpr
	right: HTraitExpr
	loc: Span = field(default_factory=Span)


@dataclass
class HTraitNot(HTraitExpr):
	expr: HTraitExpr
	loc: Span = field(default_factory=Span)


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
	loc: Span = field(default_factory=Span)


@dataclass
class HLiteralString(HExpr):
	"""String literal (UTF-8 bytes; normalization happens later)."""
	value: str
	loc: Span = field(default_factory=Span)


@dataclass
class HLiteralBool(HExpr):
	"""Boolean literal."""
	value: bool
	loc: Span = field(default_factory=Span)


@dataclass
class HLiteralUint(HExpr):
	"""Unsigned integer literal (from `u` suffix, e.g. `42u`)."""
	value: int
	loc: Span = field(default_factory=Span)


@dataclass
class HLiteralUint64(HExpr):
	"""Unsigned 64-bit integer literal (from `u64` suffix, e.g. `42u64`)."""
	value: int
	loc: Span = field(default_factory=Span)


@dataclass
class HLiteralFloat(HExpr):
	"""
	Float literal (as parsed).

	Surface `Float` in lang v1 is IEEE-754 double precision (`f64` / LLVM `double`).
	"""
	value: float
	loc: Span = field(default_factory=Span)


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
class HParam(HNode):
	name: str
	type: Optional["HTypeExpr"] = None
	is_mutable: bool = False
	binding_id: Optional[BindingId] = None
	span: Span = field(default_factory=Span)


@dataclass
class HExplicitCapture(HNode):
	"""Explicit capture list entry (v0: root identifiers only).

	`kind="share"` captures carry a `share_value` HExpr — a synthesized
	`HCall(HQualifiedMember(Share-trait, "share"), [HBorrow(<local>)])`
	that lowers to `Share::share(&local)` inline at the lambda's
	env-construction site (NOT as a pre-hoisted let).  Inline lowering
	preserves user-visible left-to-right evaluation order:
	`foo(side_effect(), || captures(share x) => ...)` runs
	`side_effect()` before `Share::share(&x)`, matching the source.

	The user-spelled name and binding id remain on `name` / `binding_id`
	in the general case (no rewrite) so:
	  - The lambda body's `HVar(<name>, binding_id)` resolves through
	    the standard capture-slot mechanism into the env field that
	    holds the share-result.
	  - The checker's focused `E-CAPTURE-SHARE-NOT-SHARE` diagnostic
	    reads the user-spelled type/name directly off `binding_id` and
	    `name` — no auxiliary `desugar_origin_*` plumbing required.

	Exception — match-arm binder captures: when a lambda appears inside
	a `match` arm and explicitly captures one of the arm binders,
	`lower_match`'s alpha-rename pass (`_rename_expr` HLambda branch
	in `ast_to_hir.py`) rewrites the capture `name` to the internal
	`__match_binder_<N>_<source>` form and propagates the binder's
	persistent `binding_id` (allocated by `lower_match` BEFORE the arm
	block is lowered) to keep the capture, the body's `HVar`, and the
	synthesized `share_value`'s subject all referring to the same
	binding identity.  This rewrite is internal HIR plumbing only:
	any diagnostic that surfaces a capture name MUST route the string
	through `lang/driftc/checker/__init__.py::user_facing_binding_name`
	so the source spelling is recovered.  Pinned by
	`lang/tests/driver/test_match_binder_diagnostic_hygiene.py` and
	the negative match-arm `captures(share x)` cases in
	`lang/tests/driver/test_match_arm_lambda_capture.py`.
	"""
	name: str
	kind: str  # "ref", "ref_mut", "copy", "move", "share"
	binding_id: Optional[BindingId] = None
	span: Span = field(default_factory=Span)
	share_value: Optional["HExpr"] = None


@dataclass
class HLambda(HExpr):
	"""Lambda expression prior to capture inference/lowering."""
	params: list[HParam]
	ret_type: Optional["HTypeExpr"] = None
	body_expr: Optional[HExpr] = None
	body_block: Optional["HBlock"] = None
	explicit_captures: Optional[list[HExplicitCapture]] = None
	declared_nothrow: bool = False
	captures: list["HCapture"] = field(default_factory=list)
	span: Span = field(default_factory=Span)


@dataclass
class HCall(HExpr):
	"""Plain function call: fn(args...)."""
	fn: HExpr
	args: List[HExpr]
	kwargs: List["HKwArg"] = field(default_factory=list)
	type_args: Optional[list["HTypeExpr"]] = None
	callsite_id: Optional[int] = None
	origin: str | None = None
	loc: Span = field(default_factory=Span)


@dataclass
class HInvoke(HExpr):
	"""
	Call through a value expression: callee(args...).

	This represents invocation of a computed callable value (e.g., function
	pointer), as distinct from a direct named call (`HCall`).
	"""
	callee: HExpr
	args: List[HExpr]
	kwargs: List["HKwArg"] = field(default_factory=list)
	type_args: Optional[list["HTypeExpr"]] = None
	callsite_id: Optional[int] = None
	loc: Span = field(default_factory=Span)


@dataclass
class HFnPtrConst(HExpr):
	"""
	Function pointer constant (resolved symbol + call signature).

	This node is introduced by the type checker when a name expression is used
	as a function value (e.g., `val f: Fn(...) -> T = abs`).
	"""
	fn_ref: FunctionRefId
	call_sig: "CallSig"


@dataclass
class HTypeApp(HExpr):
	"""Explicit type application on a callable reference (no call)."""
	fn: HExpr
	type_args: list["HTypeExpr"]


@dataclass
class HCast(HExpr):
	"""Explicit cast: cast<T>(expr)."""
	target_type_expr: "HTypeExpr"
	value: HExpr
	loc: Span = field(default_factory=Span)


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
	type_args: Optional[list["HTypeExpr"]] = None
	callsite_id: Optional[int] = None
	origin: str | None = None
	loc: Span = field(default_factory=Span)


@dataclass
class HTernary(HExpr):
	"""Conditional expression: cond ? then_expr : else_expr."""
	cond: HExpr
	then_expr: HExpr
	else_expr: HExpr
	loc: Span = field(default_factory=Span)


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
	# Field names for named binders, parallel to `binders`. Only meaningful when
	# `pattern_arg_form == "named"`.
	block: "HBlock"
	result: Optional[HExpr]
	# Qualified constructor base type (e.g., `pkg.Mod::Ctor`), if specified.
	ctor_base: Optional["TypeExpr"] = None
	# Mutability flags for binders, parallel to `binders`.
	binder_is_mutable: Optional[list[bool]] = None
	# Pattern argument form:
	# - "bare": `Ctor` (allowed only for zero-field constructors)
	# - "paren": `Ctor()` (tag-only match, ignores payload)
	# - "positional": `Ctor(a, b)` (binds fields by index, exact arity)
	# - "named": `Ctor(x = a, y = b)` (binds a subset of fields by name)
	pattern_arg_form: str = "positional"
	# Field names for named binders, parallel to `binders`. Only meaningful when
	# `pattern_arg_form == "named"`.
	binder_fields: Optional[list[str]] = None
	# Normalized mapping from binders to field indices, parallel to `binders`.
	# Filled by the typed checker once the scrutinee type is known.
	binder_field_indices: list[int] = field(default_factory=list)
	# Persistent HIR binding identity for each arm binder, parallel to
	# `binders`.  Allocated by `lower_match` in `ast_to_hir.py` BEFORE
	# the arm block is lowered, so any nested lambda inside the arm
	# constructs its `HExplicitCapture` / `HVar` references with the
	# correct `binding_id` from the start (rather than relying on
	# post-lowering name-based repair, which fails for deferred
	# lambda type-check passes that run after the arm scope has been
	# popped).  The type checker uses these IDs at arm entry instead
	# of allocating fresh ones.  Empty list for HMatchArm values
	# constructed by reconstructors that don't run through
	# `lower_match` (those paths should set `binder_ids` parallel to
	# `binders` if they want first-class scope semantics).
	binder_ids: list[BindingId] = field(default_factory=list)
	loc: Span = field(default_factory=Span)


@dataclass
class HMatchExpr(HExpr):
	"""
	Expression-form `match` (expression-only in v1).

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
	# Slice 5 (slice 3 of impl): when set, this HField is a typed catch
	# binder field projection — `subject` resolves to a typed catch
	# binder (Error envelope) and `name` is a declared schema field of
	# `typed_proj_event_fqn`'s `pub error`.  The HIR→MIR lowering
	# emits a compiler-owned typed projection from the envelope's
	# params JSON (no public-API stringing through ErrorParamsView).
	# Set by type_checker.py when the typed-catch HField branch
	# fires; read by stage2/hir_to_mir.py at HField lowering.
	typed_proj_event_fqn: Optional[str] = None


@dataclass
class HQualifiedMember(HExpr):
	"""
	Type-level qualified member reference: `TypeRef::member`.

	This is a general expression form. MVP semantics (enforced by the typed
	checker) restrict it to variant constructors and require it to be called.

	`base_type_expr` is a parser/adapter type expression object used for type
	resolution (duck-typed on `name`, `args`, and optional module id fields).
	"""

	base_type_expr: object
	member: str
	loc: Span = field(default_factory=Span)


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
	# Compiler-synthesized escape hatch for Phase 1 fluent receiver chaining:
	# permit shared borrow of rvalues only when this flag is set.
	allow_rvalue: bool = False


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
	is_implicit: bool = False


@dataclass
class HCopy(HExpr):
	"""Explicit copy of a value: `copy <expr>`."""
	subject: HExpr
	loc: Span = field(default_factory=Span)


# Slice 7c-2 (ABI 14, 2026-05-06): `HDVInit` HIR node deleted.
# Its rewriter (`stage1/normalize.py:DVInitRewriter`) is retired
# along with the lowering target (`M.ConstructDV`) and the runtime
# `drift_dv_*` exports (Slice 7c-1).  User-source DV is rejected
# at the parser boundary (Slice 7a `E_DV_PUBLIC_REMOVED`).


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
	loc: Span = field(default_factory=Span)


@dataclass
class HBinary(HExpr):
	"""Binary operation (short-circuit not lowered yet)."""
	op: BinaryOp
	left: HExpr
	right: HExpr
	loc: Span = field(default_factory=Span)


@dataclass
class HArrayLiteral(HExpr):
	"""Array literal with element expressions."""
	elements: List[HExpr]


@dataclass
class HMapEntry(HNode):
	"""Single map literal entry."""
	key: HExpr
	value: HExpr


@dataclass
class HMapLiteral(HExpr):
	"""Map literal with key/value entries."""
	entries: List[HMapEntry]


# Statements

@dataclass
class HBlock(HStmt):
	"""Ordered list of statements; used for bodies of control-flow constructs."""
	statements: List[HStmt]


@dataclass
class HUnsafeBlock(HStmt):
	"""Unsafe block statement."""
	block: HBlock


@dataclass
class HUnsafeExpr(HExpr):
	"""Expression-form unsafe block. Yields the value of the inner expression."""
	body: HBlock
	result: HExpr
	loc: Span = field(default_factory=Span)


@dataclass
class HExprStmt(HStmt):
	"""Expression used as a statement (value discarded)."""
	expr: HExpr
	loc: Span = field(default_factory=Span)


@dataclass
class HLet(HStmt):
	"""
	Binding introduction (`val` / `var`).

	`is_mutable` tracks whether the binding was introduced via `var` (mutable)
	or `val` (immutable). Borrowing/type-checking uses this to reject `&mut` of
	immutable bindings in v1.
	"""
	name: str
	value: HExpr
	declared_type_expr: Optional[object] = None
	binding_id: Optional[BindingId] = None
	is_mutable: bool = False
	capture: bool = False
	capture_alias: Optional[str] = None
	loc: Span = field(default_factory=Span)


@dataclass
class HLocalConst(HStmt):
	"""
	Block-scope constant declaration.

	Semantics: compile-time literal alias with no storage. Each use site
	re-materializes the value as a MIR literal. The binding_id participates
	in normal block scoping/shadowing but never maps to a local slot.
	"""
	name: str
	declared_type_expr: object  # TypeExpr from parser
	value: HExpr  # the literal expression (kept for checker validation)
	binding_id: Optional[BindingId] = None
	loc: Span = field(default_factory=Span)


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
	loc: Span = field(default_factory=Span)


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
	loc: Span = field(default_factory=Span)


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
	loc: Span = field(default_factory=Span)


@dataclass
class HAssert(HStmt):
	cond: HExpr
	msg: Optional[HExpr] = None
	loc: Span = field(default_factory=Span)


__all__ = [
	"HNode", "HExpr", "HStmt",
	"UnaryOp", "BinaryOp",
	"HVar", "HTraitExpr", "HTraitSubject", "HSelfRef", "HTypeNameRef", "HTraitIs", "HTraitAnd", "HTraitOr", "HTraitNot",
	"HLiteralInt", "HLiteralUint", "HLiteralUint64", "HLiteralString", "HLiteralBool", "HLiteralFloat",
	"HFString", "HFStringHole",
	"HParam", "HLambda",
	"HCall", "HInvoke", "HFnPtrConst", "HMethodCall", "HTernary", "HResultOk",
	"HTypeApp", "HCast",
	"HPlaceExpr", "HPlaceProj", "HPlaceField", "HPlaceIndex", "HPlaceDeref",
	"HField", "HQualifiedMember", "HIndex", "HBorrow",
	"HMove", "HCopy",
	"HKwArg",
	"HExceptionInit",
	"HUnary", "HBinary", "HArrayLiteral", "HMapEntry", "HMapLiteral",
	"HBlock", "HUnsafeBlock", "HExprStmt", "HLet", "HAssign", "HAugAssign", "HIf", "HLoop",
	"HBreak", "HContinue", "HReturn", "HAssert",
	"HThrow", "HRethrow",
	"HTry", "HCatchArm",
	"HTryExpr", "HTryExprArm",
	"HMatchExpr", "HMatchArm",
]
