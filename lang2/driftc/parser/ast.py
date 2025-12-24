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
    is_pub: bool = False
    require: Optional["RequireClause"] = None
    type_params: List[str] = field(default_factory=list)
    type_param_locs: List["Located"] = field(default_factory=list)


@dataclass
class ExceptionArg:
    name: str
    type_expr: "TypeExpr"


@dataclass
class ExceptionDef:
    name: str
    args: List[ExceptionArg]
    loc: "Located"
    is_pub: bool = False
    domain: Optional[str] = None


@dataclass(frozen=True)
class Located:
    line: int
    column: int


@dataclass
class TypeExpr:
    name: str
    args: List["TypeExpr"] = field(default_factory=list)
    # Optional module alias qualifier for type names in surface annotations.
    #
    # Example:
    #   import lib as x
    #   val p: x.Point = ...
    #
    # The compiler resolves `module_alias` using per-file import bindings and
    # then rewrites the type reference to carry a canonical `module_id`.
    #
    # Note: once nominal type identity is module-scoped, the compiler must not
    # discard module qualification. Instead it records the resolved module id in
    # `module_id` so later phases can resolve `(module_id, name)` deterministically.
    module_alias: Optional[str] = None
    # Canonical resolved module id for this type reference (best-effort).
    #
    # For unqualified type names this is usually the current module id, but it
    # can also refer to an imported type (`from other.mod import Point`).
    #
    # Builtins use module_id=None.
    module_id: Optional[str] = None
    # Source location of the type expression (best-effort).
    #
    # This enables source-anchored diagnostics for type-level module qualifiers
    # (e.g. `x.Point`) and internal-only type usage rejections.
    loc: Optional[Located] = None


@dataclass
class Param:
	name: str
	type_expr: TypeExpr | None
	non_escaping: bool = False


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
class AugAssignStmt(Stmt):
	"""
	Augmented assignment statement.

	MVP supports:
	`+=`, `-=`, `*=`, `/=`, `%=`, `&=`, `|=`, `^=`, `<<=`, `>>=`.

	We keep augmented assignment distinct from plain assignment in the AST so
	lowering can preserve correct evaluation semantics for complex lvalues.
	In particular, `a[i] += 1` must evaluate `a` and `i` once (compute address,
	load, add, store), not duplicate the index expression by desugaring to
	`a[i] = a[i] + 1` too early.
	"""

	loc: Located
	target: "Expr"
	op: str
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
class RethrowStmt(Stmt):
    loc: Located


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
	type_params: List[str]
	params: Sequence[Param]
	return_type: TypeExpr
	body: Block
	loc: Located
	is_pub: bool = False
	type_param_locs: List[Located] = field(default_factory=list)
	require: Optional["RequireClause"] = None
	is_method: bool = False
	self_mode: str | None = None  # "value", "ref", "ref_mut"
	impl_target: TypeExpr | None = None


class Expr:
    loc: Located


@dataclass
class RequireClause:
    expr: "TraitExpr"
    loc: Located


class TraitExpr(Expr):
    """Boolean trait requirement expression (type-level)."""
    pass


@dataclass
class TraitIs(TraitExpr):
    loc: Located
    subject: str
    trait: TypeExpr


@dataclass
class TraitAnd(TraitExpr):
    loc: Located
    left: TraitExpr
    right: TraitExpr


@dataclass
class TraitOr(TraitExpr):
    loc: Located
    left: TraitExpr
    right: TraitExpr


@dataclass
class TraitNot(TraitExpr):
    loc: Located
    expr: TraitExpr


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
    # Attribute operator used in source:
    # - "." for normal member access (`x.field`)
    # - "->" for member-through-reference access (`p->field`)
    #
    # The parser adapter lowers `->` into an explicit deref + normal member access
    # so later stages do not need to care about this flag (until we formalize
    # richer method receiver semantics).
    op: str = "."


@dataclass
class QualifiedMember(Expr):
	"""
	Type-level qualified member reference: `TypeRef::member`.

	This is intentionally a general expression node (not ctor-only). The typed
	checker determines which kinds of members are valid. In MVP, only variant
	constructor names are supported, and the qualified member must be called.

	`base_type` is a parser `TypeExpr` so later phases can resolve it into a
	concrete `TypeId` (including generic instantiation) without re-parsing.
	"""

	loc: Located
	base_type: TypeExpr
	member: str


@dataclass
class KwArg:
    """
    Keyword argument `name = value`.

    Note: we carry `loc` so later stages can emit precise diagnostics for
    unknown/duplicate keyword names without guessing from the value span.
    """
    name: str
    value: Expr
    loc: Located


@dataclass
class Call(Expr):
    loc: Located
    func: Expr
    args: List[Expr]
    kwargs: List[KwArg]
    type_args: List[TypeExpr] | None = None


@dataclass
class TypeApp(Expr):
    loc: Located
    func: Expr
    type_args: List[TypeExpr]


@dataclass
class Lambda(Expr):
    loc: Located
    params: List[Param]
    ret_type: TypeExpr | None
    body_expr: Expr | None
    body_block: Block | None


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
class FStringHole:
	"""
	Single `{expr[:spec]}` hole inside an f-string.

	Notes:
	- `expr` is a normal Drift expression AST.
	- `spec` is a compile-time substring (MVP: opaque text, no nested `{}`).
	- `loc` points at the start of the hole (the `{`).
	"""
	loc: Located
	expr: Expr
	spec: str = ""


