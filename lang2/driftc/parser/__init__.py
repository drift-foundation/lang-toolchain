"""
lang2 parser copy (self-contained, no runtime dependency on lang/).
Parses Drift source and adapts to lang2.driftc.stage0 AST + FnSignatures for the
lang2 pipeline.
"""

from __future__ import annotations

from pathlib import Path
from dataclasses import replace
from typing import Callable, Dict, Tuple, Optional, List

from lark.exceptions import UnexpectedInput

from . import parser as _parser
from . import ast as parser_ast
from lang2.driftc.stage0 import ast as s0
from lang2.driftc.stage1 import AstToHIR
from lang2.driftc import stage1 as H
from lang2.driftc.checker import FnSignature
from lang2.driftc.core.diagnostics import Diagnostic
from lang2.driftc.core.span import Span
from lang2.driftc.core.types_core import TypeKind, TypeParamId
from lang2.driftc.core.event_codes import event_code, PAYLOAD_MASK
from lang2.driftc.core.function_id import FunctionId
from lang2.driftc.core.types_core import (
	TypeTable,
	StructFieldSchema,
	VariantArmSchema,
	VariantFieldSchema,
)
from lang2.driftc.core.type_resolve_common import resolve_opaque_type
from lang2.driftc.core.generic_type_expr import GenericTypeExpr
from lang2.driftc.impl_index import ImplMeta, ImplMethodMeta


def _qualify_fn_name(module_id: str, name: str) -> str:
	# MVP: symbols in the default `main` module remain unqualified so single-module
	# programs keep legacy names. Other modules are qualified as `module::name`.
	if module_id in (None, "main"):
		return name
	return f"{module_id}::{name}"


def _validate_module_id(mid: str, *, span: Span) -> list[Diagnostic]:
	"""
	Validate a module id per the language spec (format + reserved prefixes).

	This is shared by:
	- single-module builds (`parse_drift_files_to_hir`), and
	- workspace builds (`parse_drift_workspace_to_hir`), including inferred ids
	  from `-M/--module-path`.
	"""
	if not isinstance(mid, str) or not mid:
		return [
			Diagnostic(
				message="invalid module id (empty)",
				severity="error",
				span=span,
			)
		]
	raw_len = len(mid.encode("utf-8"))
	if raw_len > 254:
		return [
			Diagnostic(
				message=f"invalid module id '{mid}': length {raw_len} exceeds 254 UTF-8 bytes",
				severity="error",
				span=span,
			)
		]
	# Reserved module namespaces. Only the dotted namespace prefixes are reserved
	# (e.g. `std.foo`), not the bare segment itself (e.g. `lib` is allowed).
	forbidden_prefixes = ("lang", "abi", "std", "core", "lib")
	for pfx in forbidden_prefixes:
		if mid.startswith(pfx + "."):
			return [
				Diagnostic(
					message=f"invalid module id '{mid}': reserved prefix '{pfx}' is not allowed",
					severity="error",
					span=span,
				)
			]
	if mid.startswith(".") or mid.endswith(".") or ".." in mid:
		return [
			Diagnostic(
				message=f"invalid module id '{mid}': dots must separate non-empty segments",
				severity="error",
				span=span,
			)
		]
	if mid.startswith("_") or mid.endswith("_") or "__" in mid:
		return [
			Diagnostic(
				message=f"invalid module id '{mid}': underscores must not be leading/trailing or consecutive",
				severity="error",
				span=span,
			)
		]
	segments = mid.split(".")
	for seg in segments:
		if not seg:
			return [
				Diagnostic(
					message=f"invalid module id '{mid}': empty segment",
					severity="error",
					span=span,
				)
			]
		if seg.startswith("_") or seg.endswith("_") or "__" in seg:
			return [
				Diagnostic(
					message=f"invalid module id '{mid}': segment '{seg}' has invalid underscore placement",
					severity="error",
					span=span,
				)
			]
		# MVP: segments must start with a lowercase letter to avoid ambiguous module
		# names and to keep directory→module inference predictable.
		if not ("a" <= seg[0] <= "z"):
			return [
				Diagnostic(
					message=f"invalid module id '{mid}': segment '{seg}' must start with a lowercase letter",
					severity="error",
					span=span,
				)
			]
		for ch in seg:
			if not (("a" <= ch <= "z") or ("0" <= ch <= "9") or ch == "_"):
				return [
					Diagnostic(
						message=f"invalid module id '{mid}': segment '{seg}' contains invalid character '{ch}'",
						severity="error",
						span=span,
					)
				]
	return []


def _format_span_short(span: Span) -> str:
	"""
	Format a span as `file:line:column` for use in `Diagnostic.notes`.

	Notes are currently plain strings (no secondary-span support), so we keep the
	format stable and human-oriented.
	"""
	f = span.file or "<unknown>"
	l = span.line if span.line is not None else "?"
	c = span.column if span.column is not None else "?"
	return f"{f}:{l}:{c}"


def _prime_builtins(table: TypeTable) -> None:
	"""
	Ensure builtin TypeIds exist and are seeded in a stable order.

	This is required for package embedding in Milestone 4: until TypeId remapping
	exists, independently-produced artifacts must agree on builtin ids.
	"""
	table.ensure_unknown()
	table.ensure_int()
	table.ensure_uint()
	table.ensure_bool()
	table.ensure_float()
	table.ensure_string()
	table.ensure_void()
	table.ensure_error()
	table.ensure_diagnostic_value()
	# Seed commonly used derived types so TypeIds are stable across builds.
	#
	# MVP: DV accessors return Optional<Int/Bool/String>, so we ensure those
	# instantiations exist even if a particular module doesn't use them directly.
	table.new_optional(table.ensure_int())
	table.new_optional(table.ensure_bool())
	table.new_optional(table.ensure_string())


def _type_expr_to_str(typ: parser_ast.TypeExpr) -> str:
	"""Render a TypeExpr into a string (e.g., Array<Int>, Result<Int, Error>)."""
	if not typ.args:
		return typ.name
	args = ", ".join(_type_expr_to_str(a) for a in typ.args)
	return f"{typ.name}<{args}>"


def _type_expr_key(typ: parser_ast.TypeExpr) -> tuple[object | None, str, tuple]:
	qual = getattr(typ, "module_id", None) or getattr(typ, "module_alias", None)
	return (qual, typ.name, tuple(_type_expr_key(a) for a in getattr(typ, "args", []) or []))


def _type_expr_key_str(typ: parser_ast.TypeExpr) -> str:
	qual = getattr(typ, "module_id", None) or getattr(typ, "module_alias", None)
	base = f"{qual}.{typ.name}" if qual else typ.name
	if not (getattr(typ, "args", []) or []):
		return base
	args = ", ".join(_type_expr_key_str(a) for a in getattr(typ, "args", []) or [])
	return f"{base}<{args}>"


def _impl_target_key(typ: parser_ast.TypeExpr, type_params: list[str]) -> tuple[object | None, str, tuple] | tuple[str, int]:
	"""Normalize impl target keys by treating type params as indexed placeholders."""
	if typ.name in type_params and not getattr(typ, "args", []):
		return ("param", type_params.index(typ.name))
	qual = getattr(typ, "module_id", None) or getattr(typ, "module_alias", None)
	return (qual, typ.name, tuple(_impl_target_key(a, type_params) for a in getattr(typ, "args", []) or []))


def _generic_type_expr_from_parser(
	typ: parser_ast.TypeExpr,
	*,
	type_params: list[str],
) -> GenericTypeExpr:
	"""
	Convert a parser `TypeExpr` into a generic-aware core `GenericTypeExpr`.

	This is used for schema-bearing declarations (variants) where field types may
	refer to generic parameters (e.g. `Some(value: T)`).
	"""
	if typ.name in type_params and not typ.args:
		return GenericTypeExpr.param(type_params.index(typ.name))
	return GenericTypeExpr.named(
		typ.name,
		[_generic_type_expr_from_parser(a, type_params=type_params) for a in getattr(typ, "args", [])],
		module_id=getattr(typ, "module_id", None),
	)


def _convert_expr(expr: parser_ast.Expr) -> s0.Expr:
	"""Convert parser AST expressions into lang2.driftc.stage0 AST expressions."""
	if isinstance(expr, parser_ast.Literal):
		return s0.Literal(value=expr.value, loc=Span.from_loc(getattr(expr, "loc", None)))
	if isinstance(expr, parser_ast.Name):
		return s0.Name(ident=expr.ident, loc=Span.from_loc(getattr(expr, "loc", None)))
	if isinstance(expr, parser_ast.TraitIs):
		return s0.TraitIs(
			subject=expr.subject,
			trait=expr.trait,
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)
	if isinstance(expr, parser_ast.TraitAnd):
		return s0.TraitAnd(
			left=_convert_expr(expr.left),
			right=_convert_expr(expr.right),
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)
	if isinstance(expr, parser_ast.TraitOr):
		return s0.TraitOr(
			left=_convert_expr(expr.left),
			right=_convert_expr(expr.right),
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)
	if isinstance(expr, parser_ast.TraitNot):
		return s0.TraitNot(
			expr=_convert_expr(expr.expr),
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)
	if isinstance(expr, parser_ast.Lambda):
		params = [
			s0.Param(
				name=p.name,
				type_expr=p.type_expr,
				non_escaping=getattr(p, "non_escaping", False),
				loc=Span.from_loc(getattr(p, "loc", None)),
			)
			for p in expr.params
		]
		body_expr = _convert_expr(expr.body_expr) if expr.body_expr is not None else None
		body_block = s0.Block(statements=_convert_block(expr.body_block)) if expr.body_block is not None else None
		return s0.Lambda(
			params=params,
			ret_type=getattr(expr, "ret_type", None),
			body_expr=body_expr,
			body_block=body_block,
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)
	if isinstance(expr, parser_ast.Call):
		return s0.Call(
			func=_convert_expr(expr.func),
			args=[_convert_expr(a) for a in expr.args],
			kwargs=[
				s0.KwArg(
					name=kw.name,
					value=_convert_expr(kw.value),
					loc=Span.from_loc(getattr(kw, "loc", None)),
				)
				for kw in getattr(expr, "kwargs", [])
			],
			type_args=getattr(expr, "type_args", None),
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)
	if isinstance(expr, parser_ast.TypeApp):
		return s0.TypeApp(
			func=_convert_expr(expr.func),
			type_args=list(expr.type_args),
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)
	if isinstance(expr, parser_ast.Attr):
		# Member-through-reference access (`p->field`) is normalized at the
		# parser→stage0 boundary by inserting an explicit deref.
		#
		# This keeps stage0/stage1 ASTs simple: later phases only need normal
		# member access plus unary deref (`*p`).
		base = _convert_expr(expr.value)
		if getattr(expr, "op", ".") == "->":
			base = s0.Unary(op="*", operand=base, loc=Span.from_loc(getattr(expr.value, "loc", None)))
		return s0.Attr(value=base, attr=expr.attr, loc=Span.from_loc(getattr(expr, "loc", None)))
	if isinstance(expr, parser_ast.QualifiedMember):
		return s0.QualifiedMember(
			base_type_expr=expr.base_type,
			member=expr.member,
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)
	if isinstance(expr, parser_ast.Index):
		return s0.Index(
			value=_convert_expr(expr.value),
			index=_convert_expr(expr.index),
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)
	if isinstance(expr, parser_ast.Binary):
		return s0.Binary(
			op=expr.op,
			left=_convert_expr(expr.left),
			right=_convert_expr(expr.right),
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)
	if isinstance(expr, parser_ast.Unary):
		return s0.Unary(op=expr.op, operand=_convert_expr(expr.operand), loc=Span.from_loc(getattr(expr, "loc", None)))
	if isinstance(expr, parser_ast.ArrayLiteral):
		return s0.ArrayLiteral(elements=[_convert_expr(e) for e in expr.elements], loc=Span.from_loc(getattr(expr, "loc", None)))
	if isinstance(expr, parser_ast.Move):
		return s0.Move(value=_convert_expr(expr.value), loc=Span.from_loc(getattr(expr, "loc", None)))
	if isinstance(expr, parser_ast.Placeholder):
		return s0.Placeholder(loc=Span.from_loc(getattr(expr, "loc", None)))
	if isinstance(expr, parser_ast.Ternary):
		return s0.Ternary(
			cond=_convert_expr(expr.condition),
			then_expr=_convert_expr(expr.then_value),
			else_expr=_convert_expr(expr.else_value),
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)
	if isinstance(expr, parser_ast.TryCatchExpr):
		catch_arms = [
			s0.CatchExprArm(
				event=arm.event,
				binder=arm.binder,
				block=_convert_block(arm.block),
				loc=Span.from_loc(getattr(arm, "loc", None)),
			)
			for arm in expr.catch_arms
		]
		return s0.TryCatchExpr(
			attempt=_convert_expr(expr.attempt),
			catch_arms=catch_arms,
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)
	if isinstance(expr, parser_ast.MatchExpr):
		arms = [
			s0.MatchArm(
				ctor=arm.ctor,
				pattern_arg_form=getattr(arm, "pattern_arg_form", "positional"),
				binders=list(arm.binders),
				binder_fields=list(arm.binder_fields) if getattr(arm, "binder_fields", None) is not None else None,
				block=_convert_block(arm.block),
				loc=Span.from_loc(getattr(arm, "loc", None)),
			)
			for arm in expr.arms
		]
		return s0.MatchExpr(
			scrutinee=_convert_expr(expr.scrutinee),
			arms=arms,
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)
	if isinstance(expr, parser_ast.ExceptionCtor):
		return s0.ExceptionCtor(
			name=expr.name,
			args=[_convert_expr(a) for a in expr.args],
			kwargs=[
				s0.KwArg(
					name=kw.name,
					value=_convert_expr(kw.value),
					loc=Span.from_loc(getattr(kw, "loc", None)),
				)
				for kw in expr.kwargs
			],
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)
	if isinstance(expr, parser_ast.FString):
		return s0.FString(
			parts=list(expr.parts),
			holes=[
				s0.FStringHole(
					expr=_convert_expr(h.expr),
					spec=h.spec,
					loc=Span.from_loc(getattr(h, "loc", None)),
				)
				for h in expr.holes
			],
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)
	raise NotImplementedError(f"Unsupported expression in adapter: {expr!r}")


def _convert_return(stmt: parser_ast.ReturnStmt) -> s0.Stmt:
	return s0.ReturnStmt(value=_convert_expr(stmt.value) if stmt.value is not None else None, loc=Span.from_loc(stmt.loc))


def _convert_expr_stmt(stmt: parser_ast.ExprStmt) -> s0.Stmt:
	return s0.ExprStmt(expr=_convert_expr(stmt.value), loc=Span.from_loc(stmt.loc))


def _convert_let(stmt: parser_ast.LetStmt) -> s0.Stmt:
	return s0.LetStmt(
		name=stmt.name,
		value=_convert_expr(stmt.value),
		type_expr=getattr(stmt, "type_expr", None),
		mutable=bool(getattr(stmt, "mutable", False)),
		loc=Span.from_loc(stmt.loc),
	)


