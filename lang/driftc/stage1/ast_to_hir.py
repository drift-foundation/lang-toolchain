# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
AST → HIR lowering (sugar removal entry point).

Pipeline placement:
  AST (lang/stage0/ast.py) → HIR (lang/stage1/hir_nodes.py) → MIR → SSA → LLVM/obj

This pass takes the parsed AST and produces the sugar-free HIR defined in
`lang/stage1/hir_nodes.py`. It currently lowers expressions/statements:
  - literals, vars, unary/binary ops, field/index
  - let/assign/if/while/for/return/break/continue/expr-stmt
  - plain/method calls, exception ctors, ternary, try/throw
Remaining sugar (raise/rethrow, TryCatchExpr) still fails loudly so it can be
filled in incrementally.

Entry points (stage API):
  - lower_expr: lower a single expression to HIR
  - lower_stmt: lower a single statement to HIR
  - lower_block: lower a list of statements into an HBlock
"""

from __future__ import annotations

from typing import List, Optional

# Import stage0 AST via package API to keep stage layering explicit.
from lang.driftc.stage0 import ast
from . import hir_nodes as H
from .macro_expander import expand_macro_call
from lang.driftc.core.span import Span


class AstToHIR:
	"""
	AST → HIR lowering (sugar removal happens here).

	Entry points (stage API):
	  - lower_expr: lower a single expression to HIR
	  - lower_stmt: lower a single statement to HIR
	  - lower_block: lower a list of statements into an HBlock
	Helper visitors are prefixed with an underscore; anything without a leading
	underscore is intended for callers of this stage.
	"""

	def __init__(self):
		# Simple counter for internal temporaries (e.g., for-loop bindings).
		self._temp_counter = 0
		# Binding id allocator and scope stack for locals/params.
		self._next_binding_id = 1
		self._scope_stack: list[dict[str, H.BindingId]] = [dict()]
		# Current module name for building canonical FQNs; set by parser adapter.
		self._module_name: str | None = None
		# Import aliases for resolving exception/catch event FQNs.
		self._module_aliases: dict[str, str] = {}
		# Optional method-body context used for implicit `self` member lookup.
		#
		# The parser adapter sets this when lowering methods declared inside an
		# `implement Type { ... }` block. Free functions leave it disabled.
		#
		# This is a *pure front-end name resolution convenience* (specified in the
		# language spec): unknown identifiers inside a method body may resolve to
		# fields/methods on `self`.
		self._implicit_self_stack: list[dict[str, object]] = []
		# Counter for generating unique, compiler-internal binder names in pattern
		# matches (e.g., `match x { Some(v) => { ... } }`).
		#
		# HIR is a flat namespace (no lexical scopes for locals), while match-arm
		# binders are block-scoped by language semantics. To preserve those
		# semantics in later pipeline stages (SSA/codegen), we alpha-rename match
		# binders to unique internal names during lowering.
		self._match_binder_counter = 0

	def _push_implicit_self(
		self,
		*,
		self_name: str,
		self_mode: str,
		field_names: set[str],
		method_names: set[str],
		module_function_names: set[str],
	) -> None:
		"""
		Enable implicit `self` member lookup while lowering a method body.

		`self_mode` is the receiver mode spelled in the method signature:
		  - "value"   for `self: T`
		  - "ref"     for `self: &T`
		  - "ref_mut" for `self: &mut T`

		We keep this context in a stack so nested lowering helpers (blocks/arms)
		can consult it without threading parameters everywhere.
		"""
		self._implicit_self_stack.append(
			{
				"self_name": str(self_name),
				"self_mode": self_mode,
				"fields": set(field_names),
				"methods": set(method_names),
				"module_funcs": set(module_function_names),
			}
		)

	def _pop_implicit_self(self) -> None:
		"""Disable implicit `self` member lookup (end of method body lowering)."""
		self._implicit_self_stack.pop()

	def _implicit_self_ctx(self) -> dict[str, object] | None:
		return self._implicit_self_stack[-1] if self._implicit_self_stack else None

	def _fresh_temp(self, prefix: str = "__tmp") -> str:
		"""Allocate a unique temporary name with a given prefix."""
		self._temp_counter += 1
		return f"{prefix}{self._temp_counter}"

	def _push_scope(self) -> None:
		self._scope_stack.append(dict())

	def _pop_scope(self) -> None:
		self._scope_stack.pop()

	def _alloc_binding(self, name: str) -> H.BindingId:
		bid = self._next_binding_id
		self._next_binding_id += 1
		self._scope_stack[-1][name] = bid
		return bid

	def _lookup_binding(self, name: str) -> H.BindingId | None:
		for scope in reversed(self._scope_stack):
			if name in scope:
				return scope[name]
		return None

	def lower_expr(self, expr: ast.Expr) -> H.HExpr:
		"""
		Dispatch an AST expression to a per-type visitor.

		Fail-loud behavior is intentional: new AST node types should add
		a visitor rather than being silently ignored.

		Public stage API: callers should use lower_expr/stmt/block; helper
		visitors are private (_visit_*).
		"""
		method = getattr(self, f"_visit_expr_{type(expr).__name__}", None)
		if method is None:
			raise NotImplementedError(f"No HIR lowering for expr type {type(expr).__name__}")
		return method(expr)

	def lower_stmt(self, stmt: ast.Stmt) -> H.HStmt:
		"""
		Dispatch an AST statement to a per-type visitor.

		Fail-loud behavior is intentional: new AST node types should add
		a visitor rather than being silently ignored.

		Public stage API: callers should use lower_expr/stmt/block; helper
		visitors are private (_visit_*).
		"""
		method = getattr(self, f"_visit_stmt_{type(stmt).__name__}", None)
		if method is None:
			raise NotImplementedError(f"No HIR lowering for stmt type {type(stmt).__name__}")
		return method(stmt)

	def lower_block(self, stmts: List[ast.Stmt]) -> H.HBlock:
		"""Lower a list of AST statements into an HIR block with scoped bindings."""
		self._push_scope()
		try:
			return H.HBlock(statements=[self.lower_stmt(s) for s in stmts])
		finally:
			self._pop_scope()

	def lower_function_block(self, stmts: List[ast.Stmt], *, param_names: List[str]) -> H.HBlock:
		"""
		Lower a function body block, seeding parameter bindings into scope.

		The HIR `HVar(binding_id=...)` mechanism is used for locals *and* params so
		that later phases (place canonicalization, borrow checking, MIR lowering)
		can reason about addressable storage uniformly.

		This is intentionally a separate entry point from `lower_block`: stage1
		unit tests can keep calling `lower_block` for statement-only snippets,
		while the real compiler pipeline calls `lower_function_block` so parameter
		names are resolvable as bindings (including `self` in methods).
		"""
		self._push_scope()
		try:
			for pname in param_names:
				# Allocate a stable binding id for each parameter name in scope.
				# If a parameter name repeats, keep the first (later stages will
				# diagnose duplicate parameters once signatures are validated).
				if pname and self._lookup_binding(pname) is None:
					self._alloc_binding(pname)
			block = H.HBlock(statements=[self.lower_stmt(s) for s in stmts])
			param_binding_ids: dict[str, int] = {}
			for pname in param_names:
				if pname:
					bid = self._lookup_binding(pname)
					if bid is not None:
						param_binding_ids[pname] = bid
			block.param_binding_ids = param_binding_ids
			return block
		finally:
			self._pop_scope()

	# --- minimal implemented handlers (trivial cases only) ---

	def _visit_expr_Name(self, expr: ast.Name) -> H.HExpr:
		"""
		Lower an identifier reference.

		Resolution policy: local bindings only. Unqualified identifiers never
		resolve to `self` members; callers must use explicit receivers.
		"""
		bid = self._lookup_binding(expr.ident)
		if bid is not None:
			return H.HVar(name=expr.ident, binding_id=bid, loc=self._as_span(getattr(expr, "loc", None)))
		# Fallback: unresolved name remains an HVar. The typed checker is the
		# authority for rejecting unknown variables with a source span.
		return H.HVar(name=expr.ident, binding_id=None, loc=self._as_span(getattr(expr, "loc", None)))

	def _visit_expr_TraitIs(self, expr: ast.TraitIs) -> H.HExpr:
		def _lower_trait_subject(subject: object) -> object:
			if isinstance(subject, ast.SelfRef):
				return H.HSelfRef(loc=self._as_span(getattr(subject, "loc", None)))
			if isinstance(subject, ast.TypeNameRef):
				return H.HTypeNameRef(name=subject.name, loc=self._as_span(getattr(subject, "loc", None)))
			return subject
		return H.HTraitIs(
			subject=_lower_trait_subject(expr.subject),
			trait=expr.trait,
			loc=self._as_span(getattr(expr, "loc", None)),
		)

	def _visit_expr_TraitAnd(self, expr: ast.TraitAnd) -> H.HExpr:
		return H.HTraitAnd(
			left=self.lower_expr(expr.left),
			right=self.lower_expr(expr.right),
			loc=self._as_span(getattr(expr, "loc", None)),
		)

	def _visit_expr_TraitOr(self, expr: ast.TraitOr) -> H.HExpr:
		return H.HTraitOr(
			left=self.lower_expr(expr.left),
			right=self.lower_expr(expr.right),
			loc=self._as_span(getattr(expr, "loc", None)),
		)

	def _visit_expr_TraitNot(self, expr: ast.TraitNot) -> H.HExpr:
		return H.HTraitNot(
			expr=self.lower_expr(expr.expr),
			loc=self._as_span(getattr(expr, "loc", None)),
		)

	def _visit_expr_Literal(self, expr: ast.Literal) -> H.HExpr:
		"""
		Map a stage0 literal to the appropriate HIR literal node.

		This is intentionally strict for production correctness: if the parser
		starts producing new literal value kinds, we want a loud failure here so
		we don't silently mis-lower the program.
		"""
		loc = self._as_span(getattr(expr, "loc", None))
		if isinstance(expr.value, bool):
			return H.HLiteralBool(value=bool(expr.value), loc=loc)
		if isinstance(expr.value, int):
			return H.HLiteralInt(value=int(expr.value), loc=loc)
		if isinstance(expr.value, float):
			return H.HLiteralFloat(value=float(expr.value), loc=loc)
		if isinstance(expr.value, str):
			return H.HLiteralString(value=str(expr.value), loc=loc)
		raise NotImplementedError(f"Literal of unsupported type: {type(expr.value).__name__}")

	def _as_span(self, loc: object | None) -> Span:
		"""
		Best-effort location normalization for HIR nodes.

		stage0 nodes typically carry a structured `Span`, but some unit tests and
		legacy call sites may still provide `None` or a parser-specific location.
		"""
		if isinstance(loc, Span):
			return loc
		return Span.from_loc(loc)

	def _visit_expr_FString(self, expr: ast.FString) -> H.HExpr:
		"""
		Lower an f-string AST node into an explicit HIR node (`HFString`).

		We keep f-strings as a dedicated HIR node until stage2 so we can use type
		information to format hole expressions (and validate format specs) while
		still guaranteeing left-to-right evaluation order.
		"""
		holes = [
			H.HFStringHole(
				expr=self.lower_expr(h.expr),
				spec=h.spec,
				loc=self._as_span(getattr(h, "loc", None)),
			)
			for h in expr.holes
		]
		return H.HFString(
			parts=list(expr.parts),
			holes=holes,
			loc=self._as_span(getattr(expr, "loc", None)),
		)

	def _visit_expr_Cast(self, expr: ast.Cast) -> H.HExpr:
		return H.HCast(
			target_type_expr=expr.target_type,
			value=self.lower_expr(expr.expr),
			loc=self._as_span(getattr(expr, "loc", None)),
		)

	def _visit_stmt_LetStmt(self, stmt: ast.LetStmt) -> H.HStmt:
		"""
		Binding introduction (`val` / `var`).

		Stage0 preserves whether the binding is mutable so later phases can:
		  - reject `&mut` borrows of immutable bindings, and
		  - lower address-taken locals to real storage when needed.
		"""
		bid = self._alloc_binding(stmt.name)
		return H.HLet(
			name=stmt.name,
			value=self.lower_expr(stmt.value),
			declared_type_expr=getattr(stmt, "type_expr", None),
			binding_id=bid,
			is_mutable=bool(getattr(stmt, "mutable", False)),
			capture=bool(getattr(stmt, "capture", False)),
			capture_alias=getattr(stmt, "capture_alias", None),
			loc=self._as_span(getattr(stmt, "loc", None)),
		)

	def _visit_stmt_ReturnStmt(self, stmt: ast.ReturnStmt) -> H.HStmt:
		"""Return with optional value."""
		val = self.lower_expr(stmt.value) if stmt.value is not None else None
		return H.HReturn(value=val, loc=self._as_span(getattr(stmt, "loc", None)))

	def _visit_stmt_AssertStmt(self, stmt: ast.AssertStmt) -> H.HStmt:
		cond = self.lower_expr(stmt.cond)
		msg = self.lower_expr(stmt.msg) if stmt.msg is not None else None
		return H.HAssert(cond=cond, msg=msg, loc=self._as_span(stmt.loc))

	def _visit_stmt_ExprStmt(self, stmt: ast.ExprStmt) -> H.HStmt:
		"""Expression as statement (value discarded)."""
		if isinstance(stmt.expr, ast.MatchExpr):
			return H.HExprStmt(expr=self._lower_match_expr(stmt.expr, value_context=False), loc=self._as_span(getattr(stmt, "loc", None)))
		if isinstance(stmt.expr, ast.TryCatchExpr):
			return H.HExprStmt(expr=self._lower_try_expr(stmt.expr, value_context=False), loc=self._as_span(getattr(stmt, "loc", None)))
		return H.HExprStmt(expr=self.lower_expr(stmt.expr), loc=self._as_span(getattr(stmt, "loc", None)))

	# --- stubs for remaining nodes ---

	def _visit_expr_Call(self, expr: ast.Call) -> H.HExpr:
		"""
		Lower calls:
		  - method sugar: Call(func=Attr(receiver, name), args=...) → HMethodCall
		  - Result.Ok(...) sugar -> HResultOk
		  - otherwise: plain HCall(fn_expr, args)
		DV-specific constructors are handled in _visit_expr_ExceptionCtor.
		"""
		kw_pairs = getattr(expr, "kwargs", []) or []
		h_kwargs = [
			H.HKwArg(
				name=kw.name,
				value=self.lower_expr(kw.value),
				loc=Span.from_loc(getattr(kw, "loc", None)),
			)
			for kw in kw_pairs
		]
		type_args = getattr(expr, "type_args", None)
		# Recognize Result.Ok constructor in source -> HResultOk for FnResult.
		if isinstance(expr.func, ast.Name) and expr.func.ident == "Ok" and len(expr.args) == 1:
			if h_kwargs:
				raise NotImplementedError("Ok(...) does not support keyword arguments")
			return H.HResultOk(value=self.lower_expr(expr.args[0]))

		# Method call sugar: receiver.method(args)
		if isinstance(expr.func, ast.Attr):
			receiver = self.lower_expr(expr.func.value)
			args = [self.lower_expr(a) for a in expr.args]
			return H.HMethodCall(
				receiver=receiver,
				method_name=expr.func.attr,
				args=args,
				kwargs=h_kwargs,
				type_args=type_args,
				loc=Span.from_loc(getattr(expr, "loc", None)),
			)

		# Plain function call or call through a computed value.
		fn_expr = self.lower_expr(expr.func)
		args = [self.lower_expr(a) for a in expr.args]
		# Keep direct name/qualified/lambda calls as HCall; lower everything else
		# to HInvoke so stage2 can treat value calls uniformly.
		if isinstance(fn_expr, (H.HVar, H.HLambda)) or (
			hasattr(H, "HQualifiedMember") and isinstance(fn_expr, getattr(H, "HQualifiedMember"))
		):
			return H.HCall(fn=fn_expr, args=args, kwargs=h_kwargs, type_args=type_args, loc=Span.from_loc(getattr(expr, "loc", None)))
		return H.HInvoke(callee=fn_expr, args=args, kwargs=h_kwargs, type_args=type_args, loc=Span.from_loc(getattr(expr, "loc", None)))

	def _visit_expr_MacroCall(self, expr: ast.MacroCall) -> H.HExpr:
		"""Expand macro call via centralized stage1 macro dispatcher."""
		expanded = expand_macro_call(expr)
		return self._visit_expr_Call(expanded)

	def _visit_expr_TypeApp(self, expr: ast.TypeApp) -> H.HExpr:
		fn_expr = self.lower_expr(expr.func)
		return H.HTypeApp(fn=fn_expr, type_args=list(expr.type_args))

	def _visit_expr_Attr(self, expr: ast.Attr) -> H.HExpr:
		"""Field access: subject.name (no method/placeholder sugar here)."""
		subject = self.lower_expr(expr.value)
		return H.HField(subject=subject, name=expr.attr)

	def _visit_expr_QualifiedMember(self, expr: ast.QualifiedMember) -> H.HExpr:
		"""
		Type-level qualified member reference: `TypeRef::member`.

		This is a general HIR node (not ctor-only). The typed checker resolves the
		member kind and enforces MVP restrictions. MIR lowering handles the
		constructor-call case as pure syntax sugar.
		"""
		return H.HQualifiedMember(
			base_type_expr=expr.base_type_expr,
			member=expr.member,
			loc=getattr(expr, "loc", None) or Span(),
		)

	def _visit_expr_Index(self, expr: ast.Index) -> H.HExpr:
		"""Indexing: subject[index] (no placeholder/index sugar here)."""
		subject = self.lower_expr(expr.value)
		index = self.lower_expr(expr.index)
		return H.HIndex(subject=subject, index=index)

	def _visit_expr_Unary(self, expr: ast.Unary) -> H.HExpr:
		"""
		Unary op lowering. Only maps the simple ops; more exotic ops can be
		added later with explicit enum entries.
		"""
		# Unary plus is a semantic no-op. We lower it by lowering its operand,
		# preserving source locations via the operand node.
		if expr.op == "+":
			return self.lower_expr(expr.operand)
		# Borrowing (& / &mut) lowers to HBorrow (lvalue-only check happens later).
		if expr.op in ("&", "&mut"):
			return H.HBorrow(subject=self.lower_expr(expr.operand), is_mut=expr.op == "&mut")
		op_map = {
			"-": H.UnaryOp.NEG,
			"not": H.UnaryOp.NOT,
			"~": H.UnaryOp.BIT_NOT,
			"*": H.UnaryOp.DEREF,
		}
		try:
			op = op_map[expr.op]
		except KeyError:
			raise NotImplementedError(f"Unsupported unary op: {expr.op}")
		return H.HUnary(op=op, expr=self.lower_expr(expr.operand), loc=self._as_span(expr.loc))

	def _visit_expr_Move(self, expr: ast.Move) -> H.HExpr:
		"""
		Lower the `move` expression.

		This introduces an explicit ownership-transfer marker in HIR.
		Addressability and move legality are validated later by:
		  - `normalize_hir` (canonical place conversion),
		  - the typed checker (operand must be a place),
		  - the borrow checker (no move while borrowed; no use-after-move).
		"""
		return H.HMove(subject=self.lower_expr(expr.value), loc=getattr(expr, "loc", None) or Span())

	def _visit_expr_Copy(self, expr: ast.Copy) -> H.HExpr:
		"""Lower the `copy` expression to an explicit HIR marker."""
		return H.HCopy(subject=self.lower_expr(expr.value), loc=getattr(expr, "loc", None) or Span())

	def _visit_expr_Binary(self, expr: ast.Binary) -> H.HExpr:
		"""
		Binary op lowering. Short-circuit behavior for &&/|| is NOT lowered
		here; that can be desugared later if needed.
		"""
		# Pipeline operator: `lhs |> stage`.
		#
		# Semantics (MVP):
		# - If `stage` is a call `f(a, b)`, desugar to `f(lhs, a, b)`.
		# - Otherwise desugar to `stage(lhs)` (where `stage` is any callable value).
		#
		# This keeps `|>` out of the core HIR operator set and makes pipelines work
		# without requiring dedicated MIR/LLVM support.
		if expr.op == "|>":
			left = self.lower_expr(expr.left)
			def _call_expr(fn_expr: H.HExpr, args: list[H.HExpr], kwargs: list[H.HKwArg]) -> H.HExpr:
				if isinstance(fn_expr, H.HVar):
					return H.HCall(fn=fn_expr, args=args, kwargs=kwargs, loc=Span.from_loc(getattr(expr, "loc", None)))
				if hasattr(H, "HQualifiedMember") and isinstance(fn_expr, getattr(H, "HQualifiedMember")):
					return H.HCall(fn=fn_expr, args=args, kwargs=kwargs, loc=Span.from_loc(getattr(expr, "loc", None)))
				if isinstance(fn_expr, H.HLambda):
					return H.HCall(fn=fn_expr, args=args, kwargs=kwargs, loc=Span.from_loc(getattr(expr, "loc", None)))
				return H.HInvoke(callee=fn_expr, args=args, kwargs=kwargs, loc=Span.from_loc(getattr(expr, "loc", None)))
			# `lhs |> f(...)` becomes `f(lhs, ...)`.
			if isinstance(expr.right, ast.Call):
				fn = self.lower_expr(expr.right.func)
				args = [left] + [self.lower_expr(a) for a in expr.right.args]
				kw_pairs = getattr(expr.right, "kwargs", []) or []
				kwargs = [
					H.HKwArg(
						name=kw.name,
						value=self.lower_expr(kw.value),
						loc=Span.from_loc(getattr(kw, "loc", None)),
					)
						for kw in kw_pairs
				]
				return _call_expr(fn, args, kwargs)
			# `lhs |> f` becomes `f(lhs)`.
			fn = self.lower_expr(expr.right)
			return _call_expr(fn, [left], [])

		op_map = {
			"+": H.BinaryOp.ADD,
			"-": H.BinaryOp.SUB,
			"*": H.BinaryOp.MUL,
			"/": H.BinaryOp.DIV,
			"%": H.BinaryOp.MOD,
			"&": H.BinaryOp.BIT_AND,
			"|": H.BinaryOp.BIT_OR,
			"^": H.BinaryOp.BIT_XOR,
			"<<": H.BinaryOp.SHL,
			">>": H.BinaryOp.SHR,
			"==": H.BinaryOp.EQ,
			"!=": H.BinaryOp.NE,
			"<": H.BinaryOp.LT,
			"<=": H.BinaryOp.LE,
			">": H.BinaryOp.GT,
			">=": H.BinaryOp.GE,
			"and": H.BinaryOp.AND,
			"or": H.BinaryOp.OR,
		}
		try:
			op = op_map[expr.op]
		except KeyError:
			raise NotImplementedError(f"Unsupported binary op: {expr.op}")
		left = self.lower_expr(expr.left)
		right = self.lower_expr(expr.right)
		return H.HBinary(op=op, left=left, right=right, loc=self._as_span(expr.loc))

	def _visit_expr_ArrayLiteral(self, expr: ast.ArrayLiteral) -> H.HExpr:
		"""Lower array literal by lowering each element expression."""
		return H.HArrayLiteral(elements=[self.lower_expr(e) for e in expr.elements])

	def _visit_expr_MapLiteral(self, expr: ast.MapLiteral) -> H.HExpr:
		"""Lower map literal by lowering each entry key/value expression."""
		return H.HMapLiteral(
			entries=[
				H.HMapEntry(key=self.lower_expr(entry.key), value=self.lower_expr(entry.value))
				for entry in expr.entries
			]
		)

	def _visit_expr_Lambda(self, expr: ast.Lambda) -> H.HExpr:
		explicit_captures: list[H.HExplicitCapture] | None = None
		if getattr(expr, "captures", None) is not None:
			explicit_captures = []
			for cap in expr.captures:
				explicit_captures.append(
					H.HExplicitCapture(
						name=cap.name,
						kind=cap.kind,
						binding_id=self._lookup_binding(cap.name),
						span=Span.from_loc(getattr(cap, "loc", None)),
					)
				)
		self._push_scope()
		try:
			params: list[H.HParam] = []
			for p in expr.params:
				bid = self._alloc_binding(p.name)
				params.append(
					H.HParam(
						name=p.name,
						type=p.type_expr,
						is_mutable=bool(getattr(p, "mutable", False)),
						binding_id=bid,
						span=Span.from_loc(getattr(p, "loc", None)),
					)
				)
			body_expr = self.lower_expr(expr.body_expr) if expr.body_expr is not None else None
			body_block = None
			if expr.body_block is not None:
				body_block = H.HBlock(statements=[self.lower_stmt(s) for s in expr.body_block.statements])
			return H.HLambda(
				params=params,
				ret_type=getattr(expr, "ret_type", None),
				body_expr=body_expr,
				body_block=body_block,
				explicit_captures=explicit_captures,
				declared_nothrow=bool(getattr(expr, "declared_nothrow", False)),
				span=Span.from_loc(getattr(expr, "loc", None)),
			)
		finally:
			self._pop_scope()

	def _visit_expr_ExceptionCtor(self, expr: ast.ExceptionCtor) -> H.HExpr:
		"""
		Exception constructor → structured exception init node.

		Exception constructors support positional and keyword arguments.

		Mapping arguments to declared exception field names happens later once
		exception schemas are available (checker / MIR lowering).
		"""
		fqn = self._resolve_event_fqn(expr.name)
		pos_args = [self.lower_expr(a) for a in getattr(expr, "args", [])]
		kw_pairs = getattr(expr, "kwargs", []) or []
		return H.HExceptionInit(
			event_fqn=fqn,
			pos_args=pos_args,
			kw_args=[
				H.HKwArg(
					name=kw.name,
					value=self.lower_expr(kw.value),
					loc=Span.from_loc(getattr(kw, "loc", None)),
				)
				for kw in kw_pairs
			],
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)

	def _visit_expr_Ternary(self, expr: ast.Ternary) -> H.HExpr:
		"""Lower ternary expression: cond ? then_expr : else_expr."""
		cond_h = self.lower_expr(expr.cond)
		then_h = self.lower_expr(expr.then_expr)
		else_h = self.lower_expr(expr.else_expr)
		return H.HTernary(cond=cond_h, then_expr=then_h, else_expr=else_h, loc=self._as_span(expr.loc))

	def _visit_expr_YieldExpr(self, expr: ast.YieldExpr) -> H.HExpr:
		# Yield is only valid as an explicit value inside value blocks; here we
		# just lower its inner expression.
		return self.lower_expr(expr.value)

	def _resolve_event_fqn(self, name: str) -> str:
		if ":" not in name:
			if not self._module_name:
				raise NotImplementedError("exception/catch lowering requires a module name to build an event FQN")
			return f"{self._module_name}:{name}"
		mod, event = name.split(":", 1)
		alias = self._module_aliases.get(mod)
		if alias:
			return f"{alias}:{event}"
		return name

	def _visit_expr_TryCatchExpr(self, expr: ast.TryCatchExpr) -> H.HExpr:
		return self._lower_try_expr(expr, value_context=True)

	def _lower_try_expr(self, expr: ast.TryCatchExpr, *, value_context: bool) -> H.HExpr:
		"""
		Lower expression-form try/catch into HTryExpr.

		Catch arms are lowered to HIR blocks; later phases enforce that they
		produce a value and do not contain control-flow statements.
		"""
		if not expr.catch_arms:
			raise NotImplementedError("try/catch requires at least one catch arm")

		arms: list[H.HTryExprArm] = []
		for arm in expr.catch_arms:
			event_fqn = self._resolve_event_fqn(arm.event) if arm.event is not None else None
			arm_result: Optional[H.HExpr] = None
			stmts = list(arm.block)
			if stmts and isinstance(stmts[-1], ast.ExprStmt):
				last_expr_stmt = stmts[-1]
				last_expr = last_expr_stmt.expr
				last_name = type(last_expr).__name__
				if last_name == "YieldExpr":
					if value_context:
						stmts.pop()
						arm_result = self.lower_expr(last_expr.value)
					else:
						stmts[-1] = ast.ExprStmt(expr=last_expr.value, loc=getattr(last_expr_stmt, "loc", None))
				elif value_context:
					stmts.pop()
					arm_result = self.lower_expr(last_expr)
			arm_block = self.lower_block(stmts)
			catch_loc = Span.from_loc(arm.loc)
			arms.append(
				H.HTryExprArm(
					event_fqn=event_fqn,
					binder=arm.binder,
					block=arm_block,
					result=arm_result,
					loc=catch_loc,
				)
			)

		return H.HTryExpr(
			attempt=self.lower_expr(expr.attempt),
			arms=arms,
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)

	def _visit_expr_MatchExpr(self, expr: ast.MatchExpr) -> H.HExpr:
		return self._lower_match_expr(expr, value_context=True)

	def _lower_match_expr(self, expr: ast.MatchExpr, *, value_context: bool) -> H.HExpr:
		"""
		Lower expression-form `match` into HIR (`HMatchExpr`).

		MVP contract (enforced by the typed checker, not here):
		  - `match` is an expression.
		  - Arms are blocks (`{ ... }`) which may contain statements.
		  - A value-producing arm is represented by a trailing `ExprStmt` in the
		    arm block; the block's "result" is the lowered expression from that
		    trailing statement.
		  - `default` is a keyword arm (`ctor=None`) and must be last.
		  - Patterns are positional-only constructor binders (no named fields).

		This lowering is intentionally structural: it preserves the arm block
		statements (minus an optional trailing `ExprStmt`) and stores the trailing
			expression separately as `HMatchArm.result` so later passes can reason
			about "arm yields a value" without re-walking the block.
			"""
		if not expr.arms:
			raise NotImplementedError("match expression requires at least one arm")

		def _rename_expr(e: H.HExpr, mapping: dict[str, str]) -> H.HExpr:
			"""
			Alpha-rename variable references within an expression.

			This is used for match-arm binder scoping. It renames *uses* of variables
			(HVar and place bases) according to `mapping`. It does not rename binding
			sites (e.g., `let` statement names); block-level renaming handles
			shadowing by adjusting the mapping as it walks statements.
			"""
			if isinstance(e, H.HVar) and e.name in mapping:
				return H.HVar(name=mapping[e.name], binding_id=e.binding_id, loc=getattr(e, "loc", Span()))
			if isinstance(e, H.HPlaceExpr):
				base = e.base
				if isinstance(base, H.HVar) and base.name in mapping:
					base = H.HVar(name=mapping[base.name], binding_id=base.binding_id, loc=getattr(base, "loc", Span()))
				return H.HPlaceExpr(base=base, projections=e.projections, loc=e.loc)
			if isinstance(e, H.HCall):
				return H.HCall(
					fn=_rename_expr(e.fn, mapping),
					args=[_rename_expr(a, mapping) for a in e.args],
					kwargs=[H.HKwArg(name=kw.name, value=_rename_expr(kw.value, mapping), loc=kw.loc) for kw in e.kwargs],
					type_args=getattr(e, "type_args", None),
					loc=getattr(e, "loc", Span()),
				)
			if isinstance(e, getattr(H, "HInvoke", ())):
				return H.HInvoke(
					callee=_rename_expr(e.callee, mapping),
					args=[_rename_expr(a, mapping) for a in e.args],
					kwargs=[H.HKwArg(name=kw.name, value=_rename_expr(kw.value, mapping), loc=kw.loc) for kw in e.kwargs],
					type_args=getattr(e, "type_args", None),
					loc=getattr(e, "loc", Span()),
				)
			if isinstance(e, H.HMethodCall):
				return H.HMethodCall(
					receiver=_rename_expr(e.receiver, mapping),
					method_name=e.method_name,
					args=[_rename_expr(a, mapping) for a in e.args],
					kwargs=[H.HKwArg(name=kw.name, value=_rename_expr(kw.value, mapping), loc=kw.loc) for kw in e.kwargs],
					type_args=getattr(e, "type_args", None),
					loc=getattr(e, "loc", Span()),
				)
			if isinstance(e, H.HField):
				return H.HField(subject=_rename_expr(e.subject, mapping), name=e.name)
			if isinstance(e, H.HIndex):
				return H.HIndex(subject=_rename_expr(e.subject, mapping), index=_rename_expr(e.index, mapping))
			if isinstance(e, H.HBorrow):
				return H.HBorrow(subject=_rename_expr(e.subject, mapping), is_mut=e.is_mut)
			if hasattr(H, "HMove") and isinstance(e, getattr(H, "HMove")):
				return H.HMove(subject=_rename_expr(e.subject, mapping), loc=e.loc)
			if hasattr(H, "HCopy") and isinstance(e, getattr(H, "HCopy")):
				return H.HCopy(subject=_rename_expr(e.subject, mapping), loc=e.loc)
			if isinstance(e, H.HUnary):
				return H.HUnary(op=e.op, expr=_rename_expr(e.expr, mapping), loc=e.loc)
			if isinstance(e, H.HBinary):
				return H.HBinary(op=e.op, left=_rename_expr(e.left, mapping), right=_rename_expr(e.right, mapping), loc=e.loc)
			if isinstance(e, H.HTernary):
				return H.HTernary(
					cond=_rename_expr(e.cond, mapping),
					then_expr=_rename_expr(e.then_expr, mapping),
					else_expr=_rename_expr(e.else_expr, mapping),
					loc=e.loc,
				)
			if isinstance(e, H.HArrayLiteral):
				return H.HArrayLiteral(elements=[_rename_expr(a, mapping) for a in e.elements])
			if hasattr(H, "HMapLiteral") and isinstance(e, getattr(H, "HMapLiteral")):
				return H.HMapLiteral(
					entries=[
						H.HMapEntry(
							key=_rename_expr(entry.key, mapping),
							value=_rename_expr(entry.value, mapping),
						)
						for entry in e.entries
					]
				)
			if isinstance(e, H.HExceptionInit):
				return H.HExceptionInit(
					event_fqn=e.event_fqn,
					pos_args=[_rename_expr(a, mapping) for a in e.pos_args],
					kw_args=[
						H.HKwArg(name=kw.name, value=_rename_expr(kw.value, mapping), loc=kw.loc)
						for kw in e.kw_args
					],
					loc=e.loc,
				)
			if isinstance(e, H.HFString):
				return H.HFString(
					parts=e.parts,
					holes=[H.HFStringHole(expr=_rename_expr(h.expr, mapping), spec=h.spec, loc=h.loc) for h in e.holes],
					loc=e.loc,
				)
			if isinstance(e, H.HTryExpr):
				renamed_arms: list[H.HTryExprArm] = []
				for arm in e.arms:
					renamed_block, after_map = _rename_block(arm.block, mapping)
					renamed_result = _rename_expr(arm.result, after_map) if arm.result is not None else None
					renamed_arms.append(
						H.HTryExprArm(
							event_fqn=arm.event_fqn,
							binder=arm.binder,
							block=renamed_block,
							result=renamed_result,
							loc=arm.loc,
						)
					)
				return H.HTryExpr(attempt=_rename_expr(e.attempt, mapping), arms=renamed_arms, loc=e.loc)
			if isinstance(e, H.HMatchExpr):
				renamed_arms: list[H.HMatchArm] = []
				for arm in e.arms:
					renamed_block, after_map = _rename_block(arm.block, mapping)
					renamed_result = _rename_expr(arm.result, after_map) if arm.result is not None else None
					renamed_arms.append(
						H.HMatchArm(
							ctor=arm.ctor,
							ctor_base=getattr(arm, "ctor_base", None),
							pattern_arg_form=getattr(arm, "pattern_arg_form", "positional"),
							binders=arm.binders,
							binder_is_mutable=getattr(arm, "binder_is_mutable", None),
							binder_fields=getattr(arm, "binder_fields", None),
							binder_field_indices=getattr(arm, "binder_field_indices", []),
							block=renamed_block,
							result=renamed_result,
							loc=arm.loc,
						)
					)
				return H.HMatchExpr(scrutinee=_rename_expr(e.scrutinee, mapping), arms=renamed_arms, loc=e.loc)
			if hasattr(H, "HQualifiedMember") and isinstance(e, getattr(H, "HQualifiedMember")):
				return e
			return e

		def _rename_stmt(st: H.HStmt, mapping: dict[str, str]) -> tuple[H.HStmt, dict[str, str]]:
			"""
			Rename variable uses within a statement, returning an updated mapping
			for subsequent statements (to model shadowing by `let`).
			"""
			if isinstance(st, H.HLet):
				new_value = _rename_expr(st.value, mapping)
				next_map = mapping
				if st.name in mapping:
					next_map = dict(mapping)
					next_map.pop(st.name, None)
				return (
					H.HLet(
						name=st.name,
						value=new_value,
						binding_id=st.binding_id,
						declared_type_expr=st.declared_type_expr,
						is_mutable=st.is_mutable,
					),
					next_map,
				)
			if isinstance(st, H.HAssign):
				return (H.HAssign(target=_rename_expr(st.target, mapping), value=_rename_expr(st.value, mapping)), mapping)
			if isinstance(st, H.HAugAssign):
				return (
					H.HAugAssign(target=_rename_expr(st.target, mapping), op=st.op, value=_rename_expr(st.value, mapping), loc=st.loc),
					mapping,
				)
			if isinstance(st, H.HReturn):
				return (H.HReturn(value=_rename_expr(st.value, mapping) if st.value is not None else None), mapping)
			if isinstance(st, H.HThrow):
				return (H.HThrow(value=_rename_expr(st.value, mapping) if st.value is not None else None), mapping)
			if isinstance(st, H.HExprStmt):
				return (H.HExprStmt(expr=_rename_expr(st.expr, mapping)), mapping)
			if isinstance(st, H.HIf):
				then_block, _ = _rename_block(st.then_block, mapping)
				else_block = None
				if st.else_block is not None:
					else_block, _ = _rename_block(st.else_block, mapping)
				return (H.HIf(cond=_rename_expr(st.cond, mapping), then_block=then_block, else_block=else_block), mapping)
			return (st, mapping)

		def _rename_block(block: H.HBlock, mapping: dict[str, str]) -> tuple[H.HBlock, dict[str, str]]:
			cur = dict(mapping)
			out: list[H.HStmt] = []
			for st in block.statements:
				new_st, cur = _rename_stmt(st, cur)
				out.append(new_st)
			return H.HBlock(statements=out), cur

		arms: list[H.HMatchArm] = []
		for arm in expr.arms:
			# Allocate unique internal names for binders to preserve binder scoping
			# even though HIR locals are function-scoped.
			rename_map: dict[str, str] = {}
			new_binders: list[str] = []
			new_binder_is_mutable: list[bool] = []
			for b in list(getattr(arm, "binders", []) or []):
				self._match_binder_counter += 1
				internal = f"__match_binder_{self._match_binder_counter}_{b}"
				rename_map[b] = internal
				new_binders.append(internal)
			binder_is_mutable = list(getattr(arm, "binder_is_mutable", []) or [])
			if binder_is_mutable:
				new_binder_is_mutable = binder_is_mutable

			arm_result: Optional[H.HExpr] = None
			stmts = list(arm.block)
			if stmts and isinstance(stmts[-1], ast.ExprStmt):
				last_expr_stmt = stmts[-1]
				last_expr = last_expr_stmt.expr
				last_name = type(last_expr).__name__
				if last_name == "YieldExpr":
					if value_context:
						stmts.pop()
						arm_result = self.lower_expr(last_expr.value)
					else:
						stmts[-1] = ast.ExprStmt(expr=last_expr.value, loc=getattr(last_expr_stmt, "loc", None))
				elif value_context:
					stmts.pop()
					arm_result = self.lower_expr(last_expr)
			arm_block = self.lower_block(stmts)
			if rename_map:
				arm_block, after_map = _rename_block(arm_block, rename_map)
				if arm_result is not None:
					arm_result = _rename_expr(arm_result, after_map)
			arms.append(
				H.HMatchArm(
					ctor=arm.ctor,
					ctor_base=getattr(arm, "ctor_base", None),
					pattern_arg_form=getattr(arm, "pattern_arg_form", "positional"),
					binders=new_binders,
					binder_is_mutable=new_binder_is_mutable if new_binder_is_mutable else None,
					binder_fields=getattr(arm, "binder_fields", None),
					# Seed positional binder indices structurally so stage2 lowering has a
					# stable mapping even in pipelines that don't run the typed checker.
					#
					# Named binders require schema-aware mapping and are normalized by the
					# typed checker; we leave `binder_field_indices` empty for that form.
					binder_field_indices=(
						list(range(len(new_binders)))
						if new_binders
						and getattr(arm, "pattern_arg_form", "positional") not in ("bare", "paren", "named")
						else []
					),
					block=arm_block,
					result=arm_result,
					loc=Span.from_loc(arm.loc),
				)
			)

		return H.HMatchExpr(
			scrutinee=self.lower_expr(expr.scrutinee),
			arms=arms,
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)

	def _visit_stmt_AssignStmt(self, stmt: ast.AssignStmt) -> H.HStmt:
		target = self.lower_expr(stmt.target)
		value = self.lower_expr(stmt.value)
		return H.HAssign(target=target, value=value, loc=self._as_span(getattr(stmt, "loc", None)))

	def _visit_stmt_AugAssignStmt(self, stmt: ast.AugAssignStmt) -> H.HStmt:
		"""
		Lower augmented assignment (`+=`) into a dedicated HIR statement.

		We intentionally *do not* desugar to `x = x + y` here because that would
		duplicate evaluation of the target expression (incorrect for `arr[i]`).
		Stage2 lowering emits a correct read-modify-write sequence for the
		canonicalized place target.
		"""
		target = self.lower_expr(stmt.target)
		value = self.lower_expr(stmt.value)
		return H.HAugAssign(target=target, op=str(getattr(stmt, "op", "+=")), value=value, loc=Span.from_loc(getattr(stmt, "loc", None)))

	def _visit_stmt_IfStmt(self, stmt: ast.IfStmt) -> H.HStmt:
		cond = self.lower_expr(stmt.cond)
		then_block = self.lower_block(stmt.then_block)
		else_block = self.lower_block(stmt.else_block) if stmt.else_block else None
		return H.HIf(cond=cond, then_block=then_block, else_block=else_block, loc=self._as_span(getattr(stmt, "loc", None)))

	def _visit_stmt_BlockStmt(self, stmt: ast.BlockStmt) -> H.HStmt:
		body = getattr(stmt, "body", None)
		return self.lower_block(body if body is not None else stmt.block)

	def _visit_stmt_UnsafeBlockStmt(self, stmt: ast.UnsafeBlockStmt) -> H.HStmt:
		return H.HUnsafeBlock(block=self.lower_block(stmt.body))

	def _visit_stmt_TryStmt(self, stmt: ast.TryStmt) -> H.HStmt:
		"""
		Lower statement-form try/catch with multiple arms into HTry + HCatchArm.

		  try { body }
		  catch EventName(e) { handler }
		  catch (e) { handler }
		  catch { handler }

		Arms are preserved in source order.
		"""
		body_block = self.lower_block(stmt.body)
		catch_arms: list[H.HCatchArm] = []
		for arm in stmt.catches:
			event_name = arm.event
			event_fqn = self._resolve_event_fqn(event_name) if event_name is not None else None
			binder = arm.binder
			handler_block = self.lower_block(arm.block)
			catch_loc = Span.from_loc(arm.loc)
			catch_arms.append(H.HCatchArm(event_fqn=event_fqn, binder=binder, block=handler_block, loc=catch_loc))

		return H.HTry(body=body_block, catches=catch_arms)

	def _lower_trycatch_expr_stmt(self, expr: ast.TryCatchExpr) -> H.HStmt:
		"""
		Lower expression-form try/catch used as a statement into HTry.

		The attempt expression is lowered as an expression statement inside the
		try body; catch arms are lowered identically to statement-form try.
		"""
		body_block = H.HBlock(statements=[H.HExprStmt(expr=self.lower_expr(expr.attempt))])
		if not expr.catch_arms:
			raise NotImplementedError("try/catch requires at least one catch arm")

		catch_arms: list[H.HCatchArm] = []
		for arm in expr.catch_arms:
			event_fqn = self._resolve_event_fqn(arm.event) if arm.event is not None else None
			binder = arm.binder
			handler_block = self.lower_block(arm.block)
			catch_loc = Span.from_loc(arm.loc)
			catch_arms.append(H.HCatchArm(event_fqn=event_fqn, binder=binder, block=handler_block, loc=catch_loc))

		return H.HTry(body=body_block, catches=catch_arms)

	def _visit_stmt_WhileStmt(self, stmt: ast.WhileStmt) -> H.HStmt:
		"""
		Desugar:
		  while cond { body }

		into:
		  loop {
		    if cond { body } else { break }
		  }
		so HIR only needs HLoop/HIf/HBreak, and MIR reuses existing lowering.
		"""
		cond_hir = self.lower_expr(stmt.cond)
		then_block = self.lower_block(stmt.body)
		else_block = H.HBlock(statements=[H.HBreak()])
		if_stmt = H.HIf(cond=cond_hir, then_block=then_block, else_block=else_block)
		loop_body = H.HBlock(statements=[if_stmt])
		return H.HLoop(body=loop_body)

	def _visit_stmt_ForStmt(self, stmt: ast.ForStmt) -> H.HStmt:
		"""
		Desugar:
		  for iter_var in iterable { body }

		into the iterator protocol using `match` + `Optional<T>`:
		  let __for_iterable = iterable
		  var __for_iter = __for_iterable.iter()
		  loop {
		    match __for_iter.next() {
		      Some(iter_var) => { body }
		      default => { break }
		    }
		  }

		Notes:
		  - iter_var is currently an identifier (pattern support can be added later).
		  - iterable expression is evaluated exactly once.
		"""
		# 1) Evaluate iterable once and bind if needed. Default is borrow-iteration; move is explicit.
		iterable_expr = self.lower_expr(stmt.iterable)
		iterable_is_move = bool(hasattr(H, "HMove") and isinstance(iterable_expr, getattr(H, "HMove")))
		iterable_is_borrow = isinstance(iterable_expr, H.HBorrow)
		iterable_borrow_mut = iterable_expr.is_mut if iterable_is_borrow else False
		base_expr = iterable_expr
		if iterable_is_move and hasattr(H, "HMove"):
			base_expr = iterable_expr.subject
		elif iterable_is_borrow:
			base_expr = iterable_expr.subject
		iterable_name: str | None = None
		iterable_bid: int | None = None
		iterable_let: H.HLet | None = None
		if isinstance(base_expr, H.HVar):
			iterable_name = base_expr.name
			iterable_bid = base_expr.binding_id
		else:
			iterable_name = self._fresh_temp("__for_iterable")
			iterable_bid = self._alloc_binding(iterable_name)
			iterable_let = H.HLet(name=iterable_name, value=base_expr, binding_id=iterable_bid)

		# 2) Build iterator: __for_iter = std.iter.Iterable.iter(__for_iterable)
		iter_name = self._fresh_temp("__for_iter")
		iter_bid = self._alloc_binding(iter_name)
		iter_trait = ast.TypeNameRef(name="Iterable", module_id="std.iter")
		iter_arg = H.HVar(iterable_name, binding_id=iterable_bid)
		if iterable_is_move and hasattr(H, "HMove"):
			iter_arg = H.HMove(
				subject=iter_arg,
				loc=getattr(stmt, "loc", None) or Span(),
				is_implicit=True,
			)
		elif iterable_is_borrow:
			iter_arg = H.HBorrow(subject=iter_arg, is_mut=iterable_borrow_mut)
		else:
			iter_arg = H.HBorrow(subject=iter_arg, is_mut=False)
		iter_call = H.HCall(
			fn=H.HQualifiedMember(base_type_expr=iter_trait, member="iter"),
			args=[iter_arg],
			origin="for_iter",
		)
		iter_let = H.HLet(name=iter_name, value=iter_call, binding_id=iter_bid, is_mutable=True)

		# 3) In loop: match std.iter.SinglePassIterator.next(__for_iter) { ... }
		next_trait = ast.TypeNameRef(name="SinglePassIterator", module_id="std.iter")
		iter_arg = H.HVar(iter_name, binding_id=iter_bid)
		iter_arg = H.HBorrow(subject=iter_arg, is_mut=True)
		next_call = H.HCall(
			fn=H.HQualifiedMember(base_type_expr=next_trait, member="next"),
			args=[iter_arg],
			origin="for_next",
		)
		body_block = self.lower_block(stmt.body)
		binder_name = stmt.iter_var
		binder_mut_flags: list[bool] | None = None
		if bool(getattr(stmt, "iter_var_mutable", False)):
			binder_mut_flags = [True]
		if getattr(stmt, "iter_var_type", None) is not None:
			tmp_name = self._fresh_temp("__for_item")
			tmp_bid = self._alloc_binding(tmp_name)
			binder_name = tmp_name
			binder_mut_flags = [False]
			decl_bid = self._alloc_binding(stmt.iter_var)
			decl_let = H.HLet(
				name=stmt.iter_var,
				value=H.HVar(tmp_name, binding_id=tmp_bid),
				declared_type_expr=getattr(stmt, "iter_var_type", None),
				binding_id=decl_bid,
				is_mutable=bool(getattr(stmt, "iter_var_mutable", False)),
				loc=self._as_span(getattr(stmt, "loc", None)),
			)
			body_block = H.HBlock(statements=[decl_let, *body_block.statements])
		arms: list[H.HMatchArm] = [
			# `for` desugaring matches `Optional<T>::Some(value: T)` positionally,
			# so the single binder always maps to field index 0.
			H.HMatchArm(ctor="Some", ctor_base=None, binders=[binder_name], binder_field_indices=[0], block=body_block, result=None, binder_is_mutable=binder_mut_flags),
			H.HMatchArm(ctor=None, ctor_base=None, binders=[], block=H.HBlock(statements=[H.HBreak()]), result=None),
		]
		match_expr = H.HMatchExpr(scrutinee=next_call, arms=arms)
		loop_body = H.HBlock(statements=[H.HExprStmt(expr=match_expr)])
		loop_stmt = H.HLoop(body=loop_body)

		# 7) Wrap iterable/iter bindings in a block to scope them.
		stmts: list[H.HStmt] = []
		if iterable_let is not None:
			stmts.append(iterable_let)
		stmts.extend([iter_let, loop_stmt])
		return H.HBlock(statements=stmts)

	def _visit_stmt_ForCountStmt(self, stmt: ast.ForCountStmt) -> H.HStmt:
		"""
		Desugar counted loop:
		  for init; cond; step { body }

		As:
		  let init
		  var __for_first = true
		  loop {
		    if __for_first { __for_first = false } else { step }
		    if cond { body } else { break }
		  }

		This preserves `continue` semantics naturally: a continue jumps to loop
		header, which runs `step` before the next condition check.
		"""
		self._push_scope()
		try:
			init_bid = self._alloc_binding(stmt.init_name)
			init_let = H.HLet(
				name=stmt.init_name,
				value=self.lower_expr(stmt.init_value),
				declared_type_expr=getattr(stmt, "init_type", None),
				binding_id=init_bid,
				is_mutable=bool(getattr(stmt, "init_mutable", False)),
				loc=self._as_span(getattr(stmt, "loc", None)),
			)
			first_name = self._fresh_temp("__for_first")
			first_bid = self._alloc_binding(first_name)
			first_let = H.HLet(
				name=first_name,
				value=H.HLiteralBool(True),
				declared_type_expr=None,
				binding_id=first_bid,
				is_mutable=True,
				loc=self._as_span(getattr(stmt, "loc", None)),
			)
			first_cond = H.HVar(first_name, binding_id=first_bid)
			clear_first = H.HAssign(target=H.HVar(first_name, binding_id=first_bid), value=H.HLiteralBool(False))
			step_stmt = self.lower_stmt(stmt.step)
			step_gate = H.HIf(
				cond=first_cond,
				then_block=H.HBlock(statements=[clear_first]),
				else_block=H.HBlock(statements=[step_stmt]),
			)
			cond_expr = self.lower_expr(stmt.cond)
			body_block = self.lower_block(stmt.body)
			cond_gate = H.HIf(
				cond=cond_expr,
				then_block=body_block,
				else_block=H.HBlock(statements=[H.HBreak()]),
			)
			loop_stmt = H.HLoop(body=H.HBlock(statements=[step_gate, cond_gate]))
			return H.HBlock(statements=[init_let, first_let, loop_stmt])
		finally:
			self._pop_scope()

	def _visit_stmt_BreakStmt(self, stmt: ast.BreakStmt) -> H.HStmt:
		return H.HBreak()

	def _visit_stmt_ContinueStmt(self, stmt: ast.ContinueStmt) -> H.HStmt:
		return H.HContinue()

	def _visit_stmt_ThrowStmt(self, stmt: ast.ThrowStmt) -> H.HStmt:
		"""Lower throw statement to HThrow; semantics are implemented in later stages."""
		value_h = self.lower_expr(stmt.value)
		return H.HThrow(value=value_h)

	def _visit_stmt_RaiseStmt(self, stmt: ast.RaiseStmt) -> H.HStmt:
		raise NotImplementedError("Raise lowering not implemented yet")

	def _visit_stmt_RethrowStmt(self, stmt: ast.RethrowStmt) -> H.HStmt:
		return H.HRethrow(loc=getattr(stmt, "loc", Span()))


__all__ = ["AstToHIR"]