@dataclass
class FString(Expr):
	"""
	f-string expression: `f"..."` with literal parts and `{...}` holes.

	Representation matches the lowering contract: `parts.len == holes.len + 1`.
	"""
	loc: Located
	parts: List[str]
	holes: List[FStringHole]


@dataclass
class Program:
	functions: List[FunctionDef] = field(default_factory=list)
	consts: List["ConstDef"] = field(default_factory=list)
	implements: List["ImplementDef"] = field(default_factory=list)
	traits: List["TraitDef"] = field(default_factory=list)
	# Module directives are tracked separately from general statements so later
	# compilation stages can ignore them without having to add stage0/HIR nodes
	# for import/export syntax.
	imports: List["ImportStmt"] = field(default_factory=list)
	exports: List["ExportStmt"] = field(default_factory=list)
	statements: List[Stmt] = field(default_factory=list)
	structs: List[StructDef] = field(default_factory=list)
	exceptions: List[ExceptionDef] = field(default_factory=list)
	variants: List["VariantDef"] = field(default_factory=list)
	module: Optional[str] = None
	# Location of the `module ...` declaration (when present).
	#
	# This is used to produce pinned diagnostics for module-id mismatch and other
	# module-directive validation rules. When absent, the file implicitly declares
	# its inferred module id (driver-level rule when module roots are provided).
	module_loc: Optional[Located] = None


@dataclass
class ConstDef:
	"""
	Top-level constant definition.

	Syntax:
	  const NAME: Type = expr;

	MVP constraints are enforced in the typed checker:
	- `expr` must be a compile-time literal (or unary +/- applied to a numeric literal),
	- the declared type must match exactly.
	"""

	loc: Located
	name: str
	type_expr: TypeExpr
	value: "Expr"
	is_pub: bool = False


@dataclass
class VariantField:
	"""
	Single constructor field in a `variant` arm.

	Example:
	  Some(value: T)

	`name` is the declared field name; patterns are positional-only in MVP, but
	we keep names for clarity and future evolution.
	"""

	name: str
	type_expr: TypeExpr


@dataclass
class VariantArm:
	"""Single arm (constructor) inside a `variant` definition."""

	name: str
	fields: List[VariantField]
	loc: Located


@dataclass
class VariantDef:
	"""
	Top-level variant (tagged union / sum type) definition.

	MVP supports generic type parameters, e.g.:
	  variant Optional<T> { Some(value: T), None }
	"""

	name: str
	type_params: List[str]
	arms: List[VariantArm]
	loc: Located
	is_pub: bool = False


@dataclass
class TraitMethodSig:
	name: str
	params: Sequence[Param]
	return_type: TypeExpr
	loc: Located


@dataclass
class TraitDef:
	name: str
	methods: List[TraitMethodSig]
	loc: Located
	is_pub: bool = False
	require: Optional["RequireClause"] = None


@dataclass
class ImplementDef:
    target: TypeExpr
    loc: Located
    is_pub: bool = False
    type_params: List[str] = field(default_factory=list)
    type_param_locs: List[Located] = field(default_factory=list)
    trait: Optional[TypeExpr] = None
    require: Optional["RequireClause"] = None
    methods: List[FunctionDef] = field(default_factory=list)


@dataclass
class ImportStmt(Stmt):
    loc: Located
    path: List[str]
    alias: Optional[str] = None


@dataclass
class ExportItem:
	loc: Located


@dataclass
class ExportName(ExportItem):
	name: str


@dataclass
class ExportModuleStar(ExportItem):
	module_path: List[str]


@dataclass
class ExportStmt(Stmt):
	"""
	Explicit export list for a module (MVP).

	Syntax:
	  export { foo, Bar, Baz }
	  export { other.module.* }
	"""

	loc: Located
	items: List[ExportItem]


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
    """
    Semantic node representing an exception constructor application.

    This is *not* a general expression in the language: it only appears under a
    `throw` statement (see grammar). We still represent it as an Expr node so it
    can share parsing infrastructure with calls and literals.

    Args may be positional or keyword. Positional arguments must precede keyword
    arguments (the parser enforces this).
    """

    loc: Located
    name: str
    args: List[Expr]
    kwargs: List[KwArg]


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


@dataclass
class MatchArm:
	"""
	Single match arm.

	Patterns are:
	- constructor pattern: `Ctor(b1, b2, ...)`
	- zero-field constructor: `Ctor`
	- default: `default`

	Arm bodies are blocks; `value_block` is represented as a trailing ExprStmt.
	"""

	loc: Located
	ctor: Optional[str]  # None means default arm
	# Pattern argument form:
	# - "bare": `Ctor` (allowed only for zero-field constructors)
	# - "paren": `Ctor()` (tag-only match, ignores payload)
	# - "positional": `Ctor(a, b)` (binds fields by index, exact arity)
	# - "named": `Ctor(x = a, y = b)` (binds a subset of fields by name)
	pattern_arg_form: str
	binders: List[str]
	block: Block
	# Field names for named binders, parallel to `binders`. Only meaningful when
	# `pattern_arg_form == "named"`.
	binder_fields: Optional[List[str]] = None


@dataclass
class MatchExpr(Expr):
	"""Expression-form match."""

	loc: Located
	scrutinee: Expr
	arms: List[MatchArm]