def _convert_assign(stmt: parser_ast.AssignStmt) -> s0.Stmt:
	return s0.AssignStmt(target=_convert_expr(stmt.target), value=_convert_expr(stmt.value), loc=Span.from_loc(stmt.loc))


def _convert_aug_assign(stmt: "parser_ast.AugAssignStmt") -> s0.Stmt:
	"""
	Convert an augmented assignment statement.

	MVP supports:
	`+=`, `-=`, `*=`, `/=`, `%=`, `&=`, `|=`, `^=`, `<<=`, `>>=`.

	We preserve this as a distinct stage0 statement so later lowering can
	implement correct read-modify-write semantics for complex lvalues.
	"""
	return s0.AugAssignStmt(
		target=_convert_expr(stmt.target),
		op=str(getattr(stmt, "op", "+=")),
		value=_convert_expr(stmt.value),
		loc=Span.from_loc(stmt.loc),
	)

def _convert_if(stmt: parser_ast.IfStmt) -> s0.Stmt:
	return s0.IfStmt(
		cond=_convert_expr(stmt.condition),
		then_block=_convert_block(stmt.then_block),
		else_block=_convert_block(stmt.else_block) if stmt.else_block else [],
		loc=Span.from_loc(stmt.loc),
	)


def _convert_break(stmt: parser_ast.BreakStmt) -> s0.Stmt:
	return s0.BreakStmt(loc=Span.from_loc(stmt.loc))


def _convert_continue(stmt: parser_ast.ContinueStmt) -> s0.Stmt:
	return s0.ContinueStmt(loc=Span.from_loc(stmt.loc))


def _convert_while(stmt: parser_ast.WhileStmt) -> s0.Stmt:
	return s0.WhileStmt(cond=_convert_expr(stmt.condition), body=_convert_block(stmt.body), loc=Span.from_loc(stmt.loc))


def _convert_for(stmt: parser_ast.ForStmt) -> s0.Stmt:
	return s0.ForStmt(iter_var=stmt.var, iterable=_convert_expr(stmt.iter_expr), body=_convert_block(stmt.body), loc=Span.from_loc(stmt.loc))


def _convert_throw(stmt: parser_ast.ThrowStmt) -> s0.Stmt:
	return s0.ThrowStmt(value=_convert_expr(stmt.expr), loc=Span.from_loc(stmt.loc))


def _convert_raise(stmt: parser_ast.RaiseStmt) -> s0.Stmt:
	# TODO: when rethrow semantics are defined, map RaiseStmt appropriately.
	# For now, treat parser RaiseStmt as a plain throw of the expression.
	expr = getattr(stmt, "expr", None) or getattr(stmt, "value")
	return s0.ThrowStmt(value=_convert_expr(expr), loc=Span.from_loc(stmt.loc))


def _convert_rethrow(stmt: parser_ast.RethrowStmt) -> s0.Stmt:
	return s0.RethrowStmt(loc=Span.from_loc(stmt.loc))


def _convert_try(stmt: parser_ast.TryStmt) -> s0.Stmt:
	catches = [
		s0.CatchExprArm(
			event=c.event,
			binder=c.binder,
			block=_convert_block(c.block),
			loc=Span.from_loc(getattr(c, "loc", None)),
		)
		for c in stmt.catches
	]
	return s0.TryStmt(body=_convert_block(stmt.body), catches=catches, loc=Span.from_loc(stmt.loc))


def _convert_import(stmt: parser_ast.ImportStmt) -> s0.Stmt:
	path = ".".join(stmt.path)
	return s0.ImportStmt(path=path, loc=Span.from_loc(stmt.loc))


_STMT_DISPATCH: dict[type[parser_ast.Stmt], Callable[[parser_ast.Stmt], s0.Stmt]] = {
	parser_ast.ReturnStmt: _convert_return,
	parser_ast.ExprStmt: _convert_expr_stmt,
	parser_ast.LetStmt: _convert_let,
	parser_ast.AssignStmt: _convert_assign,
	parser_ast.AugAssignStmt: _convert_aug_assign,
	parser_ast.IfStmt: _convert_if,
	parser_ast.BreakStmt: _convert_break,
	parser_ast.ContinueStmt: _convert_continue,
	parser_ast.WhileStmt: _convert_while,
	parser_ast.ForStmt: _convert_for,
	parser_ast.ThrowStmt: _convert_throw,
	parser_ast.RaiseStmt: _convert_raise,
	parser_ast.RethrowStmt: _convert_rethrow,
	parser_ast.TryStmt: _convert_try,
	parser_ast.ImportStmt: _convert_import,
}


def _convert_stmt(stmt: parser_ast.Stmt) -> s0.Stmt:
	"""Convert parser AST statements into lang2.driftc.stage0 AST statements."""
	fn = _STMT_DISPATCH.get(type(stmt))
	if fn is None:
		raise NotImplementedError(f"Unsupported statement in adapter: {stmt!r}")
	return fn(stmt)


def _convert_block(block: parser_ast.Block) -> list[s0.Stmt]:
	return [_convert_stmt(s) for s in block.statements]


class _FrontendParam:
	def __init__(
		self,
		name: str,
		type_expr: parser_ast.TypeExpr | None,
		loc: Optional[parser_ast.Located],
		*,
		non_escaping: bool = False,
	) -> None:
		self.name = name
		# Preserve the parsed type expression so the resolver can build real TypeIds.
		self.type = type_expr
		self.loc = loc
		self.non_escaping = non_escaping


class _FrontendDecl:
	def __init__(
		self,
		fn_id: FunctionId,
		name: str,
		method_name: Optional[str],
		type_params: list[str],
		type_param_locs: list[parser_ast.Located],
		params: list[_FrontendParam],
		return_type: parser_ast.TypeExpr,
		loc: Optional[parser_ast.Located],
		is_pub: bool = False,
		is_method: bool = False,
		self_mode: Optional[str] = None,
		impl_target: Optional[parser_ast.TypeExpr] = None,
		impl_type_params: list[str] | None = None,
		impl_type_param_locs: list[parser_ast.Located] | None = None,
		impl_owner: FunctionId | None = None,
		module: Optional[str] = None,
	) -> None:
		self.fn_id = fn_id
		self.name = name
		self.method_name = method_name
		self.type_params = type_params
		self.type_param_locs = type_param_locs
		self.params = params
		self.return_type = return_type
		self.throws = ()
		self.loc = loc
		self.is_pub = is_pub
		self.is_extern = False
		self.is_intrinsic = False
		self.is_method = is_method
		self.self_mode = self_mode
		self.impl_target = impl_target
		self.impl_type_params = list(impl_type_params or [])
		self.impl_type_param_locs = list(impl_type_param_locs or [])
		self.impl_owner = impl_owner
		self.module = module


def _decl_from_parser_fn(
	fn: parser_ast.FunctionDef,
	*,
	fn_id: FunctionId,
	impl_type_params: list[str] | None = None,
	impl_type_param_locs: list[parser_ast.Located] | None = None,
	impl_owner: FunctionId | None = None,
) -> _FrontendDecl:
	params = [
		_FrontendParam(
			p.name,
			p.type_expr,
			getattr(p, "loc", None),
			non_escaping=getattr(p, "non_escaping", False),
		)
		for p in fn.params
	]
	return _FrontendDecl(
		fn_id,
		fn.name,
		fn.orig_name,
		fn.type_params,
		list(getattr(fn, "type_param_locs", []) or []),
		params,
		fn.return_type,
		getattr(fn, "loc", None),
		fn.is_pub,
		fn.is_method,
		fn.self_mode,
		fn.impl_target,
		impl_type_params,
		impl_type_param_locs,
		impl_owner,
	)


def _diagnostic(message: str, loc: object | None) -> Diagnostic:
	"""Helper to create a Diagnostic from a parser location."""
	return Diagnostic(message=message, severity="error", span=Span.from_loc(loc))


def _typeexpr_uses_internal_fnresult(typ: parser_ast.TypeExpr) -> bool:
	"""
	Return True if a surface type annotation mentions `FnResult` anywhere.

	`FnResult<T, Error>` is an internal ABI carrier used by lang2 for can-throw
	functions. It is not a surface type in the Drift language: user code should
	write `returns T` and use exceptions/try/catch for control flow.
	"""
	if typ.name == "FnResult":
		return True
	for arg in getattr(typ, "args", []) or []:
		if _typeexpr_uses_internal_fnresult(arg):
			return True
	return False


def _typeexpr_is_callable(typ: parser_ast.TypeExpr | None) -> bool:
	if typ is None:
		return False
	if typ.name in {"&", "&mut"} and getattr(typ, "args", None):
		return _typeexpr_is_callable(typ.args[0])
	return typ.name in {"Fn", "Callable"}


def _report_internal_fnresult_in_surface_type(
	*,
	kind: str,
	symbol: str,
	loc: object | None,
	diagnostics: list[Diagnostic],
) -> None:
	diagnostics.append(
		_diagnostic(
			f"{kind} '{symbol}' uses internal-only type 'FnResult' in a surface annotation; "
			"write `returns T` and use exceptions/try-catch instead",
			loc,
		)
	)


def _build_exception_catalog(exceptions: list[parser_ast.ExceptionDef], module_name: str | None, diagnostics: list[Diagnostic]) -> dict[str, int]:
	"""
	Assign deterministic event codes to exception declarations using the shared ABI hash.

	Collisions on the payload bits are reported as errors and the colliding
	exceptions are omitted from the catalog to avoid undefined dispatch.
	"""
	catalog: dict[str, int] = {}
	payload_seen: dict[int, str] = {}
	seen_names: set[str] = set()
	for exc in exceptions:
		if exc.name in seen_names:
			diagnostics.append(_diagnostic(f"duplicate exception '{exc.name}'", getattr(exc, "loc", None)))
			continue
		seen_names.add(exc.name)
		fqn = f"{module_name}:{exc.name}" if module_name else exc.name
		code = event_code(fqn)
		payload = code & PAYLOAD_MASK
		if payload in payload_seen and payload_seen[payload] != fqn:
			other = payload_seen[payload]
			diagnostics.append(
				_diagnostic(
					f"exception code collision between '{other}' and '{fqn}' (payload {payload})",
					getattr(exc, "loc", None),
				)
			)
			continue
		payload_seen[payload] = fqn
		catalog[fqn] = code
	return catalog


def _span_in_file(path: Path, loc: object | None) -> Span:
	"""
	Construct a Span that is anchored to a specific source file.

	The parser AST location objects do not carry a filename; for multi-file module
	builds we need the file to be explicit so diagnostics can point at the right
	origin.
	"""
	if loc is None:
		return Span(file=str(path))
	span = Span.from_loc(loc)
	if span.file is None:
		return Span(
			file=str(path),
			line=span.line,
			column=span.column,
			end_line=span.end_line,
			end_column=span.end_column,
			raw=span.raw,
		)
	return span


def _diag_duplicate(
	*,
	kind: str,
	name: str,
	first_path: Path,
	first_loc: object | None,
	second_path: Path,
	second_loc: object | None,
) -> list[Diagnostic]:
	"""
	Build a primary error + secondary note diagnostic for a cross-file duplicate.

	The error is pinned to the second definition; the note is pinned to the first.
	"""
	first_span = _span_in_file(first_path, first_loc)
	second_span = _span_in_file(second_path, second_loc)
	return [
		Diagnostic(
			message=f"duplicate {kind} definition for '{name}'",
			severity="error",
			span=second_span,
		),
		Diagnostic(
			message=f"previous definition of '{name}' is here",
			severity="note",
			span=first_span,
		),
	]


def parse_drift_files_to_hir(
	paths: list[Path],
) -> Tuple[Dict[FunctionId, H.HBlock], Dict[FunctionId, FnSignature], Dict[str, List[FunctionId]], "TypeTable", Dict[str, int], List[Diagnostic]]:
	"""
	Parse and lower a set of Drift source files into a single module unit.

	MVP (Milestone 1): accepts multiple files that all declare the same `module`
	id (or default to `main`). The module is lowered as if it were one merged file:
	- top-level declarations are combined,
	- cross-file collisions are diagnosed (with a pinned note pointing at the
	  first definition),
	- then the existing parser→stage0→HIR pipeline runs on the merged program.

	This does not implement cross-module imports yet; it only handles multiple
	files *within* the same module.
	"""
	diagnostics: list[Diagnostic] = []
	if not paths:
		return {}, {}, {}, TypeTable(), {}, [Diagnostic(message="no input files", severity="error")]

	programs: list[tuple[Path, parser_ast.Program]] = []
	for path in paths:
		source = path.read_text()
		try:
			prog = _parser.parse_program(source)
		except _parser.ModuleDeclError as err:
			diagnostics.append(Diagnostic(message=str(err), severity="error", span=_span_in_file(path, err.loc)))
			continue
		except _parser.QualifiedMemberParseError as err:
			diagnostics.append(Diagnostic(message=str(err), severity="error", span=_span_in_file(path, err.loc)))
			continue
		except _parser.FStringParseError as err:
			diagnostics.append(Diagnostic(message=str(err), severity="error", span=_span_in_file(path, err.loc)))
			continue
		except UnexpectedInput as err:
			span = Span(
				file=str(path),
				line=getattr(err, "line", None),
				column=getattr(err, "column", None),
				raw=err,
			)
			diagnostics.append(Diagnostic(message=str(err), severity="error", span=span))
			continue
		programs.append((path, prog))

	if any(d.severity == "error" for d in diagnostics):
		return {}, {}, {}, TypeTable(), {}, diagnostics

	# Enforce single-module membership across the file set.
	def _effective_module_id(p: parser_ast.Program) -> str:
		return getattr(p, "module", None) or "main"

	module_id = _effective_module_id(programs[0][1])
	for path, prog in programs:
		mid = _effective_module_id(prog)
		decl_span = _span_in_file(path, getattr(prog, "module_loc", None))
		diagnostics.extend(_validate_module_id(mid, span=decl_span))
	for path, prog in programs[1:]:
		mid = _effective_module_id(prog)
		if mid != module_id:
			diagnostics.append(
				Diagnostic(
					message=f"module id mismatch: expected '{module_id}', found '{mid}'",
					severity="error",
					span=Span(file=str(path), line=1, column=1),
				)
			)
	if any(d.severity == "error" for d in diagnostics):
		return {}, {}, {}, TypeTable(), {}, diagnostics

	merged, _origins = _merge_module_files(module_id, programs, diagnostics)

	# Lower the merged program using the existing single-file pipeline.
	func_hirs, sigs, fn_ids, table, excs, _impl_metas, diags = _lower_parsed_program_to_hir(
		merged,
		diagnostics=diagnostics,
	)
	return func_hirs, sigs, fn_ids, table, excs, diags


