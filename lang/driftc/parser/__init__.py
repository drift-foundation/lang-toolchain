"""
lang parser copy (self-contained, no runtime dependency on lang/).
Parses Drift source and adapts to lang.driftc.stage0 AST + FnSignatures for the
lang pipeline.
"""

from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import hashlib
import os
from typing import Callable, Dict, Tuple, Optional, List, TYPE_CHECKING

from lark.exceptions import UnexpectedInput

from . import parser as _parser
from . import ast as parser_ast
from lang.driftc.stage0 import ast as s0
from lang.driftc.stage1 import AstToHIR
from lang.driftc import stage1 as H
from lang.driftc.checker import FnSignature
from lang.driftc.core.diagnostics import Diagnostic

# Parser diagnostics should always carry phase.
def _p_diag(*args, **kwargs):
	if "phase" not in kwargs or kwargs.get("phase") is None:
		kwargs["phase"] = "parser"
	return Diagnostic(*args, **kwargs)


def stdlib_root() -> Path | None:
	root = Path(__file__).resolve().parents[3] / "stdlib"
	return root if root.exists() else None

from lang.driftc.core.span import Span
from lang.driftc.core.source_manager import SourceManager
from lang.driftc.core.types_core import TypeKind, TypeParamId
from lang.driftc.core.event_codes import event_code, PAYLOAD_MASK
from lang.driftc.core.function_id import FunctionId, function_symbol
from lang.driftc.core.types_core import (
	TypeTable,
	InterfaceMethodSchema,
	InterfaceParamSchema,
	StructFieldSchema,
	VariantArmSchema,
	VariantFieldSchema,
)
from lang.driftc.core.type_resolve_common import resolve_opaque_type
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.impl_index import ImplMeta, ImplMethodMeta
from lang.driftc.module_lowered import ModuleLowered
from lang.driftc.stage1 import assign_callsite_ids
if TYPE_CHECKING:
	from lang.driftc.traits.world import TraitKey

_RESERVED_NOMINAL_TYPE_NAMES: set[str] = {
	"Int",
	"Uint",
	"Byte",
	"Bool",
	"Float",
	"String",
	"Void",
	"Error",
	"DiagnosticValue",
	"Array",
	"Optional",
	"FnResult",
}


def _validate_module_id(
	mid: str,
	*,
	span: Span,
) -> list[Diagnostic]:
	"""
	Validate a module id per the language spec (format only; reserved namespaces
	are enforced by loader trust policy).

	This is shared by:
	- single-module builds (`parse_drift_files_to_hir`), and
	- workspace builds (`parse_drift_workspace_to_hir`), including inferred ids from `-M/--module-path`.
	"""
	if not isinstance(mid, str) or not mid:
		return [
			_p_diag(
				message="invalid module id (empty)",
				severity="error",
				span=span,
			)
		]
	raw_len = len(mid.encode("utf-8"))
	if raw_len > 254:
		return [
			_p_diag(
				message=f"invalid module id '{mid}': length {raw_len} exceeds 254 UTF-8 bytes",
				severity="error",
				span=span,
			)
		]
	if mid.startswith(".") or mid.endswith(".") or ".." in mid:
		return [
			_p_diag(
				message=f"invalid module id '{mid}': dots must separate non-empty segments",
				severity="error",
				span=span,
			)
		]
	if mid.startswith("_") or mid.endswith("_") or "__" in mid:
		return [
			_p_diag(
				message=f"invalid module id '{mid}': underscores must not be leading/trailing or consecutive",
				severity="error",
				span=span,
			)
		]
	segments = mid.split(".")
	for seg in segments:
		if not seg:
			return [
				_p_diag(
					message=f"invalid module id '{mid}': empty segment",
					severity="error",
					span=span,
				)
			]
		if seg.startswith("_") or seg.endswith("_") or "__" in seg:
			return [
				_p_diag(
					message=f"invalid module id '{mid}': segment '{seg}' has invalid underscore placement",
					severity="error",
					span=span,
				)
			]
		# MVP: segments must start with a lowercase letter to avoid ambiguous module
		# names and to keep directory→module inference predictable.
		if not ("a" <= seg[0] <= "z"):
			return [
				_p_diag(
					message=f"invalid module id '{mid}': segment '{seg}' must start with a lowercase letter",
					severity="error",
					span=span,
				)
			]
		for ch in seg:
			if not (("a" <= ch <= "z") or ("0" <= ch <= "9") or ch == "_"):
				return [
					_p_diag(
						message=f"invalid module id '{mid}': segment '{seg}' contains invalid character '{ch}'",
						severity="error",
						span=span,
					)
				]
	return []


def _reject_reserved_nominal_type(
	name: str,
	*,
	loc: object | None,
	diagnostics: list[Diagnostic],
) -> bool:
	if name in _RESERVED_NOMINAL_TYPE_NAMES:
		diagnostics.append(
			_p_diag(
				message=f"type name '{name}' is reserved by the compiler",
				severity="error",
				span=Span.from_loc(loc),
			)
		)
		return True
	return False


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
	table.ensure_byte()
	table.ensure_bool()
	table.ensure_float()
	table.ensure_string()
	table.ensure_void()
	table.ensure_error()
	table.ensure_diagnostic_value()
	# Seed commonly used derived types so TypeIds are stable across builds.


def _type_expr_to_str(typ: parser_ast.TypeExpr) -> str:
	"""Render a TypeExpr into a string (e.g., Array<Int>, Result<Int, Error>)."""
	if typ.name == "fn":
		args = list(getattr(typ, "args", []) or [])
		ret = args[-1] if args else None
		params = args[:-1] if args else []
		params_s = ", ".join(_type_expr_to_str(a) for a in params)
		ret_s = _type_expr_to_str(ret) if ret is not None else "<unknown>"
		if typ.can_throw():
			return f"Fn({params_s}) -> {ret_s}"
		return f"Fn({params_s}) nothrow -> {ret_s}"
	if not typ.args:
		return typ.name
	args = ", ".join(_type_expr_to_str(a) for a in typ.args)
	return f"{typ.name}<{args}>"


def _type_expr_key(typ: parser_ast.TypeExpr) -> tuple[object | None, str, tuple]:
	qual = getattr(typ, "module_id", None) or getattr(typ, "module_alias", None)
	if typ.name == "fn":
		throws_key = typ.fn_throws_raw()
		return (qual, typ.name, throws_key, tuple(_type_expr_key(a) for a in getattr(typ, "args", []) or []))
	return (qual, typ.name, tuple(_type_expr_key(a) for a in getattr(typ, "args", []) or []))


def _trait_subject_key(subject: object) -> object:
	if isinstance(subject, parser_ast.SelfRef):
		return ("self",)
	if isinstance(subject, parser_ast.TypeNameRef):
		return ("name", subject.name)
	return ("name", subject)


def _trait_expr_key(expr: parser_ast.TraitExpr | None) -> tuple | None:
	if expr is None:
		return None
	if isinstance(expr, parser_ast.TraitIs):
		return ("is", _trait_subject_key(expr.subject), _type_expr_key(expr.trait))
	if isinstance(expr, parser_ast.TraitAnd):
		return ("and", _trait_expr_key(expr.left), _trait_expr_key(expr.right))
	if isinstance(expr, parser_ast.TraitOr):
		return ("or", _trait_expr_key(expr.left), _trait_expr_key(expr.right))
	if isinstance(expr, parser_ast.TraitNot):
		return ("not", _trait_expr_key(expr.expr))
	return ("unknown",)


def _type_expr_key_str(typ: parser_ast.TypeExpr) -> str:
	qual = getattr(typ, "module_id", None) or getattr(typ, "module_alias", None)
	base = f"{qual}.{typ.name}" if qual else typ.name
	if typ.name == "fn":
		args = list(getattr(typ, "args", []) or [])
		ret = args[-1] if args else None
		params = args[:-1] if args else []
		params_s = ", ".join(_type_expr_key_str(a) for a in params)
		ret_s = _type_expr_key_str(ret) if ret is not None else "<unknown>"
		if typ.can_throw():
			return f"Fn({params_s}) -> {ret_s}"
		return f"Fn({params_s}) nothrow -> {ret_s}"
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
	if typ.name == "fn":
		return GenericTypeExpr.named(
			typ.name,
			[_generic_type_expr_from_parser(a, type_params=type_params) for a in getattr(typ, "args", [])],
			module_id=getattr(typ, "module_id", None),
			fn_throws=typ.fn_throws_raw(),
		)
	return GenericTypeExpr.named(
		typ.name,
		[_generic_type_expr_from_parser(a, type_params=type_params) for a in getattr(typ, "args", [])],
		module_id=getattr(typ, "module_id", None),
	)


def _build_interface_method_schemas(
	interface_def: parser_ast.InterfaceDef,
	*,
	module_id: str,
	type_table: TypeTable,
	diagnostics: list[Diagnostic],
) -> list[InterfaceMethodSchema]:
	interface_id = type_table.require_nominal(kind=TypeKind.INTERFACE, module_id=module_id, name=interface_def.name)
	interface_type_params = list(getattr(interface_def, "type_params", []) or [])
	seen_methods: set[str] = set()
	methods: list[InterfaceMethodSchema] = []
	for m in getattr(interface_def, "methods", []) or []:
		if m.name in seen_methods:
			diagnostics.append(
				_p_diag(
					message=f"duplicate method '{m.name}' in interface '{interface_def.name}'",
					severity="error",
					span=Span.from_loc(getattr(m, "loc", None)),
				)
			)
			continue
		seen_methods.add(m.name)
		method_type_params = list(getattr(m, "type_params", []) or [])
		method_param_set: set[str] = set()
		conflict = False
		for tp in method_type_params:
			if tp in method_param_set:
				conflict = True
				diagnostics.append(
					_p_diag(
						message=f"duplicate type parameter '{tp}' in interface method '{m.name}'",
						severity="error",
						span=Span.from_loc(getattr(m, "loc", None)),
					)
				)
			method_param_set.add(tp)
		for tp in method_type_params:
			if tp in interface_type_params:
				conflict = True
				diagnostics.append(
					_p_diag(
						message=f"interface method '{m.name}' shadows interface type parameter '{tp}'",
						severity="error",
						span=Span.from_loc(getattr(m, "loc", None)),
					)
				)
		if conflict:
			continue
		combined_type_params = list(interface_type_params) + list(method_type_params)
		method_param_schemas: list[InterfaceParamSchema] = []
		for p in getattr(m, "params", []) or []:
			method_param_schemas.append(
				InterfaceParamSchema(
					name=p.name,
					type_expr=_generic_type_expr_from_parser(p.type_expr, type_params=combined_type_params),
				)
			)
		method_schema = InterfaceMethodSchema(
			name=m.name,
			params=method_param_schemas,
			return_type=_generic_type_expr_from_parser(m.return_type, type_params=combined_type_params),
			type_params=list(method_type_params),
			declared_nothrow=bool(getattr(m, "declared_nothrow", False)),
			is_unsafe=bool(getattr(m, "is_unsafe", False)),
		)
		methods.append(method_schema)
	return methods


def _convert_expr(expr: parser_ast.Expr) -> s0.Expr:
	"""Convert parser AST expressions into lang.driftc.stage0 AST expressions."""
	def _convert_trait_subject(subject: object) -> object:
		if isinstance(subject, parser_ast.SelfRef):
			return s0.SelfRef(loc=Span.from_loc(getattr(subject, "loc", None)))
		if isinstance(subject, parser_ast.TypeNameRef):
			return s0.TypeNameRef(name=subject.name, loc=Span.from_loc(getattr(subject, "loc", None)))
		return subject

	if isinstance(expr, parser_ast.Literal):
		return s0.Literal(value=expr.value, loc=Span.from_loc(getattr(expr, "loc", None)))
	if isinstance(expr, parser_ast.Name):
		return s0.Name(ident=expr.ident, loc=Span.from_loc(getattr(expr, "loc", None)))
	if isinstance(expr, parser_ast.TraitIs):
		return s0.TraitIs(
			subject=_convert_trait_subject(expr.subject),
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
	if isinstance(expr, parser_ast.YieldExpr):
		return s0.YieldExpr(
			value=_convert_expr(expr.value),
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)
	if isinstance(expr, parser_ast.Lambda):
		params = [
			s0.Param(
				name=p.name,
				type_expr=p.type_expr,
				mutable=bool(getattr(p, "mutable", False)),
				loc=Span.from_loc(getattr(p, "loc", None)),
			)
			for p in expr.params
		]
		captures = None
		if getattr(expr, "captures", None) is not None:
			captures = [
				s0.CaptureItem(
					name=cap.name,
					kind=cap.kind,
					loc=Span.from_loc(getattr(cap, "loc", None)),
				)
				for cap in expr.captures
			]
		body_expr = _convert_expr(expr.body_expr) if expr.body_expr is not None else None
		body_block = s0.Block(statements=_convert_block(expr.body_block)) if expr.body_block is not None else None
		return s0.Lambda(
			params=params,
			ret_type=getattr(expr, "ret_type", None),
			captures=captures,
			body_expr=body_expr,
			body_block=body_block,
			declared_nothrow=bool(getattr(expr, "declared_nothrow", False)),
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
	if isinstance(expr, parser_ast.MacroCall):
		return s0.MacroCall(
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
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)
	if isinstance(expr, parser_ast.TypeApp):
		return s0.TypeApp(
			func=_convert_expr(expr.func),
			type_args=list(expr.type_args),
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)
	if isinstance(expr, parser_ast.Cast):
		return s0.Cast(
			target_type=expr.target_type,
			expr=_convert_expr(expr.expr),
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
	if isinstance(expr, parser_ast.MapLiteral):
		return s0.MapLiteral(
			entries=[
				s0.MapEntry(
					key=_convert_expr(entry.key),
					value=_convert_expr(entry.value),
					loc=Span.from_loc(getattr(entry, "loc", None)),
				)
				for entry in expr.entries
			],
			loc=Span.from_loc(getattr(expr, "loc", None)),
		)
	if isinstance(expr, parser_ast.Move):
		return s0.Move(value=_convert_expr(expr.value), loc=Span.from_loc(getattr(expr, "loc", None)))
	if isinstance(expr, parser_ast.Copy):
		return s0.Copy(value=_convert_expr(expr.value), loc=Span.from_loc(getattr(expr, "loc", None)))
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
				ctor_base=getattr(arm, "ctor_base", None),
				pattern_arg_form=getattr(arm, "pattern_arg_form", "positional"),
				binders=list(arm.binders),
				binder_fields=list(arm.binder_fields) if getattr(arm, "binder_fields", None) is not None else None,
				binder_is_mutable=list(arm.binder_is_mutable) if getattr(arm, "binder_is_mutable", None) is not None else None,
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
		capture=bool(getattr(stmt, "capture", False)),
		capture_alias=getattr(stmt, "capture_alias", None),
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
	return s0.ForStmt(
		iter_var=stmt.var,
		iterable=_convert_expr(stmt.iter_expr),
		body=_convert_block(stmt.body),
		iter_var_mutable=bool(getattr(stmt, "var_mutable", False)),
		iter_var_type=getattr(stmt, "var_type_expr", None),
		loc=Span.from_loc(stmt.loc),
	)


def _convert_for_count(stmt: parser_ast.ForCountStmt) -> s0.Stmt:
	return s0.ForCountStmt(
		init_name=stmt.init_name,
		init_value=_convert_expr(stmt.init_value),
		cond=_convert_expr(stmt.condition),
		step=_convert_stmt(stmt.step),
		body=_convert_block(stmt.body),
		init_mutable=bool(getattr(stmt, "init_mutable", False)),
		init_type=getattr(stmt, "init_type_expr", None),
		loc=Span.from_loc(stmt.loc),
	)


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


def _convert_unsafe_block(stmt: parser_ast.UnsafeBlockStmt) -> s0.Stmt:
	return s0.UnsafeBlockStmt(body=_convert_block(stmt.block), loc=Span.from_loc(stmt.loc))


def _convert_assert(stmt: parser_ast.AssertStmt) -> s0.Stmt:
	return s0.AssertStmt(
		cond=_convert_expr(stmt.cond),
		msg=_convert_expr(stmt.msg) if stmt.msg is not None else None,
		loc=Span.from_loc(stmt.loc),
	)


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
	parser_ast.ForCountStmt: _convert_for_count,
	parser_ast.ThrowStmt: _convert_throw,
	parser_ast.RaiseStmt: _convert_raise,
	parser_ast.RethrowStmt: _convert_rethrow,
	parser_ast.TryStmt: _convert_try,
	parser_ast.AssertStmt: _convert_assert,
	parser_ast.ImportStmt: _convert_import,
	parser_ast.UnsafeBlockStmt: _convert_unsafe_block,
}


def _convert_stmt(stmt: parser_ast.Stmt) -> s0.Stmt:
	"""Convert parser AST statements into lang.driftc.stage0 AST statements."""
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
		mutable: bool = False,
	) -> None:
		self.name = name
		# Preserve the parsed type expression so the resolver can build real TypeIds.
		self.type = type_expr
		self.loc = loc
		self.mutable = bool(mutable)


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
		declared_nothrow: bool = False,
		declared_throws: bool = False,
		is_unsafe: bool = False,
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
		self.declared_nothrow = declared_nothrow
		self.declared_throws = declared_throws
		self.is_unsafe = is_unsafe
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
			mutable=bool(getattr(p, "mutable", False)),
		)
		for p in fn.params
	]
	decl = _FrontendDecl(
		fn_id,
		fn.name,
		fn.orig_name,
		fn.type_params,
		list(getattr(fn, "type_param_locs", []) or []),
		params,
		fn.return_type,
		getattr(fn, "loc", None),
		bool(getattr(fn, "declared_nothrow", False)),
		bool(getattr(fn, "declared_throws", False)),
		bool(getattr(fn, "is_unsafe", False)),
		fn.is_pub,
		fn.is_method,
		fn.self_mode,
		fn.impl_target,
		impl_type_params,
		impl_type_param_locs,
		impl_owner,
	)
	decl.is_intrinsic = bool(getattr(fn, "is_intrinsic", False))
	return decl