def _merge_module_files(
	module_id: str,
	files: list[tuple[Path, parser_ast.Program]],
	diagnostics: list[Diagnostic],
) -> tuple[parser_ast.Program, dict[FunctionId, Path]]:
	"""
	Merge a module's file set into a single parser AST `Program` (Milestone 1 rule set).

	This is the single source of truth for “multi-file module” merge behavior.
	Both `parse_drift_files_to_hir` (single-module build) and the workspace loader
	(Milestone 2) must call this helper to avoid drift.
	"""
	merged = parser_ast.Program(module=module_id)
	# Provenance map for module-local callable symbols (free functions and methods).
	#
	# Used by the workspace loader to implement per-file import environments:
	# we need to know which source file a given function body came from so we can
	# apply that file's imports while rewriting call sites.
	origin_by_fn_id: dict[FunctionId, Path] = {}

	first_fn_sig: dict[tuple, tuple[Path, object | None]] = {}
	name_ord: dict[str, int] = {}
	free_names: set[str] = set()
	for path, prog in files:
		for fn in getattr(prog, "functions", []) or []:
			sig_key = (
				fn.name,
				len(getattr(fn, "params", []) or []),
				tuple(_type_expr_key(p.type_expr) for p in getattr(fn, "params", []) or []),
			)
			if sig_key in first_fn_sig:
				first_path, first_loc = first_fn_sig[sig_key]
				diagnostics.extend(
					_diag_duplicate(
						kind="function",
						name=fn.name,
						first_path=first_path,
						first_loc=first_loc,
						second_path=path,
						second_loc=getattr(fn, "loc", None),
					)
				)
				continue
			first_fn_sig[sig_key] = (path, getattr(fn, "loc", None))
			free_names.add(fn.name)
			merged.functions.append(fn)
			ordinal = name_ord.get(fn.name, 0)
			name_ord[fn.name] = ordinal + 1
			fn_id = FunctionId(module=module_id, name=fn.name, ordinal=ordinal)
			origin_by_fn_id.setdefault(fn_id, path)

	first_const: dict[str, tuple[Path, object | None]] = {}
	for path, prog in files:
		for c in getattr(prog, "consts", []) or []:
			if c.name in first_const:
				first_path, first_loc = first_const[c.name]
				diagnostics.extend(
					_diag_duplicate(
						kind="const",
						name=c.name,
						first_path=first_path,
						first_loc=first_loc,
						second_path=path,
						second_loc=getattr(c, "loc", None),
					)
				)
				continue
			first_const[c.name] = (path, getattr(c, "loc", None))
			merged.consts.append(c)

	first_struct: dict[str, tuple[Path, object | None]] = {}
	for path, prog in files:
		for s in getattr(prog, "structs", []) or []:
			if s.name in first_struct:
				first_path, first_loc = first_struct[s.name]
				diagnostics.extend(
					_diag_duplicate(
						kind="struct",
						name=s.name,
						first_path=first_path,
						first_loc=first_loc,
						second_path=path,
						second_loc=getattr(s, "loc", None),
					)
				)
				continue
			first_struct[s.name] = (path, getattr(s, "loc", None))
			merged.structs.append(s)

	first_exc: dict[str, tuple[Path, object | None]] = {}
	for path, prog in files:
		for exc in getattr(prog, "exceptions", []) or []:
			if exc.name in first_exc:
				first_path, first_loc = first_exc[exc.name]
				diagnostics.extend(
					_diag_duplicate(
						kind="exception",
						name=exc.name,
						first_path=first_path,
						first_loc=first_loc,
						second_path=path,
						second_loc=getattr(exc, "loc", None),
					)
				)
				continue
			first_exc[exc.name] = (path, getattr(exc, "loc", None))
			merged.exceptions.append(exc)

	first_variant: dict[str, tuple[Path, object | None]] = {}
	for path, prog in files:
		for v in getattr(prog, "variants", []) or []:
			if v.name in first_variant:
				first_path, first_loc = first_variant[v.name]
				diagnostics.extend(
					_diag_duplicate(
						kind="variant",
						name=v.name,
						first_path=first_path,
						first_loc=first_loc,
						second_path=path,
						second_loc=getattr(v, "loc", None),
					)
				)
				continue
			first_variant[v.name] = (path, getattr(v, "loc", None))
			merged.variants.append(v)

	first_trait: dict[str, tuple[Path, object | None]] = {}
	for path, prog in files:
		for tr in getattr(prog, "traits", []) or []:
			if tr.name in first_trait:
				first_path, first_loc = first_trait[tr.name]
				diagnostics.extend(
					_diag_duplicate(
						kind="trait",
						name=tr.name,
						first_path=first_path,
						first_loc=first_loc,
						second_path=path,
						second_loc=getattr(tr, "loc", None),
					)
				)
				continue
			first_trait[tr.name] = (path, getattr(tr, "loc", None))
			merged.traits.append(tr)

	# Combine module directives (imports/exports).
	for _, prog in files:
		merged.imports.extend(getattr(prog, "imports", []) or [])
		merged.exports.extend(getattr(prog, "exports", []) or [])

	# Merge implement blocks by target repr.
	impls_by_key: dict[tuple[tuple | None, tuple], parser_ast.ImplementDef] = {}
	for path, prog in files:
		for impl in getattr(prog, "implements", []) or []:
			impl_type_params = list(getattr(impl, "type_params", []) or [])
			target_key = _impl_target_key(impl.target, impl_type_params)
			target_str = _type_expr_key_str(impl.target)
			trait_key = _type_expr_key(impl.trait) if getattr(impl, "trait", None) is not None else None
			trait_str = _type_expr_key_str(impl.trait) if getattr(impl, "trait", None) is not None else None
			key = (trait_key, target_key)
			dst = impls_by_key.get(key)
			if dst is None:
				dst = parser_ast.ImplementDef(
					target=impl.target,
					trait=getattr(impl, "trait", None),
					require=getattr(impl, "require", None),
					loc=getattr(impl, "loc", None),
					is_pub=getattr(impl, "is_pub", False),
					type_params=list(getattr(impl, "type_params", []) or []),
					type_param_locs=list(getattr(impl, "type_param_locs", []) or []),
					methods=[],
				)
				impls_by_key[key] = dst
			elif list(getattr(dst, "type_params", []) or []) != impl_type_params:
				impl_label = f"{trait_str} for {target_str}" if trait_str else target_str
				diagnostics.append(
					Diagnostic(
						message=f"conflicting type parameter lists for implement block '{impl_label}'",
						severity="error",
						span=_span_in_file(path, getattr(impl, "loc", None)),
					)
				)
			elif getattr(dst, "require", None) != getattr(impl, "require", None):
				impl_label = f"{trait_str} for {target_str}" if trait_str else target_str
				diagnostics.append(
					Diagnostic(
						message=f"conflicting require clauses for implement block '{impl_label}'",
						severity="error",
						span=_span_in_file(path, getattr(impl, "loc", None)),
					)
				)
			for m in getattr(impl, "methods", []) or []:
				if m.name in free_names:
					first_path, _first_loc = first_fn[m.name]
					diagnostics.append(
						Diagnostic(
							message=f"method '{m.name}' conflicts with existing free function of the same name",
							severity="error",
							span=_span_in_file(path, getattr(m, "loc", None)),
							notes=[f"previous free function definition is in {first_path}"],
						)
					)
					continue
				dst.methods.append(m)
				if trait_str:
					symbol_name = f"{target_str}::{trait_str}::{m.name}"
				else:
					symbol_name = f"{target_str}::{m.name}"
				ordinal = name_ord.get(symbol_name, 0)
				name_ord[symbol_name] = ordinal + 1
				fn_id = FunctionId(module=module_id, name=symbol_name, ordinal=ordinal)
				origin_by_fn_id.setdefault(fn_id, path)
	merged.implements = list(impls_by_key.values())
	return merged, origin_by_fn_id


def parse_drift_workspace_to_hir(
	paths: list[Path],
	*,
	module_paths: list[Path] | None = None,
	external_module_exports: dict[str, dict[str, object]] | None = None,
) -> Tuple[
	Dict[FunctionId, H.HBlock],
	Dict[FunctionId, FnSignature],
	Dict[str, List[FunctionId]],
	"TypeTable",
	Dict[str, int],
	Dict[str, Dict[str, object]],
	Dict[str, set[str]],
	List[Diagnostic],
]:
	"""
	Parse and lower a set of Drift source files that may belong to multiple modules.

	This is Milestone 2 (“module imports and cross-module resolution”) scaffolding:
	- input is an unordered set of files (typically all `*.drift` files in a build),
	- files are grouped by their declared `module <id>` (or default to `main`),
	- each module is merged from its file set (Milestone 1 behavior),
	- imports are resolved across modules (MVP: module-only imports),
	- resulting HIR/signatures are returned as a single program unit suitable for
	  the existing HIR→MIR→SSA→LLVM pipeline.

		Important MVP constraints (pinned for clarity):
		- Imports are treated as **per-file** bindings:
		  - Duplicate identical imports in one file are idempotent (“no-op after first”).
		  - Conflicting aliases/bindings in one file are diagnosed as errors.
		  - Different files may import the same module/symbol freely; the module is still
		    parsed/merged/compiled once per build and referenced from all import sites.
		- Module-qualified access (`import m` then `m.foo()`) is supported for calling
		  exported free functions and for struct constructor calls (`m.Point(...)`).
		- Cross-module import validation supports both value and type namespaces
		  (types: structs, variants, exceptions).

	Returns:
	  (func_hirs, signatures, fn_ids_by_name, type_table, exception_catalog, module_exports, module_deps, diagnostics)
	"""
	diagnostics: list[Diagnostic] = []
	if not paths:
		return {}, {}, {}, TypeTable(), {}, {}, {}, [Diagnostic(message="no input files", severity="error")]

	def _effective_module_id(p: parser_ast.Program) -> str:
		return getattr(p, "module", None) or "main"

	# Parse all files first.
	parsed: list[tuple[Path, parser_ast.Program]] = []
	for path in paths:
		source = path.read_text()
		try:
			prog = _parser.parse_program(source)
		except _parser.ModuleDeclError as err:
			diagnostics.append(Diagnostic(message=str(err), severity="error", span=_span_in_file(path, err.loc)))
			continue
		except _parser.QualifiedMemberParseError as err:
			diagnostics.append(Diagnostic(message=str(err), severity="error", span=_span_in_file(path, err.loc)))
			continue
		except _parser.FStringParseError as err:
			diagnostics.append(Diagnostic(message=str(err), severity="error", span=_span_in_file(path, err.loc)))
			continue
		except UnexpectedInput as err:
			span = Span(
				file=str(path),
				line=getattr(err, "line", None),
				column=getattr(err, "column", None),
				raw=err,
			)
			diagnostics.append(Diagnostic(message=str(err), severity="error", span=span))
			continue
		parsed.append((path, prog))

	if any(d.severity == "error" for d in diagnostics):
		return {}, {}, {}, TypeTable(), {}, {}, {}, diagnostics

	def _infer_module_id_from_paths(path: Path) -> tuple[str, Path] | tuple[None, None]:
		"""
		Infer the module id for a file from the configured module roots.

		Rule (MVP, pinned in work-progress):
		- find the first module root that is a prefix of the file's absolute path,
		- module id is derived from the directory relative to the root:
		  - empty relative path => "main"
		  - otherwise path segments joined by '.' (platform-independent).
		"""
		if not module_paths:
			return None, None
		abs_path = path.resolve()
		candidates: list[tuple[Path, Path]] = []
		for root in module_paths:
			abs_root = root.resolve()
			try:
				rel_dir = abs_path.parent.relative_to(abs_root)
			except ValueError:
				continue
			candidates.append((abs_root, rel_dir))

		if not candidates:
			return None, None

		# Deterministic root selection: pick the most-specific root (longest prefix).
		# This prevents module identity from depending on CLI flag ordering when roots
		# overlap (e.g. `-M src` and `-M src/vendor`).
		candidates.sort(key=lambda r: len(r[0].parts), reverse=True)
		best_len = len(candidates[0][0].parts)
		best = [c for c in candidates if len(c[0].parts) == best_len]
		if len(best) != 1:
			# Ambiguous configuration: multiple roots at the same specificity match
			# the same file.
			return None, None

		abs_root, rel_dir = best[0]
		parts = list(rel_dir.parts)
		if not parts or parts == ["."]:
			return "main", abs_root
		for seg in parts:
			if seg in {".", ".."}:
				return None, None
			if not seg:
				return None, None
		return ".".join(parts), abs_root

	# Group by module id (declared or inferred).
	by_module: dict[str, list[tuple[Path, parser_ast.Program]]] = {}
	roots_by_module: dict[str, set[Path]] = {}
	# For pinned diagnostics, keep at least one representative file per (module, root).
	root_file_by_module: dict[str, dict[Path, Path]] = {}
	for path, prog in parsed:
		if module_paths:
			inferred, root = _infer_module_id_from_paths(path)
			if inferred is None or root is None:
				diagnostics.append(
					Diagnostic(
						message=f"file '{path}' is not under exactly one configured module root",
						severity="error",
						span=Span(file=str(path), line=1, column=1),
					)
				)
				continue
			# Validate inferred id before using it as a module-graph key.
			diagnostics.extend(_validate_module_id(inferred, span=Span(file=str(path), line=1, column=1)))
			declared = getattr(prog, "module", None)
			if declared is not None:
				decl_span = _span_in_file(path, getattr(prog, "module_loc", None))
				diagnostics.extend(_validate_module_id(declared, span=decl_span))
				if any(d.severity == "error" for d in diagnostics):
					continue
				if declared != inferred:
					notes = [f"inferred module id is '{inferred}' from root '{root}'"]
					diagnostics.append(
						Diagnostic(
							message=f"module id mismatch: expected '{inferred}', found '{declared}'",
							severity="error",
							span=decl_span,
							notes=notes,
						)
					)
					continue
			# Treat missing module header as implicit declaration of the inferred id.
			if declared is None:
				prog = replace(prog, module=inferred)
			by_module.setdefault(inferred, []).append((path, prog))
			roots_by_module.setdefault(inferred, set()).add(root)
			root_file_by_module.setdefault(inferred, {}).setdefault(root, path)
		else:
			mid = _effective_module_id(prog)
			decl_span = _span_in_file(path, getattr(prog, "module_loc", None))
			diagnostics.extend(_validate_module_id(mid, span=decl_span))
			by_module.setdefault(mid, []).append((path, prog))

	if any(d.severity == "error" for d in diagnostics):
		return {}, {}, {}, TypeTable(), {}, {}, {}, diagnostics

	# When module roots are used, reject ambiguous module ids coming from
	# multiple roots (prevents accidental shadowing/selection by search order).
	if module_paths:
		for mid, roots in roots_by_module.items():
			if len(roots) > 1:
				root_list = ", ".join(str(r) for r in sorted(roots))
				span_file = None
				# Anchor the diagnostic to a concrete file under one of the roots.
				for r in sorted(roots):
					span_file = root_file_by_module.get(mid, {}).get(r)
					if span_file is not None:
						break
				span = Span(file=str(span_file), line=1, column=1) if span_file else Span()
				diagnostics.append(
					Diagnostic(
						message=f"multiple module roots provide module '{mid}' ({root_list})",
						severity="error",
						span=span,
					)
				)
	if any(d.severity == "error" for d in diagnostics):
		return {}, {}, {}, TypeTable(), {}, {}, {}, diagnostics
	# Merge each module (Milestone 1 rules) and retain callable provenance per file.
	merged_programs: dict[str, parser_ast.Program] = {}
	origin_by_module: dict[str, dict[FunctionId, Path]] = {}
	for mid, files in by_module.items():
		merged, origins = _merge_module_files(mid, files, diagnostics)
		merged_programs[mid] = merged
		origin_by_module[mid] = origins

	if any(d.severity == "error" for d in diagnostics):
		return {}, {}, {}, TypeTable(), {}, {}, {}, diagnostics

	def _build_export_interface(
		*,
		module_id: str,
		merged_prog: parser_ast.Program,
		module_files: list[tuple[Path, parser_ast.Program]],
	) -> tuple[
		dict[str, tuple[str, str]],
		dict[str, set[str]],
		set[str],
		dict[str, Span],
		dict[str, tuple[str, str]],
		dict[str, tuple[tuple[str, str], tuple[str, str], Span, Span]],
	]:
		"""
		Build the exported interface for a module.

		MVP visibility model:
		- items are private by default,
		- `export { Name, ... }` lists the exported names,
		- `export { other.module.* }` re-exports the other module's export set,
		- `export` cannot elevate visibility; only `pub` items may be exported.
		- both values and types may be exported, but in separate namespaces:
		  - values: free functions
		  - types: structs, variants, exceptions

		Because `export { ... }` syntax is unqualified, exporting a name that exists
		in both namespaces is ambiguous. Until we add explicit qualifiers, that is
		a compile-time error.

		Because `exports.types` is kind-separated (structs/variants/exceptions), an
		exported name that resolves to multiple type kinds is also a compile-time
		error.

		Spans are anchored to the source file that contained the `export { ... }`
		statement so diagnostics remain useful in multi-file modules.
		"""
		module_fn_names: set[str] = {fn.name for fn in getattr(merged_prog, "functions", []) or []}
		module_const_names: set[str] = {c.name for c in getattr(merged_prog, "consts", []) or []}
		module_struct_names: set[str] = {s.name for s in getattr(merged_prog, "structs", []) or []}
		module_variant_names: set[str] = {v.name for v in getattr(merged_prog, "variants", []) or []}
		module_exception_names: set[str] = {e.name for e in getattr(merged_prog, "exceptions", []) or []}
		module_pub_fn_names: set[str] = {fn.name for fn in getattr(merged_prog, "functions", []) or [] if getattr(fn, "is_pub", False)}
		module_pub_const_names: set[str] = {c.name for c in getattr(merged_prog, "consts", []) or [] if getattr(c, "is_pub", False)}
		module_pub_struct_names: set[str] = {s.name for s in getattr(merged_prog, "structs", []) or [] if getattr(s, "is_pub", False)}
		module_pub_variant_names: set[str] = {v.name for v in getattr(merged_prog, "variants", []) or [] if getattr(v, "is_pub", False)}
		module_pub_exception_names: set[str] = {e.name for e in getattr(merged_prog, "exceptions", []) or [] if getattr(e, "is_pub", False)}

		raw_export_entries: list[tuple[str, Span]] = []
		star_export_entries: list[tuple[str, Span]] = []
		for path, parsed_prog in module_files:
			for ex in getattr(parsed_prog, "exports", []) or []:
				for item in getattr(ex, "items", []) or []:
					item_span = _span_in_file(path, getattr(item, "loc", None))
					if isinstance(item, parser_ast.ExportName):
						raw_export_entries.append((item.name, item_span))
					elif isinstance(item, parser_ast.ExportModuleStar):
						mod = ".".join(getattr(item, "module_path", []) or [])
						if mod:
							star_export_entries.append((mod, item_span))

		# MVP rule: exporting the same name multiple times within a module is a
		# deterministic user error (even if it would be a no-op). We treat it as a
		# duplicate declaration so the module interface remains crisp and tooling
		# never has to guess which export site is authoritative.
		seen_export_names: dict[str, Span] = {}
		seen_star_modules: dict[str, Span] = {}

		# Exported values map exported local name -> underlying (module_id, symbol).
		#
		# Export entries always name symbols in the *current* module interface
		# (e.g., `a::foo`). Re-exports preserve the origin module in the map so
		# consumers always bind to the defining symbol.
		exported_values: dict[str, tuple[str, str]] = {}
		exported_types: dict[str, set[str]] = {"structs": set(), "variants": set(), "exceptions": set()}
		exported_consts: set[str] = set()
		star_reexports: dict[str, Span] = {}
		for mod, ex_span in star_export_entries:
			prev = seen_star_modules.get(mod)
			if prev is None:
				seen_star_modules[mod] = ex_span
				star_reexports[mod] = ex_span
			else:
				diagnostics.append(
					Diagnostic(
						message=f"duplicate export of module '{mod}.*' in module '{module_id}'",
						severity="error",
						span=ex_span,
						notes=[f"first export was here: {_format_span_short(prev)}"],
					)
				)

		for n, ex_span in raw_export_entries:
			first_span = seen_export_names.get(n)
			if first_span is None:
				seen_export_names[n] = ex_span
			else:
				diagnostics.append(
					Diagnostic(
						message=f"duplicate export of symbol '{n}' in module '{module_id}'",
						severity="error",
						span=ex_span,
						notes=[f"first export was here: {_format_span_short(first_span)}"],
					)
				)
				continue

			in_values = n in module_fn_names
			in_consts = n in module_const_names
			in_struct = n in module_struct_names
			in_variant = n in module_variant_names
			in_exc = n in module_exception_names
			type_hits = int(in_struct) + int(in_variant) + int(in_exc)
			in_types = type_hits > 0

			if (in_values and in_consts) or (in_values and in_types) or (in_consts and in_types):
				diagnostics.append(
					Diagnostic(
						message=f"exported name '{n}' is ambiguous (defined as multiple kinds in module '{module_id}')",
						severity="error",
						span=ex_span,
					)
				)
				continue
			if type_hits > 1:
				diagnostics.append(
					Diagnostic(
						message=f"exported type name '{n}' is ambiguous (defined as multiple type kinds in module '{module_id}')",
						severity="error",
						span=ex_span,
					)
				)
				continue

			if not in_values and not in_consts and not in_types:
				diagnostics.append(
					Diagnostic(
						message=f"module '{module_id}' exports unknown symbol '{n}'",
						severity="error",
						span=ex_span,
					)
				)
				continue

			if in_values:
				if n not in module_pub_fn_names:
					diagnostics.append(
						Diagnostic(
							message=f"cannot export '{n}' from module '{module_id}': symbol is not public (mark it 'pub')",
							severity="error",
							span=ex_span,
						)
					)
					continue
				exported_values[n] = (module_id, n)
			if in_consts:
				if n not in module_pub_const_names:
					diagnostics.append(
						Diagnostic(
							message=f"cannot export '{n}' from module '{module_id}': symbol is not public (mark it 'pub')",
							severity="error",
							span=ex_span,
						)
					)
					continue
				exported_consts.add(n)
			if in_struct:
				if n not in module_pub_struct_names:
					diagnostics.append(
						Diagnostic(
							message=f"cannot export '{n}' from module '{module_id}': symbol is not public (mark it 'pub')",
							severity="error",
							span=ex_span,
						)
					)
					continue
				exported_types["structs"].add(n)
			if in_variant:
				if n not in module_pub_variant_names:
					diagnostics.append(
						Diagnostic(
							message=f"cannot export '{n}' from module '{module_id}': symbol is not public (mark it 'pub')",
							severity="error",
							span=ex_span,
						)
					)
					continue
				exported_types["variants"].add(n)
			if in_exc:
				if n not in module_pub_exception_names:
					diagnostics.append(
						Diagnostic(
							message=f"cannot export '{n}' from module '{module_id}': symbol is not public (mark it 'pub')",
							severity="error",
							span=ex_span,
						)
					)
					continue
				exported_types["exceptions"].add(n)

		return (
			exported_values,
			exported_types,
			exported_consts,
			star_reexports,
		)

	# Note: module-scoped nominal type identity is implemented in lang2.
	# Multiple modules may define types with the same short name without
	# colliding; identity is `(module_id, name, kind)`.

	# Export sets (private by default, explicit exports required).
	#
	# MVP supports exporting/importing both value-level and type-level symbols,
	# but keeps them in separate namespaces:
	# - values: currently just free functions
	# - types: structs, variants, exceptions
	#
	# Export lists are unqualified identifiers, so to avoid ambiguity we reject
	# any module that defines the same name in both namespaces (until the language
	# adds explicit `export type ...` / `export fn ...` syntax).
	exports_values_by_module: dict[str, dict[str, tuple[str, str]]] = {}
	exports_types_by_module: dict[str, dict[str, set[str]]] = {}
	exports_consts_by_module: dict[str, set[str]] = {}
	star_reexports_by_module: dict[str, dict[str, Span]] = {}
	exported_const_origins_by_module: dict[str, dict[str, tuple[str, str]]] = {}
	exported_type_origins_by_module: dict[str, dict[str, dict[str, tuple[str, str]]]] = {}
	# Re-export target maps (for types/consts). Values are materialized as
	# trampolines, so consumers always reference the exporting module id.
	reexported_type_targets_by_module: dict[str, dict[str, dict[str, tuple[str, str]]]] = {}
	reexported_const_targets_by_module: dict[str, dict[str, tuple[str, str]]] = {}
	for mid, prog in merged_programs.items():
		(
			exported_values,
			exported_types,
			exported_consts,
			star_reexports,
		) = _build_export_interface(
			module_id=mid,
			merged_prog=prog,
			module_files=by_module.get(mid, []),
		)
		exports_values_by_module[mid] = exported_values
		exports_types_by_module[mid] = exported_types
		exports_consts_by_module[mid] = exported_consts
		star_reexports_by_module[mid] = star_reexports
		exported_const_origins_by_module[mid] = {n: (mid, n) for n in exported_consts}
		exported_type_origins_by_module[mid] = {
			"structs": {n: (mid, n) for n in exported_types.get("structs") or set()},
			"variants": {n: (mid, n) for n in exported_types.get("variants") or set()},
			"exceptions": {n: (mid, n) for n in exported_types.get("exceptions") or set()},
		}
		reexported_type_targets_by_module[mid] = {"structs": {}, "variants": {}, "exceptions": {}}
		reexported_const_targets_by_module[mid] = {}

	# Resolve star re-exports across modules deterministically.
	def _export_origin_lookup(
		mod: str,
	) -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]], dict[str, dict[str, tuple[str, str]]]]:
		"""Return (exported_values, exported_consts, exported_types_by_kind) with origin targets."""
		if mod in exports_values_by_module or mod in exports_types_by_module or mod in exports_consts_by_module:
			return (
				exports_values_by_module.get(mod) or {},
				exported_const_origins_by_module.get(mod) or {},
				exported_type_origins_by_module.get(mod) or {"structs": {}, "variants": {}, "exceptions": {}},
			)
		if external_module_exports is not None and mod in external_module_exports:
			ext = external_module_exports.get(mod) or {}
			values_obj = {n: (mod, n) for n in sorted(ext.get("values") or set())}
			consts_obj = {n: (mod, n) for n in sorted(ext.get("consts") or set())}
			types_obj: dict[str, dict[str, tuple[str, str]]] = {"structs": {}, "variants": {}, "exceptions": {}}
			ext_types = ext.get("types")
			if isinstance(ext_types, dict):
				for kind in ("structs", "variants", "exceptions"):
					for name in sorted(ext_types.get(kind) or set()):
						types_obj[kind][name] = (mod, name)
			ext_reexp = ext.get("reexports")
			if isinstance(ext_reexp, dict):
				ext_reexp_types = ext_reexp.get("types")
				ext_reexp_consts = ext_reexp.get("consts")
				if isinstance(ext_reexp_consts, dict):
					for name, v in ext_reexp_consts.items():
						if isinstance(v, dict):
							tm = v.get("module")
							tn = v.get("name")
							if isinstance(tm, str) and isinstance(tn, str):
								consts_obj[name] = (tm, tn)
				if isinstance(ext_reexp_types, dict):
					for kind in ("structs", "variants", "exceptions"):
						km = ext_reexp_types.get(kind)
						if isinstance(km, dict):
							for name, v in km.items():
								if isinstance(v, dict):
									tm = v.get("module")
									tn = v.get("name")
									if isinstance(tm, str) and isinstance(tn, str):
										types_obj[kind][name] = (tm, tn)
			return values_obj, consts_obj, types_obj
		return {}, {}, {"structs": {}, "variants": {}, "exceptions": {}}

	# We iterate until no progress so multi-hop star re-exports resolve deterministically.
	for _ in range(len(merged_programs) + 1):
		progress = False
		for mid, stars in star_reexports_by_module.items():
			for target_mod, ex_span in stars.items():
				if target_mod not in merged_programs and (external_module_exports is None or target_mod not in external_module_exports):
					diagnostics.append(
						Diagnostic(
							message=f"module '{mid}' re-exports unknown module '{target_mod}'",
							severity="error",
							span=ex_span,
						)
					)
					continue
				vals, consts, types_obj = _export_origin_lookup(target_mod)
				for name, origin in vals.items():
					prev = exports_values_by_module[mid].get(name)
					if prev is None:
						exports_values_by_module[mid][name] = origin
						progress = True
					elif prev != origin:
						diagnostics.append(
							Diagnostic(
								message=(
									f"exported name '{name}' is ambiguous due to re-exports "
									f"('{prev[0]}' vs '{origin[0]}') in module '{mid}'"
								),
								severity="error",
								span=ex_span,
							)
						)
				for name, origin in consts.items():
					prev = exported_const_origins_by_module[mid].get(name)
					if prev is None:
						exports_consts_by_module[mid].add(name)
						exported_const_origins_by_module[mid][name] = origin
						if origin[0] != mid:
							reexported_const_targets_by_module[mid][name] = origin
						progress = True
					elif prev != origin:
						diagnostics.append(
							Diagnostic(
								message=(
									f"exported const '{name}' is ambiguous due to re-exports "
									f"('{prev[0]}' vs '{origin[0]}') in module '{mid}'"
								),
								severity="error",
								span=ex_span,
							)
						)
				for kind, origins in types_obj.items():
					for name, origin in origins.items():
						prev = exported_type_origins_by_module[mid][kind].get(name)
						if prev is None:
							exports_types_by_module[mid][kind].add(name)
							exported_type_origins_by_module[mid][kind][name] = origin
							if origin[0] != mid:
								reexported_type_targets_by_module[mid][kind][name] = origin
							progress = True
						elif prev != origin:
							diagnostics.append(
								Diagnostic(
									message=(
										f"exported type '{name}' is ambiguous due to re-exports "
										f"('{prev[0]}' vs '{origin[0]}') in module '{mid}'"
									),
									severity="error",
									span=ex_span,
								)
							)
		if not progress:
			break

	def _union_exported_types(types_obj: dict[str, set[str]] | None) -> set[str]:
		if not types_obj:
			return set()
		out: set[str] = set()
		for vs in types_obj.values():
			out |= set(vs)
		return out

	if any(d.severity == "error" for d in diagnostics):
		return {}, {}, {}, TypeTable(), {}, {}, {}, diagnostics

	# Export interface summary (used by package emission and future tooling).
	module_exports: dict[str, dict[str, object]] = {}
	for mid in merged_programs.keys():
		vals = exports_values_by_module.get(mid, {})
		types = exports_types_by_module.get(mid, {"structs": set(), "variants": set(), "exceptions": set()})
		consts = exports_consts_by_module.get(mid, set())
		reexp_types = reexported_type_targets_by_module.get(mid, {"structs": {}, "variants": {}, "exceptions": {}})
		reexp_consts = reexported_const_targets_by_module.get(mid, {})
		module_exports[mid] = {
			"values": sorted(list(vals.keys())),
			"types": {
				"structs": sorted(list(types.get("structs", set()))),
				"variants": sorted(list(types.get("variants", set()))),
				"exceptions": sorted(list(types.get("exceptions", set()))),
			},
			"consts": sorted(list(consts)),
			"reexports": {
				"types": {
					"structs": {n: {"module": m, "name": s} for n, (m, s) in sorted(reexp_types.get("structs", {}).items())},
					"variants": {n: {"module": m, "name": s} for n, (m, s) in sorted(reexp_types.get("variants", {}).items())},
					"exceptions": {n: {"module": m, "name": s} for n, (m, s) in sorted(reexp_types.get("exceptions", {}).items())},
				},
				"consts": {n: {"module": m, "name": s} for n, (m, s) in sorted(reexp_consts.items())},
			},
		}

	# Resolve imports and build a dependency graph.
	#
	# MVP rule: import bindings are per-file. Module dependencies, however, are
	# computed at module granularity (a module depends on another module if any
	# of its files import it).
	#
	# Keep per-edge provenance so cycle diagnostics can be source-anchored.
	# Each edge is (to_module, span).
	dep_edges: dict[str, list[tuple[str, Span]]] = {mid: [] for mid in merged_programs}
	module_aliases_by_file: dict[Path, dict[str, str]] = {}
	for mid, files in by_module.items():
		for path, prog in files:
			file_module_aliases: dict[str, str] = {}

			for imp in getattr(prog, "imports", []) or []:
				mod = ".".join(getattr(imp, "path", []) or [])
				if not mod:
					continue
				span = _span_in_file(path, getattr(imp, "loc", None))
				dep_edges[mid].append((mod, span))
				if mod not in merged_programs and (external_module_exports is None or mod not in external_module_exports):
					diagnostics.append(Diagnostic(message=f"imported module '{mod}' not found", severity="error", span=span))
					continue
				alias = getattr(imp, "alias", None) or (getattr(imp, "path", []) or [mod])[-1]
				prev = file_module_aliases.get(alias)
				if prev is None:
					file_module_aliases[alias] = mod
				elif prev != mod:
					diagnostics.append(
						Diagnostic(
							message=f"import alias '{alias}' conflicts: cannot import both '{prev}' and '{mod}' as '{alias}'",
							severity="error",
							span=span,
						)
					)

			# Record module aliases for later module-qualified access resolution.
			# This is per-file by design (imports are file-scoped in MVP).
			module_aliases_by_file[path] = dict(file_module_aliases)
		for target_mod, ex_span in (star_reexports_by_module.get(mid) or {}).items():
			dep_edges[mid].append((target_mod, ex_span))

	# Collapse edge lists into a simple adjacency set for cycle detection.
	# Include external modules so visibility rules can see package imports.
	deps: dict[str, set[str]] = {
		mid: {to for (to, _sp) in edges if to in merged_programs or (external_module_exports and to in external_module_exports)}
		for mid, edges in dep_edges.items()
	}

	# Resolve module-qualified type references using per-file module aliases and
	# module export interfaces.
	#
	# After successful resolution we record the canonical `module_id` on the type
	# expression (and rewrite imported aliases to their original symbol name). This
	# preserves module-scoped nominal identity end-to-end.
	def _exported_types_for_module(mod: str) -> set[str]:
		if mod in exports_types_by_module:
			return _union_exported_types(exports_types_by_module.get(mod))
		if external_module_exports is not None and mod in external_module_exports:
			ext = external_module_exports.get(mod) or {}
			ext_types = ext.get("types")
			if isinstance(ext_types, dict):
				return set(ext_types.get("structs") or set()) | set(ext_types.get("variants") or set()) | set(
					ext_types.get("exceptions") or set()
				)
			return set()
		return set()

	def _resolve_type_expr_in_file(
		path: Path,
		file_aliases: dict[str, str],
		te: parser_ast.TypeExpr | None,
	) -> None:
		if te is None:
			return
		if getattr(te, "module_alias", None):
			alias = te.module_alias
			mod = file_aliases.get(alias or "")
			span = _span_in_file(path, getattr(te, "loc", None))
			if mod is None:
				diagnostics.append(
					Diagnostic(
						message=f"unknown module alias '{alias}' in type reference '{alias}.{te.name}'",
						severity="error",
						span=span,
					)
				)
			else:
				types = _exported_types_for_module(mod)
				if te.name not in types:
					available = ", ".join(sorted(types))
					notes = (
						[f"available exported types: {available}"]
						if available
						else [f"module '{mod}' exports no types (private by default)"]
					)
					diagnostics.append(
						Diagnostic(
							message=f"module '{mod}' does not export type '{te.name}'",
							severity="error",
							span=span,
							notes=notes,
						)
					)
				else:
					# Record the canonical module id for later lowering.
					#
					# If `mod` re-exports this type, resolve it to the defining module
					# identity (no type duplication across module interfaces).
					def_mod, def_name = (mod, te.name)
					reexp = reexported_type_targets_by_module.get(mod)
					if reexp is not None:
						for kind in ("structs", "variants", "exceptions"):
							if te.name in (exports_types_by_module.get(mod) or {}).get(kind, set()):
								def_mod, def_name = reexp.get(kind, {}).get(te.name, (mod, te.name))
								break
					elif external_module_exports is not None and mod in external_module_exports:
						ext = external_module_exports.get(mod) or {}
						ext_reexp = ext.get("reexports")
						ext_types = ext.get("types")
						if isinstance(ext_reexp, dict) and isinstance(ext_types, dict):
							ext_reexp_types = ext_reexp.get("types")
							if isinstance(ext_reexp_types, dict):
								for kind in ("structs", "variants", "exceptions"):
									kind_set = set(ext_types.get(kind) or set())
									if te.name in kind_set:
										tgt = ext_reexp_types.get(kind, {}).get(te.name) if isinstance(ext_reexp_types.get(kind), dict) else None
										if isinstance(tgt, dict):
											tm = tgt.get("module")
											tn = tgt.get("name")
											if isinstance(tm, str) and isinstance(tn, str):
												def_mod, def_name = (tm, tn)
										break
					te.module_id = def_mod
					te.name = def_name
					te.module_alias = None
		for a in getattr(te, "args", []) or []:
			_resolve_type_expr_in_file(path, file_aliases, a)

	def _resolve_types_in_block(path: Path, file_aliases: dict[str, str], blk: parser_ast.Block) -> None:
		for st in getattr(blk, "statements", []) or []:
			# Resolve any type-level references embedded in expressions (e.g.,
			# `TypeRef::Ctor(...)` where `TypeRef` may include a module alias).
			def _resolve_types_in_expr(expr: parser_ast.Expr) -> None:
				if isinstance(expr, parser_ast.QualifiedMember):
					_resolve_type_expr_in_file(path, file_aliases, expr.base_type)
					return
				if isinstance(expr, parser_ast.Call):
					_resolve_types_in_expr(expr.func)
					for a in getattr(expr, "args", []) or []:
						_resolve_types_in_expr(a)
					for kw in getattr(expr, "kwargs", []) or []:
						_resolve_types_in_expr(kw.value)
					return
				if isinstance(expr, parser_ast.Attr):
					_resolve_types_in_expr(expr.value)
					return
				if isinstance(expr, parser_ast.Index):
					_resolve_types_in_expr(expr.value)
					_resolve_types_in_expr(expr.index)
					return
				if isinstance(expr, parser_ast.Unary):
					_resolve_types_in_expr(expr.operand)
					return
				if isinstance(expr, parser_ast.Binary):
					_resolve_types_in_expr(expr.left)
					_resolve_types_in_expr(expr.right)
					return
				if isinstance(expr, parser_ast.Move):
					_resolve_types_in_expr(expr.value)
					return
				if isinstance(expr, parser_ast.Ternary):
					_resolve_types_in_expr(expr.condition)
					_resolve_types_in_expr(expr.then_value)
					_resolve_types_in_expr(expr.else_value)
					return
				if isinstance(expr, parser_ast.ArrayLiteral):
					for e in getattr(expr, "elements", []) or []:
						_resolve_types_in_expr(e)
					return
				if isinstance(expr, parser_ast.TryCatchExpr):
					_resolve_types_in_expr(expr.attempt)
					for arm in getattr(expr, "catch_arms", []) or []:
						_resolve_types_in_block(path, file_aliases, arm.block)
					return
				if isinstance(expr, parser_ast.MatchExpr):
					_resolve_types_in_expr(expr.scrutinee)
					for arm in getattr(expr, "arms", []) or []:
						_resolve_types_in_block(path, file_aliases, arm.block)
					return
				if isinstance(expr, parser_ast.ExceptionCtor):
					for a in getattr(expr, "args", []) or []:
						_resolve_types_in_expr(a)
					for kw in getattr(expr, "kwargs", []) or []:
						_resolve_types_in_expr(kw.value)
					return
				if isinstance(expr, parser_ast.FString):
					for h in getattr(expr, "holes", []) or []:
						_resolve_types_in_expr(h.expr)
					return
				# literals/names/placeholders are leaf nodes

			if isinstance(st, parser_ast.LetStmt) and getattr(st, "type_expr", None) is not None:
				_resolve_type_expr_in_file(path, file_aliases, st.type_expr)
			if isinstance(st, parser_ast.LetStmt):
				_resolve_types_in_expr(st.value)
			if isinstance(st, parser_ast.AssignStmt):
				_resolve_types_in_expr(st.target)
				_resolve_types_in_expr(st.value)
			if isinstance(st, parser_ast.AugAssignStmt):
				_resolve_types_in_expr(st.target)
				_resolve_types_in_expr(st.value)
			if isinstance(st, parser_ast.ReturnStmt) and st.value is not None:
				_resolve_types_in_expr(st.value)
			if isinstance(st, parser_ast.ExprStmt):
				_resolve_types_in_expr(st.value)
			if isinstance(st, parser_ast.IfStmt):
				_resolve_types_in_expr(st.condition)
				_resolve_types_in_block(path, file_aliases, st.then_block)
				if st.else_block is not None:
					_resolve_types_in_block(path, file_aliases, st.else_block)
			if isinstance(st, parser_ast.TryStmt):
				if isinstance(getattr(st, "attempt", None), parser_ast.Expr):
					_resolve_types_in_expr(st.attempt)
				_resolve_types_in_block(path, file_aliases, st.body)
				for c in getattr(st, "catches", []) or []:
					_resolve_types_in_block(path, file_aliases, c.block)
			if isinstance(st, parser_ast.WhileStmt):
				_resolve_types_in_expr(st.condition)
				_resolve_types_in_block(path, file_aliases, st.body)
			if isinstance(st, parser_ast.ForStmt):
				_resolve_types_in_expr(st.iter_expr)
				_resolve_types_in_block(path, file_aliases, st.body)
			if isinstance(st, parser_ast.ThrowStmt):
				_resolve_types_in_expr(st.expr)

	for mid, files in by_module.items():
		for path, prog in files:
			file_aliases = module_aliases_by_file.get(path, {})
			# Top-level declarations.
			for fn in getattr(prog, "functions", []) or []:
				for p in getattr(fn, "params", []) or []:
					_resolve_type_expr_in_file(path, file_aliases, p.type_expr)
				_resolve_type_expr_in_file(path, file_aliases, getattr(fn, "return_type", None))
				_resolve_types_in_block(path, file_aliases, fn.body)
			for impl in getattr(prog, "implements", []) or []:
				_resolve_type_expr_in_file(path, file_aliases, impl.target)
				for mfn in getattr(impl, "methods", []) or []:
					for p in getattr(mfn, "params", []) or []:
						_resolve_type_expr_in_file(path, file_aliases, p.type_expr)
					_resolve_type_expr_in_file(path, file_aliases, getattr(mfn, "return_type", None))
					_resolve_types_in_block(path, file_aliases, mfn.body)
			for s in getattr(prog, "structs", []) or []:
				for f in getattr(s, "fields", []) or []:
					_resolve_type_expr_in_file(path, file_aliases, f.type_expr)
			for e in getattr(prog, "exceptions", []) or []:
				for a in getattr(e, "args", []) or []:
					_resolve_type_expr_in_file(path, file_aliases, a.type_expr)
			for v in getattr(prog, "variants", []) or []:
				for arm in getattr(v, "arms", []) or []:
					for f in getattr(arm, "fields", []) or []:
						_resolve_type_expr_in_file(path, file_aliases, f.type_expr)

	# Cycle detection (MVP: reject import cycles).
	def _find_cycle() -> list[str] | None:
		vis: set[str] = set()
		stack: list[str] = []
		onstack: set[str] = set()

		def dfs(n: str) -> list[str] | None:
			vis.add(n)
			stack.append(n)
			onstack.add(n)
			for m in deps.get(n, set()):
				if m not in merged_programs:
					continue
				if m not in vis:
					c = dfs(m)
					if c is not None:
						return c
				elif m in onstack:
					try:
						i = stack.index(m)
					except ValueError:
						i = 0
					return stack[i:] + [m]
			stack.pop()
			onstack.remove(n)
			return None

		for n in merged_programs:
			if n not in vis:
				c = dfs(n)
				if c is not None:
					return c
		return None

	cycle = _find_cycle()
	if cycle is not None:
		# Anchor the diagnostic to one concrete import site in the cycle.
		# (We choose the first edge in the reported cycle.)
		primary_span: Span | None = None
		notes: list[str] = []
		for i in range(len(cycle) - 1):
			a = cycle[i]
			b = cycle[i + 1]
			for to, sp in dep_edges.get(a, []):
				if to == b:
					if i == 0:
						primary_span = sp
					notes.append(f"{a} imports {b}")
					break
		if primary_span is None:
			# Fallback: pick any import edge span from any node in the cycle.
			for node in cycle:
				for _to, sp in dep_edges.get(node, []):
					primary_span = sp
					break
				if primary_span is not None:
					break
		diagnostics.append(
			Diagnostic(
				message=f"import cycle detected: {' -> '.join(cycle)}",
				severity="error",
				span=primary_span or Span(),
				notes=notes,
			)
		)

	if any(d.severity == "error" for d in diagnostics):
		return {}, {}, {}, TypeTable(), {}, {}, {}, diagnostics

	# Lower modules using a shared TypeTable so TypeIds remain comparable across the workspace.
	shared_type_table = TypeTable()
	_prime_builtins(shared_type_table)
	# Pre-declare all nominal type names across the workspace before lowering any
	# individual module.
	#
	# This prevents cross-module type references (e.g. `import lib as x; val p: x.Point`)
	# from accidentally minting placeholder scalar TypeIds via `ensure_named` when
	# the defining module hasn't been lowered yet. `declare_struct`/`declare_variant`
	# are idempotent when the kind already matches, so later per-module lowering
	# can safely re-run its local declaration passes.
	for _mid, _prog in merged_programs.items():
		for _s in getattr(_prog, "structs", []) or []:
			try:
				struct_id = shared_type_table.declare_struct(
					_mid,
					_s.name,
					[f.name for f in getattr(_s, "fields", []) or []],
					list(getattr(_s, "type_params", []) or []),
				)
				field_templates = [
					StructFieldSchema(
						name=_f.name,
						type_expr=_generic_type_expr_from_parser(
							_f.type_expr, type_params=list(getattr(_s, "type_params", []) or [])
						),
					)
					for _f in getattr(_s, "fields", []) or []
				]
				shared_type_table.define_struct_schema_fields(struct_id, field_templates)
			except ValueError as err:
				diagnostics.append(Diagnostic(message=str(err), severity="error", span=Span.from_loc(getattr(_s, "loc", None))))
		for _v in getattr(_prog, "variants", []) or []:
			arms: list[VariantArmSchema] = []
			for _arm in getattr(_v, "arms", []) or []:
				fields = [
					VariantFieldSchema(
						name=_f.name,
						type_expr=_generic_type_expr_from_parser(
							_f.type_expr, type_params=list(getattr(_v, "type_params", []) or [])
						),
					)
					for _f in getattr(_arm, "fields", []) or []
				]
				arms.append(VariantArmSchema(name=_arm.name, fields=fields))
			try:
				shared_type_table.declare_variant(
					_mid,
					_v.name,
					list(getattr(_v, "type_params", []) or []),
					arms,
				)
			except ValueError as err:
				diagnostics.append(Diagnostic(message=str(err), severity="error", span=Span.from_loc(getattr(_v, "loc", None))))

		if any(d.severity == "error" for d in diagnostics):
			return {}, {}, {}, TypeTable(), {}, {}, {}, diagnostics

	def _qualify_fn_name(module_id: str, name: str) -> str:
		# MVP: symbols in the default `main` module remain unqualified so
		# single-module programs keep legacy names.
		if module_id in (None, "main"):
			return name
		return f"{module_id}::{name}"

	def _qualify_symbol(module_id: str, sym: str, *, local_free_fns: set[str]) -> str:
		"""
		Qualify a module-local callable symbol for inclusion in a multi-module build.

		MVP intent: avoid collisions when multiple modules are lowered into a single
		LLVM module by making callable symbols module-scoped:
		- free functions: `foo` → `mod::foo` (except the entry `main`)
		- methods: `Type::method` → `mod::Type::method`

		This does not define a long-term symbol identity model; it's a pragmatic
		naming layer until the resolver carries explicit module/type identities.
		"""
		if sym in local_free_fns:
			return _qualify_fn_name(module_id, sym)
		if "::" in sym and not sym.startswith(f"{module_id}::"):
			return f"{module_id}::{sym}"
		return sym

	all_func_hirs: dict[FunctionId, H.HBlock] = {}
	all_sigs: dict[FunctionId, FnSignature] = {}
	fn_ids_by_name: dict[str, list[FunctionId]] = {}
	exc_catalog: dict[str, int] = {}
	fn_owner_module: dict[FunctionId, str] = {}
	fn_symbol_by_id: dict[FunctionId, str] = {}
	impls_by_module: dict[str, list[ImplMeta]] = {}

	# Lower each module and qualify its callable symbols.
	for mid, prog in merged_programs.items():
		func_hirs, sigs, ids_by_name, _table, excs, impl_metas, diags = _lower_parsed_program_to_hir(
			prog,
			diagnostics=[],
			type_table=shared_type_table,
		)
		diagnostics.extend(diags)
		exc_catalog.update(excs)
		impls_by_module[mid] = list(impl_metas)

		local_free_fns = {fn.name for fn in getattr(prog, "functions", []) or []}
		exported_values = exports_values_by_module.get(mid, {})

		# Qualify and copy function bodies/signatures.
		for fn_id, block in func_hirs.items():
			local_name = fn_id.name
			global_name = _qualify_symbol(mid, local_name, local_free_fns=local_free_fns)
			all_func_hirs[fn_id] = block
			fn_owner_module[fn_id] = mid
			fn_symbol_by_id[fn_id] = global_name
			fn_ids_by_name.setdefault(global_name, []).append(fn_id)

		for fn_id, sig in sigs.items():
			local_name = fn_id.name
			global_name = _qualify_symbol(mid, local_name, local_free_fns=local_free_fns)
			# Mark module-interface entry points early so downstream phases can
			# enforce visibility and (later) ABI-boundary rules consistently.
			is_exported = (local_name in local_free_fns) and (local_name in exported_values) and (local_name != "main")
			all_sigs[fn_id] = replace(sig, name=global_name, is_exported_entrypoint=is_exported)

	# Attach impl metadata after lowering so downstream phases can build
	# the global impl index without rescanning signatures.
	for mid, impls in impls_by_module.items():
		if mid in module_exports:
			module_exports[mid]["impls"] = impls

	# Materialize re-exported functions as trampoline entry points.
	#
	# Even though `export { foo }` can refer to an imported binding (re-export),
	# module interfaces must remain self-contained: importing a symbol from a module
	# binds to `module::symbol`, not to some hidden downstream module.
	#
	# MVP implementation strategy:
	# - if module `a` exports `foo` that maps to an underlying target `(b, foo)`,
	#   synthesize `a::foo` as a trivial trampoline calling `b::foo`.
	# - this ensures exported entrypoints exist in the exporting module and keeps
	#   future package/interface metadata straightforward.
	for mid, exported_values in exports_values_by_module.items():
		for export_name, (target_mod, target_sym) in exported_values.items():
			if export_name == "main":
				continue
			if (target_mod, target_sym) == (mid, export_name):
				continue
			trampoline_name = _qualify_fn_name(mid, export_name)
			if fn_ids_by_name.get(trampoline_name):
				continue
			target_name = _qualify_fn_name(target_mod, target_sym)
			target_ids = fn_ids_by_name.get(target_name) or []
			if not target_ids:
				diagnostics.append(
					Diagnostic(
						message=f"internal: missing signature for re-export target '{target_mod}::{target_sym}'",
						severity="error",
						span=Span(),
					)
				)
				continue
			if len(target_ids) > 1:
				diagnostics.append(
					Diagnostic(
						message=f"ambiguous re-export target '{target_mod}::{target_sym}' (overloaded)",
						severity="error",
						span=Span(),
					)
				)
				continue
			target_id = target_ids[0]
			target_sig = all_sigs.get(target_id)
			if target_sig is None:
				diagnostics.append(
					Diagnostic(
						message=f"internal: missing signature for re-export target '{target_mod}::{target_sym}'",
						severity="error",
						span=Span(),
					)
				)
				continue
			ordinal = len(fn_ids_by_name.get(trampoline_name, []))
			trampoline_id = FunctionId(module=mid, name=export_name, ordinal=ordinal)
			all_sigs[trampoline_id] = replace(
				target_sig,
				name=trampoline_name,
				module=mid,
				is_exported_entrypoint=True,
			)
			fn_owner_module[trampoline_id] = mid
			fn_symbol_by_id[trampoline_id] = trampoline_name
			fn_ids_by_name.setdefault(trampoline_name, []).append(trampoline_id)

			# Build a minimal HIR body that forwards to the underlying target.
			arg_exprs: list[H.HExpr] = []
			for p in getattr(target_sig, "param_names", None) or []:
				if p:
					arg_exprs.append(H.HVar(name=p))
			callee = H.HVar(name=target_name)
			call_expr = H.HCall(fn=callee, args=arg_exprs)
			if target_sig.return_type_id is not None and shared_type_table.is_void(target_sig.return_type_id):
				all_func_hirs[trampoline_id] = H.HBlock(
					statements=[
						H.HExprStmt(expr=call_expr),
						H.HReturn(value=None),
					]
				)
			else:
				all_func_hirs[trampoline_id] = H.HBlock(statements=[H.HReturn(value=call_expr)])

		if any(d.severity == "error" for d in diagnostics):
			return {}, {}, {}, TypeTable(), {}, {}, {}, diagnostics

	# Materialize const re-exports into the exporting module’s const table when
	# the origin const value is already available in the shared TypeTable.
	#
	# This covers the source-only workspace case (all modules provided as source).
	# When the origin const is provided by a package, the value is imported later
	# in the driver pipeline (after package TypeId remapping); in that case we
	# leave the const unresolved here and let `driftc` materialize it once the
	# origin const becomes available.
	for exporting_mid, targets in reexported_const_targets_by_module.items():
		for local_name, (origin_mid, origin_name) in targets.items():
			origin_sym = f"{origin_mid}::{origin_name}"
			dst_sym = f"{exporting_mid}::{local_name}"
			origin_entry = shared_type_table.lookup_const(origin_sym)
			if origin_entry is None:
				continue
			origin_tid, origin_val = origin_entry
			prev = shared_type_table.lookup_const(dst_sym)
			if prev is not None:
				if prev != (origin_tid, origin_val):
					diagnostics.append(
						Diagnostic(
							message=f"const '{dst_sym}' defined with a different value than re-export target '{origin_sym}'",
							severity="error",
							span=Span(),
						)
					)
				continue
			shared_type_table.define_const(module_id=exporting_mid, name=local_name, type_id=origin_tid, value=origin_val)

	# Rewrite call sites: HCall(fn=HVar(name="foo")) -> HVar(name="m::foo") for imported/local functions.
	local_maps: dict[str, dict[str, str]] = {
		mid: {fn.name: _qualify_fn_name(mid, fn.name) for fn in getattr(prog, "functions", []) or []}
		for mid, prog in merged_programs.items()
	}

	def _rewrite_calls_in_block(
		block: H.HBlock,
		*,
		module_id: str,
		fn_id: FunctionId,
		fn_symbol: str,
		origin_file: Path | None,
	) -> None:
		local_map = local_maps.get(module_id, {})
		file_module_aliases = module_aliases_by_file.get(origin_file or Path(), {})
		# Call-site rewriting must be scope-correct: a local binding shadows only
		# within its lexical block, not across the whole function.
		#
		# This is still a limited MVP resolver (it only rewrites direct calls
		# represented as `HCall(HVar("foo"))`), but it avoids silent miscompiles by:
		# - never rewriting names that are currently bound (params, lets, binders),
		# - applying bindings as statements are traversed (let-binding is visible
		#   only *after* its initializer).
		param_names: list[str] = []
		sig = all_sigs.get(fn_id)
		if sig is not None and getattr(sig, "param_names", None):
			param_names = [p for p in sig.param_names if p]

		def rewrite_name(name: str, *, bound: set[str]) -> str:
			if name in bound:
				return name
			if name in local_map:
				return local_map[name]
			return name

		def rewrite_const_name(name: str, *, bound: set[str]) -> str:
			if name in bound:
				return name
			return name

		def exported_value_names(mod: str) -> set[str]:
			if mod in exports_values_by_module:
				return set((exports_values_by_module.get(mod) or {}).keys())
			if external_module_exports is not None and mod in external_module_exports:
				ext = external_module_exports.get(mod) or {}
				return set(ext.get("values") or set())
			return set()

		def exported_type_names(mod: str) -> set[str]:
			if mod in exports_types_by_module:
				return _union_exported_types(exports_types_by_module.get(mod))
			if external_module_exports is not None and mod in external_module_exports:
				ext = external_module_exports.get(mod) or {}
				ext_types = ext.get("types")
				if isinstance(ext_types, dict):
					return (
						set(ext_types.get("structs") or set())
						| set(ext_types.get("variants") or set())
						| set(ext_types.get("exceptions") or set())
					)
				return set()
			return set()

		def exported_const_names(mod: str) -> set[str]:
			if mod in exports_consts_by_module:
				return set(exports_consts_by_module.get(mod) or set())
			if external_module_exports is not None and mod in external_module_exports:
				ext = external_module_exports.get(mod) or {}
				return set(ext.get("consts") or set())
			return set()

		def exported_struct_names(mod: str) -> set[str]:
			if mod in exports_types_by_module:
				return set((exports_types_by_module.get(mod) or {}).get("structs") or set())
			if external_module_exports is not None and mod in external_module_exports:
				ext = external_module_exports.get(mod) or {}
				ext_types = ext.get("types")
				if isinstance(ext_types, dict):
					return set(ext_types.get("structs") or set())
				return set()
			return set()

		def _rewrite_module_qualified_call(
			*,
			receiver: H.HExpr,
			member: str,
			args: list[H.HExpr],
			kwargs: list[H.HKwArg],
			type_args: list[object] | None,
		) -> H.HExpr | None:
			"""
			Rewrite a syntactic member call `x.member(...)` when `x` is a module alias.

			MVP surface rule (pinned):
			  import lib as x
			  x.foo(1, 2)   // call exported function foo from module lib
			  x.Point(...)  // call struct constructor Point from module lib

			We do *not* create a runtime module object. Instead, we resolve the
			member at compile time and rewrite the callee to a fully-qualified
			callable symbol (`lib::foo`) or an unqualified struct constructor (`Point`).

			Note on representation: in stage1 HIR, a `.`-call like `x.foo(...)` is
			represented as `HMethodCall(receiver=x, method_name=\"foo\", ...)` (method
			sugar). We reuse that syntactic form for module-qualified access and
			rewrite it here into a plain `HCall` once we confirm `x` is a module alias.
			"""
			if not isinstance(receiver, H.HVar):
				return None
			if receiver.binding_id is not None:
				# Local/param shadowing wins: `x.foo` refers to the local `x`, not a module.
				return None
			alias = receiver.name
			mod = file_module_aliases.get(alias)
			if mod is None:
				return None
			vals = exported_value_names(mod)
			types = exported_type_names(mod)
			structs = exported_struct_names(mod)
			if member in vals:
				return H.HCall(
					fn=H.HVar(name=_qualify_fn_name(mod, member)),
					args=args,
					kwargs=kwargs,
					type_args=type_args,
				)
			if member in structs:
				# Constructor call through a module alias. MVP supports only struct ctors.
				def_mod, def_name = reexported_type_targets_by_module.get(mod, {}).get("structs", {}).get(member, (mod, member))
				struct_id = shared_type_table.get_nominal(kind=TypeKind.STRUCT, module_id=def_mod, name=def_name)
				if struct_id is None:
					diagnostics.append(
						Diagnostic(
							message=f"module-qualified constructor call '{alias}.{member}(...)' is only supported for structs in MVP",
							severity="error",
							span=getattr(receiver, "loc", Span()),
						)
					)
					return None
				# Rewrite to an internal fully-qualified constructor name so later
				# phases can resolve it deterministically even when multiple modules
				# define the same short type name.
				return H.HCall(
					fn=H.HVar(name=f"{def_mod}::{def_name}"),
					args=args,
					kwargs=kwargs,
					type_args=type_args,
				)
			available = ", ".join(sorted(vals | types))
			notes = (
				[f"available exports: {available}"]
				if available
				else [f"module '{mod}' exports nothing (private by default)"]
			)
			diagnostics.append(
				Diagnostic(
					message=f"module '{mod}' does not export symbol '{member}'",
					severity="error",
					span=getattr(receiver, "loc", Span()),
					notes=notes,
				)
			)
			return None

		def walk_block(b: H.HBlock, *, bound: set[str]) -> None:
			scope_bound = set(bound)
			for st in b.statements:
				walk_stmt(st, bound=scope_bound)
				if isinstance(st, H.HLet):
					scope_bound.add(st.name)

		def walk_expr(expr: H.HExpr, *, bound: set[str]) -> H.HExpr:
			# Module-qualified access: the surface syntax is `x.foo(...)`. Stage1
			# initially represents this as `HMethodCall`, so we rewrite that form
			# when `x` resolves to a module alias in the current file.
			if isinstance(expr, H.HMethodCall):
				expr.receiver = walk_expr(expr.receiver, bound=bound)
				expr.args = [walk_expr(a, bound=bound) for a in expr.args]
				for kw in getattr(expr, "kwargs", []) or []:
					if getattr(kw, "value", None) is not None:
						kw.value = walk_expr(kw.value, bound=bound)
				rewritten = _rewrite_module_qualified_call(
					receiver=expr.receiver,
					member=expr.method_name,
					args=expr.args,
					kwargs=getattr(expr, "kwargs", []) or [],
					type_args=getattr(expr, "type_args", None),
				)
				if rewritten is not None:
					return rewritten
				return expr

			if isinstance(expr, H.HCall):
				expr.fn = walk_expr(expr.fn, bound=bound)
				expr.args = [walk_expr(a, bound=bound) for a in expr.args]
				for kw in getattr(expr, "kwargs", []) or []:
					if getattr(kw, "value", None) is not None:
						kw.value = walk_expr(kw.value, bound=bound)
				# Resolve direct calls that name a local/imported/free function.
				if isinstance(expr.fn, H.HVar):
					expr.fn.name = rewrite_name(expr.fn.name, bound=bound)
				elif isinstance(expr.fn, H.HField) and isinstance(expr.fn.subject, H.HVar):
					# Handle the (rarer) explicit field-call form: `(x.foo)(...)`.
					q = _rewrite_module_qualified_call(
						receiver=expr.fn.subject,
						member=expr.fn.name,
						args=expr.args,
						kwargs=getattr(expr, "kwargs", []) or [],
						type_args=getattr(expr, "type_args", None),
					)
					if isinstance(q, H.HCall):
						# Preserve the rewritten call and ignore the original callee expression.
						return q
				return expr

			if isinstance(expr, H.HVar):
				expr.name = rewrite_const_name(expr.name, bound=bound)
				return expr

			if isinstance(expr, H.HField) and isinstance(expr.subject, H.HVar) and expr.subject.binding_id is None:
				mod = file_module_aliases.get(expr.subject.name)
				if mod is not None:
					if expr.name in exported_value_names(mod):
						return H.HVar(name=_qualify_fn_name(mod, expr.name))
					if expr.name in exported_const_names(mod):
						# Module-qualified const access always targets the module’s own
						# const table. Const re-exports are materialized by copying the
						# literal value into the exporting module, so consumers do not
						# need to reference the origin module.
						return H.HVar(name=f"{mod}::{expr.name}")
					available = ", ".join(sorted(exported_value_names(mod) | exported_const_names(mod) | exported_type_names(mod)))
					notes = (
						[f"available exports: {available}"]
						if available
						else [f"module '{mod}' exports nothing (private by default)"]
					)
					diagnostics.append(
						Diagnostic(
							message=f"module '{mod}' does not export symbol '{expr.name}'",
							severity="error",
							span=getattr(expr.subject, "loc", Span()),
							notes=notes,
						)
					)
					# Note: module-qualified type names are handled in type positions
					# via TypeExpr.module_id. Expression-position `x.Point` without
					# call is not a supported surface construct in MVP.
				return expr

			# Generic recursion for other expression shapes.
			for k, child in list(getattr(expr, "__dict__", {}).items()):
				if isinstance(child, H.HExpr):
					setattr(expr, k, walk_expr(child, bound=bound))
				elif isinstance(child, H.HBlock):
					walk_block(child, bound=bound)
				elif isinstance(child, list):
					new_list = []
					for it in child:
						if isinstance(it, H.HExpr):
							new_list.append(walk_expr(it, bound=bound))
						elif isinstance(it, H.HBlock):
							walk_block(it, bound=bound)
							new_list.append(it)
						# Expression-form arms (match/try) live under expression nodes and
						# must be handled here so binders introduce lexical scopes.
						elif hasattr(H, "HMatchArm") and isinstance(it, getattr(H, "HMatchArm")):
							arm_bound = set(bound)
							for bname in getattr(it, "binders", []) or []:
								arm_bound.add(bname)
							walk_block(it.block, bound=arm_bound)
							if getattr(it, "result", None) is not None:
								it.result = walk_expr(it.result, bound=arm_bound)
							new_list.append(it)
						elif hasattr(H, "HTryExprArm") and isinstance(it, getattr(H, "HTryExprArm")):
							arm_bound = set(bound)
							if getattr(it, "binder", None):
								arm_bound.add(it.binder)
							walk_block(it.block, bound=arm_bound)
							if getattr(it, "result", None) is not None:
								it.result = walk_expr(it.result, bound=arm_bound)
							new_list.append(it)
						else:
							new_list.append(it)
					setattr(expr, k, new_list)
			return expr

		def walk_stmt(stmt: H.HStmt, *, bound: set[str]) -> None:
			if isinstance(stmt, H.HTry):
				walk_block(stmt.body, bound=bound)
				for arm in stmt.catches:
					arm_bound = set(bound)
					if arm.binder:
						arm_bound.add(arm.binder)
					walk_block(arm.block, bound=arm_bound)
				return
			for k, child in list(getattr(stmt, "__dict__", {}).items()):
				if isinstance(child, H.HExpr):
					setattr(stmt, k, walk_expr(child, bound=bound))
				elif isinstance(child, H.HBlock):
					walk_block(child, bound=bound)
				elif isinstance(child, list):
					new_list = []
					for it in child:
						if isinstance(it, H.HStmt):
							walk_stmt(it, bound=bound)
							new_list.append(it)
						elif isinstance(it, H.HExpr):
							new_list.append(walk_expr(it, bound=bound))
						elif isinstance(it, H.HBlock):
							walk_block(it, bound=bound)
							new_list.append(it)
						elif hasattr(H, "HCatchArm") and isinstance(it, getattr(H, "HCatchArm")):
							arm_bound = set(bound)
							if getattr(it, "binder", None):
								arm_bound.add(it.binder)
							walk_block(it.block, bound=arm_bound)
							new_list.append(it)
						elif hasattr(H, "HMatchArm") and isinstance(it, getattr(H, "HMatchArm")):
							arm_bound = set(bound)
							for bname in getattr(it, "binders", []) or []:
								arm_bound.add(bname)
							walk_block(it.block, bound=arm_bound)
							if getattr(it, "result", None) is not None:
								it.result = walk_expr(it.result, bound=arm_bound)
							new_list.append(it)
						elif hasattr(H, "HTryExprArm") and isinstance(it, getattr(H, "HTryExprArm")):
							arm_bound = set(bound)
							if getattr(it, "binder", None):
								arm_bound.add(it.binder)
							walk_block(it.block, bound=arm_bound)
							if getattr(it, "result", None) is not None:
								it.result = walk_expr(it.result, bound=arm_bound)
							new_list.append(it)
						else:
							new_list.append(it)
					setattr(stmt, k, new_list)

		initial_bound = set(param_names)
		walk_block(block, bound=initial_bound)

	# Apply rewrite to each function body using its origin file’s import environment.
	fn_origin_file: dict[str, Path] = {}
	for _mid, origins in origin_by_module.items():
		for fn_id, src_path in origins.items():
			fn_symbol = fn_symbol_by_id.get(fn_id, _qualify_fn_name(fn_id.module, fn_id.name))
			fn_origin_file[fn_symbol] = src_path

	for fn_id, block in all_func_hirs.items():
		fn_symbol = fn_symbol_by_id.get(fn_id, _qualify_fn_name(fn_id.module, fn_id.name))
		_rewrite_calls_in_block(
			block,
			module_id=fn_owner_module.get(fn_id, "main"),
			fn_id=fn_id,
			fn_symbol=fn_symbol,
			origin_file=fn_origin_file.get(fn_symbol),
		)

	# Cross-module exception code collision detection: event codes are derived
	# from the canonical event FQN (`module:Event`). Collisions are extremely
	# unlikely, but if they happen we must diagnose them deterministically.
	payload_seen: dict[int, str] = {}
	for fqn, code in exc_catalog.items():
		payload = code & PAYLOAD_MASK
		other = payload_seen.get(payload)
		if other is not None and other != fqn:
			diagnostics.append(Diagnostic(message=f"exception code collision between '{other}' and '{fqn}' (payload {payload})", severity="error", span=Span()))
		else:
			payload_seen[payload] = fqn

	return all_func_hirs, all_sigs, fn_ids_by_name, shared_type_table, exc_catalog, module_exports, deps, diagnostics