def _diagnostic(message: str, loc: object | None) -> Diagnostic:
	"""Helper to create a Diagnostic from a parser location."""
	return _p_diag(message=message, severity="error", span=Span.from_loc(loc))


def _is_trait_prop_value_pos_error(err: UnexpectedInput) -> bool:
	token = getattr(err, "token", None)
	if token is None or getattr(token, "type", None) != "IS":
		return False
	expected = set(getattr(err, "expected", None) or [])
	if not expected:
		return False
	expr_continuations = {
		"TERMINATOR",
		"RPAR",
		"BAR",
		"OR",
		"AND",
		"EQEQ",
		"NOTEQ",
		"LT",
		"LTE",
		"GT",
		"GTE",
		"PLUS",
		"MINUS",
		"STAR",
		"SLASH",
		"PERCENT",
		"AMP",
		"CARET",
		"PIPE_FWD",
		"LSHIFT",
		"SHR",
		"DOT",
		"DCOLON",
		"LSQB",
		"CALL_TYPE_LT",
		"QUAL_TYPE_LT",
		"ARROW",
		"QMARK",
	}
	return bool(expected & expr_continuations)


def _is_expr_block_missing_value_error(err: UnexpectedInput) -> bool:
	token = getattr(err, "token", None)
	if token is None or getattr(token, "type", None) != "RBRACE":
		return False
	expected = set(getattr(err, "expected", None) or [])
	if not expected:
		return False
	expr_starters = {
		"NAME",
		"SIGNED_INT",
		"FLOAT",
		"STRING",
		"TRUE",
		"FALSE",
		"LPAR",
		"LBRACE",
		"LSQB",
		"MATCH",
		"TRY",
		"IF",
		"RETURN",
		"MOVE",
		"COPY",
		"THROW",
		"RAISE",
		"YIELD",
	}
	return bool(expected & expr_starters)


def _parse_error_code(err: UnexpectedInput) -> str | None:
	expected = getattr(err, "expected", None)
	token = getattr(err, "token", None)
	if _is_trait_prop_value_pos_error(err):
		return "E-TRAIT-PROP-VALUE-POS"
	if _is_expr_block_missing_value_error(err):
		return "E_EXPR_BLOCK_MISSING_VALUE"
	if expected and "COMMA" in expected:
		token_type = getattr(token, "type", None) if token is not None else None
		if token_type in {"NAME", "DEFAULT"}:
			return "E_EXPECTED_COMMA_BETWEEN_MATCH_ARMS"
	if expected and "TERMINATOR" in expected:
		return "E_EXPECTED_SEMICOLON"
	if token is not None and getattr(token, "type", None) == "TERMINATOR":
		return "E_UNEXPECTED_SEMICOLON_AFTER_COMPOUND"
	return None


def _parse_error_message(err: UnexpectedInput, code: str | None) -> str:
	if code == "E-TRAIT-PROP-VALUE-POS":
		return "trait propositions are only allowed in require clauses or if guards"
	if code == "E_EXPR_BLOCK_MISSING_VALUE":
		return "expression block must end with a value expression; return is not allowed in expression-form blocks"
	return str(err)


def _missing_import_module_message(
	mod: str,
	*,
	single_entry: bool,
) -> str:
	base = f"imported module '{mod}' not found"
	if single_entry:
		return base + "; module discovery uses provided sources/module roots: pass -M <dir> and compile all module files, or use --package-root for packaged modules"
	return base + "; ensure the module is included in compile sources (or available via --package-root for packaged modules)"


def _typeexpr_uses_internal_fnresult(typ: parser_ast.TypeExpr) -> bool:
	"""
	Return True if a surface type annotation mentions `FnResult` anywhere.

	`FnResult<T, Error>` is an internal ABI carrier used by lang for can-throw
	functions. It is not a surface type in the Drift language: user code should
	write `-> T` and use exceptions/try/catch for control flow.
	"""
	if typ.name == "FnResult":
		return True
	for arg in getattr(typ, "args", []) or []:
		if _typeexpr_uses_internal_fnresult(arg):
			return True
	return False


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
			"write `-> T` and use exceptions/try-catch instead",
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
		if _reject_reserved_nominal_type(getattr(exc, "name", ""), loc=getattr(exc, "loc", None), diagnostics=diagnostics):
			continue
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


def _span_in_file(path: Path, loc: object | None, source_manager: SourceManager | None = None) -> Span:
	"""
	Construct a Span that is anchored to a specific source file.

	The parser AST location objects do not carry a filename; for multi-file module
	builds we need the file to be explicit so diagnostics can point at the right
	origin.
	"""
	sm = source_manager or _ACTIVE_SOURCE_MANAGER
	if loc is None:
		file_id = sm.file_id_for_path(str(path)) if sm is not None else None
		return Span(file=str(path), file_id=file_id)
	span = Span.from_loc(loc)
	if span.file is None:
		file_id = span.file_id
		if file_id is None and sm is not None:
			file_id = sm.file_id_for_path(str(path))
		return Span(
			file=str(path),
			file_id=file_id,
			line=span.line,
			column=span.column,
			end_line=span.end_line,
			end_column=span.end_column,
			start_pos=span.start_pos,
			end_pos=span.end_pos,
			raw=span.raw,
		)
	return span


def _relabel_diagnostics(diags: list[Diagnostic], label_by_path: dict[str, str]) -> None:
	for diag in diags:
		span = diag.span
		if not span or not span.file:
			continue
		if isinstance(span.file, str) and os.path.isabs(span.file):
			label = label_by_path.get(span.file)
			if label is not None:
				diag.span = replace(span, file=label)


def _filter_test_build_only(prog: parser_ast.Program, *, test_build_only: bool) -> parser_ast.Program:
	if test_build_only:
		return prog
	test_only_names: set[str] = set()
	for fn in getattr(prog, "functions", []) or []:
		if getattr(fn, "test_build_only", False):
			test_only_names.add(fn.name)
	for c in getattr(prog, "consts", []) or []:
		if getattr(c, "test_build_only", False):
			test_only_names.add(c.name)
	for a in getattr(prog, "type_aliases", []) or []:
		if getattr(a, "test_build_only", False):
			test_only_names.add(a.name)
	for s in getattr(prog, "structs", []) or []:
		if getattr(s, "test_build_only", False):
			test_only_names.add(s.name)
	for v in getattr(prog, "variants", []) or []:
		if getattr(v, "test_build_only", False):
			test_only_names.add(v.name)
	for e in getattr(prog, "exceptions", []) or []:
		if getattr(e, "test_build_only", False):
			test_only_names.add(e.name)
	for t in getattr(prog, "traits", []) or []:
		if getattr(t, "test_build_only", False):
			test_only_names.add(t.name)
	for i in getattr(prog, "interfaces", []) or []:
		if getattr(i, "test_build_only", False):
			test_only_names.add(i.name)

	prog.functions = [fn for fn in getattr(prog, "functions", []) or [] if not getattr(fn, "test_build_only", False)]
	prog.consts = [c for c in getattr(prog, "consts", []) or [] if not getattr(c, "test_build_only", False)]
	prog.type_aliases = [a for a in getattr(prog, "type_aliases", []) or [] if not getattr(a, "test_build_only", False)]
	prog.structs = [s for s in getattr(prog, "structs", []) or [] if not getattr(s, "test_build_only", False)]
	prog.variants = [v for v in getattr(prog, "variants", []) or [] if not getattr(v, "test_build_only", False)]
	prog.exceptions = [e for e in getattr(prog, "exceptions", []) or [] if not getattr(e, "test_build_only", False)]
	prog.traits = [t for t in getattr(prog, "traits", []) or [] if not getattr(t, "test_build_only", False)]
	prog.interfaces = [i for i in getattr(prog, "interfaces", []) or [] if not getattr(i, "test_build_only", False)]

	impls: list[parser_ast.ImplementDef] = []
	for impl in getattr(prog, "implements", []) or []:
		if getattr(impl, "test_build_only", False):
			continue
		impl.methods = [m for m in getattr(impl, "methods", []) or [] if not getattr(m, "test_build_only", False)]
		impls.append(impl)
	prog.implements = impls

	exports: list[parser_ast.ExportStmt] = []
	for exp in getattr(prog, "exports", []) or []:
		items: list[parser_ast.ExportItem] = []
		for item in getattr(exp, "items", []) or []:
			if isinstance(item, parser_ast.ExportName) and item.name in test_only_names:
				continue
			items.append(item)
		if not items:
			continue
		exp.items = items
		exports.append(exp)
	prog.exports = exports
	return prog


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
		_p_diag(
			message=f"duplicate {kind} definition for '{name}'",
			severity="error",
			span=second_span,
		),
		_p_diag(
			message=f"previous definition of '{name}' is here",
			severity="note",
			span=first_span,
		),
	]


def _collect_type_defs(prog: parser_ast.Program) -> dict[str, list[str]]:
	return {
		"structs": [s.name for s in getattr(prog, "structs", []) or []],
		"variants": [v.name for v in getattr(prog, "variants", []) or []],
		"exceptions": [e.name for e in getattr(prog, "exceptions", []) or []],
		"interfaces": [i.name for i in getattr(prog, "interfaces", []) or []],
		"aliases": [a.name for a in getattr(prog, "type_aliases", []) or []],
	}


def _collect_requires_for_module(
	type_table: TypeTable,
	module_id: str,
) -> tuple[dict[FunctionId, parser_ast.TraitExpr], dict["TypeKey", parser_ast.TraitExpr]]:
	from lang.driftc.traits.world import TypeKey

	trait_worlds = getattr(type_table, "trait_worlds", None)
	if not isinstance(trait_worlds, dict):
		return {}, {}
	world = trait_worlds.get(module_id)
	if world is None:
		return {}, {}
	requires_by_fn = dict(getattr(world, "requires_by_fn", {}) or {})
	requires_by_struct: dict[TypeKey, parser_ast.TraitExpr] = {}
	for key, req in (getattr(world, "requires_by_struct", {}) or {}).items():
		if not isinstance(key, TypeKey):
			raise AssertionError(f"requires_by_struct key is not a TypeKey: {key}")
		requires_by_struct[key] = req
	return requires_by_fn, requires_by_struct


def parse_drift_files_to_hir(
	paths: list[Path],
	*,
	package_id: str | None = None,
	test_build_only: bool = False,
	) -> Tuple[ModuleLowered, "TypeTable", Dict[str, int], List[Diagnostic]]:
	"""
	Parse and lower a set of Drift source files into a single module unit.

	MVP: only one file may define a module. This helper accepts a single file and
	treats it as one module unit; multiple files are a hard error.
	"""
	diagnostics: list[Diagnostic] = []
	source_manager = SourceManager()
	prev_source_manager = _ACTIVE_SOURCE_MANAGER
	_set_active_source_manager(source_manager)
	if not paths:
		empty = ModuleLowered(
			module_id="main",
			package_id=package_id,
			source_path=Path("<unknown>"),
			func_hirs={},
			signatures_by_id={},
			fn_ids_by_name={},
			requires_by_fn={},
			requires_by_struct={},
			type_defs={},
			impl_defs=[],
			origin_by_fn_id={},
		)
		table = TypeTable()
		table.set_source_manager(source_manager)
		_set_active_source_manager(prev_source_manager)
		return empty, table, {}, [_p_diag(message="no input files", severity="error")]

	paths = [p.resolve() for p in paths]
	programs: list[tuple[Path, parser_ast.Program]] = []
	for path in paths:
		source = path.read_text()
		file_id = source_manager.add(str(path), source)
		try:
			prog = _parser.parse_program(source, filename=str(path), file_id=file_id)
		except _parser.ModuleDeclError as err:
			diagnostics.append(_p_diag(message=str(err), severity="error", span=_span_in_file(path, err.loc)))
			continue
		except _parser.QualifiedMemberParseError as err:
			diagnostics.append(_p_diag(message=str(err), severity="error", span=_span_in_file(path, err.loc)))
			continue
		except _parser.FStringParseError as err:
			diagnostics.append(_p_diag(message=str(err), severity="error", span=_span_in_file(path, err.loc)))
			continue
		except UnexpectedInput as err:
			code = _parse_error_code(err)
			message = _parse_error_message(err, code)
			span = Span(
				file=str(path),
				line=getattr(err, "line", None),
				column=getattr(err, "column", None),
				raw=err,
			)
			diagnostics.append(_p_diag(message=message, severity="error", span=span, code=code))
			continue
		programs.append((path, prog))

	label_by_path = {str(p): "<source>" for p in paths}
	_relabel_diagnostics(diagnostics, label_by_path)
	if any(d.severity == "error" for d in diagnostics):
		empty = ModuleLowered(
			module_id="main",
			package_id=package_id,
			source_path=paths[0],
			func_hirs={},
			signatures_by_id={},
			fn_ids_by_name={},
			requires_by_fn={},
			requires_by_struct={},
			type_defs={},
			impl_defs=[],
			origin_by_fn_id={},
		)
		table = TypeTable()
		table.set_source_manager(source_manager)
		_set_active_source_manager(prev_source_manager)
		return empty, table, {}, diagnostics

	if len(programs) > 1:
		span = Span(file=str(programs[0][0]), line=1, column=1)
		diagnostics.append(
			_p_diag(
				message="multiple source files declare one module",
				severity="error",
				span=span,
			)
		)
		_relabel_diagnostics(diagnostics, label_by_path)
		empty = ModuleLowered(
			module_id="main",
			package_id=package_id,
			source_path=paths[0],
			func_hirs={},
			signatures_by_id={},
			fn_ids_by_name={},
			requires_by_fn={},
			requires_by_struct={},
			type_defs={},
			impl_defs=[],
			origin_by_fn_id={},
		)
		table = TypeTable()
		table.set_source_manager(source_manager)
		_set_active_source_manager(prev_source_manager)
		return empty, table, {}, diagnostics

	# Enforce single-module membership across the file set.
	def _effective_module_id(p: parser_ast.Program) -> str:
		return getattr(p, "module", None) or "main"

	module_id = _effective_module_id(programs[0][1])
	for path, prog in programs:
		mid = _effective_module_id(prog)
		decl_span = _span_in_file(path, getattr(prog, "module_loc", None))
		diagnostics.extend(
			_validate_module_id(
				mid,
				span=decl_span,
							)
		)
	for path, prog in programs[1:]:
		mid = _effective_module_id(prog)
		if mid != module_id:
			diagnostics.append(
				_p_diag(
					message=f"module id mismatch: expected '{module_id}', found '{mid}'",
					severity="error",
					span=Span(file=str(path), line=1, column=1),
				)
			)
	label = f"<{module_id}>"
	label_by_path = {str(path): label for path, _prog in programs}
	_relabel_diagnostics(diagnostics, label_by_path)
	if any(d.severity == "error" for d in diagnostics):
		empty = ModuleLowered(
			module_id="main",
			package_id=package_id,
			source_path=paths[0],
			func_hirs={},
			signatures_by_id={},
			fn_ids_by_name={},
			requires_by_fn={},
			requires_by_struct={},
			type_defs={},
			impl_defs=[],
			origin_by_fn_id={},
		)
		table = TypeTable()
		table.set_source_manager(source_manager)
		_set_active_source_manager(prev_source_manager)
		return empty, table, {}, diagnostics

	path, prog = programs[0]
	prog = _filter_test_build_only(prog, test_build_only=test_build_only)
	func_hirs, sigs, fn_ids, table, excs, impl_metas, diags = _lower_parsed_program_to_hir(
		prog,
		diagnostics=diagnostics,
		package_id=package_id,
	)
	requires_by_fn, requires_by_struct = _collect_requires_for_module(table, module_id)
	origins = {fn_id: path for fn_id in func_hirs.keys()}
	module = ModuleLowered(
		module_id=module_id,
		package_id=package_id,
		source_path=path,
		func_hirs=func_hirs,
		signatures_by_id=sigs,
		fn_ids_by_name=fn_ids,
		requires_by_fn=requires_by_fn,
		requires_by_struct=requires_by_struct,
		type_defs=_collect_type_defs(prog),
		impl_defs=list(impl_metas),
		origin_by_fn_id=origins,
	)
	for block in func_hirs.values():
		assign_callsite_ids(block, start=0)
	table.set_source_manager(source_manager)
	_set_active_source_manager(prev_source_manager)
	return module, table, excs, diags