def _lower_parsed_program_to_hir(
	prog: parser_ast.Program,
	*,
	diagnostics: list[Diagnostic] | None = None,
	type_table: TypeTable | None = None,
) -> Tuple[
	Dict[FunctionId, H.HBlock],
	Dict[FunctionId, FnSignature],
	Dict[str, List[FunctionId]],
	"TypeTable",
	Dict[str, int],
	List[ImplMeta],
	List[Diagnostic],
]:
	"""
	Lower an already-parsed `Program` to HIR/signatures/type table.

	This is shared by both single-file and multi-file entry points.
	"""
	diagnostics = list(diagnostics or [])
	module_name = getattr(prog, "module", None)
	module_id = module_name or "main"
	func_hirs: Dict[FunctionId, H.HBlock] = {}
	fn_ids_by_name: Dict[str, List[FunctionId]] = {}
	decls: list[_FrontendDecl] = []
	signatures: Dict[FunctionId, FnSignature] = {}
	impl_metas: list[ImplMeta] = []
	lowerer = AstToHIR()
	lowerer._module_name = module_id
	from lang2.driftc.traits.world import build_trait_world
	module_function_names: set[str] = {fn.name for fn in getattr(prog, "functions", []) or []}
	exception_schemas: dict[str, tuple[str, list[str]]] = {}
	struct_defs = list(getattr(prog, "structs", []) or [])
	variant_defs = list(getattr(prog, "variants", []) or [])
	exception_catalog: dict[str, int] = _build_exception_catalog(prog.exceptions, module_name, diagnostics)
	for exc in prog.exceptions:
		fqn = f"{module_name}:{exc.name}" if module_name else exc.name
		field_names = [arg.name for arg in getattr(exc, "args", [])]
		exception_schemas[fqn] = (fqn, field_names)
	# Build a TypeTable early so we can register user-defined type names (structs)
	# before resolving function signatures. This prevents `resolve_opaque_type`
	# from minting unrelated placeholder TypeIds for struct names.
	type_table = type_table or TypeTable()
	_prime_builtins(type_table)
	# Build a per-module TraitWorld and stash it on the shared TypeTable so later
	# phases can enforce requirements without re-parsing sources.
	world = build_trait_world(prog, diagnostics=diagnostics)
	trait_worlds = getattr(type_table, "trait_worlds", None)
	if not isinstance(trait_worlds, dict):
		trait_worlds = {}
	trait_worlds[module_id] = world
	type_table.trait_worlds = trait_worlds

	# Register module-local compile-time constants.
	#
	# MVP: const initializers are restricted to literal values (or unary +/- applied
	# to a numeric literal). We evaluate them here so later phases can
	# treat const references as typed literals without requiring whole-program
	# evaluation infrastructure.
	def _eval_const_value(expr: parser_ast.Expr) -> object | None:
		if isinstance(expr, parser_ast.Literal):
			return expr.value
		if isinstance(expr, parser_ast.Unary) and getattr(expr, "op", None) in ("-", "+"):
			inner = getattr(expr, "operand", None)
			if isinstance(inner, parser_ast.Literal) and isinstance(inner.value, (int, float)):
				if getattr(expr, "op", None) == "-":
					return -inner.value
				return inner.value
		return None

	for c in getattr(prog, "consts", []) or []:
		decl_ty = resolve_opaque_type(c.type_expr, type_table, module_id=module_id)
		val = _eval_const_value(c.value)
		if val is None:
			diagnostics.append(
				Diagnostic(
					phase="typecheck",
					message=(
						f"const '{c.name}' initializer must be a compile-time literal in MVP "
						"(Int/Uint/Bool/String/Float, optionally with unary '+' or '-')"
					),
					severity="error",
					span=Span.from_loc(getattr(c, "loc", None)),
				)
			)
			continue
		# Enforce that the declared type matches the literal kind exactly.
		#
		# Consts are intentionally strict: they form part of the module interface,
		# and packages must be able to embed them deterministically without
		# re-running the evaluator.
		ok = False
		if decl_ty == type_table.ensure_int() and isinstance(val, int):
			ok = True
		elif decl_ty == type_table.ensure_uint() and isinstance(val, int) and val >= 0:
			ok = True
		elif decl_ty == type_table.ensure_bool() and isinstance(val, bool):
			ok = True
		elif decl_ty == type_table.ensure_string() and isinstance(val, str):
			ok = True
		elif decl_ty == type_table.ensure_float() and isinstance(val, float):
			ok = True
		if not ok:
			diagnostics.append(
				Diagnostic(
					phase="typecheck",
					message=f"const '{c.name}' declared type does not match initializer value",
					severity="error",
					span=Span.from_loc(getattr(c, "loc", None)),
				)
			)
			continue
		type_table.define_const(module_id=module_id, name=c.name, type_id=decl_ty, value=val)
	# Prelude: `Optional<T>` is required for iterator-style `for` desugaring and
	# other control-flow sugar. Until modules are supported, the compiler injects
	# a canonical `Optional<T>` variant base into every compilation unit unless
	# user code declares its own `variant Optional<...>`.
	#
	# MVP contract:
	#   variant Optional<T> { Some(value: T), None }
	if not any(getattr(v, "name", None) == "Optional" for v in variant_defs) and type_table.get_variant_base(
		module_id="lang.core", name="Optional"
	) is None:
		type_table.declare_variant(
			"lang.core",
			"Optional",
			["T"],
			[
				VariantArmSchema(
					name="Some",
					fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr.param(0))],
				),
				VariantArmSchema(name="None", fields=[]),
			],
		)
	# Declare all struct names first (placeholder field types) to support recursion.
	for s in struct_defs:
		field_names = [f.name for f in getattr(s, "fields", [])]
		try:
			type_table.declare_struct(
				module_id,
				s.name,
				field_names,
				list(getattr(s, "type_params", []) or []),
			)
		except ValueError as err:
			diagnostics.append(Diagnostic(message=str(err), severity="error", span=Span.from_loc(getattr(s, "loc", None))))
	# Declare all variant names/schemas next so type resolution can instantiate
	# variants (e.g., Optional<Int>) while resolving later annotations/fields.
	for v in variant_defs:
		arms: list[VariantArmSchema] = []
		for arm in getattr(v, "arms", []) or []:
			fields = [
				VariantFieldSchema(
					name=f.name,
					type_expr=_generic_type_expr_from_parser(f.type_expr, type_params=list(getattr(v, "type_params", []) or [])),
				)
				for f in getattr(arm, "fields", []) or []
			]
			arms.append(VariantArmSchema(name=arm.name, fields=fields))
		try:
			type_table.declare_variant(
				module_id,
				v.name,
				list(getattr(v, "type_params", []) or []),
				arms,
			)
		except ValueError as err:
			diagnostics.append(Diagnostic(message=str(err), severity="error", span=Span.from_loc(getattr(v, "loc", None))))
	# Fill field TypeIds in a second pass now that all names exist.
	for s in struct_defs:
		struct_id = type_table.require_nominal(kind=TypeKind.STRUCT, module_id=module_id, name=s.name)
		type_params = list(getattr(s, "type_params", []) or [])
		field_types = []
		field_templates = []
		for f in getattr(s, "fields", []):
			field_templates.append(
				StructFieldSchema(
					name=f.name,
					type_expr=_generic_type_expr_from_parser(f.type_expr, type_params=type_params),
				)
			)
			if type_params:
				continue
			ft = resolve_opaque_type(f.type_expr, type_table, module_id=module_id)
			# MVP escape policy: references cannot be stored in long-lived memory.
			# Struct fields are long-lived by construction, so `struct S(r: &T)` is
			# rejected early (before lowering/typecheck) with a source-anchored
			# diagnostic.
			try:
				td = type_table.get(ft)
			except Exception:
				td = None
			if td is not None and td.kind is TypeKind.REF:
				diagnostics.append(
					Diagnostic(
						message=f"struct '{s.name}' field '{f.name}' cannot have a reference type in MVP",
						severity="error",
						span=Span.from_loc(getattr(f.type_expr, "loc", getattr(f, "loc", None))),
					)
				)
			field_types.append(ft)
		type_table.define_struct_schema_fields(struct_id, field_templates)
		if not type_params:
			type_table.define_struct_fields(struct_id, field_types)
	# After all variant schemas are known and structs are declared, finalize
	# non-generic variants so their concrete arm types are available.
	type_table.finalize_variants()
	seen_sig: dict[tuple, object | None] = {}
	name_ord: dict[str, int] = {}
	for fn in prog.functions:
		sig_key = (
			module_id,
			fn.name,
			len(getattr(fn, "params", []) or []),
			tuple(_type_expr_key(p.type_expr) for p in getattr(fn, "params", []) or []),
		)
		if sig_key in seen_sig:
			diagnostics.append(
				Diagnostic(
					message=f"duplicate function signature for '{fn.name}'",
					severity="error",
					span=Span.from_loc(getattr(fn, "loc", None)),
				)
			)
			continue
		seen_sig[sig_key] = getattr(fn, "loc", None)
		ordinal = name_ord.get(fn.name, 0)
		name_ord[fn.name] = ordinal + 1
		fn_id = FunctionId(module=module_id, name=fn.name, ordinal=ordinal)
		qualified_name = _qualify_fn_name(module_id, fn.name)
		fn_ids_by_name.setdefault(qualified_name, []).append(fn_id)
		decl_decl = _decl_from_parser_fn(fn, fn_id=fn_id)
		decl_decl.module = module_id
		# Reject FnResult in surface type annotations (return or parameter types).
		# FnResult is an internal ABI carrier in lang2, not a user-facing type.
		if _typeexpr_uses_internal_fnresult(decl_decl.return_type):
			_report_internal_fnresult_in_surface_type(
				kind="function",
				symbol=fn.name,
				loc=getattr(fn.return_type, "loc", getattr(fn, "loc", None)),
				diagnostics=diagnostics,
			)
		for p in getattr(fn, "params", []) or []:
			if _typeexpr_uses_internal_fnresult(p.type_expr):
				_report_internal_fnresult_in_surface_type(
					kind="parameter",
					symbol=f"{fn.name}({p.name})",
					loc=getattr(p.type_expr, "loc", getattr(p, "loc", None)),
					diagnostics=diagnostics,
				)
			if getattr(p, "non_escaping", False) and not _typeexpr_is_callable(p.type_expr):
				diagnostics.append(
					_diagnostic(
						f"nonescaping parameter '{fn.name}({p.name})' must have a callable type",
						getattr(p.type_expr, "loc", getattr(p, "loc", None)),
					)
				)
		decls.append(decl_decl)
		stmt_block = _convert_block(fn.body)
		param_names = [p.name for p in getattr(fn, "params", []) or []]
		hir_block = lowerer.lower_function_block(stmt_block, param_names=param_names)
		func_hirs[fn_id] = hir_block
	# Methods inside implement blocks.
	for impl_index, impl in enumerate(getattr(prog, "implements", [])):
		# Reject reference-qualified impl headers in v1 (must be nominal types).
		if getattr(impl.target, "name", None) in {"&", "&mut"}:
			diagnostics.append(
				Diagnostic(
					message="implement header must use a nominal type, not a reference type",
					severity="error",
					span=Span.from_loc(getattr(impl, "loc", None)),
				)
			)
			continue
		impl_type_params = list(getattr(impl, "type_params", []) or [])
		impl_type_param_locs = list(getattr(impl, "type_param_locs", []) or [])
		impl_target_str = _type_expr_key_str(impl.target)
		impl_trait_str = _type_expr_key_str(impl.trait) if getattr(impl, "trait", None) is not None else None
		impl_owner = FunctionId(
			module="lang.__internal",
			name=f"__impl_{module_id}::{impl_trait_str or 'inherent'}::{impl_target_str}",
			ordinal=impl_index,
		)
		impl_param_ids = {name: TypeParamId(impl_owner, idx) for idx, name in enumerate(impl_type_params)}
		impl_target_type_id = resolve_opaque_type(
			impl.target,
			type_table,
			module_id=module_id,
			type_params=impl_param_ids,
		)
		impl_meta = ImplMeta(
			impl_id=impl_index,
			def_module=module_id,
			target_type_id=impl_target_type_id,
			loc=Span.from_loc(getattr(impl, "loc", None)),
			methods=[],
		)
		for fn in impl.methods:
			# Note: receiver shape/name/type are semantic rules enforced by the
			# typecheck phase. The parser adapter stays structural-only here so
			# related errors consistently report as typecheck diagnostics.
			receiver_ty = fn.params[0].type_expr if fn.params else None
			self_mode: str | None = None
			if receiver_ty is not None:
				self_mode = "value"
				if receiver_ty.name == "&":
					self_mode = "ref"
				elif receiver_ty.name == "&mut":
					self_mode = "ref_mut"

			trait_key = _type_expr_key(impl.trait) if getattr(impl, "trait", None) is not None else None
			trait_str = _type_expr_key_str(impl.trait) if getattr(impl, "trait", None) is not None else None
			# Compute the canonical symbol for this method early so any diagnostics
			# (including type-annotation validation) can reference it.
			target_key = _impl_target_key(impl.target, impl_type_params)
			target_str = _type_expr_key_str(impl.target)
			if trait_str:
				symbol_name = f"{target_str}::{trait_str}::{fn.name}"
			else:
				symbol_name = f"{target_str}::{fn.name}"

			params = [
				_FrontendParam(
					p.name,
					p.type_expr,
					getattr(p, "loc", None),
					non_escaping=getattr(p, "non_escaping", False),
				)
				for p in fn.params
			]
			# Reject FnResult in method surface type annotations too.
			if _typeexpr_uses_internal_fnresult(fn.return_type):
				_report_internal_fnresult_in_surface_type(
					kind="method",
					symbol=symbol_name,
					loc=getattr(fn.return_type, "loc", getattr(fn, "loc", None)),
					diagnostics=diagnostics,
				)
			for p in getattr(fn, "params", []) or []:
				if _typeexpr_uses_internal_fnresult(p.type_expr):
					_report_internal_fnresult_in_surface_type(
						kind="parameter",
						symbol=f"{symbol_name}({p.name})",
						loc=getattr(p.type_expr, "loc", getattr(p, "loc", None)),
						diagnostics=diagnostics,
					)
				if getattr(p, "non_escaping", False) and not _typeexpr_is_callable(p.type_expr):
					diagnostics.append(
						_diagnostic(
							f"nonescaping parameter '{symbol_name}({p.name})' must have a callable type",
							getattr(p.type_expr, "loc", getattr(p, "loc", None)),
						)
					)
			if fn.name in module_function_names:
				diagnostics.append(
					Diagnostic(
						message=f"method '{fn.name}' conflicts with existing free function of the same name",
						severity="error",
						span=Span.from_loc(getattr(fn, "loc", None)),
					)
				)
				continue
			ordinal = name_ord.get(symbol_name, 0)
			name_ord[symbol_name] = ordinal + 1
			fn_id = FunctionId(module=module_id, name=symbol_name, ordinal=ordinal)
			fn_ids_by_name.setdefault(symbol_name, []).append(fn_id)
			impl_meta.methods.append(
				ImplMethodMeta(
					fn_id=fn_id,
					name=fn.name,
					is_pub=bool(getattr(fn, "is_pub", False)),
					loc=Span.from_loc(getattr(fn, "loc", None)),
				)
			)
			decls.append(
				_FrontendDecl(
					fn_id,
					symbol_name,
					fn.orig_name,
					fn.type_params,
					list(getattr(fn, "type_param_locs", []) or []),
					params,
					fn.return_type,
					getattr(fn, "loc", None),
					fn.is_pub,
					is_method=True,
					self_mode=self_mode,
					impl_target=impl.target,
					impl_type_params=impl_type_params,
					impl_type_param_locs=impl_type_param_locs,
					impl_owner=impl_owner,
					module=module_id,
				)
			)
			stmt_block = _convert_block(fn.body)
			# Enable implicit `self` member lookup for method bodies (spec §3.9).
			# Unknown identifiers may resolve to fields/methods on `self` after
			# locals and module-scope items are considered.
			#
			# We only need names here; semantic validation happens in the typed checker.
			# Collect receiver field names for implicit `self` member lookup.
			#
			# IMPORTANT: structs are module-scoped. We must resolve the impl target
			# in the current module context, not by bare name.
			field_names: set[str] = set()
			try:
				origin_mod = getattr(impl.target, "module_id", None) or module_name or "main"
				struct_id = type_table.get_struct_base(module_id=origin_mod, name=impl.target.name)
				if struct_id is not None:
					td = type_table.get(struct_id)
					if td.field_names is not None:
						field_names = set(td.field_names)
			except Exception:
				field_names = set()
			method_names: set[str] = {m.name for m in getattr(impl, "methods", []) or []}
			param_names = [p.name for p in getattr(fn, "params", []) or []]
			if fn.params and self_mode is not None:
				lowerer._push_implicit_self(
					self_name=str(getattr(fn.params[0], "name", "self")),
					self_mode=self_mode,
					field_names=field_names,
					method_names=method_names,
					module_function_names=module_function_names,
				)
				try:
					hir_block = lowerer.lower_function_block(stmt_block, param_names=param_names)
				finally:
					lowerer._pop_implicit_self()
			else:
				hir_block = lowerer.lower_function_block(stmt_block, param_names=param_names)
			func_hirs[fn_id] = hir_block
		impl_metas.append(impl_meta)
	# Build signatures with resolved TypeIds from parser decls.
	from lang2.driftc.type_resolver import resolve_program_signatures

	type_table, sigs = resolve_program_signatures(decls, table=type_table)
	signatures.update(sigs)
	# Resolve function require subjects (T -> TypeParamId) now that signatures exist.
	from lang2.driftc.traits.world import resolve_fn_require_subjects

	resolve_fn_require_subjects(world, signatures)
	# Thread exception schemas through the shared type table for downstream validators.
	#
	# In a multi-module build, this function may be called repeatedly with a
	# shared TypeTable; preserve previously registered schemas and extend them.
	prev_schemas = getattr(type_table, "exception_schemas", None)
	if not isinstance(prev_schemas, dict):
		prev_schemas = {}
	prev_schemas.update(exception_schemas)
	type_table.exception_schemas = prev_schemas
	return func_hirs, signatures, fn_ids_by_name, type_table, exception_catalog, impl_metas, diagnostics