def parse_drift_workspace_to_hir(
	paths: list[Path],
	*,
	module_paths: list[Path] | None = None,
	external_module_exports: dict[str, dict[str, object]] | None = None,
	external_module_packages: dict[str, str] | None = None,
	package_id: str | None = None,
	stdlib_root: Path | None = None,
	test_build_only: bool = False,
	) -> Tuple[
	Dict[str, ModuleLowered],
	"TypeTable",
	Dict[str, int],
	Dict[str, Dict[str, object]],
	Dict[str, set[str]],
	List[Diagnostic],
]:
	"""
	Parse and lower a set of Drift source files that may belong to multiple modules.

	MVP (“module imports and cross-module resolution”) scaffolding:
	- input is an unordered set of files (typically all `*.drift` files in a build),
	- each file must declare a `module <id>` (one file defines one module),
	- modules are resolved and lowered independently (no multi-file merges),
	- imports are resolved across modules (module-scoped),
	- resulting HIR/signatures are returned as a single program unit suitable for
	  the existing HIR→MIR→SSA→LLVM pipeline.

	Important MVP constraints (pinned for clarity):
	- Imports are **module-scoped** bindings (one file per module):
	  - Duplicate identical imports in one module are idempotent (“no-op after first”).
	  - Conflicting aliases/bindings in one module are diagnosed as errors.
		- Module-qualified access (`import m` then `m.foo()`) is supported for calling
		  exported free functions and for struct constructor calls (`m.Point(...)`).
		- Cross-module import validation supports both value and type namespaces
		  (types: structs, variants, exceptions, interfaces).

	Returns:
	  (modules, type_table, exception_catalog, module_exports, module_deps, diagnostics)
	"""
	from lang.driftc.traits.world import TraitKey

	diagnostics: list[Diagnostic] = []
	source_manager = SourceManager()
	prev_source_manager = _ACTIVE_SOURCE_MANAGER
	_set_active_source_manager(source_manager)
	user_paths = list(paths)
	user_path_set = {p.resolve() for p in user_paths}

	if stdlib_root is not None and module_paths:
		_reserved_roots = {"std", "lang", "drift"}
		for root in module_paths:
			try:
				root_resolved = root.resolve()
			except OSError:
				continue
			for path in user_paths:
				try:
					rel = path.resolve().relative_to(root_resolved)
				except ValueError:
					continue
				if rel.parts and rel.parts[0] in _reserved_roots:
					stdlib_root = None
					break
			if stdlib_root is None:
				break

	if stdlib_root is not None:
		std_root = stdlib_root
		std_paths = sorted(std_root.rglob("*.drift"))
		if std_paths:
			seen: set[Path] = set()
			all_paths: list[Path] = []
			for path in list(paths) + std_paths:
				resolved = path.resolve()
				if resolved in seen:
					continue
				seen.add(resolved)
				all_paths.append(path)
			paths = all_paths
			if module_paths is not None:
				roots = list(module_paths)
				if std_root not in roots:
					roots.append(std_root)
				module_paths = roots
	if not paths:
		table = TypeTable()
		table.set_source_manager(source_manager)
		_set_active_source_manager(prev_source_manager)
		return {}, table, {}, {}, {}, [_p_diag(message="no input files", severity="error")]

	def _sort_key_for_path(path: Path) -> tuple[str]:
		try:
			data = path.read_bytes()
		except OSError:
			data = b""
		digest = hashlib.sha256(data).hexdigest()
		return (digest,)

	paths = sorted({p.resolve() for p in paths}, key=_sort_key_for_path)
	label_by_path_all = {str(p): "<source>" for p in paths}

	def _effective_module_id(p: parser_ast.Program) -> str:
		return getattr(p, "module", None) or "main"

	# Parse all files first.
	parsed: list[tuple[Path, parser_ast.Program]] = []
	for path in paths:
		source = path.read_text()
		file_id = source_manager.add(str(path), source)
		try:
			prog = _parser.parse_program(source, filename=str(path), file_id=file_id)
		except _parser.ModuleDeclError as err:
			diagnostics.append(_p_diag(message=str(err), severity="error", span=_span_in_file(path, err.loc)))
			continue
		except _parser.QualifiedMemberParseError as err:
			diagnostics.append(_p_diag(message=str(err), severity="error", span=_span_in_file(path, err.loc)))
			continue
		except _parser.FStringParseError as err:
			diagnostics.append(_p_diag(message=str(err), severity="error", span=_span_in_file(path, err.loc)))
			continue
		except UnexpectedInput as err:
			code = _parse_error_code(err)
			message = _parse_error_message(err, code)
			span = Span(
				file=str(path),
				line=getattr(err, "line", None),
				column=getattr(err, "column", None),
				raw=err,
			)
			diagnostics.append(_p_diag(message=message, severity="error", span=span, code=code))
			continue
		parsed.append((path, prog))

	if any(d.severity == "error" for d in diagnostics):
		_relabel_diagnostics(diagnostics, label_by_path_all)
		table = TypeTable()
		table.set_source_manager(source_manager)
		_set_active_source_manager(prev_source_manager)
		return {}, table, {}, {}, {}, diagnostics

	def _file_root_for_path(path: Path) -> Path | None:
		if not module_paths:
			return None
		abs_path = path.resolve()
		candidates: list[Path] = []
		for root in module_paths:
			abs_root = root.resolve()
			try:
				abs_path.parent.relative_to(abs_root)
			except ValueError:
				continue
			candidates.append(abs_root)
		if not candidates:
			return None
		candidates.sort(key=lambda r: len(r.parts), reverse=True)
		best_len = len(candidates[0].parts)
		best = [c for c in candidates if len(c.parts) == best_len]
		if len(best) != 1:
			return None
		return best[0]

	# Group by module id (declared only).
	multiple_files = len(user_path_set) > 1
	parsed = sorted(parsed, key=lambda it: _effective_module_id(it[1]))
	by_module: dict[str, list[tuple[Path, parser_ast.Program]]] = {}
	roots_by_module: dict[str, set[Path]] = {}
	# For pinned diagnostics, keep at least one representative file per (module, root).
	root_file_by_module: dict[str, dict[Path, Path]] = {}
	for path, prog in parsed:
		is_user_file = path.resolve() in user_path_set
		prog = _filter_test_build_only(prog, test_build_only=test_build_only)
		if module_paths:
			root = _file_root_for_path(path)
			if root is None:
				diagnostics.append(
					_p_diag(
						message="file is not under exactly one configured module root",
						severity="error",
						span=Span(file=str(path), line=1, column=1),
					)
				)
				continue
			declared = getattr(prog, "module", None)
			if declared is None:
				diagnostics.append(
					_p_diag(
						message="module declaration is required for workspace builds",
						severity="error",
						span=_span_in_file(path, getattr(prog, "module_loc", None)),
					)
				)
				continue
			decl_span = _span_in_file(path, getattr(prog, "module_loc", None))
			diagnostics.extend(_validate_module_id(declared, span=decl_span))
			if any(d.severity == "error" for d in diagnostics):
				continue
			by_module.setdefault(declared, []).append((path, prog))
			roots_by_module.setdefault(declared, set()).add(root)
			root_file_by_module.setdefault(declared, {}).setdefault(root, path)
		else:
			if getattr(prog, "module", None) is None and multiple_files and is_user_file:
				diagnostics.append(
					_p_diag(
						message="module declaration is required for multi-file builds",
						severity="error",
						span=_span_in_file(path, getattr(prog, "module_loc", None)),
					)
				)
				continue
			mid = _effective_module_id(prog)
			decl_span = _span_in_file(path, getattr(prog, "module_loc", None))
			diagnostics.extend(_validate_module_id(mid, span=decl_span))
			by_module.setdefault(mid, []).append((path, prog))

	label_by_path = dict(label_by_path_all)
	label_by_path.update(
		{str(path): f"<{mid}>" for mid, files in by_module.items() for path, _prog in files}
	)
	_relabel_diagnostics(diagnostics, label_by_path)
	if any(d.severity == "error" for d in diagnostics):
		table = TypeTable()
		table.set_source_manager(source_manager)
		_set_active_source_manager(prev_source_manager)
		return {}, table, {}, {}, {}, diagnostics

	module_source_path: dict[str, Path] = {}
	for mid, files in by_module.items():
		if files:
			module_source_path[mid] = files[0][0]

	std_root_resolved: Path | None = None
	if stdlib_root is not None:
		std_root_resolved = stdlib_root.resolve()

	def _is_path_under(path: Path, root: Path) -> bool:
		try:
			path.resolve().relative_to(root)
		except ValueError:
			return False
		return True

	def _is_stdlib_module(mid: str) -> bool:
		if std_root_resolved is None:
			return False
		path = module_source_path.get(mid)
		if path is None:
			return False
		return _is_path_under(path, std_root_resolved)

	if module_paths and std_root_resolved is not None:
		for mid, files in list(by_module.items()):
			if len(files) < 2:
				continue
			std_files: list[tuple[Path, parser_ast.Program]] = []
			user_files: list[tuple[Path, parser_ast.Program]] = []
			for path, prog in files:
				try:
					path.resolve().relative_to(std_root_resolved)
				except ValueError:
					user_files.append((path, prog))
				else:
					std_files.append((path, prog))
			if std_files and user_files:
				by_module[mid] = user_files
				roots_by_module[mid] = {
					r
					for r in roots_by_module.get(mid, set())
					if not _is_path_under(r, std_root_resolved)
				}
				if mid in root_file_by_module:
					root_file_by_module[mid] = {
						r: p for r, p in root_file_by_module[mid].items() if not _is_path_under(r, std_root_resolved)
					}

	# When module roots are used, reject ambiguous module ids coming from
	# multiple roots (prevents accidental shadowing/selection by search order).
	if module_paths:
		for mid, roots in roots_by_module.items():
			if len(roots) > 1:
				span_file = None
				# Anchor the diagnostic to a concrete file under one of the roots.
				for r in sorted(roots):
					span_file = root_file_by_module.get(mid, {}).get(r)
					if span_file is not None:
						break
				span = Span(file=str(span_file), line=1, column=1) if span_file else Span()
				diagnostics.append(
					_p_diag(
						message=f"multiple module roots provide module '{mid}'",
						severity="error",
						span=span,
					)
				)
	_relabel_diagnostics(diagnostics, label_by_path)
	if any(d.severity == "error" for d in diagnostics):
		table = TypeTable()
		table.set_source_manager(source_manager)
		_set_active_source_manager(prev_source_manager)
		return {}, table, {}, {}, {}, diagnostics

	# MVP: one source file defines one module.
	for mid, files in by_module.items():
		if len(files) > 1:
			span = Span(file=str(files[0][0]), line=1, column=1)
			diagnostics.append(
				_p_diag(
					message=f"multiple source files declare module '{mid}'",
					severity="error",
					span=span,
				)
			)
	_relabel_diagnostics(diagnostics, label_by_path)
	if any(d.severity == "error" for d in diagnostics):
		table = TypeTable()
		table.set_source_manager(source_manager)
		_set_active_source_manager(prev_source_manager)
		return {}, table, {}, {}, {}, diagnostics

	# MVP: one file defines one module (no merge).
	merged_programs: dict[str, parser_ast.Program] = {}
	module_file_by_id: dict[str, Path] = {}
	for mid, files in by_module.items():
		path, prog = files[0]
		merged_programs[mid] = prog
		module_file_by_id[mid] = path
	source_modules = set(merged_programs.keys())
	if isinstance(external_module_packages, dict):
		override_modules = sorted(mod for mod in external_module_packages.keys() if mod in source_modules)
		if override_modules and not test_build_only:
			diagnostics.append(
				_p_diag(
					message=(
						"external module package mapping may not override source modules: "
						+ ", ".join(override_modules)
					),
					severity="error",
				)
			)

	if any(d.severity == "error" for d in diagnostics):
		table = TypeTable()
		table.set_source_manager(source_manager)
		_set_active_source_manager(prev_source_manager)
		return {}, table, {}, {}, {}, diagnostics

	# Public symbol maps (used for same-workspace module-qualified access).
	pub_values_by_module: dict[str, set[str]] = {}
	pub_consts_by_module: dict[str, set[str]] = {}
	pub_types_by_module: dict[str, dict[str, set[str]]] = {}
	pub_traits_by_module: dict[str, set[str]] = {}

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
		  - types: structs, variants, exceptions, interfaces

		Because `export { ... }` syntax is unqualified, exporting a name that exists
		in both namespaces is ambiguous. Until we add explicit qualifiers, that is
		a compile-time error.

		Because `exports.types` is kind-separated (structs/variants/exceptions/interfaces), an
		exported name that resolves to multiple type kinds is also a compile-time
		error.

		Spans are anchored to the module's source file that contained the
		`export { ... }` statement so diagnostics remain useful.
		"""
		module_fn_names: set[str] = {fn.name for fn in getattr(merged_prog, "functions", []) or []}
		module_const_names: set[str] = {c.name for c in getattr(merged_prog, "consts", []) or []}
		module_struct_names: set[str] = {s.name for s in getattr(merged_prog, "structs", []) or []}
		module_variant_names: set[str] = {v.name for v in getattr(merged_prog, "variants", []) or []}
		module_exception_names: set[str] = {e.name for e in getattr(merged_prog, "exceptions", []) or []}
		module_interface_names: set[str] = {i.name for i in getattr(merged_prog, "interfaces", []) or []}
		module_alias_names: set[str] = {a.name for a in getattr(merged_prog, "type_aliases", []) or []}
		module_trait_names: set[str] = {t.name for t in getattr(merged_prog, "traits", []) or []}
		module_pub_fn_names: set[str] = {fn.name for fn in getattr(merged_prog, "functions", []) or [] if getattr(fn, "is_pub", False)}
		module_pub_const_names: set[str] = {c.name for c in getattr(merged_prog, "consts", []) or [] if getattr(c, "is_pub", False)}
		module_pub_struct_names: set[str] = {s.name for s in getattr(merged_prog, "structs", []) or [] if getattr(s, "is_pub", False)}
		module_pub_variant_names: set[str] = {v.name for v in getattr(merged_prog, "variants", []) or [] if getattr(v, "is_pub", False)}
		module_pub_exception_names: set[str] = {e.name for e in getattr(merged_prog, "exceptions", []) or [] if getattr(e, "is_pub", False)}
		module_pub_interface_names: set[str] = {i.name for i in getattr(merged_prog, "interfaces", []) or [] if getattr(i, "is_pub", False)}
		module_pub_alias_names: set[str] = {a.name for a in getattr(merged_prog, "type_aliases", []) or [] if getattr(a, "is_pub", False)}
		module_pub_trait_names: set[str] = {t.name for t in getattr(merged_prog, "traits", []) or [] if getattr(t, "is_pub", False)}
		builtin_struct_names: dict[str, set[str]] = {"std.mem": {"Ptr"}}
		builtin_public_structs = builtin_struct_names.get(module_id)
		if builtin_public_structs:
			module_struct_names |= set(builtin_public_structs)
			module_pub_struct_names |= set(builtin_public_structs)
		pub_values_by_module[module_id] = set(module_pub_fn_names)
		pub_consts_by_module[module_id] = set(module_pub_const_names)
		pub_types_by_module[module_id] = {
			"structs": set(module_pub_struct_names),
			"variants": set(module_pub_variant_names),
			"exceptions": set(module_pub_exception_names),
			"interfaces": set(module_pub_interface_names),
			"aliases": set(module_pub_alias_names),
		}
		pub_traits_by_module[module_id] = set(module_pub_trait_names)

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
		exported_types: dict[str, set[str]] = {"structs": set(), "variants": set(), "exceptions": set(), "interfaces": set(), "aliases": set()}
		exported_consts: set[str] = set()
		exported_traits: set[str] = set()
		star_reexports: dict[str, Span] = {}
		for mod, ex_span in star_export_entries:
			prev = seen_star_modules.get(mod)
			if prev is None:
				seen_star_modules[mod] = ex_span
				star_reexports[mod] = ex_span
			else:
				diagnostics.append(
					_p_diag(
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
					_p_diag(
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
			in_alias = n in module_alias_names
			in_interface = n in module_interface_names
			in_trait = n in module_trait_names
			type_hits = int(in_struct) + int(in_variant) + int(in_exc) + int(in_interface) + int(in_alias)
			in_types = type_hits > 0
			if (in_values and in_consts) or (in_values and in_types) or (in_consts and in_types):
				diagnostics.append(
					_p_diag(
						message=f"exported name '{n}' is ambiguous (defined as multiple kinds in module '{module_id}')",
						severity="error",
						span=ex_span,
					)
				)
				continue
			if in_trait and (in_values or in_consts or in_types):
				diagnostics.append(
					_p_diag(
						message=f"exported name '{n}' is ambiguous (defined as multiple kinds in module '{module_id}')",
						severity="error",
						span=ex_span,
					)
				)
				continue
			if type_hits > 1:
				diagnostics.append(
					_p_diag(
						message=f"exported type name '{n}' is ambiguous (defined as multiple type kinds in module '{module_id}')",
						severity="error",
						span=ex_span,
					)
				)
				continue

			if not in_values and not in_consts and not in_types and not in_trait:
				diagnostics.append(
					_p_diag(
						message=f"module '{module_id}' exports unknown symbol '{n}'",
						severity="error",
						span=ex_span,
					)
				)
				continue

			if in_values:
				if n not in module_pub_fn_names:
					diagnostics.append(
						_p_diag(
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
						_p_diag(
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
						_p_diag(
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
						_p_diag(
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
						_p_diag(
							message=f"cannot export '{n}' from module '{module_id}': symbol is not public (mark it 'pub')",
							severity="error",
							span=ex_span,
						)
					)
					continue
				exported_types["exceptions"].add(n)
			if in_interface:
				if n not in module_pub_interface_names:
					diagnostics.append(
						_p_diag(
							message=f"cannot export '{n}' from module '{module_id}': symbol is not public (mark it 'pub')",
							severity="error",
							span=ex_span,
						)
					)
					continue
				exported_types["interfaces"].add(n)
			if in_alias:
				if n not in module_pub_alias_names:
					diagnostics.append(
						_p_diag(
							message=f"cannot export '{n}' from module '{module_id}': symbol is not public (mark it 'pub')",
							severity="error",
							span=ex_span,
						)
					)
					continue
				exported_types["aliases"].add(n)
			if in_trait:
				if n not in module_pub_trait_names:
					diagnostics.append(
						_p_diag(
							message=f"cannot export '{n}' from module '{module_id}': symbol is not public (mark it 'pub')",
							severity="error",
							span=ex_span,
						)
					)
					continue
				exported_traits.add(n)

		return (
			exported_values,
			exported_types,
			exported_consts,
			exported_traits,
			star_reexports,
		)

	# Note: module-scoped nominal type identity is implemented in lang.
	# Multiple modules may define types with the same short name without
	# colliding; identity is `(module_id, name, kind)`.

	# Export sets (private by default, explicit exports required).
	#
	# MVP supports exporting/importing both value-level and type-level symbols,
	# but keeps them in separate namespaces:
	# - values: currently just free functions
	# - types: structs, variants, exceptions, interfaces
	#
	# Export lists are unqualified identifiers, so to avoid ambiguity we reject
	# any module that defines the same name in both namespaces (until the language
	# adds explicit `export type ...` / `export fn ...` syntax).
	exports_values_by_module: dict[str, dict[str, tuple[str, str]]] = {}
	exports_types_by_module: dict[str, dict[str, set[str]]] = {}
	exports_consts_by_module: dict[str, set[str]] = {}
	exports_traits_by_module: dict[str, set[str]] = {}
	star_reexports_by_module: dict[str, dict[str, Span]] = {}
	exported_const_origins_by_module: dict[str, dict[str, tuple[str, str]]] = {}
	exported_type_origins_by_module: dict[str, dict[str, dict[str, tuple[str, str]]]] = {}
	exported_trait_origins_by_module: dict[str, dict[str, tuple[str, str]]] = {}
	# Re-export target maps (for types/consts). Values are materialized as
	# metadata-only aliases so consumers can resolve origin symbols.
	reexported_value_targets_by_module: dict[str, dict[str, tuple[str, str]]] = {}
	reexported_type_targets_by_module: dict[str, dict[str, dict[str, tuple[str, str]]]] = {}
	reexported_const_targets_by_module: dict[str, dict[str, tuple[str, str]]] = {}
	reexported_trait_targets_by_module: dict[str, dict[str, tuple[str, str]]] = {}
	for mid, prog in merged_programs.items():
		(
			exported_values,
			exported_types,
			exported_consts,
			exported_traits,
			star_reexports,
		) = _build_export_interface(
			module_id=mid,
			merged_prog=prog,
			module_files=by_module.get(mid, []),
		)
		exports_values_by_module[mid] = exported_values
		exports_types_by_module[mid] = exported_types
		exports_consts_by_module[mid] = exported_consts
		exports_traits_by_module[mid] = exported_traits
		star_reexports_by_module[mid] = star_reexports
		exported_const_origins_by_module[mid] = {n: (mid, n) for n in exported_consts}
		exported_type_origins_by_module[mid] = {
			"structs": {n: (mid, n) for n in exported_types.get("structs") or set()},
			"variants": {n: (mid, n) for n in exported_types.get("variants") or set()},
			"exceptions": {n: (mid, n) for n in exported_types.get("exceptions") or set()},
			"interfaces": {n: (mid, n) for n in exported_types.get("interfaces") or set()},
			"aliases": {n: (mid, n) for n in exported_types.get("aliases") or set()},
		}
		exported_trait_origins_by_module[mid] = {n: (mid, n) for n in exported_traits}
		reexported_value_targets_by_module[mid] = {}
		reexported_type_targets_by_module[mid] = {"structs": {}, "variants": {}, "exceptions": {}, "interfaces": {}, "aliases": {}}
		reexported_const_targets_by_module[mid] = {}
		reexported_trait_targets_by_module[mid] = {}

	# Resolve star re-exports across modules deterministically.
	def _export_origin_lookup(
		mod: str,
	) -> tuple[
		dict[str, tuple[str, str]],
		dict[str, tuple[str, str]],
		dict[str, dict[str, tuple[str, str]]],
		dict[str, tuple[str, str]],
	]:
		"""Return (exported_values, exported_consts, exported_types_by_kind, exported_traits) with origin targets."""
		if mod in exports_values_by_module or mod in exports_types_by_module or mod in exports_consts_by_module:
			return (
				exports_values_by_module.get(mod) or {},
				exported_const_origins_by_module.get(mod) or {},
				exported_type_origins_by_module.get(mod) or {"structs": {}, "variants": {}, "exceptions": {}, "interfaces": {}, "aliases": {}},
				exported_trait_origins_by_module.get(mod) or {},
			)
		if external_module_exports is not None and mod in external_module_exports:
			ext = external_module_exports.get(mod) or {}
			values_obj = {n: (mod, n) for n in sorted(ext.get("values") or set())}
			consts_obj = {n: (mod, n) for n in sorted(ext.get("consts") or set())}
			traits_obj = {n: (mod, n) for n in sorted(ext.get("traits") or set())}
			types_obj: dict[str, dict[str, tuple[str, str]]] = {"structs": {}, "variants": {}, "exceptions": {}, "interfaces": {}, "aliases": {}}
			ext_types = ext.get("types")
			if isinstance(ext_types, dict):
				for kind in ("structs", "variants", "exceptions", "interfaces", "aliases"):
					for name in sorted(ext_types.get(kind) or set()):
						types_obj[kind][name] = (mod, name)
			ext_reexp = ext.get("reexports")
			if isinstance(ext_reexp, dict):
				ext_reexp_vals = ext_reexp.get("values")
				ext_reexp_types = ext_reexp.get("types")
				ext_reexp_consts = ext_reexp.get("consts")
				ext_reexp_traits = ext_reexp.get("traits")
				if isinstance(ext_reexp_vals, dict):
					for name, v in ext_reexp_vals.items():
						if isinstance(v, dict):
							tm = v.get("module")
							tn = v.get("name")
							if isinstance(tm, str) and isinstance(tn, str):
								values_obj[name] = (tm, tn)
				if isinstance(ext_reexp_consts, dict):
					for name, v in ext_reexp_consts.items():
						if isinstance(v, dict):
							tm = v.get("module")
							tn = v.get("name")
							if isinstance(tm, str) and isinstance(tn, str):
								consts_obj[name] = (tm, tn)
				if isinstance(ext_reexp_types, dict):
					for kind in ("structs", "variants", "exceptions", "interfaces", "aliases"):
						km = ext_reexp_types.get(kind)
						if isinstance(km, dict):
							for name, v in km.items():
								if isinstance(v, dict):
									tm = v.get("module")
									tn = v.get("name")
									if isinstance(tm, str) and isinstance(tn, str):
										types_obj[kind][name] = (tm, tn)
				if isinstance(ext_reexp_traits, dict):
					for name, v in ext_reexp_traits.items():
						if isinstance(v, dict):
							tm = v.get("module")
							tn = v.get("name")
							if isinstance(tm, str) and isinstance(tn, str):
								traits_obj[name] = (tm, tn)
			return values_obj, consts_obj, types_obj, traits_obj
		return {}, {}, {"structs": {}, "variants": {}, "exceptions": {}, "interfaces": {}, "aliases": {}}, {}

	# We iterate until no progress so multi-hop star re-exports resolve deterministically.
	for _ in range(len(merged_programs) + 1):
		progress = False
		for mid, stars in star_reexports_by_module.items():
			for target_mod, ex_span in stars.items():
				if target_mod not in merged_programs and (external_module_exports is None or target_mod not in external_module_exports):
					diagnostics.append(
						_p_diag(
							message=f"module '{mid}' re-exports unknown module '{target_mod}'",
							severity="error",
							span=ex_span,
						)
					)
					continue
				vals, consts, types_obj, traits_obj = _export_origin_lookup(target_mod)
				for name, origin in vals.items():
					prev = exports_values_by_module[mid].get(name)
					if prev is None:
						exports_values_by_module[mid][name] = origin
						progress = True
					elif prev != origin:
						diagnostics.append(
							_p_diag(
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
							_p_diag(
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
								_p_diag(
									message=(
										f"exported type '{name}' is ambiguous due to re-exports "
										f"('{prev[0]}' vs '{origin[0]}') in module '{mid}'"
									),
									severity="error",
									span=ex_span,
								)
							)
				for name, origin in traits_obj.items():
					prev = exported_trait_origins_by_module[mid].get(name)
					if prev is None:
						exports_traits_by_module[mid].add(name)
						exported_trait_origins_by_module[mid][name] = origin
						if origin[0] != mid:
							reexported_trait_targets_by_module[mid][name] = origin
						progress = True
					elif prev != origin:
						diagnostics.append(
							_p_diag(
								message=(
									f"exported trait '{name}' is ambiguous due to re-exports "
									f"('{prev[0]}' vs '{origin[0]}') in module '{mid}'"
								),
								severity="error",
								span=ex_span,
							)
						)
		if not progress:
			break

	# Record value re-exports as metadata-only aliases (no trampolines).
	for mid, exported_values in exports_values_by_module.items():
		for name, origin in exported_values.items():
			if origin[0] != mid:
				reexported_value_targets_by_module[mid][name] = origin

	def _union_exported_types(types_obj: dict[str, set[str]] | None) -> set[str]:
		if not types_obj:
			return set()
		out: set[str] = set()
		for vs in types_obj.values():
			out |= set(vs)
		return out

	label_by_path = {
		str(path.resolve()): f"<{mid}>"
		for mid, files in by_module.items()
		for path, _prog in files
	}
	_relabel_diagnostics(diagnostics, label_by_path)
	if any(d.severity == "error" for d in diagnostics):
		table = TypeTable()
		table.set_source_manager(source_manager)
		_set_active_source_manager(prev_source_manager)
		return {}, table, {}, {}, {}, diagnostics

	# Export interface summary (used by package emission and future tooling).
	module_exports: dict[str, dict[str, object]] = {}
	for mid in merged_programs.keys():
		vals = exports_values_by_module.get(mid, {})
		types = exports_types_by_module.get(mid, {"structs": set(), "variants": set(), "exceptions": set(), "interfaces": set(), "aliases": set()})
		consts = exports_consts_by_module.get(mid, set())
		traits = exports_traits_by_module.get(mid, set())
		reexp_types = reexported_type_targets_by_module.get(mid, {"structs": {}, "variants": {}, "exceptions": {}, "interfaces": {}, "aliases": {}})
		reexp_consts = reexported_const_targets_by_module.get(mid, {})
		reexp_traits = reexported_trait_targets_by_module.get(mid, {})
		reexp_values = reexported_value_targets_by_module.get(mid, {})
		module_exports[mid] = {
			"values": sorted(list(vals.keys())),
			"types": {
				"structs": sorted(list(types.get("structs", set()))),
				"variants": sorted(list(types.get("variants", set()))),
				"exceptions": sorted(list(types.get("exceptions", set()))),
				"interfaces": sorted(list(types.get("interfaces", set()))),
				"aliases": sorted(list(types.get("aliases", set()))),
			},
			"consts": sorted(list(consts)),
			"traits": sorted(list(traits)),
			"reexports": {
				"values": {n: {"module": m, "name": s} for n, (m, s) in sorted(reexp_values.items())},
				"types": {
					"structs": {n: {"module": m, "name": s} for n, (m, s) in sorted(reexp_types.get("structs", {}).items())},
					"variants": {n: {"module": m, "name": s} for n, (m, s) in sorted(reexp_types.get("variants", {}).items())},
					"exceptions": {n: {"module": m, "name": s} for n, (m, s) in sorted(reexp_types.get("exceptions", {}).items())},
					"interfaces": {n: {"module": m, "name": s} for n, (m, s) in sorted(reexp_types.get("interfaces", {}).items())},
					"aliases": {n: {"module": m, "name": s} for n, (m, s) in sorted(reexp_types.get("aliases", {}).items())},
				},
				"consts": {n: {"module": m, "name": s} for n, (m, s) in sorted(reexp_consts.items())},
				"traits": {n: {"module": m, "name": s} for n, (m, s) in sorted(reexp_traits.items())},
			},
		}

	# Resolve imports and build a dependency graph.
	#
	# MVP rule: import bindings are module-scoped (one file per module). Module
	# dependencies are computed at module granularity.
	#
	# Keep per-edge provenance so cycle diagnostics can be source-anchored.
	# Each edge is (to_module, span).
	dep_edges: dict[str, list[tuple[str, Span]]] = {mid: [] for mid in merged_programs}
	module_aliases_by_module: dict[str, dict[str, str]] = {}
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
					diagnostics.append(
						_p_diag(
							message=_missing_import_module_message(mod, single_entry=(len(user_paths) == 1)),
							severity="error",
							span=span,
						)
					)
					continue
				alias = getattr(imp, "alias", None) or (getattr(imp, "path", []) or [mod])[-1]
				prev = file_module_aliases.get(alias)
				if prev is None:
					file_module_aliases[alias] = mod
				elif prev != mod:
					diagnostics.append(
						_p_diag(
							message=f"import alias '{alias}' conflicts: cannot import both '{prev}' and '{mod}' as '{alias}'",
							severity="error",
							span=span,
					)
					)

			# Record module aliases for later module-qualified access resolution.
			# MVP: one file per module, so module-scoped aliases are sufficient.
			module_aliases_by_module[mid] = dict(file_module_aliases)
		for target_mod, ex_span in (star_reexports_by_module.get(mid) or {}).items():
			dep_edges[mid].append((target_mod, ex_span))

	# Resolve `use trait ...` directives into module trait scopes.
	trait_scope_by_module: dict[str, list[TraitKey]] = {mid: [] for mid in merged_programs}
	trait_scope_seen_by_module: dict[str, set[TraitKey]] = {mid: set() for mid in merged_programs}
	module_packages_for_scope: dict[str, str] = {}
	local_pkg = package_id or "__local__"
	has_stdlib = stdlib_root is not None
	std_pkg = "std" if package_id is not None else local_pkg
	if isinstance(external_module_packages, dict):
		for mod, pkg in external_module_packages.items():
			if mod in merged_programs:
				continue
			if isinstance(mod, str) and isinstance(pkg, str):
				module_packages_for_scope.setdefault(mod, pkg)
	for mod in merged_programs:
		if has_stdlib and _is_stdlib_module(mod):
			module_packages_for_scope.setdefault(mod, std_pkg)
		elif mod == "lang.core":
			module_packages_for_scope.setdefault(mod, "lang.core" if has_stdlib else local_pkg)
		else:
			module_packages_for_scope.setdefault(mod, local_pkg)
	module_packages_for_scope.setdefault("lang.core", "lang.core" if has_stdlib else local_pkg)
	module_packages_for_scope.setdefault("lang.__internal", local_pkg)

	def _check_alias_binding_conflicts(path: Path, file_aliases: dict[str, str], prog: parser_ast.Program) -> None:
		if not file_aliases:
			return

		def _diag_conflict(name: str, loc: Located | None) -> None:
			if name not in file_aliases:
				return
			diagnostics.append(
				_p_diag(
					message=f"value binding '{name}' conflicts with module alias '{name}'",
					severity="error",
					span=_span_in_file(path, loc),
				)
			)

		def _check_block(block: parser_ast.Block) -> None:
			for st in getattr(block, "statements", []) or []:
				if isinstance(st, parser_ast.LetStmt):
					_diag_conflict(st.name, st.loc)
				elif isinstance(st, parser_ast.ForStmt):
					_diag_conflict(st.var, st.loc)
					_check_block(st.body)
				elif isinstance(st, parser_ast.ForCountStmt):
					_diag_conflict(st.init_name, st.loc)
					_check_block(st.body)
				elif isinstance(st, parser_ast.TryStmt):
					_check_block(st.body)
					for c in getattr(st, "catches", []) or []:
						if getattr(c, "binder", None):
							_diag_conflict(c.binder, getattr(c, "loc", None))
						_check_block(c.block)
				elif isinstance(st, parser_ast.WhileStmt):
					_check_block(st.body)
				elif isinstance(st, parser_ast.BlockStmt):
					_check_block(st.block)
				elif isinstance(st, parser_ast.UnsafeBlockStmt):
					_check_block(st.block)
				elif isinstance(st, parser_ast.ExprStmt):
					_check_expr_for_binders(st.value)
				elif isinstance(st, parser_ast.ReturnStmt):
					_check_expr_for_binders(getattr(st, "value", None))

		def _check_expr_for_binders(expr: parser_ast.Expr | None) -> None:
			if expr is None:
				return
			if isinstance(expr, parser_ast.MatchExpr):
				for arm in expr.arms:
					for binder in getattr(arm, "binders", []) or []:
						_diag_conflict(binder, arm.loc)
					_check_block(arm.block)

		for fn in getattr(prog, "functions", []) or []:
			for param in getattr(fn, "params", []) or []:
				_diag_conflict(param.name, getattr(fn, "loc", None))
			_check_block(fn.body)
		for impl in getattr(prog, "implements", []) or []:
			for mfn in getattr(impl, "methods", []) or []:
				for param in getattr(mfn, "params", []) or []:
					_diag_conflict(param.name, getattr(mfn, "loc", None))
				_check_block(mfn.body)

	def _exported_traits_for_module(mod: str) -> set[str]:
		if mod in exports_traits_by_module:
			return set(exports_traits_by_module.get(mod) or set())
		if external_module_exports is not None and mod in external_module_exports:
			ext = external_module_exports.get(mod) or {}
			traits = ext.get("traits")
			if isinstance(traits, (list, set)):
				return set(traits)
			return set()
		return set()

	def _resolve_trait_origin(mod: str, trait_name: str) -> tuple[str, str]:
		if mod in reexported_trait_targets_by_module:
			origin = reexported_trait_targets_by_module.get(mod, {}).get(trait_name)
			if origin is not None:
				return origin
		if external_module_exports is not None and mod in external_module_exports:
			ext = external_module_exports.get(mod) or {}
			ext_reexp = ext.get("reexports")
			if isinstance(ext_reexp, dict):
				tr = ext_reexp.get("traits")
				if isinstance(tr, dict):
					entry = tr.get(trait_name)
					if isinstance(entry, dict):
						tm = entry.get("module")
						tn = entry.get("name")
						if isinstance(tm, str) and isinstance(tn, str):
							return tm, tn
		return mod, trait_name

	for mid, files in by_module.items():
		local_trait_names = {t.name for t in getattr(merged_programs.get(mid), "traits", []) or []}
		module_aliases = module_aliases_by_module.get(mid, {})
		for path, prog in files:
			_check_alias_binding_conflicts(path, module_aliases, prog)
			for tr in getattr(prog, "used_traits", []) or []:
				ref_path = list(getattr(tr, "module_path", []) or [])
				if not ref_path:
					mod = mid
					alias = mid
				else:
					alias = ".".join(ref_path)
					mod = module_aliases.get(alias)
					if mod is None:
						# Allow direct module paths in use-trait without prior import alias.
						if alias in merged_programs or (external_module_exports is not None and alias in external_module_exports):
							mod = alias
				span = _span_in_file(path, getattr(tr, "loc", None))
				if mod is None:
					diagnostics.append(
						_p_diag(
							message=f"unknown module alias '{alias}' in trait reference '{alias}.{tr.name}'",
							severity="error",
							span=span,
						)
					)
					continue
				if mod not in merged_programs and (external_module_exports is None or mod not in external_module_exports):
					diagnostics.append(
						_p_diag(
							message=f"unknown module '{mod}' in trait reference '{alias}.{tr.name}'",
							severity="error",
							span=span,
						)
					)
					continue
				if mod == mid:
					if tr.name not in local_trait_names:
						diagnostics.append(
							_p_diag(
								message=f"module '{mod}' does not define trait '{tr.name}'",
								severity="error",
								span=span,
							)
						)
						continue
				else:
					exported_traits = _exported_traits_for_module(mod)
					if tr.name not in exported_traits:
						available = ", ".join(sorted(exported_traits))
						notes = (
							[f"available exported traits: {available}"]
							if available
							else [f"module '{mod}' exports no traits (private by default)"]
						)
						diagnostics.append(
							_p_diag(
								message=f"module '{mod}' does not export trait '{tr.name}'",
								severity="error",
								span=span,
								notes=notes,
							)
						)
						continue
				origin_mod, origin_name = _resolve_trait_origin(mod, tr.name)
				origin_pkg = module_packages_for_scope.get(origin_mod, package_id)
				key = TraitKey(package_id=origin_pkg, module=origin_mod, name=origin_name)
				seen = trait_scope_seen_by_module[mid]
				if key in seen:
					continue
				seen.add(key)
				trait_scope_by_module[mid].append(key)

	for mid in merged_programs:
		if mid in module_exports:
			module_exports[mid]["trait_scope"] = list(trait_scope_by_module.get(mid, []))

	# Collapse edge lists into a simple adjacency set for cycle detection.
	# Include external modules so visibility rules can see package imports.
	deps: dict[str, set[str]] = {
		mid: {to for (to, _sp) in edges if to in merged_programs or (external_module_exports and to in external_module_exports)}
		for mid, edges in dep_edges.items()
	}

	# Resolve module-qualified type references using module-scoped aliases and
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
				return (
					set(ext_types.get("structs") or set())
					| set(ext_types.get("variants") or set())
					| set(ext_types.get("exceptions") or set())
					| set(ext_types.get("interfaces") or set())
					| set(ext_types.get("aliases") or set())
				)
			return set()
		return set()

	def _resolve_type_expr_in_file(
		path: Path,
		file_aliases: dict[str, str],
		te: parser_ast.TypeExpr | None,
		*,
		allow_traits: bool = False,
	) -> None:
		if te is None:
			return
		if getattr(te, "module_alias", None):
			alias = te.module_alias
			mod = file_aliases.get(alias or "")
			span = _span_in_file(path, getattr(te, "loc", None))
			if mod is None and alias:
				# Allow direct module paths in type references without prior import alias.
				if alias in merged_programs or (external_module_exports is not None and alias in external_module_exports):
					mod = alias
			if mod is None:
				diagnostics.append(
					_p_diag(
						message=f"unknown module alias '{alias}' in type reference '{alias}.{te.name}'",
						severity="error",
						span=span,
					)
				)
			else:
				types = _exported_types_for_module(mod)
				if te.name in types:
					# Record the canonical module id for later lowering.
					#
					# If `mod` re-exports this type, resolve it to the defining module
					# identity (no type duplication across module interfaces).
					def_mod, def_name = (mod, te.name)
					reexp = reexported_type_targets_by_module.get(mod)
					if reexp is not None:
						for kind in ("structs", "variants", "exceptions", "interfaces", "aliases"):
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
								for kind in ("structs", "variants", "exceptions", "interfaces", "aliases"):
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
				elif allow_traits:
					traits = _exported_traits_for_module(mod)
					if te.name in traits:
						def_mod, def_name = _resolve_trait_origin(mod, te.name)
						te.module_id = def_mod
						te.name = def_name
						te.module_alias = None
					else:
						available = ", ".join(sorted(traits))
						notes = (
							[f"available exported traits: {available}"]
							if available
							else [f"module '{mod}' exports no traits (private by default)"]
						)
						diagnostics.append(
							_p_diag(
								message=f"module '{mod}' does not export trait '{te.name}'",
								severity="error",
								span=span,
								notes=notes,
							)
						)
				else:
					available = ", ".join(sorted(types))
					notes = (
						[f"available exported types: {available}"]
						if available
						else [f"module '{mod}' exports no types (private by default)"]
					)
					diagnostics.append(
						_p_diag(
							message=f"module '{mod}' does not export type '{te.name}'",
							severity="error",
							span=span,
							notes=notes,
						)
					)
		for a in getattr(te, "args", []) or []:
			_resolve_type_expr_in_file(path, file_aliases, a, allow_traits=allow_traits)

	def _resolve_types_in_block(path: Path, file_aliases: dict[str, str], blk: parser_ast.Block) -> None:
		for st in getattr(blk, "statements", []) or []:
			if isinstance(st, parser_ast.UnsafeBlockStmt):
				_resolve_types_in_block(path, file_aliases, st.block)
				continue
			# Resolve any type-level references embedded in expressions (e.g.,
			# `TypeRef::Ctor(...)` where `TypeRef` may include a module alias).
			def _resolve_types_in_expr(expr: parser_ast.Expr) -> None:
				if isinstance(expr, parser_ast.QualifiedMember):
					_resolve_type_expr_in_file(path, file_aliases, expr.base_type, allow_traits=True)
					return
				if isinstance(expr, parser_ast.Call):
					_resolve_types_in_expr(expr.func)
					for a in getattr(expr, "args", []) or []:
						_resolve_types_in_expr(a)
					for kw in getattr(expr, "kwargs", []) or []:
						_resolve_types_in_expr(kw.value)
					for t in getattr(expr, "type_args", []) or []:
						_resolve_type_expr_in_file(path, file_aliases, t, allow_traits=False)
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
				if isinstance(expr, parser_ast.YieldExpr):
					_resolve_types_in_expr(expr.value)
					return
				if isinstance(expr, parser_ast.MatchExpr):
					_resolve_types_in_expr(expr.scrutinee)
					for arm in getattr(expr, "arms", []) or []:
						_resolve_type_expr_in_file(path, file_aliases, getattr(arm, "ctor_base", None), allow_traits=True)
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
				if isinstance(expr, parser_ast.Cast):
					_resolve_type_expr_in_file(path, file_aliases, expr.target_type)
					_resolve_types_in_expr(expr.expr)
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
				if getattr(st, "var_type_expr", None) is not None:
					_resolve_type_expr_in_file(path, file_aliases, st.var_type_expr)
				_resolve_types_in_block(path, file_aliases, st.body)
			if isinstance(st, parser_ast.ForCountStmt):
				if getattr(st, "init_type_expr", None) is not None:
					_resolve_type_expr_in_file(path, file_aliases, st.init_type_expr)
				_resolve_types_in_expr(st.init_value)
				_resolve_types_in_expr(st.condition)
				if isinstance(st.step, parser_ast.ExprStmt):
					_resolve_types_in_expr(st.step.value)
				elif isinstance(st.step, parser_ast.AssignStmt):
					_resolve_types_in_expr(st.step.target)
					_resolve_types_in_expr(st.step.value)
				elif isinstance(st.step, parser_ast.AugAssignStmt):
					_resolve_types_in_expr(st.step.target)
					_resolve_types_in_expr(st.step.value)
				_resolve_types_in_block(path, file_aliases, st.body)
			if isinstance(st, parser_ast.ThrowStmt):
				_resolve_types_in_expr(st.expr)

	def _resolve_trait_expr_in_file(
		path: Path,
		file_aliases: dict[str, str],
		expr: parser_ast.TraitExpr | None,
	) -> None:
		if expr is None:
			return
		if isinstance(expr, parser_ast.TraitIs):
			_resolve_type_expr_in_file(path, file_aliases, expr.trait, allow_traits=True)
			return
		if isinstance(expr, parser_ast.TraitAnd):
			_resolve_trait_expr_in_file(path, file_aliases, expr.left)
			_resolve_trait_expr_in_file(path, file_aliases, expr.right)
			return
		if isinstance(expr, parser_ast.TraitOr):
			_resolve_trait_expr_in_file(path, file_aliases, expr.left)
			_resolve_trait_expr_in_file(path, file_aliases, expr.right)
			return
		if isinstance(expr, parser_ast.TraitNot):
			_resolve_trait_expr_in_file(path, file_aliases, expr.expr)
			return

	for mid, files in by_module.items():
		for path, prog in files:
			file_aliases = module_aliases_by_module.get(mid, {})
			# Top-level declarations.
			for fn in getattr(prog, "functions", []) or []:
				for p in getattr(fn, "params", []) or []:
					_resolve_type_expr_in_file(path, file_aliases, p.type_expr)
				_resolve_type_expr_in_file(path, file_aliases, getattr(fn, "return_type", None))
				_resolve_trait_expr_in_file(path, file_aliases, getattr(fn, "require", None).expr if getattr(fn, "require", None) is not None else None)
				_resolve_types_in_block(path, file_aliases, fn.body)
			for tr in getattr(prog, "traits", []) or []:
				_resolve_trait_expr_in_file(path, file_aliases, getattr(tr, "require", None).expr if getattr(tr, "require", None) is not None else None)
			for impl in getattr(prog, "implements", []) or []:
				_resolve_type_expr_in_file(path, file_aliases, impl.target)
				_resolve_type_expr_in_file(path, file_aliases, getattr(impl, "trait", None), allow_traits=True)
				_resolve_trait_expr_in_file(path, file_aliases, getattr(impl, "require", None).expr if getattr(impl, "require", None) is not None else None)
				for mfn in getattr(impl, "methods", []) or []:
					for p in getattr(mfn, "params", []) or []:
						_resolve_type_expr_in_file(path, file_aliases, p.type_expr)
					_resolve_type_expr_in_file(path, file_aliases, getattr(mfn, "return_type", None))
					_resolve_types_in_block(path, file_aliases, mfn.body)
			for s in getattr(prog, "structs", []) or []:
				_resolve_trait_expr_in_file(path, file_aliases, getattr(s, "require", None).expr if getattr(s, "require", None) is not None else None)
				for f in getattr(s, "fields", []) or []:
					_resolve_type_expr_in_file(path, file_aliases, f.type_expr)
			for a in getattr(prog, "type_aliases", []) or []:
				_resolve_type_expr_in_file(path, file_aliases, getattr(a, "target", None))
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
			_p_diag(
				message=f"import cycle detected: {' -> '.join(cycle)}",
				severity="error",
				span=primary_span or Span(),
				notes=notes,
			)
		)

	if any(d.severity == "error" for d in diagnostics):
		table = TypeTable()
		table.set_source_manager(source_manager)
		_set_active_source_manager(prev_source_manager)
		return {}, table, {}, {}, {}, diagnostics

	# Lower modules using a shared TypeTable so TypeIds remain comparable across the workspace.
	shared_type_table = TypeTable()
	if package_id is not None:
		shared_type_table.package_id = package_id
	local_pkg = package_id or "__local__"
	has_stdlib = stdlib_root is not None
	if isinstance(external_module_packages, dict):
		for mod, pkg in external_module_packages.items():
			if mod in merged_programs and not test_build_only:
				continue
			if not isinstance(mod, str) or not isinstance(pkg, str):
				continue
			shared_type_table.module_packages.setdefault(mod, pkg)
	for mod in merged_programs:
		if has_stdlib and _is_stdlib_module(mod):
			shared_type_table.module_packages.setdefault(mod, std_pkg)
		elif mod == "lang.core":
			shared_type_table.module_packages.setdefault(mod, "lang.core" if has_stdlib else local_pkg)
		else:
			shared_type_table.module_packages.setdefault(mod, local_pkg)
	shared_type_table.module_packages.setdefault("lang.core", "lang.core" if has_stdlib else local_pkg)
	shared_type_table.module_packages.setdefault("lang.__internal", local_pkg)
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
			if _reject_reserved_nominal_type(getattr(_s, "name", ""), loc=getattr(_s, "loc", None), diagnostics=diagnostics):
				continue
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
						is_pub=bool(getattr(_f, "is_pub", False)),
					)
					for _f in getattr(_s, "fields", []) or []
				]
				shared_type_table.define_struct_schema_fields(struct_id, field_templates)
			except ValueError as err:
				diagnostics.append(_p_diag(message=str(err), severity="error", span=Span.from_loc(getattr(_s, "loc", None))))
		for _i in getattr(_prog, "interfaces", []) or []:
			if _reject_reserved_nominal_type(getattr(_i, "name", ""), loc=getattr(_i, "loc", None), diagnostics=diagnostics):
				continue
			try:
				shared_type_table.declare_interface(
					_mid,
					_i.name,
					list(getattr(_i, "type_params", []) or []),
				)
				interface_type_params = list(getattr(_i, "type_params", []) or [])
				parent_exprs = [
					_generic_type_expr_from_parser(p, type_params=interface_type_params)
					for p in getattr(_i, "parents", []) or []
				]
				parent_base_ids: list[TypeId] = []
				for pexpr in parent_exprs:
					if pexpr.param_index is not None:
						diagnostics.append(
							_p_diag(
								message=f"interface '{_i.name}' parent cannot be a type parameter",
								severity="error",
								span=Span.from_loc(getattr(_i, "loc", None)),
							)
						)
						continue
					parent_mod = pexpr.module_id or _mid
					try:
						base_id = shared_type_table.require_nominal(
							kind=TypeKind.INTERFACE,
							module_id=parent_mod,
							name=pexpr.name,
						)
						parent_base_ids.append(base_id)
					except ValueError as err:
						diagnostics.append(
							_p_diag(
								message=str(err),
								severity="error",
								span=Span.from_loc(getattr(_i, "loc", None)),
							)
						)
				methods = _build_interface_method_schemas(
					_i,
					module_id=_mid,
					type_table=shared_type_table,
					diagnostics=diagnostics,
				)
				interface_id = shared_type_table.require_nominal(kind=TypeKind.INTERFACE, module_id=_mid, name=_i.name)
				shared_type_table.define_interface_schema_methods(
					interface_id,
					methods,
					parents=parent_exprs,
					parent_base_ids=parent_base_ids,
				)
			except ValueError as err:
				diagnostics.append(_p_diag(message=str(err), severity="error", span=Span.from_loc(getattr(_i, "loc", None))))
		for _v in getattr(_prog, "variants", []) or []:
			if _reject_reserved_nominal_type(getattr(_v, "name", ""), loc=getattr(_v, "loc", None), diagnostics=diagnostics):
				continue
			arms: list[VariantArmSchema] = []
			tombstone_ctor: str | None = None
			invalid_variant = False
			for _arm in getattr(_v, "arms", []) or []:
				if getattr(_arm, "tombstone", False):
					if tombstone_ctor is not None:
						diagnostics.append(
							_p_diag(
								message=f"variant '{_v.name}' has multiple @tombstone arms",
								severity="error",
								span=Span.from_loc(getattr(_arm, "loc", None)),
							)
						)
						invalid_variant = True
					else:
						tombstone_ctor = _arm.name
					if getattr(_arm, "fields", []) or []:
						diagnostics.append(
							_p_diag(
								message=f"variant '{_v.name}' tombstone arm '{_arm.name}' must have no payload",
								severity="error",
								span=Span.from_loc(getattr(_arm, "loc", None)),
							)
						)
						invalid_variant = True
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
			if invalid_variant:
				continue
			try:
				shared_type_table.declare_variant(
					_mid,
					_v.name,
					list(getattr(_v, "type_params", []) or []),
					arms,
					tombstone_ctor=tombstone_ctor,
				)
			except ValueError as err:
				diagnostics.append(_p_diag(message=str(err), severity="error", span=Span.from_loc(getattr(_v, "loc", None))))

		if any(d.severity == "error" for d in diagnostics):
			table = TypeTable()
			table.set_source_manager(source_manager)
			_set_active_source_manager(prev_source_manager)
			return {}, table, {}, {}, {}, diagnostics

	all_func_hirs: dict[FunctionId, H.HBlock] = {}
	all_sigs: dict[FunctionId, FnSignature] = {}
	fn_ids_by_name: dict[str, list[FunctionId]] = {}
	func_hirs_by_module: dict[str, dict[FunctionId, H.HBlock]] = {}
	signatures_by_module: dict[str, dict[FunctionId, FnSignature]] = {}
	fn_ids_by_name_by_module: dict[str, dict[str, list[FunctionId]]] = {}
	exc_catalog: dict[str, int] = {}
	fn_owner_module: dict[FunctionId, str] = {}
	impls_by_module: dict[str, list[ImplMeta]] = {}

	# Lower each module and qualify its callable symbols.
	for mid, prog in merged_programs.items():
		func_hirs, sigs, ids_by_name, _table, excs, impl_metas, diags = _lower_parsed_program_to_hir(
			prog,
			diagnostics=[],
			type_table=shared_type_table,
			package_id=package_id,
		)
		diagnostics.extend(diags)
		exc_catalog.update(excs)
		impls_by_module[mid] = list(impl_metas)

		local_free_fns = {fn.name for fn in getattr(prog, "functions", []) or []}
		exported_values = exports_values_by_module.get(mid, {})

		module_func_hirs = func_hirs_by_module.setdefault(mid, {})
		module_sigs = signatures_by_module.setdefault(mid, {})
		module_fn_ids_by_name = fn_ids_by_name_by_module.setdefault(mid, {})

		# Copy function bodies/signatures.
		for fn_id, block in func_hirs.items():
			display_name = function_symbol(fn_id)
			all_func_hirs[fn_id] = block
			module_func_hirs[fn_id] = block
			fn_owner_module[fn_id] = mid
			fn_ids_by_name.setdefault(display_name, []).append(fn_id)
			module_fn_ids_by_name.setdefault(display_name, []).append(fn_id)

		for fn_id, sig in sigs.items():
			local_name = fn_id.name
			# Mark module-interface entry points early so downstream phases can
			# enforce visibility and (later) ABI-boundary rules consistently.
			is_exported = (local_name in local_free_fns) and (local_name in exported_values) and (local_name != "main")
			updated_sig = replace(sig, name=function_symbol(fn_id), is_exported_entrypoint=is_exported)
			all_sigs[fn_id] = updated_sig
			module_sigs[fn_id] = updated_sig

	# Attach impl metadata after lowering so downstream phases can build
	# the global impl index without rescanning signatures.
	for mid, impls in impls_by_module.items():
		if mid in module_exports:
			module_exports[mid]["impls"] = impls

	# Re-exported values are metadata-only aliases; call sites are rewritten to
	# the origin module/value during lowering (no trampolines).

		if any(d.severity == "error" for d in diagnostics):
			table = TypeTable()
			table.set_source_manager(source_manager)
			_set_active_source_manager(prev_source_manager)
			return {}, table, {}, {}, {}, diagnostics

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
						_p_diag(
							message=f"const '{dst_sym}' defined with a different value than re-export target '{origin_sym}'",
							severity="error",
							span=Span(),
						)
					)
				continue
			shared_type_table.define_const(module_id=exporting_mid, name=local_name, type_id=origin_tid, value=origin_val)

	def _rewrite_calls_in_block(
		block: H.HBlock,
		*,
		module_id: str,
		fn_id: FunctionId,
		origin_file: Path | None,
	) -> None:
		file_module_aliases = module_aliases_by_module.get(module_id, {})
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

		def rewrite_const_name(name: str, *, bound: set[str]) -> str:
			if name in bound:
				return name
			return name

		def exported_value_names(mod: str) -> set[str]:
			if external_module_exports is not None and mod in external_module_exports:
				ext = external_module_exports.get(mod) or {}
				return set(ext.get("values") or set())
			values = set((exports_values_by_module.get(mod) or {}).keys())
			values |= set(pub_values_by_module.get(mod) or set())
			return values

		def exported_type_names(mod: str) -> set[str]:
			if external_module_exports is not None and mod in external_module_exports:
				ext = external_module_exports.get(mod) or {}
				ext_types = ext.get("types")
				if isinstance(ext_types, dict):
					return (
						set(ext_types.get("structs") or set())
						| set(ext_types.get("variants") or set())
						| set(ext_types.get("exceptions") or set())
						| set(ext_types.get("interfaces") or set())
						| set(ext_types.get("aliases") or set())
					)
				return set()
			types = _union_exported_types(exports_types_by_module.get(mod))
			pub_types = pub_types_by_module.get(mod) or {}
			types |= set(pub_types.get("structs") or set())
			types |= set(pub_types.get("variants") or set())
			types |= set(pub_types.get("exceptions") or set())
			types |= set(pub_types.get("interfaces") or set())
			types |= set(pub_types.get("aliases") or set())
			return types

		def exported_const_names(mod: str) -> set[str]:
			if external_module_exports is not None and mod in external_module_exports:
				ext = external_module_exports.get(mod) or {}
				return set(ext.get("consts") or set())
			consts = set(exports_consts_by_module.get(mod) or set())
			consts |= set(pub_consts_by_module.get(mod) or set())
			return consts

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

		def exported_alias_names(mod: str) -> set[str]:
			if mod in exports_types_by_module:
				return set((exports_types_by_module.get(mod) or {}).get("aliases") or set())
			if external_module_exports is not None and mod in external_module_exports:
				ext = external_module_exports.get(mod) or {}
				ext_types = ext.get("types")
				if isinstance(ext_types, dict):
					return set(ext_types.get("aliases") or set())
				return set()
			return set()

		def _resolve_alias_target_struct(module_id: str, alias_name: str, seen: set[tuple[str, str]] | None = None) -> tuple[str, str] | None:
			seen = seen if seen is not None else set()
			key = (module_id, alias_name)
			if key in seen:
				return None
			seen.add(key)
			alias_def = shared_type_table.lookup_type_alias(module_id=module_id, name=alias_name)
			if alias_def is None:
				return None
			type_params, target_te, _target_loc = alias_def
			if type_params:
				return None
			target_mod = str(getattr(target_te, "module_id", None) or module_id)
			target_name = getattr(target_te, "name", None)
			if not isinstance(target_name, str) or not target_name:
				return None
			if shared_type_table.get_nominal(kind=TypeKind.STRUCT, module_id=target_mod, name=target_name) is not None:
				return (target_mod, target_name)
			return _resolve_alias_target_struct(target_mod, target_name, seen)

		def _resolve_exported_ctor_target(mod: str, member: str) -> tuple[str, str] | None:
			if member in exported_struct_names(mod):
				return reexported_type_targets_by_module.get(mod, {}).get("structs", {}).get(member, (mod, member))
			if member not in exported_alias_names(mod):
				return None
			def_mod, def_name = reexported_type_targets_by_module.get(mod, {}).get("aliases", {}).get(member, (mod, member))
			return _resolve_alias_target_struct(def_mod, def_name)

		def exported_value_origin(mod: str, name: str) -> tuple[str, str] | None:
			if mod in exports_values_by_module:
				origin = (exports_values_by_module.get(mod) or {}).get(name)
				if origin is not None:
					return origin
			if external_module_exports is not None and mod in external_module_exports:
				ext = external_module_exports.get(mod) or {}
				ext_reexp = ext.get("reexports")
				if isinstance(ext_reexp, dict):
					vals = ext_reexp.get("values")
					if isinstance(vals, dict):
						entry = vals.get(name)
						if isinstance(entry, dict):
							tm = entry.get("module")
							tn = entry.get("name")
							if isinstance(tm, str) and isinstance(tn, str):
								return (tm, tn)
				if name in (ext.get("values") or set()):
					return (mod, name)
			return None

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
			  import lib as x;
			  x.foo(1, 2)   // call exported function foo from module lib
			  x.Point(...)  // call struct constructor Point from module lib

			We do *not* create a runtime module object. Instead, we resolve the
			member at compile time and rewrite the callee to carry the target
			module id, letting later phases resolve by `(module_id, name)`.

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
			if member in vals:
				origin = exported_value_origin(mod, member) or (mod, member)
				return H.HCall(
					fn=H.HVar(name=origin[1], module_id=origin[0]),
					args=args,
					kwargs=kwargs,
					type_args=type_args,
				)
			ctor_target = _resolve_exported_ctor_target(mod, member)
			if ctor_target is not None:
				# Constructor call through a module alias. MVP supports only struct ctors.
				def_mod, def_name = ctor_target
				struct_id = shared_type_table.get_nominal(kind=TypeKind.STRUCT, module_id=def_mod, name=def_name)
				if struct_id is None:
					diagnostics.append(
						_p_diag(
							message=f"module-qualified constructor call '{alias}.{member}(...)' is only supported for structs in MVP",
							severity="error",
							span=getattr(receiver, "loc", Span()),
						)
					)
					return None
				# Record the target module id so later phases can resolve the
				# constructor deterministically even when multiple modules define
				# the same short type name.
				return H.HCall(
					fn=H.HVar(name=def_name, module_id=def_mod),
					args=args,
					kwargs=kwargs,
					type_args=type_args,
				)
			ext_vals = exported_value_names(mod)
			ext_types = exported_type_names(mod)
			if member not in ext_vals and member not in ext_types:
				available = ", ".join(sorted(ext_vals | ext_types))
				notes = (
					[f"available exports: {available}"]
					if available
					else [f"module '{mod}' exports nothing (private by default)"]
				)
				diagnostics.append(
					_p_diag(
						message=f"module '{mod}' does not export symbol '{member}'",
						severity="error",
						span=getattr(receiver, "loc", Span()),
						notes=notes,
					)
				)
				return None
			if member in ext_types:
				diagnostics.append(
					_p_diag(
						message=f"module-qualified constructor call '{alias}.{member}(...)' is only supported for structs in MVP",
						severity="error",
						span=getattr(receiver, "loc", Span()),
					)
				)
				return None
			# Defer export/visibility checks to type resolution for non-module-qualified
			# value references; qualified calls are validated here.
			return H.HCall(
				fn=H.HVar(name=member, module_id=mod),
				args=args,
				kwargs=kwargs,
				type_args=type_args,
			)

		def _rewrite_module_qualified_value(
			*,
			receiver: H.HExpr,
			member: str,
			bound: set[str],
		) -> H.HExpr | None:
			"""
			Rewrite a module-qualified value reference `x.member` when `x` is a module alias.

			MVP supports exported values (functions/consts) in value position so
			function references can be formed via `x.member`.
			"""
			if not isinstance(receiver, H.HVar):
				return None
			if receiver.binding_id is not None or receiver.name in bound:
				return None
			alias = receiver.name
			mod = file_module_aliases.get(alias)
			if mod is None:
				return None
			vals = exported_value_names(mod)
			consts = exported_const_names(mod)
			types = exported_type_names(mod)
			if member in vals:
				return H.HVar(name=member, module_id=mod)
			if member in consts:
				return H.HVar(name=member, module_id=mod)
			available = ", ".join(sorted(vals | types | consts))
			notes = (
				[f"available exports: {available}"]
				if available
				else [f"module '{mod}' exports nothing (private by default)"]
			)
			diagnostics.append(
				_p_diag(
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

			if isinstance(expr, H.HField):
				expr.subject = walk_expr(expr.subject, bound=bound)
				rewritten = _rewrite_module_qualified_value(
					receiver=expr.subject,
					member=expr.name,
					bound=bound,
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
				if isinstance(expr.fn, H.HField) and isinstance(expr.fn.subject, H.HVar):
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
			if isinstance(expr, getattr(H, "HInvoke", ())):
				expr.callee = walk_expr(expr.callee, bound=bound)
				expr.args = [walk_expr(a, bound=bound) for a in expr.args]
				for kw in getattr(expr, "kwargs", []) or []:
					if getattr(kw, "value", None) is not None:
						kw.value = walk_expr(kw.value, bound=bound)
				if isinstance(expr.callee, H.HField) and isinstance(expr.callee.subject, H.HVar):
					q = _rewrite_module_qualified_call(
						receiver=expr.callee.subject,
						member=expr.callee.name,
						args=expr.args,
						kwargs=getattr(expr, "kwargs", []) or [],
						type_args=getattr(expr, "type_args", None),
					)
					if isinstance(q, H.HCall):
						return q
				return expr

			if isinstance(expr, H.HVar):
				expr.name = rewrite_const_name(expr.name, bound=bound)
				return expr

			if isinstance(expr, H.HField) and isinstance(expr.subject, H.HVar) and expr.subject.binding_id is None:
				mod = file_module_aliases.get(expr.subject.name)
				if mod is not None:
					if expr.name in exported_value_names(mod):
						return H.HVar(name=expr.name, module_id=mod)
					if expr.name in exported_const_names(mod):
						# Module-qualified const access always targets the module’s own
						# const table. Const re-exports are materialized by copying the
						# literal value into the exporting module, so consumers do not
						# need to reference the origin module.
						return H.HVar(name=expr.name, module_id=mod)
					available = ", ".join(sorted(exported_value_names(mod) | exported_const_names(mod) | exported_type_names(mod)))
					notes = (
						[f"available exports: {available}"]
						if available
						else [f"module '{mod}' exports nothing (private by default)"]
					)
					diagnostics.append(
						_p_diag(
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
			if hasattr(H, "HUnsafeBlock") and isinstance(stmt, getattr(H, "HUnsafeBlock")):
				walk_block(stmt.block, bound=bound)
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
	for fn_id, block in all_func_hirs.items():
		fn_mod = fn_owner_module.get(fn_id, "main")
		src_path = module_file_by_id.get(fn_mod)
		_rewrite_calls_in_block(
			block,
			module_id=fn_mod,
			fn_id=fn_id,
			origin_file=src_path,
		)

	# Cross-module exception code collision detection: event codes are derived
	# from the canonical event FQN (`module:Event`). Collisions are extremely
	# unlikely, but if they happen we must diagnose them deterministically.
	payload_seen: dict[int, str] = {}
	for fqn, code in exc_catalog.items():
		payload = code & PAYLOAD_MASK
		other = payload_seen.get(payload)
		if other is not None and other != fqn:
			diagnostics.append(_p_diag(message=f"exception code collision between '{other}' and '{fqn}' (payload {payload})", severity="error", span=Span()))
		else:
			payload_seen[payload] = fqn

	type_defs_by_module: dict[str, dict[str, list[str]]] = {}
	for mid, prog in merged_programs.items():
		type_defs_by_module[mid] = {
			"structs": [s.name for s in getattr(prog, "structs", []) or []],
			"variants": [v.name for v in getattr(prog, "variants", []) or []],
			"exceptions": [e.name for e in getattr(prog, "exceptions", []) or []],
			"aliases": [a.name for a in getattr(prog, "type_aliases", []) or []],
		}

	trait_worlds = getattr(shared_type_table, "trait_worlds", None)
	if not isinstance(trait_worlds, dict):
		trait_worlds = {}
	requires_by_fn_by_module: dict[str, dict[FunctionId, parser_ast.TraitExpr]] = {}
	requires_by_struct_by_module: dict[str, dict["TypeKey", parser_ast.TraitExpr]] = {}
	for mid in merged_programs.keys():
		world = trait_worlds.get(mid)
		if world is None:
			requires_by_fn_by_module[mid] = {}
			requires_by_struct_by_module[mid] = {}
		else:
			requires_by_fn_by_module[mid] = dict(getattr(world, "requires_by_fn", {}) or {})
			requires_by_struct_by_module[mid] = dict(getattr(world, "requires_by_struct", {}) or {})

	modules: dict[str, ModuleLowered] = {}
	for mid in merged_programs.keys():
		modules[mid] = ModuleLowered(
			module_id=mid,
			package_id=package_id,
			source_path=module_file_by_id.get(mid, Path("<unknown>")),
			func_hirs=func_hirs_by_module.get(mid, {}),
			signatures_by_id=signatures_by_module.get(mid, {}),
			fn_ids_by_name=fn_ids_by_name_by_module.get(mid, {}),
			requires_by_fn=requires_by_fn_by_module.get(mid, {}),
			requires_by_struct=requires_by_struct_by_module.get(mid, {}),
			type_defs=type_defs_by_module.get(mid, {}),
			impl_defs=impls_by_module.get(mid, []),
			origin_by_fn_id={
				fn_id: module_file_by_id.get(mid)
				for fn_id in func_hirs_by_module.get(mid, {}).keys()
				if module_file_by_id.get(mid) is not None
			},
		)
	# After all modules are parsed, normalize exception-named forward nominals in
	# signatures using the full exception schema set.
	def _coerce_exception_nominal(tid: TypeId) -> TypeId:
		try:
			td = shared_type_table.get(tid)
		except Exception:
			return tid
		if td.kind is TypeKind.FORWARD_NOMINAL and td.module_id:
			fqn = f"{td.module_id}:{td.name}"
			if fqn in shared_type_table.exception_schemas:
				return shared_type_table.ensure_error()
		if td.kind is TypeKind.REF and td.param_types:
			inner = td.param_types[0]
			new_inner = _coerce_exception_nominal(inner)
			if new_inner != inner:
				return shared_type_table.ensure_ref_mut(new_inner) if td.ref_mut else shared_type_table.ensure_ref(new_inner)
		return tid
	for mod in modules.values():
		for sig in mod.signatures_by_id.values():
			if sig.param_type_ids:
				sig.param_type_ids = [_coerce_exception_nominal(t) for t in sig.param_type_ids]
			if sig.return_type_id is not None:
				sig.return_type_id = _coerce_exception_nominal(sig.return_type_id)
	for module in modules.values():
		for block in module.func_hirs.values():
			assign_callsite_ids(block, start=0)

	shared_type_table.set_source_manager(source_manager)
	_set_active_source_manager(prev_source_manager)
	return modules, shared_type_table, exc_catalog, module_exports, deps, diagnostics


def _lower_parsed_program_to_hir(
	prog: parser_ast.Program,
	*,
	diagnostics: list[Diagnostic] | None = None,
	type_table: TypeTable | None = None,
	package_id: str | None = None,
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
	from lang.driftc.traits.world import (
		TypeKey,
		build_trait_world,
		resolve_trait_subjects,
		resolve_struct_require_subjects,
		trait_key_from_expr,
	)

	diagnostics = list(diagnostics or [])
	module_name = getattr(prog, "module", None)
	module_id = module_name or "main"
	type_table = type_table or TypeTable()
	if package_id is not None:
		type_table.module_packages.setdefault(module_id, package_id)
	func_hirs: Dict[FunctionId, H.HBlock] = {}
	fn_ids_by_name: Dict[str, List[FunctionId]] = {}
	decls: list[_FrontendDecl] = []
	signatures: Dict[FunctionId, FnSignature] = {}
	impl_metas: list[ImplMeta] = []
	lowerer = AstToHIR()
	lowerer._module_name = module_id
	module_aliases: dict[str, str] = {}
	for imp in getattr(prog, "imports", []) or []:
		mod = ".".join(getattr(imp, "path", []) or [])
		if not mod:
			continue
		alias = getattr(imp, "alias", None) or (getattr(imp, "path", []) or [mod])[-1]
		if alias not in module_aliases:
			module_aliases[alias] = mod
	lowerer._module_aliases = module_aliases
	module_function_names: set[str] = {fn.name for fn in getattr(prog, "functions", []) or []}
	exception_schemas: dict[str, tuple[str, list[str]]] = {}
	struct_defs = list(getattr(prog, "structs", []) or [])
	variant_defs = list(getattr(prog, "variants", []) or [])
	interface_defs = list(getattr(prog, "interfaces", []) or [])
	type_alias_defs = list(getattr(prog, "type_aliases", []) or [])
	struct_param_maps: dict[TypeKey, dict[str, TypeParamId]] = {}
	exception_catalog: dict[str, int] = _build_exception_catalog(prog.exceptions, module_id, diagnostics)
	for exc in prog.exceptions:
		fqn = f"{module_id}:{exc.name}"
		field_names = [arg.name for arg in getattr(exc, "args", [])]
		exception_schemas[fqn] = (fqn, field_names)
	# Make exception schemas visible before signature resolution so exception
	# types can be used in annotations without minting forward-nominal types.
	prev_exc = getattr(type_table, "exception_schemas", None)
	if not isinstance(prev_exc, dict):
		prev_exc = {}
	prev_exc.update(exception_schemas)
	type_table.exception_schemas = prev_exc
	# Build a TypeTable early so we can register user-defined type names (structs)
	# before resolving function signatures. This prevents `resolve_opaque_type`
	# from minting unrelated placeholder TypeIds for struct names.
	if package_id is not None:
		type_table.package_id = package_id
	_prime_builtins(type_table)
	# Build a per-module TraitWorld and stash it on the shared TypeTable so later
	# phases can enforce requirements without re-parsing sources.
	world = build_trait_world(
		prog,
		diagnostics=diagnostics,
		package_id=package_id,
		module_packages=getattr(type_table, "module_packages", None),
		diag_phase="parser",
	)
	trait_worlds = getattr(type_table, "trait_worlds", None)
	if not isinstance(trait_worlds, dict):
		trait_worlds = {}
	trait_worlds[module_id] = world
	type_table.trait_worlds = trait_worlds

	# Register module-local type aliases (MVP: module-scoped only).
	alias_names: set[str] = set()
	nominal_names: set[str] = {s.name for s in struct_defs} | {v.name for v in variant_defs} | {i.name for i in interface_defs} | {e.name for e in getattr(prog, "exceptions", []) or []}
	for a in type_alias_defs:
		if _reject_reserved_nominal_type(getattr(a, "name", ""), loc=getattr(a, "loc", None), diagnostics=diagnostics):
			continue
		if a.name in alias_names:
			diagnostics.append(
				_p_diag(
					phase="parser",
					message=f"duplicate type alias '{a.name}' in module '{module_id}'",
					severity="error",
					span=Span.from_loc(getattr(a, "loc", None)),
				)
			)
			continue
		if a.name in nominal_names:
			diagnostics.append(
				_p_diag(
					phase="parser",
					message=f"type alias '{a.name}' conflicts with existing type in module '{module_id}'",
					severity="error",
					span=Span.from_loc(getattr(a, "loc", None)),
				)
			)
			continue
		alias_names.add(a.name)
		type_table.define_type_alias(module_id=module_id, name=a.name, type_params=list(getattr(a, "type_params", []) or []), target=getattr(a, "target", None), loc=getattr(a, "loc", None))

	# Detect alias cycles within the module (best-effort).
	if type_alias_defs:
		alias_targets: dict[str, parser_ast.TypeExpr | None] = {a.name: getattr(a, "target", None) for a in type_alias_defs}
		def _collect_alias_refs(te: parser_ast.TypeExpr | None, refs: set[str]) -> None:
			if te is None:
				return
			name = getattr(te, "name", None)
			mod = getattr(te, "module_id", None)
			if name in alias_targets and (mod is None or mod == module_id):
				refs.add(str(name))
			for arg in getattr(te, "args", []) or []:
				_collect_alias_refs(arg, refs)
		alias_graph: dict[str, set[str]] = {}
		for name, te in alias_targets.items():
			refs: set[str] = set()
			_collect_alias_refs(te, refs)
			alias_graph[name] = refs
		visiting: set[str] = set()
		visited: set[str] = set()
		def _visit(name: str, stack: list[str]) -> None:
			if name in visited:
				return
			if name in visiting:
				cycle = " -> ".join(stack + [name])
				diag_loc = next((a.loc for a in type_alias_defs if a.name == name), None)
				diagnostics.append(
					_p_diag(
						phase="parser",
						message=f"type alias cycle detected: {cycle}",
						severity="error",
						span=Span.from_loc(diag_loc),
					)
				)
				return
			visiting.add(name)
			for dep in alias_graph.get(name, set()):
				_visit(dep, stack + [name])
			visiting.remove(name)
			visited.add(name)
		for name in alias_graph:
			_visit(name, [])

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
				_p_diag(
					phase="parser",
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
		if decl_ty == type_table.ensure_byte() and isinstance(val, int):
			if val < 0 or val > 255:
				diagnostics.append(
					_p_diag(
						phase="parser",
						message=f"const '{c.name}' Byte literal out of range [0, 255]",
						severity="error",
						span=Span.from_loc(getattr(c, "loc", None)),
					)
				)
				continue
			ok = True
		if decl_ty == type_table.ensure_int() and isinstance(val, int):
			ok = True
		elif decl_ty == type_table.ensure_uint() and isinstance(val, int) and val >= 0:
			ok = True
		elif decl_ty == type_table.ensure_uint64() and isinstance(val, int) and val >= 0:
			ok = True
		elif decl_ty == type_table.ensure_bool() and isinstance(val, bool):
			ok = True
		elif decl_ty == type_table.ensure_string() and isinstance(val, str):
			ok = True
		elif decl_ty == type_table.ensure_float() and isinstance(val, float):
			ok = True
		if not ok:
			diagnostics.append(
				_p_diag(
					phase="parser",
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
	#   variant Optional<T> { None, Some(value: T) }
	if not any(getattr(v, "name", None) == "Optional" for v in variant_defs) and type_table.get_variant_base(
		module_id="lang.core", name="Optional"
	) is None:
		type_table.ensure_optional_base()
	# Declare all struct names first (placeholder field types) to support recursion.
	for s in struct_defs:
		if _reject_reserved_nominal_type(getattr(s, "name", ""), loc=getattr(s, "loc", None), diagnostics=diagnostics):
			continue
		field_names = [f.name for f in getattr(s, "fields", [])]
		try:
			struct_base_id = type_table.declare_struct(
				module_id,
				s.name,
				field_names,
				list(getattr(s, "type_params", []) or []),
			)
			param_ids = type_table.get_struct_type_param_ids(struct_base_id) or []
			if param_ids:
				struct_param_maps[TypeKey(package_id=package_id, module=module_id, name=s.name, args=())] = {
					name: pid for name, pid in zip(getattr(s, "type_params", []) or [], param_ids)
				}
		except ValueError as err:
			diagnostics.append(_p_diag(message=str(err), severity="error", span=Span.from_loc(getattr(s, "loc", None))))
	# Declare interfaces next so type resolution can bind nominal references.
	for i in interface_defs:
		if _reject_reserved_nominal_type(getattr(i, "name", ""), loc=getattr(i, "loc", None), diagnostics=diagnostics):
			continue
		try:
			type_table.declare_interface(
				module_id,
				i.name,
				list(getattr(i, "type_params", []) or []),
			)
			interface_type_params = list(getattr(i, "type_params", []) or [])
			parent_exprs = [
				_generic_type_expr_from_parser(p, type_params=interface_type_params)
				for p in getattr(i, "parents", []) or []
			]
			parent_base_ids: list[TypeId] = []
			for pexpr in parent_exprs:
				if pexpr.param_index is not None:
					diagnostics.append(
						_p_diag(
							message=f"interface '{i.name}' parent cannot be a type parameter",
							severity="error",
							span=Span.from_loc(getattr(i, "loc", None)),
						)
					)
					continue
				parent_mod = pexpr.module_id or module_id
				try:
					base_id = type_table.require_nominal(
						kind=TypeKind.INTERFACE,
						module_id=parent_mod,
						name=pexpr.name,
					)
					parent_base_ids.append(base_id)
				except ValueError as err:
					diagnostics.append(
						_p_diag(
							message=str(err),
							severity="error",
							span=Span.from_loc(getattr(i, "loc", None)),
						)
					)
			methods = _build_interface_method_schemas(
				i,
				module_id=module_id,
				type_table=type_table,
				diagnostics=diagnostics,
			)
			interface_id = type_table.require_nominal(kind=TypeKind.INTERFACE, module_id=module_id, name=i.name)
			type_table.define_interface_schema_methods(
				interface_id,
				methods,
				parents=parent_exprs,
				parent_base_ids=parent_base_ids,
			)
		except ValueError as err:
			diagnostics.append(_p_diag(message=str(err), severity="error", span=Span.from_loc(getattr(i, "loc", None))))
	# Declare all variant names/schemas next so type resolution can instantiate
	# variants (e.g., Optional<Int>) while resolving later annotations/fields.
	for v in variant_defs:
		if _reject_reserved_nominal_type(getattr(v, "name", ""), loc=getattr(v, "loc", None), diagnostics=diagnostics):
			continue
		arms: list[VariantArmSchema] = []
		tombstone_ctor: str | None = None
		invalid_variant = False
		for arm in getattr(v, "arms", []) or []:
			if getattr(arm, "tombstone", False):
				if tombstone_ctor is not None:
					diagnostics.append(
						_p_diag(
							message=f"variant '{v.name}' has multiple @tombstone arms",
							severity="error",
							span=Span.from_loc(getattr(arm, "loc", None)),
						)
					)
					invalid_variant = True
				else:
					tombstone_ctor = arm.name
				if getattr(arm, "fields", []) or []:
					diagnostics.append(
						_p_diag(
							message=f"variant '{v.name}' tombstone arm '{arm.name}' must have no payload",
							severity="error",
							span=Span.from_loc(getattr(arm, "loc", None)),
						)
					)
					invalid_variant = True
			fields = [
				VariantFieldSchema(
					name=f.name,
					type_expr=_generic_type_expr_from_parser(f.type_expr, type_params=list(getattr(v, "type_params", []) or [])),
				)
				for f in getattr(arm, "fields", []) or []
			]
			arms.append(VariantArmSchema(name=arm.name, fields=fields))
		if invalid_variant:
			continue
		try:
			type_table.declare_variant(
				module_id,
				v.name,
				list(getattr(v, "type_params", []) or []),
				arms,
				tombstone_ctor=tombstone_ctor,
			)
		except ValueError as err:
			diagnostics.append(_p_diag(message=str(err), severity="error", span=Span.from_loc(getattr(v, "loc", None))))
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
						is_pub=bool(getattr(f, "is_pub", False)),
					)
				)
			if type_params:
				continue
			ft = resolve_opaque_type(f.type_expr, type_table, module_id=module_id)
			field_types.append(ft)
		type_table.define_struct_schema_fields(struct_id, field_templates)
		if not type_params:
			type_table.define_struct_fields(struct_id, field_types)
	# After all variant schemas are known and structs are declared, finalize
	# non-generic variants so their concrete arm types are available.
	type_table.finalize_variants()
	# Resolve struct require subjects now that struct type params are known.
	resolve_struct_require_subjects(world, struct_param_maps)
	seen_sig: dict[tuple, object | None] = {}
	name_ord: dict[str, int] = {}
	for fn in prog.functions:
		require_key = _trait_expr_key(fn.require.expr) if getattr(fn, "require", None) is not None else None
		sig_key = (
			module_id,
			fn.name,
			len(getattr(fn, "params", []) or []),
			tuple(_type_expr_key(p.type_expr) for p in getattr(fn, "params", []) or []),
			require_key,
		)
		if sig_key in seen_sig:
			diagnostics.append(
				_p_diag(
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
		fn_ids_by_name.setdefault(function_symbol(fn_id), []).append(fn_id)
		if getattr(fn, "require", None) is not None:
			world.requires_by_fn[fn_id] = fn.require.expr
		decl_decl = _decl_from_parser_fn(fn, fn_id=fn_id)
		decl_decl.module = module_id
		# Reject FnResult in surface type annotations (return or parameter types).
		# FnResult is an internal ABI carrier in lang, not a user-facing type.
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
		decls.append(decl_decl)
		stmt_block = _convert_block(fn.body)
		param_names = [p.name for p in getattr(fn, "params", []) or []]
		try:
			hir_block = lowerer.lower_function_block(stmt_block, param_names=param_names)
		except ValueError as err:
			diagnostics.append(
				_p_diag(
					phase="parser",
					message=str(err),
					severity="error",
					span=Span.from_loc(getattr(fn, "loc", None)),
				)
			)
			hir_block = H.HBlock(statements=[])
		func_hirs[fn_id] = hir_block
	# Methods inside implement blocks.
	for impl_index, impl in enumerate(getattr(prog, "implements", [])):
		# Allow reference-qualified impl headers (e.g., for Iterable<&T, ...>).
		impl_type_params = list(getattr(impl, "type_params", []) or [])
		impl_type_param_locs = list(getattr(impl, "type_param_locs", []) or [])
		impl_target_str = _type_expr_key_str(impl.target)
		impl_trait_str = _type_expr_key_str(impl.trait) if getattr(impl, "trait", None) is not None else None
		impl_trait_key = None
		if getattr(impl, "trait", None) is not None:
			trait_mod = getattr(impl.trait, "module_id", None) or module_id
			if type_table.get_interface_base(module_id=trait_mod, name=impl.trait.name) is None:
				impl_trait_key = trait_key_from_expr(
					impl.trait,
					default_module=module_id,
					default_package=package_id,
					module_packages=getattr(type_table, "module_packages", None),
				)
		impl_owner = FunctionId(
			module="lang.__internal",
			name=f"__impl_{module_id}::{impl_trait_str or 'inherent'}::{impl_target_str}",
			ordinal=impl_index,
		)
		impl_param_ids = {name: TypeParamId(impl_owner, idx) for idx, name in enumerate(impl_type_params)}
		impl_trait_args: list[TypeId] = []
		if getattr(impl, "trait", None) is not None:
			for arg in (getattr(impl.trait, "args", []) or []):
				impl_trait_args.append(
					resolve_opaque_type(
						arg,
						type_table,
						module_id=module_id,
						type_params=impl_param_ids,
					)
				)
		impl_target_type_id = resolve_opaque_type(
			impl.target,
			type_table,
			module_id=module_id,
			type_params=impl_param_ids,
		)
		require_expr = None
		if getattr(impl, "require", None) is not None:
			require_expr = resolve_trait_subjects(impl.require.expr, impl_param_ids)
		impl_meta = ImplMeta(
			impl_id=impl_index,
			def_module=module_id,
			target_type_id=impl_target_type_id,
			trait_key=impl_trait_key,
			trait_expr=impl.trait,
			trait_args=impl_trait_args,
			require_expr=require_expr,
			target_expr=impl.target,
			impl_type_params=list(impl_type_params),
			loc=Span.from_loc(getattr(impl, "loc", None)),
			methods=[],
		)
		for fn in impl.methods:
			# Note: receiver shape/name/type are semantic rules enforced by the
			# typecheck phase. The parser adapter stays structural-only here so
			# related errors consistently report as typecheck diagnostics.
			receiver_ty = fn.params[0].type_expr if fn.params else None
			self_mode: str | None = None
			if receiver_ty is not None and getattr(fn.params[0], "name", None) == "self":
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
					mutable=bool(getattr(p, "mutable", False)),
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
			ordinal = name_ord.get(symbol_name, 0)
			name_ord[symbol_name] = ordinal + 1
			fn_id = FunctionId(module=module_id, name=symbol_name, ordinal=ordinal)
			fn_ids_by_name.setdefault(function_symbol(fn_id), []).append(fn_id)
			impl_req = getattr(impl, "require", None)
			fn_req = getattr(fn, "require", None)
			if impl_req is not None or fn_req is not None:
				req_expr = fn_req.expr if fn_req is not None else None
				if impl_req is not None:
					req_expr = impl_req.expr if req_expr is None else parser_ast.TraitAnd(
						loc=getattr(impl_req, "loc", None),
						left=impl_req.expr,
						right=req_expr,
					)
				if req_expr is not None:
					world.requires_by_fn[fn_id] = req_expr
			impl_meta.methods.append(
				ImplMethodMeta(
					fn_id=fn_id,
					name=fn.name,
					is_pub=bool(getattr(fn, "is_pub", False)),
					loc=Span.from_loc(getattr(fn, "loc", None)),
				)
			)
			trait_method_declared_nothrow = False
			if getattr(impl, "trait", None) is not None:
				trait_mod = getattr(impl.trait, "module_id", None) or module_id
				trait_worlds_map = getattr(type_table, "trait_worlds", None)
				trait_world = trait_worlds_map.get(trait_mod) if isinstance(trait_worlds_map, dict) else None
				if trait_world is not None:
					trait_defs = getattr(trait_world, "traits", {}) or {}
					for trait_key, trait_def in trait_defs.items():
						if getattr(trait_key, "module", None) != trait_mod:
							continue
						if getattr(trait_key, "name", None) != impl.trait.name:
							continue
						for meth in list(getattr(trait_def, "methods", []) or []):
							if meth.name == fn.name:
								trait_method_declared_nothrow = bool(getattr(meth, "declared_nothrow", False))
								break
						if trait_method_declared_nothrow:
							break
			if getattr(impl, "trait", None) is not None and not trait_method_declared_nothrow:
				trait_mod = getattr(impl.trait, "module_id", None) or module_id
				trait_base_id = type_table.get_interface_base(module_id=trait_mod, name=impl.trait.name)
				if trait_base_id is not None:
					try:
						trait_linear = type_table.interface_linearization(trait_base_id)
					except Exception:
						trait_linear = [trait_base_id]
					for owner_id in trait_linear:
						owner_schema = type_table.interface_bases.get(owner_id)
						for meth in list(getattr(owner_schema, "methods", []) or []):
							if meth.name == fn.name:
								trait_method_declared_nothrow = bool(getattr(meth, "declared_nothrow", False))
								break
						if trait_method_declared_nothrow:
							break
			declared_nothrow = bool(getattr(fn, "declared_nothrow", False)) or trait_method_declared_nothrow
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
					declared_nothrow,
					is_unsafe=bool(getattr(fn, "is_unsafe", False)),
					is_pub=bool(getattr(fn, "is_pub", False)),
					is_method=bool(self_mode is not None),
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
				except ValueError as err:
					diagnostics.append(
						_p_diag(
							phase="parser",
							message=str(err),
							severity="error",
							span=Span.from_loc(getattr(fn, "loc", None)),
						)
					)
					hir_block = H.HBlock(statements=[])
				finally:
					lowerer._pop_implicit_self()
			else:
				try:
					hir_block = lowerer.lower_function_block(stmt_block, param_names=param_names)
				except ValueError as err:
					diagnostics.append(
						_p_diag(
							phase="parser",
							message=str(err),
							severity="error",
							span=Span.from_loc(getattr(fn, "loc", None)),
						)
					)
					hir_block = H.HBlock(statements=[])
			func_hirs[fn_id] = hir_block
		impl_metas.append(impl_meta)
	# Build signatures with resolved TypeIds from parser decls.
	from lang.driftc.type_resolver import resolve_program_signatures

	type_table, sigs = resolve_program_signatures(decls, table=type_table)
	signatures.update(sigs)
	# Normalize any exception-named forward nominals in signatures to Error so
	# helpers can accept exception types in annotations.
	def _coerce_exception_nominal(tid: TypeId) -> TypeId:
		try:
			td = type_table.get(tid)
		except Exception:
			return tid
		if td.kind is TypeKind.FORWARD_NOMINAL and td.module_id:
			fqn = f"{td.module_id}:{td.name}"
			if fqn in type_table.exception_schemas:
				return type_table.ensure_error()
		if td.kind is TypeKind.REF and td.param_types:
			inner = td.param_types[0]
			new_inner = _coerce_exception_nominal(inner)
			if new_inner != inner:
				return type_table.ensure_ref_mut(new_inner) if td.ref_mut else type_table.ensure_ref(new_inner)
		return tid
	for sig in signatures.values():
		sig.param_type_ids = [_coerce_exception_nominal(t) for t in sig.param_type_ids]
		sig.return_type_id = _coerce_exception_nominal(sig.return_type_id)
	for impl in impl_metas:
		for method in getattr(impl, "methods", []) or []:
			if not getattr(method, "is_pub", False):
				continue
			sig = signatures.get(getattr(method, "fn_id", None))
			if sig is None:
				continue
			if not getattr(sig, "is_pub", False):
				diagnostics.append(
					_p_diag(
						message=f"compiler bug: lost pub on method signature for '{sig.name}'",
						severity="error",
						span=getattr(method, "loc", None),
					)
				)
	# Resolve function require subjects (T -> TypeParamId) now that signatures exist.
	from lang.driftc.traits.world import resolve_fn_require_subjects

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


def parse_drift_to_hir(
	path: Path,
	*,
	package_id: str | None = None,
	test_build_only: bool = False,
) -> Tuple[ModuleLowered, "TypeTable", Dict[str, int], List[Diagnostic]]:
	"""
	Parse a Drift source file into lang HIR blocks + FnSignatures + TypeTable.

	Collects parser/adapter diagnostics (e.g., duplicate functions) instead of
	throwing, so callers can report them alongside later pipeline checks.
	"""
	path = path.resolve()
	source_manager = SourceManager()
	prev_source_manager = _ACTIVE_SOURCE_MANAGER
	_set_active_source_manager(source_manager)
	source = path.read_text()
	file_id = source_manager.add(str(path), source)
	try:
		prog = _parser.parse_program(source, filename=str(path), file_id=file_id)
		prog = _filter_test_build_only(prog, test_build_only=test_build_only)
	except _parser.FStringParseError as err:
		empty = ModuleLowered(
			module_id="main",
			package_id=package_id,
			source_path=path,
			func_hirs={},
			signatures_by_id={},
			fn_ids_by_name={},
			requires_by_fn={},
			requires_by_struct={},
			type_defs={},
			impl_defs=[],
			origin_by_fn_id={},
		)
		diags = [_p_diag(message=str(err), severity="error", span=_span_in_file(path, err.loc))]
		_relabel_diagnostics(diags, {str(path): "<source>"})
		table = TypeTable()
		table.set_source_manager(source_manager)
		_set_active_source_manager(prev_source_manager)
		return empty, table, {}, diags
	except _parser.QualifiedMemberParseError as err:
		empty = ModuleLowered(
			module_id="main",
			package_id=package_id,
			source_path=path,
			func_hirs={},
			signatures_by_id={},
			fn_ids_by_name={},
			requires_by_fn={},
			requires_by_struct={},
			type_defs={},
			impl_defs=[],
			origin_by_fn_id={},
		)
		diags = [_p_diag(message=str(err), severity="error", span=_span_in_file(path, err.loc))]
		_relabel_diagnostics(diags, {str(path): "<source>"})
		table = TypeTable()
		table.set_source_manager(source_manager)
		_set_active_source_manager(prev_source_manager)
		return empty, table, {}, diags
	except UnexpectedInput as err:
		code = _parse_error_code(err)
		message = _parse_error_message(err, code)
		span = Span(
			file=str(path),
			line=getattr(err, "line", None),
			column=getattr(err, "column", None),
			raw=err,
		)
		empty = ModuleLowered(
			module_id="main",
			package_id=package_id,
			source_path=path,
			func_hirs={},
			signatures_by_id={},
			fn_ids_by_name={},
			requires_by_fn={},
			requires_by_struct={},
			type_defs={},
			impl_defs=[],
			origin_by_fn_id={},
		)
		diags = [_p_diag(message=message, severity="error", span=span, code=code)]
		_relabel_diagnostics(diags, {str(path): "<source>"})
		table = TypeTable()
		table.set_source_manager(source_manager)
		_set_active_source_manager(prev_source_manager)
		return empty, table, {}, diags
	func_hirs, sigs, fn_ids, table, excs, impl_metas, diags = _lower_parsed_program_to_hir(
		prog,
		diagnostics=[],
		package_id=package_id,
	)
	module_id = getattr(prog, "module", None) or "main"
	requires_by_fn, requires_by_struct = _collect_requires_for_module(table, module_id)
	module = ModuleLowered(
		module_id=module_id,
		package_id=package_id,
		source_path=path,
		func_hirs=func_hirs,
		signatures_by_id=sigs,
		fn_ids_by_name=fn_ids,
		requires_by_fn=requires_by_fn,
		requires_by_struct=requires_by_struct,
		type_defs=_collect_type_defs(prog),
		impl_defs=list(impl_metas),
		origin_by_fn_id={fn_id: path for fn_id in func_hirs.keys()},
	)
	label = f"<{module_id}>"
	_relabel_diagnostics(diags, {str(path): label})
	table.set_source_manager(source_manager)
	_set_active_source_manager(prev_source_manager)
	return module, table, excs, diags


__all__ = ["parse_drift_to_hir", "parse_drift_files_to_hir", "parse_drift_workspace_to_hir", "stdlib_root"]
_ACTIVE_SOURCE_MANAGER: SourceManager | None = None


def _set_active_source_manager(source_manager: SourceManager | None) -> None:
	global _ACTIVE_SOURCE_MANAGER
	_ACTIVE_SOURCE_MANAGER = source_manager