def parse_drift_to_hir(path: Path) -> Tuple[Dict[FunctionId, H.HBlock], Dict[FunctionId, FnSignature], Dict[str, List[FunctionId]], "TypeTable", Dict[str, int], List[Diagnostic]]:
	"""
	Parse a Drift source file into lang2 HIR blocks + FnSignatures + TypeTable.

	Collects parser/adapter diagnostics (e.g., duplicate functions) instead of
	throwing, so callers can report them alongside later pipeline checks.
	"""
	source = path.read_text()
	try:
		prog = _parser.parse_program(source)
	except _parser.FStringParseError as err:
		return {}, {}, {}, TypeTable(), {}, [Diagnostic(message=str(err), severity="error", span=_span_in_file(path, err.loc))]
	except _parser.QualifiedMemberParseError as err:
		return {}, {}, {}, TypeTable(), {}, [Diagnostic(message=str(err), severity="error", span=_span_in_file(path, err.loc))]
	except UnexpectedInput as err:
		span = Span(
			file=str(path),
			line=getattr(err, "line", None),
			column=getattr(err, "column", None),
			raw=err,
		)
		return {}, {}, {}, TypeTable(), {}, [Diagnostic(message=str(err), severity="error", span=span)]
	func_hirs, sigs, fn_ids, table, excs, _impl_metas, diags = _lower_parsed_program_to_hir(
		prog,
		diagnostics=[],
	)
	return func_hirs, sigs, fn_ids, table, excs, diags


__all__ = ["parse_drift_to_hir", "parse_drift_files_to_hir", "parse_drift_workspace_to_hir"]
