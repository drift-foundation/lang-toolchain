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
from lang.driftc import _events as _events
from lang.driftc.stage1 import AstToHIR
from lang.driftc.stage1.call_info import IntrinsicKind
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

# Slice 7c-3 (ABI 14, 2026-05-06): `DiagnosticValue` removed from the
# reserved-nominal set — `TypeKind.DIAGNOSTICVALUE` is gone, so the
# name is no longer a builtin and may be used as an ordinary user
# identifier.  Spec §9.4 reflects this.
_RESERVED_NOMINAL_TYPE_NAMES: set[str] = {
	"Int",
	"Uint",
	"Byte",
	"Bool",
	"Float",
	"String",
	"Void",
	"Error",
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
	# Iterative post-order builder. The recursive form (one frame per
	# type-nesting level) overflowed Python's recursion stack on deeply
	# nested types like `Array<Array<...<Int>>>` at d≥5000. Surfaced by
	# the row #11 cleanup pass on the robustness matrix; same fix shape
	# as rows #2 and #5.
	#
	# Strategy: walk the type tree post-order with a two-phase work stack
	# (first visit pushes children, second visit consumes their cached
	# keys to build this node's key). Cache is keyed by `id(node)`; the
	# original recursive form did not dedup shared subtrees either, so
	# this preserves behavior — a node that appears twice in the tree
	# gets two cache slots, matching the recursive form's two recursive
	# evaluations.
	keys: dict[int, tuple] = {}
	stack: list[tuple[parser_ast.TypeExpr, bool]] = [(typ, False)]
	while stack:
		node, expanded = stack.pop()
		if expanded:
			qual = getattr(node, "module_id", None) or getattr(node, "module_alias", None)
			args = getattr(node, "args", []) or []
			child_keys = tuple(keys[id(a)] for a in args)
			if node.name == "fn":
				throws_key = node.fn_throws_raw()
				keys[id(node)] = (qual, node.name, throws_key, child_keys)
			else:
				keys[id(node)] = (qual, node.name, child_keys)
			continue
		# First visit: schedule the post-order build, then push children.
		stack.append((node, True))
		for child in getattr(node, "args", []) or []:
			stack.append((child, False))
	return keys[id(typ)]


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
		# Phase 1 v3 of terminal-`throws`: interface methods may use any of
		# the four signature shapes (see grammar.lark:func_def comment). The
		# parser-level InterfaceMethodSig sets declared_throws and/or
		# declared_terminal_throws appropriately, and `m.return_type` is
		# `None` exactly when the bare terminal form was used. The schema
		# faithfully carries `None` for `return_type` in that case (no Void
		# synthesis). Readers of `schema.return_type` MUST guard on
		# `declared_terminal_throws` (or check for None) before consuming it.
		# Package round-trip of declared_terminal_throws is Phase 3 territory.
		method_declared_terminal_throws = bool(getattr(m, "declared_terminal_throws", False))
		if method_declared_terminal_throws:
			method_return_type: Optional[GenericTypeExpr] = None
		else:
			method_return_type = _generic_type_expr_from_parser(m.return_type, type_params=combined_type_params)
		method_schema = InterfaceMethodSchema(
			name=m.name,
			params=method_param_schemas,
			return_type=method_return_type,
			type_params=list(method_type_params),
			declared_nothrow=bool(getattr(m, "declared_nothrow", False)),
			is_unsafe=bool(getattr(m, "is_unsafe", False)),
			declared_throws=bool(getattr(m, "declared_throws", False)),
			declared_terminal_throws=method_declared_terminal_throws,
		)
		methods.append(method_schema)
	return methods


def _convert_expr(expr: parser_ast.Expr) -> s0.Expr:
	"""Convert parser AST expressions into lang.driftc.stage0 AST expressions."""
	def _convert_trait_subject(subject: object) -> object:
		if isinstance(subject, parser_ast.SelfRef):
			return s0.SelfRef(loc=Span.from_loc(getattr(subject, "loc", None)))
		if isinstance(subject, parser_ast.TypeNameRef):
			# Forward `module_id` (parser_ast.TypeNameRef gained the
			# field at 0.31.29 to round-trip qualified subjects);
			# `getattr` is a defensive guard against any caller that
			# constructs a TypeNameRef before the field landed.
			return s0.TypeNameRef(
				name=subject.name,
				module_id=getattr(subject, "module_id", None),
				loc=Span.from_loc(getattr(subject, "loc", None)),
			)
		return subject

	if isinstance(expr, parser_ast.Literal):
		return s0.Literal(value=expr.value, loc=Span.from_loc(getattr(expr, "loc", None)))
	if hasattr(parser_ast, "UintLiteral") and isinstance(expr, parser_ast.UintLiteral):
		return s0.UintLiteral(value=expr.value, loc=Span.from_loc(getattr(expr, "loc", None)))
	if hasattr(parser_ast, "Uint64Literal") and isinstance(expr, parser_ast.Uint64Literal):
		return s0.Uint64Literal(value=expr.value, loc=Span.from_loc(getattr(expr, "loc", None)))
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
	if isinstance(expr, parser_ast.Share):
		return s0.Share(value=_convert_expr(expr.value), loc=Span.from_loc(getattr(expr, "loc", None)))
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
	if isinstance(expr, parser_ast.UnsafeExpr):
		return s0.UnsafeExpr(
			body=_convert_block(expr.block),
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
				scalar_literal_kind=getattr(arm, "scalar_literal_kind", None),
				scalar_literal_magnitude=getattr(arm, "scalar_literal_magnitude", None),
				scalar_const_qual_base=getattr(arm, "scalar_const_qual_base", None),
				scalar_const_qual_name=getattr(arm, "scalar_const_qual_name", None),
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
	# Iteratively flatten else-if chains to avoid Python recursion overflow.
	# The recursive shape was `_convert_if → _convert_block →
	# _convert_stmt → _convert_if → ...`, ~4 frames per source
	# `else if` level.  Long chains (`if x==0 {} else if x==1 {}
	# else if x==2 {} ...`) blew the earlier recursion-limit bump
	# (8192) at ~2000 source levels.
	#
	# Strategy: walk the chain iteratively from outer to inner, collecting
	# `(cond, then_block, loc)` tuples. The chain ends when we hit a
	# non-`else if` else-block (multi-statement block, non-IfStmt single
	# statement, or no else block at all). Then we build the resulting
	# `s0.IfStmt` nodes from innermost out, so the deepest level holds
	# the final-else conversion and each outer level wraps the previous.
	#
	# Inner blocks reachable through `then_block` are still converted
	# recursively via `_convert_block` — those are typically shallow (the
	# pathological shape is the else chain, not the then bodies). If a
	# user actually nests deep blocks inside `then` arms, row #1's parser
	# nesting limit catches it before this code is reached.
	chain: list[tuple[parser_ast.Expr, parser_ast.Block, object]] = []
	tail_else_block: parser_ast.Block | None = None
	current = stmt
	while True:
		chain.append((current.condition, current.then_block, current.loc))
		eb = current.else_block
		if not eb:
			tail_else_block = None
			break
		body = eb.statements
		if len(body) == 1 and isinstance(body[0], parser_ast.IfStmt):
			current = body[0]
			continue
		tail_else_block = eb
		break

	# Build the chain inside-out. `else_stmts` carries the already-built
	# else block for the level we're about to wrap.
	if tail_else_block is None:
		else_stmts: list[s0.Stmt] = []
	else:
		else_stmts = _convert_block(tail_else_block)
	result: s0.IfStmt | None = None
	for cond, then_block, loc in reversed(chain):
		result = s0.IfStmt(
			cond=_convert_expr(cond),
			then_block=_convert_block(then_block),
			else_block=else_stmts,
			loc=Span.from_loc(loc),
		)
		else_stmts = [result]
	assert result is not None  # chain is non-empty by construction
	return result


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
		init_value=_convert_expr(stmt.init_value) if stmt.init_value is not None else None,
		cond=_convert_expr(stmt.condition) if stmt.condition is not None else None,
		step=_convert_stmt(stmt.step) if stmt.step is not None else None,
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


def _convert_block_stmt(stmt: parser_ast.BlockStmt) -> s0.Stmt:
	return s0.BlockStmt(body=_convert_block(stmt.block), loc=Span.from_loc(stmt.loc))


def _convert_unsafe_block(stmt: parser_ast.UnsafeBlockStmt) -> s0.Stmt:
	return s0.UnsafeBlockStmt(body=_convert_block(stmt.block), loc=Span.from_loc(stmt.loc))


def _convert_assert(stmt: parser_ast.AssertStmt) -> s0.Stmt:
	return s0.AssertStmt(
		cond=_convert_expr(stmt.cond),
		msg=_convert_expr(stmt.msg) if stmt.msg is not None else None,
		loc=Span.from_loc(stmt.loc),
	)


def _convert_local_const(stmt: parser_ast.LocalConstStmt) -> s0.Stmt:
	return s0.LocalConstStmt(
		name=stmt.name,
		type_expr=stmt.type_expr,
		value=_convert_expr(stmt.value),
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
	parser_ast.BlockStmt: _convert_block_stmt,
	parser_ast.UnsafeBlockStmt: _convert_unsafe_block,
	parser_ast.LocalConstStmt: _convert_local_const,
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
		return_type: Optional[parser_ast.TypeExpr],
		loc: Optional[parser_ast.Located],
		declared_nothrow: bool = False,
		declared_throws: bool = False,
		declared_terminal_throws: bool = False,
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
		# Auto-try value-returning `throws -> T` form (existing behavior).
		self.declared_throws = declared_throws
		# NEW Phase 1: bare terminal `throws` form. Phase 2 body-flow check
		# enforces termination only on this flag, NOT on declared_throws.
		self.declared_terminal_throws = declared_terminal_throws
		# Slice 5: optional `throws TYPE_LIST` resolved to TypeExprs.
		# Empty list means generic throws (existing semantics).  Resolver
		# converts these to canonical event FQNs.
		self.declared_throws_types: list[parser_ast.TypeExpr] = []
		self.is_unsafe = is_unsafe
		self.throws = ()
		self.loc = loc
		self.is_pub = is_pub
		self.is_extern = False
		self.is_extern_c = False
		self.is_intrinsic = False
		self.intrinsic_kind: IntrinsicKind | None = None
		self.is_method = is_method
		self.self_mode = self_mode
		self.impl_target = impl_target
		self.impl_type_params = list(impl_type_params or [])
		self.impl_type_param_locs = list(impl_type_param_locs or [])
		self.impl_owner = impl_owner
		self.module = module
		# Slice 6: trait identity for impl-block methods, plumbed
		# through to FnSignature.impl_trait_{module,name} so consumers
		# (e.g. the manual-Diagnostic Site C lowering's
		# `_lookup_manual_diagnostic_to_json_text_fn_id`) can filter
		# on the canonical trait without scanning all impls or relying
		# on method-name uniqueness.
		self.impl_trait_module: Optional[str] = None
		self.impl_trait_name: Optional[str] = None


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
		# Phase 1 v3: insert declared_terminal_throws between declared_throws
		# and is_unsafe to match `_FrontendDecl.__init__` positional order.
		# Using a keyword argument here would also work; the positional form
		# is preserved for symmetry with the existing call shape.
		bool(getattr(fn, "declared_terminal_throws", False)),
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
	decl.is_extern_c = bool(getattr(fn, "is_extern_c", False))
	# Slice 5: forward `throws TYPE_LIST` to the frontend decl so the type
	# resolver can canonicalize each TypeExpr to its event FQN.  See
	# `_resolve_declared_throws_types` in `lang/driftc/type_resolver.py`.
	decl.declared_throws_types = list(getattr(fn, "declared_throws_types", []) or [])
	return decl


def _diagnostic(message: str, loc: object | None, *, code: str | None = None) -> Diagnostic:
	"""Helper to create a Diagnostic from a parser location.

	`code` is an optional stable diagnostic code; when None, the
	caller relies on the auto-generated `E-AUTO-...` hash code from
	the message.  Stable codes are required for tests/tooling that
	need to assert on a specific failure mode.
	"""
	return _p_diag(message=message, severity="error", span=Span.from_loc(loc), code=code)


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
		"INT",
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


def _is_if_in_expression_position_error(err: UnexpectedInput) -> bool:
	"""Detect `if` appearing where an expression is expected.

	Drift v1's grammar has `if` as a statement only — there is no
	`if`-as-expression. Users coming from Rust/Swift/etc. routinely
	write `val n = if cond { a } else { b };` or
	`f(if cond { a } else { b })` and get a cryptic Lark "Unexpected
	token Token('IF', 'if'). Expected: NOT, CAST, LSQB, ..." error.
	This predicate identifies that case so we can swap in a
	Drift-specific message pointing at the `match Bool { ... }`
	idiom that stdlib uses for the same purpose.

	Signature: unexpected token is `IF`, AND the expected set
	contains at least one canonical expression-start token (we
	check `NAME`, which appears in every expression-start position).
	The expected-set check avoids false-positives where `IF` is
	wrong for a non-expression reason (e.g., inside a context that
	expects a specific keyword).
	"""
	token = getattr(err, "token", None)
	if token is None or getattr(token, "type", None) != "IF":
		return False
	expected = set(getattr(err, "expected", None) or [])
	if not expected:
		return False
	# `NAME` is allowed at every expression-start position in the
	# grammar; its presence in the expected set is the cheapest
	# proxy for "expression expected here."
	return "NAME" in expected


def _parse_error_code(err: UnexpectedInput) -> str | None:
	expected = getattr(err, "expected", None)
	token = getattr(err, "token", None)
	if _is_trait_prop_value_pos_error(err):
		return "E-TRAIT-PROP-VALUE-POS"
	if _is_expr_block_missing_value_error(err):
		return "E_EXPR_BLOCK_MISSING_VALUE"
	if _is_if_in_expression_position_error(err):
		return "E_IF_NOT_AN_EXPRESSION"
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
		return (
			"expression block must end with a value expression; `return` (and other "
			"control flow) is not allowed in an expression-form block such as a "
			"`match`/`try` arm or a lambda value body. Make the block's last line the "
			"value, or use the statement form (e.g. statement-form `match` whose arms "
			"`return`, or `return match e { ... }`)."
		)
	if code == "E_IF_NOT_AN_EXPRESSION":
		return (
			"`if` is a statement in Drift v1, not an expression — it cannot appear as "
			"a `val`/`var` initializer, a call argument, a `return` value, a struct "
			"field initializer, or an array element. Use `match` over a Bool for "
			"conditional values: `match cond { true => { a }, false => { b } }`. "
			"`match` is an expression and works in every expression position."
		)
	raw = str(err)
	expected = set(getattr(err, "expected", None) or [])
	top_level_kws = {"MODULE", "STRUCT", "FN_KW", "VARIANT", "IMPORT", "TRAIT", "IMPLEMENT", "EXCEPTION"}
	if expected & top_level_kws:
		line = getattr(err, "line", None)
		col = getattr(err, "column", None)
		loc = f" at line {line}, column {col}" if line is not None else ""
		return f"input is not valid Drift source; verify this is a text .drift file (parse detail{loc}: {raw})"
	if code == "E_EXPECTED_SEMICOLON":
		line = getattr(err, "line", None)
		col = getattr(err, "column", None)
		loc = f" (line {line}, column {col})" if line is not None else ""
		return (
			f"expected ';' after statement{loc}. Every statement ends with ';'. "
			"Drift v1 has no implicit return: a bare tail expression — e.g. a "
			"`match` at the end of a function body — is NOT a function return. To "
			"return its value, write `return <expr>;` (e.g. `return match e { ... };`); "
			"to use it, bind it with `val x = <expr>;`. A `match`/`try` expression "
			"combined with operators needs parentheses or a binding "
			"(e.g. `(match e { ... }) - 7`). (Top-level `import`/`export`/`const` "
			"also need a trailing ';'.)"
		)
	return raw


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
	# Phase 1 v3 of terminal-`throws`: a `None` return_type indicates the bare
	# terminal `throws` form which has no annotated return type, so it
	# trivially cannot mention FnResult.
	if typ is None:
		return False
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

	Slice 5 hard-break direction (K, 2026-05-03): `pub exception` /
	`exception` user-facing syntax is REMOVED in favor of `pub error` /
	`error`.  Stdlib was migrated to `pub error` in slice 2 prep; the
	test corpus was mass-migrated in the test-corpus sub-slice.  This
	catalog now REJECTS any kind="exception" entry with
	`E_PUB_EXCEPTION_REMOVED` and a migration hint.  The grammar still
	parses paren-form internally so the rejection diagnostic can point
	at a clean migration target.
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
		# Slice 5 hard-break (K, 2026-05-03): reject `pub exception` /
		# `exception` legacy declarations at the user-source boundary.
		# Stdlib + test corpus both migrated to `pub error` before this
		# rejection landed; the diagnostic now fires unconditionally.
		if getattr(exc, "kind", "exception") == "exception":
			diagnostics.append(_p_diag(
				message=f"`pub exception {exc.name}(...)` is removed in 0.32.0 — use `pub error {exc.name} {{ ... }}` instead",
				severity="error",
				span=Span.from_loc(getattr(exc, "loc", None)),
				code="E_PUB_EXCEPTION_REMOVED",
			))
			continue
		fqn = f"{module_name}:{exc.name}" if module_name else exc.name
		# Slice 5: `pub error E(0x1234) { ... }` pins an explicit event_code.
		# When set, use it directly instead of the FQN-derived hash.  The
		# existing payload-collision check below catches duplicates.
		explicit_code = getattr(exc, "explicit_event_code", None)
		code = explicit_code if explicit_code is not None else event_code(fqn)
		payload = code & PAYLOAD_MASK
		if payload in payload_seen and payload_seen[payload] != fqn:
			other = payload_seen[payload]
			diagnostics.append(
				_diagnostic(
					f"exception code collision between '{other}' and '{fqn}' (payload {payload})",
					getattr(exc, "loc", None),
					code="E_EVENT_CODE_DUPLICATE",
				)
			)
			continue
		payload_seen[payload] = fqn
		catalog[fqn] = code
	return catalog


def _synthesize_auto_throw_impls(
	prog: parser_ast.Program,
	*,
	module_id: str,
	module_aliases: dict[str, str] | None = None,
	blocked_error_names: set[str] | None = None,
) -> None:
	"""Slice 5: auto-generate `implement core.Throw for E` for every
	`pub error E` declaration in the module unless an explicit
	`implement std.core.Throw for E` already exists.

	Rationale (K, 2026-05-04): `pub error` is the canonical throwable
	error type — requiring users to also write `implement Throw for E`
	is backwards.  The compiler synthesizes that contract.

	The synthesized body routes through the existing `throw E(...)`
	lowering so envelope construction (event_code, params JSON) stays
	centralized — no special HIR rewrite of `or_throw`.

	Skip-when-manual is gated on the trait resolving to `std.core.Throw`
	specifically — a user-defined trait that happens to be named `Throw`
	in a different module must NOT suppress synthesis of the std.core
	contract.
	"""
	exceptions = [
		e for e in (getattr(prog, "exceptions", []) or [])
		if getattr(e, "kind", "exception") == "error"
	]
	if not exceptions:
		return
	impls = list(getattr(prog, "implements", []) or [])
	aliases = module_aliases or {}
	manual_throw_targets: set[str] = set()
	for impl in impls:
		trait = getattr(impl, "trait", None)
		if trait is None or getattr(trait, "name", None) != "Throw":
			continue
		# Resolve the trait to its canonical module_id.  We accept
		# (a) explicit module_id == "std.core",
		# (b) module_alias resolving via the file's import map to "std.core",
		# (c) unqualified `Throw` only when the impl lives inside std.core
		#     itself (where the trait is the in-module declaration).
		trait_module = getattr(trait, "module_id", None)
		if trait_module is None:
			alias_name = getattr(trait, "module_alias", None)
			if alias_name is not None:
				trait_module = aliases.get(alias_name)
			elif module_id == "std.core":
				trait_module = "std.core"
		if trait_module != "std.core":
			continue
		target = getattr(impl, "target", None)
		if target is None or getattr(target, "name", None) is None:
			continue
		manual_throw_targets.add(target.name)
	# LANGUAGE_BUG follow-up (2026-05-06): dedupe by error name so a
	# duplicate `error Boom { ... }` decl does not produce two
	# synthesized `Throw for Boom` impls (which would surface as a
	# noisy `duplicate impl for trait 'std.core.Throw' on 'main.Boom'`
	# cascade on top of the catalog's `duplicate exception 'Boom'`
	# diagnostic).  Also skip names blocked by an upstream Path-A
	# struct-face collision with a user-source struct (Finding 1) —
	# the synthesized Throw body would reference a target whose
	# struct face is gone.
	_blocked = blocked_error_names or set()
	_seen_throw_targets: set[str] = set()
	for exc in exceptions:
		if exc.name in manual_throw_targets:
			continue
		if exc.name in _blocked:
			continue
		if exc.name in _seen_throw_targets:
			continue
		_seen_throw_targets.add(exc.name)
		loc = exc.loc
		target = parser_ast.TypeExpr(name=exc.name, module_id=module_id, loc=loc)
		trait = parser_ast.TypeExpr(name="Throw", module_id="std.core", loc=loc)
		self_ty = parser_ast.TypeExpr(name=exc.name, module_id=module_id, loc=loc)
		self_param = parser_ast.Param(name="self", type_expr=self_ty, mutable=False)
		self_var = parser_ast.Name(loc=loc, ident="self")
		kwargs: list[parser_ast.KwArg] = []
		for arg in exc.args:
			field_access = parser_ast.Attr(loc=loc, value=self_var, attr=arg.name, op=".")
			kwargs.append(parser_ast.KwArg(name=arg.name, value=field_access, loc=loc))
		ctor = parser_ast.ExceptionCtor(loc=loc, name=exc.name, args=[], kwargs=kwargs)
		throw_stmt = parser_ast.RaiseStmt(loc=loc, value=ctor, domain=None)
		body = parser_ast.Block(statements=[throw_stmt])
		# Narrow `throws E` so typed-catch coverage analysis can claim
		# coverage of `or_throw()` chains via this impl (Slice 2B narrow
		# coverage relies on `declared_throws_event_fqns`).
		throws_ty = parser_ast.TypeExpr(name=exc.name, module_id=module_id, loc=loc)
		throw_self_fn = parser_ast.FunctionDef(
			name="throw_self",
			orig_name="throw_self",
			type_params=[],
			params=[self_param],
			return_type=None,
			body=body,
			loc=loc,
			declared_terminal_throws=True,
			declared_throws_types=[throws_ty],
			is_pub=True,
			is_method=True,
			self_mode="value",
			impl_target=target,
		)
		impls.append(parser_ast.ImplementDef(
			target=target,
			loc=loc,
			methods=[throw_self_fn],
			trait=trait,
		))
	prog.implements = impls


_PROJECTABLE_SCALARS: frozenset[str] = frozenset(
	# Slice 7b (2026-05-06): `DiagnosticValue` removed from the
	# projectable set.  Slice 7a deleted DV from the public surface
	# (user-source `DiagnosticValue` references are rejected with
	# `E_DV_PUBLIC_REMOVED`), and Slice 7b retired the DV-attachment
	# throw lowering — no production path reaches DV through field
	# projection anymore.
	{"Int", "Uint", "Bool", "Float", "String"}
)
# Container / pointer / opaque types explicitly NOT auto-projectable
# (K, 2026-05-04).  Each must be wrapped behind a user-defined
# Diagnostic carrier — we reject at the field site with
# E_PUB_ERROR_FIELD_NOT_PROJECTABLE rather than silently dumping.
_NON_PROJECTABLE_BUILTINS: frozenset[str] = frozenset(
	{"Optional", "Array", "Map", "RawPtr", "Ptr", "TypeBox", "fn"}
)


def _resolve_field_type_module(
	field_ty_expr: object,
	*,
	module_id: str,
	module_aliases: dict[str, str],
) -> str | None:
	"""Best-effort resolve a field TypeExpr's canonical module_id at
	parser-synthesis time (before resolve_opaque_type runs).

	Order:
	  1. explicit `module_id` on the TypeExpr (if filled by an earlier pass);
	  2. `module_alias` resolved via the file's import map;
	  3. fallback to the current module (unqualified name).
	"""
	mod = getattr(field_ty_expr, "module_id", None)
	if mod:
		return mod
	alias = getattr(field_ty_expr, "module_alias", None)
	if alias and alias in module_aliases:
		return module_aliases[alias]
	return module_id


def _field_is_projectable(
	field_ty_expr: object,
	*,
	module_id: str,
	module_aliases: dict[str, str],
	exception_kinds: dict[str, str],
	exception_pub: dict[str, bool],
	diagnostic_targets: set[tuple[str | None, str]],
) -> bool:
	"""Slice 5 projectability rule (K, 2026-05-04; trimmed in Slice 7b):

	  * scalars (Int / Uint / Bool / Float / String) ✓
	  * `pub error E` (recursively projectable through synthesis) ✓
	  * any type with an explicit `implement core.Diagnostic for T` ✓
	  * collections (Optional / Array / Map) ✗ — wrap behind a carrier
	  * pointer / opaque / function types ✗
	  * private (non-pub) `error E` ✗ — synthesis does not fire for
	    non-pub error decls, so they have no Diagnostic impl to reach.

	Slice 7b (2026-05-06): `DiagnosticValue` removed from the scalar
	set.  See `_PROJECTABLE_SCALARS` above.

	`diagnostic_targets` carries (module_id, name) keys for explicit
	manual `implement core.Diagnostic for T` impls we know about
	(local + external trait worlds).
	"""
	ty_name = getattr(field_ty_expr, "name", None)
	if not ty_name:
		return False
	if ty_name in _PROJECTABLE_SCALARS:
		# Scalars take no type args — guard against pathological forms.
		if getattr(field_ty_expr, "args", None):
			return False
		return True
	if ty_name in _NON_PROJECTABLE_BUILTINS:
		return False
	target_mod = _resolve_field_type_module(
		field_ty_expr, module_id=module_id, module_aliases=module_aliases,
	)
	# `pub error E` is reachable as a projectable field type through
	# its own synthesized or manual Diagnostic impl.  Slice 7b:
	# synthesis fires for non-pub `error E` too, but its TYPE is
	# private — using it as a field type of another error would leak
	# a private type through a public surface, so the gate stays
	# pub-only here.  Private errors can still be thrown directly
	# and project their own fields via `e.params.get(...)` (intra-
	# module use only); to use them as a field type, the user must
	# add an explicit `implement core.Diagnostic for PrivateE` (which
	# the `diagnostic_targets` check below picks up).
	if target_mod:
		fqn = f"{target_mod}:{ty_name}"
		if exception_kinds.get(fqn) == "error" and bool(exception_pub.get(fqn, False)):
			return True
	# Explicit Diagnostic impl in scope (current module or known
	# external — external impls are sourced from already-loaded
	# package trait worlds at the synthesizer call site).
	if (target_mod, ty_name) in diagnostic_targets or (None, ty_name) in diagnostic_targets:
		return True
	return False


def _is_std_core_diagnostic_trait(
	trait_expr: object,
	*,
	module_id: str,
	module_aliases: dict[str, str],
) -> bool:
	"""Mirror of the std.core.Throw gating used by the auto-Throw
	synthesizer.  Accepts (a) explicit `module_id == "std.core"`,
	(b) `module_alias` resolving to `std.core`, (c) unqualified
	`Diagnostic` only when this is std.core itself."""
	if getattr(trait_expr, "name", None) != "Diagnostic":
		return False
	mod = getattr(trait_expr, "module_id", None)
	if mod is None:
		alias = getattr(trait_expr, "module_alias", None)
		if alias is not None:
			mod = module_aliases.get(alias)
		elif module_id == "std.core":
			mod = "std.core"
	return mod == "std.core"


def _is_std_log_debuggable_trait(
	trait_expr: object,
	*,
	module_id: str,
	module_aliases: dict[str, str],
) -> bool:
	"""Identify `log.Debuggable` (or in-module `Debuggable` from std.log).
	Same shape as `_is_std_core_diagnostic_trait`."""
	if getattr(trait_expr, "name", None) != "Debuggable":
		return False
	mod = getattr(trait_expr, "module_id", None)
	if mod is None:
		alias = getattr(trait_expr, "module_alias", None)
		if alias is not None:
			mod = module_aliases.get(alias)
		elif module_id == "std.log":
			mod = "std.log"
	return mod == "std.log"


def _reject_deprecated_trait_method_shapes(
	prog: parser_ast.Program,
	*,
	module_id: str,
	module_aliases: dict[str, str] | None,
	diagnostics: list,
) -> None:
	"""Slice 7a (0.31.62, 2026-05-05): reject the legacy `to_diag` /
	`to_debug` method shapes on user impls.

	`Diagnostic.to_diag(self) -> DiagnosticValue` (retired in Slice 5)
	and `Debuggable.to_debug(self) -> DiagnosticValue` (retired in
	Slice 7a) are gone.  Their new contracts are
	`Diagnostic.to_json_text(self) -> String` and
	`Debuggable.to_debug_json_text(self) -> String`, with values projected
	through `core.diagnostic_json_*`.  Without explicit rejection here,
	an old-shape impl would surface as a confusing trait-satisfaction
	error elsewhere; emitting `E_TO_DIAG_DEPRECATED` /
	`E_TO_DEBUG_DEPRECATED` at the impl-block boundary points consumers
	at the migration directly.

	The check is keyed on (trait identity, method name) — return type
	is intentionally not inspected.  Accidental stdlib backslide will
	also trip the check (stdlib was migrated before this rejection
	landed)."""
	aliases = module_aliases or {}
	for impl in getattr(prog, "implements", []) or []:
		trait = getattr(impl, "trait", None)
		if trait is None:
			continue
		is_diagnostic = _is_std_core_diagnostic_trait(
			trait, module_id=module_id, module_aliases=aliases,
		)
		is_debuggable = _is_std_log_debuggable_trait(
			trait, module_id=module_id, module_aliases=aliases,
		)
		if not (is_diagnostic or is_debuggable):
			continue
		for method in getattr(impl, "methods", []) or []:
			mname = getattr(method, "name", None)
			loc = getattr(method, "loc", None)
			if is_diagnostic and mname == "to_diag":
				diagnostics.append(_p_diag(
					message=(
						"`Diagnostic.to_diag(...) -> DiagnosticValue` is removed in 0.31.62; "
						"implement `to_json_text(self: &Self) nothrow -> String` instead and "
						"project values via `core.diagnostic_json_*`"
					),
					severity="error",
					span=Span.from_loc(loc),
					code="E_TO_DIAG_DEPRECATED",
				))
			elif is_debuggable and mname == "to_debug":
				diagnostics.append(_p_diag(
					message=(
						"`Debuggable.to_debug(...) -> DiagnosticValue` is removed in 0.31.62; "
						"implement `to_debug_json_text(self: &Self) nothrow -> String` instead "
						"and project values via `core.diagnostic_json_*`"
					),
					severity="error",
					span=Span.from_loc(loc),
					code="E_TO_DEBUG_DEPRECATED",
				))


def _diagnostic_body_emission_enabled(
	*, type_table: object | None, module_id: str,
) -> bool:
	"""Return True iff this compilation context can resolve the
	synthesized `core.Diagnostic for E.to_json_text` body's
	`(&self.<f>).to_json_text()` calls against stdlib's scalar
	impls.

	NARROW signal — only suppresses BODY emission, not
	projectability validation, manual-Diagnostic ownership tracking,
	or the `_PUB_ERROR_FIELD_NOT_PROJECTABLE` diagnostic.  Those
	always run regardless of stdlib reachability so that synthesis
	semantics (single-owner contract, projectability rule, typed-
	catch boundary) stay coherent across all parse paths.

	Reachability is determined by whether a workspace orchestrator
	has set up `workspace_diagnostic_targets` on the shared
	type_table.  Even when the workspace pre-scan finds no
	cross-module Diagnostic impls (a common case for small
	projects), `_orchestrate_workspace` sets the attribute to an
	empty set — its mere presence indicates "the driftc workspace
	loader ran with stdlib in scope and per-module synthesis
	bodies will be link-resolved against std.core's scalar impls."

	The narrow case where this returns False is the single-file
	`parse_drift_to_hir(path)` test path: no orchestrator runs, the
	attribute is never set on the fresh TypeTable, and the
	synthesized body's `to_json_text` calls would not resolve
	(stdlib was never parsed).  Bootstrap exemptions:

	- `module_id == "std.core"` — std.core's own scalar impls live
	  in this same module; the body's recursive calls resolve
	  intra-module.
	- `module_id` is `std.*` or `lang.*` — driftc always parses
	  std.core when any stdlib module is being compiled; cross-
	  module dispatch will resolve once trait worlds are built.
	"""
	if module_id == "std.core":
		return True
	if isinstance(module_id, str) and (
		module_id.startswith("std.") or module_id.startswith("lang.")
	):
		return True
	if type_table is not None and hasattr(type_table, "workspace_diagnostic_targets"):
		return True
	return False


def _scan_external_diagnostic_targets(
	type_table: object,
) -> set[tuple[str | None, str]]:
	"""Slice 5 cross-package projectability: scan already-loaded
	external trait worlds for explicit `implement core.Diagnostic
	for T` impls so a `pub error` field in this module that names
	an imported type with its own Diagnostic impl is recognized as
	projectable.

	Without this, the rule "struct fields participate when that
	struct has an explicit Diagnostic impl" silently rejects every
	cross-package case at the field site.

	`TraitWorld.impls` entries are `ImplDef` records whose `trait`
	is a `TraitKey(package_id, module, name)` and `target` is a
	`TypeKey(package_id, module, name, ...)` — not `trait_key` /
	`target_key`, and the field is `module` (not `module_id`).
	"""
	out: set[tuple[str | None, str]] = set()
	trait_worlds = getattr(type_table, "trait_worlds", None)
	if not isinstance(trait_worlds, dict):
		return out
	for ext_world in trait_worlds.values():
		if ext_world is None:
			continue
		impls = getattr(ext_world, "impls", []) or []
		for impl in impls:
			trait_key = getattr(impl, "trait", None)
			if trait_key is None:
				continue
			tk_module = getattr(trait_key, "module", None)
			tk_name = getattr(trait_key, "name", None)
			if tk_module != "std.core" or tk_name != "Diagnostic":
				continue
			target_key = getattr(impl, "target", None)
			if target_key is None:
				continue
			t_mod = getattr(target_key, "module", None)
			t_name = getattr(target_key, "name", None)
			if t_name:
				out.add((t_mod, t_name))
	return out


def _synthesize_auto_diagnostic_impls(
	prog: parser_ast.Program,
	*,
	module_id: str,
	type_table: object | None = None,
	module_aliases: dict[str, str] | None = None,
	exception_kinds: dict[str, str] | None = None,
	exception_pub: dict[str, bool] | None = None,
	diagnostics: list[Diagnostic] | None = None,
	blocked_error_names: set[str] | None = None,
) -> None:
	"""Slice 5 (Phase 5a): auto-generate `implement core.Diagnostic for E`
	for every `error E` in the module whose fields are all
	projectable, unless an explicit `implement std.core.Diagnostic for E`
	already exists.

	Single-owner contract (K, 2026-05-04): a type has EXACTLY ONE
	Diagnostic JSON owner.  A manual impl owns the whole shape; the
	compiler does not blend manual field behavior with a synthesized
	outer shape.  When ANY field is non-projectable, the `pub error`
	declaration is rejected at the field site with
	`E_PUB_ERROR_FIELD_NOT_PROJECTABLE` — synthesis fails closed.

	Body shape (spec §7.3): String concat over lex-utf8-sorted field
	names with pre-quoted keys + per-field `<self>.<f>.to_json_text()`
	dispatch.  Empty error → returns the literal `"{}"`.

	Slice 7b (K, 2026-05-06; LANGUAGE_BUG fix): synthesis fires for
	BOTH `pub error E` AND non-pub `error E`.  Pre-fix, non-pub
	`error E { msg: String }` had no synthesized impl, so the unified
	throw lowering's `to_json_text` lookup missed and the throw
	emitted an empty params envelope — silent data loss when the
	catch site read `e.params.get("msg")`.  The synthesized impl on
	a non-pub error stays module-internal (the type itself is not
	exported); projectability AS A FIELD TYPE in another `pub error`
	remains pub-only via the gate in `_field_is_projectable`.
	"""
	exc_kinds = exception_kinds or {}
	exc_pub = exception_pub or {}
	aliases = module_aliases or {}
	diags = diagnostics if diagnostics is not None else []
	exceptions = [
		e for e in (getattr(prog, "exceptions", []) or [])
		if getattr(e, "kind", "exception") == "error"
	]
	if not exceptions:
		return
	# Slice 7c-3 follow-up #2 (K, 2026-05-06): NARROW gate on body
	# emission only.  Projectability validation, manual-Diagnostic
	# ownership tracking, and the `E_PUB_ERROR_FIELD_NOT_PROJECTABLE`
	# diagnostic ALWAYS run — those define synthesis semantics and
	# downstream gates (typed-catch boundary, single-owner contract)
	# regardless of whether stdlib happens to be reachable in this
	# compile.  Only the actual `impls.append(...)` of the synthesized
	# `to_json_text` body is suppressed when `_diagnostic_body_emission_enabled`
	# returns False — the narrow `parse_drift_to_hir(path)` test path
	# where the synthesized body's recursive `to_json_text` dispatch
	# cannot resolve against any stdlib scalar impl.
	body_emission_enabled = _diagnostic_body_emission_enabled(
		type_table=type_table, module_id=module_id,
	)
	impls = list(getattr(prog, "implements", []) or [])
	manual_diag_targets_local: set[str] = set()
	diagnostic_targets: set[tuple[str | None, str]] = set()
	# Scan ALREADY-LOADED external (package) trait worlds for explicit
	# `implement core.Diagnostic for T` impls so cross-package field
	# types with their own Diagnostic impls are recognized as
	# projectable rather than silently rejected at the field site.
	if type_table is not None:
		diagnostic_targets |= _scan_external_diagnostic_targets(type_table)
		# Workspace pre-scan (intra-project cross-module case).  The
		# workspace loader pre-scans every module's prog.implements for
		# `implement core.Diagnostic for T` and stashes the (mod, name)
		# set on shared_type_table so the per-module synthesizer sees
		# cross-module impls regardless of module visit order.
		ws_targets = getattr(type_table, "workspace_diagnostic_targets", None)
		if ws_targets:
			diagnostic_targets |= ws_targets
	for impl in impls:
		trait = getattr(impl, "trait", None)
		if trait is None:
			continue
		target = getattr(impl, "target", None)
		if target is None or getattr(target, "name", None) is None:
			continue
		if not _is_std_core_diagnostic_trait(
			trait, module_id=module_id, module_aliases=aliases,
		):
			continue
		manual_diag_targets_local.add(target.name)
		# Record (module_id, name) for projectability lookup.
		t_mod = _resolve_field_type_module(
			target, module_id=module_id, module_aliases=aliases,
		)
		diagnostic_targets.add((t_mod, target.name))
	# Built-in scalars are always projectable via the trait but also
	# count as Diagnostic-impl targets for the projectability lookup
	# (an external module that names `core.String` as a field type
	# resolves through this set).
	for scalar in _PROJECTABLE_SCALARS:
		diagnostic_targets.add((None, scalar))
		diagnostic_targets.add(("std.core", scalar))
	# Slice 6: stash the FQN set of pub errors with user-owned
	# Diagnostic projection on the type table for downstream gates
	# (Sites A/B/C + typed-catch boundary).  K-rule: once a user
	# writes `implement core.Diagnostic for E`, the compiler stops
	# interpreting E's fields for diagnostic projection.  See
	# `TypeTable.manual_diagnostic_pub_errors` (types_core.py).
	if type_table is not None:
		manual_owners_set = getattr(type_table, "manual_diagnostic_pub_errors", None)
		if manual_owners_set is None:
			manual_owners_set = set()
			type_table.manual_diagnostic_pub_errors = manual_owners_set
		for exc in exceptions:
			if exc.name in manual_diag_targets_local:
				manual_owners_set.add(f"{module_id}:{exc.name}")
	synthesized_any = False
	# LANGUAGE_BUG follow-up (2026-05-06): dedupe by error name +
	# skip blocked names so duplicate `error Boom { ... }` decls
	# don't cascade into a second `Diagnostic for Boom` impl
	# (which would surface as a `duplicate impl for trait
	# 'std.core.Diagnostic'` error on top of the catalog's
	# `duplicate exception 'Boom'` diagnostic), and so an error
	# name whose Path-A struct face was suppressed by a user-source
	# struct collision (Finding 1) doesn't get a Diagnostic impl
	# pointing at the now-missing struct face.
	_blocked = blocked_error_names or set()
	_seen_diag_targets: set[str] = set()
	for exc in exceptions:
		if exc.name in manual_diag_targets_local:
			continue
		if exc.name in _blocked:
			continue
		if exc.name in _seen_diag_targets:
			continue
		_seen_diag_targets.add(exc.name)
		loc = exc.loc
		# Projectability gate.  On any non-projectable field, emit a
		# diagnostic at the field site and skip synthesis for this E.
		first_bad: parser_ast.ExceptionArg | None = None
		for arg in exc.args:
			if not _field_is_projectable(
				arg.type_expr,
				module_id=module_id,
				module_aliases=aliases,
				exception_kinds=exc_kinds,
				exception_pub=exc_pub,
				diagnostic_targets=diagnostic_targets,
			):
				first_bad = arg
				break
		if first_bad is not None:
			ty_name = getattr(first_bad.type_expr, "name", "?")
			# Slice 7b (K, 2026-05-06): synthesis now fires for both
			# `pub error` and non-pub `error`; the message has to
			# reflect the actual decl visibility instead of
			# hard-coding `pub error`.
			err_label = (
				f"pub error {exc.name}"
				if bool(getattr(exc, "is_pub", False))
				else f"error {exc.name}"
			)
			# Container / nested-struct help: the carrier error owns
			# the whole JSON shape via a manual `core.Diagnostic for E`
			# impl.  We deliberately do NOT suggest
			# `implement core.Diagnostic for Array<T>` — that would
			# steer users toward a global collection serializer, which
			# the projectability rule explicitly rejects (K, slice 5).
			# For non-collection field types, naming a manual
			# Diagnostic on the field type itself is still legitimate;
			# guard the suggestion accordingly.
			collection_or_pointer_help = ty_name in _NON_PROJECTABLE_BUILTINS
			notes_list = [
				"container types (Array, Optional, Map) and ordinary "
				"structs/variants are not auto-projected.",
			]
			if collection_or_pointer_help:
				notes_list.append(
					f"implement `core.Diagnostic for {exc.name}` manually so "
					f"the carrier owns the whole JSON shape (project a "
					f"compact preview / size summary, not a full collection "
					f"dump), or change the field type to a projectable "
					f"scalar / `pub error`."
				)
			else:
				notes_list.append(
					f"either implement `core.Diagnostic for {ty_name}` so "
					f"the field carries its own JSON shape, or change the "
					f"field type to a projectable scalar / `pub error`, or "
					f"implement `core.Diagnostic for {exc.name}` manually "
					f"to take ownership of the whole shape."
				)
			diags.append(_p_diag(
				message=(
					f"cannot synthesize `core.Diagnostic` for `{err_label}`: "
					f"field '{first_bad.name}' (type '{ty_name}') is not projectable"
				),
				severity="error",
				span=Span.from_loc(getattr(first_bad.type_expr, "loc", None) or loc),
				code="E_PUB_ERROR_FIELD_NOT_PROJECTABLE",
				notes=notes_list,
			))
			continue
		# Slice 7c-3 follow-up #2: stdlib-less single-file parse path
		# never reaches stdlib's scalar `Diagnostic` impls — the
		# synthesized body would be unresolvable.  Skip the impl
		# emission only; projectability validation already ran.
		if not body_emission_enabled:
			continue
		# All fields projectable — synthesize the impl.
		target = parser_ast.TypeExpr(name=exc.name, module_id=module_id, loc=loc)
		trait = parser_ast.TypeExpr(name="Diagnostic", module_id="std.core", loc=loc)
		self_inner = parser_ast.TypeExpr(name=exc.name, module_id=module_id, loc=loc)
		self_ty = parser_ast.TypeExpr(name="&", args=[self_inner], loc=loc)
		self_param = parser_ast.Param(name="self", type_expr=self_ty, mutable=False)
		ret_ty = parser_ast.TypeExpr(name="String", loc=loc)
		# Build the body: lex-utf8 sorted field projection.
		sorted_fields = sorted(exc.args, key=lambda a: a.name)
		body_expr: parser_ast.Expr
		if not sorted_fields:
			body_expr = parser_ast.Literal(loc=loc, value="{}")
		else:
			chunks: list[parser_ast.Expr] = [parser_ast.Literal(loc=loc, value="{")]
			for i, field in enumerate(sorted_fields):
				if i > 0:
					chunks.append(parser_ast.Literal(loc=loc, value=","))
				chunks.append(parser_ast.Literal(loc=loc, value=f'"{field.name}":'))
				# self.<field>.to_json_text() — auto-borrow handles &Self.
				self_ref = parser_ast.Name(loc=loc, ident="self")
				field_access = parser_ast.Attr(loc=loc, value=self_ref, attr=field.name, op=".")
				method_attr = parser_ast.Attr(loc=loc, value=field_access, attr="to_json_text", op=".")
				method_call = parser_ast.Call(
					loc=loc, func=method_attr, args=[], kwargs=[], type_args=None,
				)
				chunks.append(method_call)
			chunks.append(parser_ast.Literal(loc=loc, value="}"))
			body_expr = chunks[0]
			for chunk in chunks[1:]:
				body_expr = parser_ast.Binary(loc=loc, op="+", left=body_expr, right=chunk)
		return_stmt = parser_ast.ReturnStmt(loc=loc, value=body_expr)
		body = parser_ast.Block(statements=[return_stmt])
		to_json_fn = parser_ast.FunctionDef(
			name="to_json_text",
			orig_name="to_json_text",
			type_params=[],
			params=[self_param],
			return_type=ret_ty,
			body=body,
			loc=loc,
			declared_nothrow=True,
			is_pub=True,
			is_method=True,
			self_mode="ref",
			impl_target=target,
		)
		impls.append(parser_ast.ImplementDef(
			target=target,
			loc=loc,
			methods=[to_json_fn],
			trait=trait,
		))
		synthesized_any = True
	prog.implements = impls
	# Bring `core.Diagnostic` into trait scope so the synthesized
	# `<self>.<field>.to_json_text()` UFCS dispatch resolves through
	# the std.core scalar impls without forcing every user module
	# that declares a `pub error` to write `use trait core.Diagnostic`
	# explicitly.  Idempotent — only added when not already present
	# AND at least one synthesis actually emitted (the post-scan
	# `emitted_diag` form would also fire on pre-existing manual
	# impls, which is misleading; `synthesized_any` is the precise
	# signal).
	used_traits = list(getattr(prog, "used_traits", []) or [])
	already_used = any(
		getattr(t, "name", None) == "Diagnostic"
		and tuple(getattr(t, "module_path", []) or []) in (("std", "core"), ("core",))
		for t in used_traits
	)
	if synthesized_any and not already_used:
		# Synthesize `use trait std.core.Diagnostic;`.  TraitRef takes
		# a module_path list; we use the canonical std.core form.
		used_traits.append(parser_ast.TraitRef(
			loc=parser_ast.Located(line=0, column=0),
			module_path=["std", "core"],
			name="Diagnostic",
		))
		prog.used_traits = used_traits


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
		except _parser.ParserNestingLimitError as err:
			diagnostics.append(_p_diag(message=str(err), severity="error", span=_span_in_file(path, err.loc)))
			continue
		except _parser.ParserIdentifierLengthError as err:
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
	external_exception_schemas: dict[str, tuple[str, list[str]]] | None = None,
	external_type_aliases: list[tuple[str, str, list[str], object]] | None = None,
	external_diagnostic_targets: set[tuple[str | None, str]] | None = None,
	package_id: str | None = None,
	stdlib_root: Path | None = None,
	test_build_only: bool = False,
	word_bits: int | None = None,
	type_table: "TypeTable | None" = None,
	semantic_world: "Any | None" = None,
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

	# Adapter: unpack from SemanticWorld if provided.
	if semantic_world is not None:
		# For package-consumer builds, packages must be ingressed first.
		# For source-only builds, the world may go directly to SOURCE_INGRESS.
		if semantic_world.type_table is not None:
			semantic_world.assert_packages_ready()
			# Consistency: if both are provided, they must be the same object.
			if type_table is not None and type_table is not semantic_world.type_table:
				raise RuntimeError(
					"conflicting type_table: explicit argument differs from semantic_world.type_table"
				)
		if type_table is None and semantic_world.type_table is not None:
			type_table = semantic_world.type_table

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

	# Workload classification: explicit compile inputs vs files the
	# driver implicitly appended from `stdlib_root`.  `user_path_set`
	# was snapshotted ABOVE before stdlib expansion, so membership is
	# the authoritative split (no path heuristics).  Tokens are
	# tallied only for files that successfully parse -- failed files
	# contribute their bytes/files but not tokens, since
	# `_PARSER.parse(...)` raised before producing a tree.  See
	# `doc/timing.md`.
	#
	# Cheap-disabled-path contract: the per-file `path.resolve()` /
	# `source.encode("utf-8")` / per-class accumulation cost is paid
	# ONLY when a workload sink is installed.  Without `--timing`,
	# `_collect_workload` stays False and the parse loop skips every
	# counter side-effect (the parse and `source_manager.add` already
	# read+keep the source; the workload path adds nothing extra in
	# that case).
	_collect_workload = _events.current_sink() is not None
	_src_input_files = 0
	_src_input_bytes = 0
	_src_input_tokens = 0
	_src_stdlib_files = 0
	_src_stdlib_bytes = 0
	_src_stdlib_tokens = 0

	# Parse all files first.
	parsed: list[tuple[Path, parser_ast.Program]] = []
	for path in paths:
		source = path.read_text()
		_is_input_file = False
		if _collect_workload:
			_is_input_file = path.resolve() in user_path_set
			_src_bytes = len(source.encode("utf-8"))
			if _is_input_file:
				_src_input_files += 1
				_src_input_bytes += _src_bytes
			else:
				_src_stdlib_files += 1
				_src_stdlib_bytes += _src_bytes
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
		except _parser.ParserNestingLimitError as err:
			diagnostics.append(_p_diag(message=str(err), severity="error", span=_span_in_file(path, err.loc)))
			continue
		except _parser.ParserIdentifierLengthError as err:
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
		# Tokens are stamped onto prog by `parse_program` ONLY when a
		# sink is installed -- mirror that gate so we don't read a
		# never-set attribute or do per-file classification work on
		# the no-sink path.
		if _collect_workload:
			_tok = int(getattr(prog, "_parse_tree_token_count", 0))
			if _is_input_file:
				_src_input_tokens += _tok
			else:
				_src_stdlib_tokens += _tok
		parsed.append((path, prog))

	# Workload snapshot: emit AFTER the parse loop completes so every
	# downstream return path (including the parse-error short-circuit
	# below) carries the observed source-side counters.  Gated by the
	# same sink check used above so the no-sink path skips the six
	# `set_workload` calls entirely (each is itself a cheap no-op, but
	# avoiding the six `ContextVar.get()` lookups keeps the cost
	# stratum lower-bounded by "nothing").
	if _collect_workload:
		_events.set_workload("source.input.files", _src_input_files)
		_events.set_workload("source.input.utf8_bytes", _src_input_bytes)
		_events.set_workload("source.input.parse_tree_tokens", _src_input_tokens)
		_events.set_workload("source.implicit_stdlib.files", _src_stdlib_files)
		_events.set_workload("source.implicit_stdlib.utf8_bytes", _src_stdlib_bytes)
		_events.set_workload(
			"source.implicit_stdlib.parse_tree_tokens", _src_stdlib_tokens,
		)

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

	# Slice 7a follow-up (K finding 1, 2026-05-05): module-id-by-path lookup
	# powers the DV-public-removed gate's "is stdlib?" check.  The
	# path-under-stdlib_root path is unreliable when callers invoke driftc
	# with `--stdlib-root <empty>` and pass stdlib files as inputs (e.g. the
	# stdlib-self-deploy + hir-funcs round-trip tests); the gate must still
	# allow stdlib internal use, gated by the file's declared `module`
	# matching `std.*` / `lang.*`.
	module_id_by_path: dict[str, str] = {}
	for mid, files in by_module.items():
		for fpath, _prog in files:
			module_id_by_path[str(fpath.resolve())] = mid

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
	# Slice 5 (Diagnostic / projectability — K, 2026-05-04):
	# pre-scan every workspace module's `implement core.Diagnostic
	# for T` impls so a `pub error` field in module B that names
	# a struct from module A is recognized as projectable, regardless
	# of the order in which `_lower_parsed_program_to_hir` happens
	# to visit modules.  Gated on canonical `std.core.Diagnostic`
	# resolution (matching the auto-Throw / auto-Diagnostic gate)
	# so a user-defined trait that happens to be named `Diagnostic`
	# in another module does NOT incorrectly mark a target as
	# projectable.  Stashed on the shared TypeTable below.
	workspace_diag_targets: set[tuple[str | None, str]] = set()
	for _mid, _prog in merged_programs.items():
		# Build the file's import aliases on the fly — the canonical
		# `module_aliases_by_module` map is populated later in this
		# function, so we need a local view of (alias → module_id)
		# for the std.core gating below.
		_file_aliases: dict[str, str] = {}
		for _imp in (getattr(_prog, "imports", []) or []):
			_path = getattr(_imp, "path", []) or []
			_mod = ".".join(_path)
			if not _mod:
				continue
			_alias = getattr(_imp, "alias", None) or (_path[-1] if _path else _mod)
			if _alias not in _file_aliases:
				_file_aliases[_alias] = _mod
		for _impl in (getattr(_prog, "implements", []) or []):
			_trait = getattr(_impl, "trait", None)
			if _trait is None or getattr(_trait, "name", None) != "Diagnostic":
				continue
			# Canonical std.core gating: accept (a) explicit module_id ==
			# "std.core", (b) module_alias resolving via this module's
			# imports to "std.core", (c) unqualified `Diagnostic` only
			# when the impl lives inside std.core itself.
			_trait_module = getattr(_trait, "module_id", None)
			if _trait_module is None:
				_alias_name = getattr(_trait, "module_alias", None)
				if _alias_name is not None:
					_trait_module = _file_aliases.get(_alias_name)
				elif _mid == "std.core":
					_trait_module = "std.core"
			if _trait_module != "std.core":
				continue
			_target = getattr(_impl, "target", None)
			if _target is None or getattr(_target, "name", None) is None:
				continue
			_t_mod = getattr(_target, "module_id", None) or _mid
			workspace_diag_targets.add((_t_mod, _target.name))
	if isinstance(external_module_packages, dict) and package_id:
		# Self-exclusion defense-in-depth: if the current build's package_id
		# leaked into external_module_packages (should not happen — driftc.py
		# filters loaded_pkgs before building this dict), drop all modules
		# that belong to the current package.
		_self_mods = {
			mod for mod, pkg in external_module_packages.items()
			if pkg == package_id
		}
		if _self_mods:
			external_module_packages = {
				mod: pkg for mod, pkg in external_module_packages.items()
				if pkg != package_id
			}
			if isinstance(external_module_exports, dict):
				external_module_exports = {
					mod: exp for mod, exp in external_module_exports.items()
					if mod not in _self_mods
				}
			if isinstance(external_exception_schemas, dict):
				external_exception_schemas = {
					key: val for key, val in external_exception_schemas.items()
					if key.split(":")[0] not in _self_mods
				}

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
		# Slice 5 Path A: `pub error E { ... }` lowers to BOTH an ExceptionDef
		# (event-identity) AND a parallel StructDef (value-type machinery).  The
		# parallel StructDef is synthesized — it shares its name with the
		# ExceptionDef on purpose, and the export-ambiguity check below MUST
		# count it as a single "error" type kind, not as both "struct" and
		# "exception".  Filter synthesized struct co-registrations out of the
		# module_struct_names set so the export validator only sees the
		# ExceptionDef face for these names.  See `_struct_from_error_decl`
		# in `lang/driftc/parser/parser.py` for the parser-side of Path A.
		_synthesized_error_struct_names: set[str] = {
			e.name for e in getattr(merged_prog, "exceptions", []) or [] if getattr(e, "kind", "exception") == "error"
		}
		module_struct_names: set[str] = {
			s.name for s in getattr(merged_prog, "structs", []) or []
			if s.name not in _synthesized_error_struct_names
		}
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
	# Stdlib modules always belong to canonical package "std", regardless of
	# whether we are building a package (package_id set) or compiling a
	# consumer (package_id is None).  This ensures source-compiled stdlib
	# types have the same NominalKey identity as package-serialized stdlib
	# types, eliminating TypeId collisions when both paths coexist.
	std_pkg = "std"
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
			module_packages_for_scope.setdefault(mod, "lang.core")
		else:
			module_packages_for_scope.setdefault(mod, local_pkg)
	module_packages_for_scope.setdefault("lang.core", "lang.core")
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
		# Slice 7a (0.31.62, 2026-05-05) → Slice 7c-3 (0.31.65,
		# 2026-05-06): user code may not name `core.DiagnosticValue` /
		# `core.DiagnosticEntry`.  The DV/DE public surface was removed
		# in 7a; the runtime exports + compiler-internal substrate were
		# deleted in 7c-1 / 7c-2 / 7c-3.  The rejection here is now a
		# pure migration diagnostic — without it user source gets the
		# generic "module 'std.core' does not export type X" error,
		# which doesn't point at the migration.  Stdlib source no longer
		# uses these names either (only tombstone comments remain), but
		# we keep the stdlib-source allowance as a guard against future
		# accidental re-introduction inside stdlib.
		#
		# The rejection covers both module-aliased forms
		# (`core.DiagnosticValue`) AND unqualified uses
		# (`DiagnosticValue` directly).
		if te.name in ("DiagnosticValue", "DiagnosticEntry"):
			alias = getattr(te, "module_alias", None)
			mod = file_aliases.get(alias or "") if alias else None
			is_aliased_to_std_core = (alias is not None and mod == "std.core")
			is_unqualified = (
				alias is None
				and getattr(te, "module_id", None) is None
			)
			# `is_stdlib_source` allows stdlib internal use of the DV/DE
			# bridge.  Path-under-stdlib_root catches the standard
			# configuration; module-id-prefix catches the case where stdlib
			# files are passed as inputs with `--stdlib-root` pointing at a
			# different directory (stdlib-self-deploy / hir-funcs round-trip
			# tests).  Either signal suffices.
			path_str = str(path.resolve()) if path is not None else ""
			file_mid = module_id_by_path.get(path_str)
			is_stdlib_source = (
				(std_root_resolved is not None and _is_path_under(path, std_root_resolved))
				or (
					isinstance(file_mid, str)
					and (file_mid.startswith("std.") or file_mid == "std.core" or file_mid.startswith("lang."))
				)
			)
			if (is_aliased_to_std_core or is_unqualified) and not is_stdlib_source:
				display_name = f"core.{te.name}" if is_aliased_to_std_core else te.name
				diagnostics.append(_p_diag(
					message=(
						f"`{display_name}` is removed in 0.31.62; user code may not "
						f"name the DV public surface — produce canonical JSON text via "
						f"`core.diagnostic_json_*` and pass `String` instead"
					),
					severity="error",
					span=_span_in_file(path, getattr(te, "loc", None)),
					code="E_DV_PUBLIC_REMOVED",
				))
				if is_aliased_to_std_core:
					te.module_id = mod
					te.module_alias = None
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
			if isinstance(st, parser_ast.BlockStmt):
				_resolve_types_in_block(path, file_aliases, st.block)
				continue
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
					# Note: unqualified `diagnostic_entry(...)` / `diagnostic_value(...)`
					# from user source falls through to the natural unknown-name /
					# no-matching-overload diagnostic — those names resolve at the
					# call-resolver level, not by spelling.  A spelling-based gate
					# here would create false positives for user-defined functions
					# of the same name.  The qualified `core.diagnostic_entry(...)`
					# path is identity-gated in the module-qualified call rewriter
					# (see `_rewrite_module_qualified_call`).
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
				if isinstance(expr, parser_ast.UnsafeExpr):
					_resolve_types_in_block(path, file_aliases, expr.block)
					return
				if isinstance(expr, parser_ast.YieldExpr):
					_resolve_types_in_expr(expr.value)
					return
				if isinstance(expr, parser_ast.MatchExpr):
					_resolve_types_in_expr(expr.scrutinee)
					for arm in getattr(expr, "arms", []) or []:
						_resolve_type_expr_in_file(path, file_aliases, getattr(arm, "ctor_base", None), allow_traits=True)
						# A qualified scalar-const pattern (`tokens.X => ...`) resolves
						# its module alias EXACTLY like a value-expression reference:
						# alias → module id via `file_aliases`.
						#
						# SOURCE SYNTAX IS ONLY `NAME.NAME` (one dot, two names) — the
						# `match_qual_const` grammar rule.  So `base` is always a single
						# segment: either an import alias (`import my.tokens as tok` →
						# `tok.X`) or a single-segment module spelled outright.  A
						# DOTTED module path (`acme.tokens.TOK`, three+ names) does NOT
						# parse as this pattern and requires an `as` alias — there is no
						# longer qualified value-path pattern form in v1.
						#
						# The "direct-module-path fallback" below only covers the case
						# where that single `base` segment IS itself a known module name
						# (no `as` alias needed because alias == module id).  The base is
						# rewritten in place to the resolved module id; the checker then
						# resolves the const through that module's table only (re-exports
						# already materialized there resolve too).  An unresolvable base
						# is left as-written and the checker reports E-MATCH-SCALAR-CONST.
						_qbase = getattr(arm, "scalar_const_qual_base", None)
						if _qbase is not None:
							_qmod = file_aliases.get(_qbase)
							if _qmod is None and (
								_qbase in merged_programs
								or (external_module_exports is not None and _qbase in external_module_exports)
							):
								_qmod = _qbase
							if _qmod is not None:
								arm.scalar_const_qual_base = _qmod
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
				if isinstance(expr, parser_ast.Lambda):
					# LANGUAGE_BUG fix (0.31.23): module aliases used in
					# qualified type references inside a lambda body
					# (e.g. `core.Result::Ok(...)` where `core` aliases
					# `std.core`) must be resolved here.  Without this
					# recursion the alias survives into HIR — variant /
					# struct ctor resolution then sees `module_id="core"`
					# (the alias spelling), `resolve_opaque_type` returns
					# a FORWARD_NOMINAL, ctor resolution returns None,
					# no CallInfo is recorded, and `_lambda_can_throw`
					# falls back to the conservative may-throw default.
					# The bare lambda then fails the implicit
					# `core.callback{N}` wrap because the wrap target is
					# nothrow.  See `test_lambda_result_ctor_nothrow.py`.
					for p in getattr(expr, "params", []) or []:
						_resolve_type_expr_in_file(path, file_aliases, getattr(p, "type_expr", None))
					_resolve_type_expr_in_file(path, file_aliases, getattr(expr, "ret_type", None))
					body_expr = getattr(expr, "body_expr", None)
					if body_expr is not None:
						_resolve_types_in_expr(body_expr)
					body_block = getattr(expr, "body_block", None)
					if body_block is not None:
						_resolve_types_in_block(path, file_aliases, body_block)
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
			if isinstance(st, parser_ast.RaiseStmt):
				# Source `throw ...` parses to a `parser_ast.RaiseStmt`; the
				# separate `parser_ast.ThrowStmt` node is not produced for this
				# syntax (`_convert_raise` later lowers RaiseStmt to the stage0
				# `s0.ThrowStmt`).  Without this branch the throw operand never gets
				# alias-canonicalized, so a nested qualified type inside it (e.g.
				# `throw E(kind = a.K::Bad(...))`) reaches HIR with the import alias
				# un-resolved on its base_type_expr, and cross-package constructor
				# resolution fails (missing-CallInfo ICE).
				_resolve_types_in_expr(st.value)
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
				# Slice 7a (0.31.62, 2026-05-05): reject legacy Diagnostic.to_diag /
				# Debuggable.to_debug method shapes at the workspace pre-scan so the
				# rejection fires regardless of whether the workspace bails out
				# before per-module lowering (it does, on the first DV reference
				# in user code via E_DV_PUBLIC_REMOVED).  Identical guards as the
				# duplicate hook in `_lower_parsed_program_to_hir` — which still
				# runs for clean (no-DV-leak) impls so the diagnostic still fires
				# when the rest of the file is fine.  See
				# `_reject_deprecated_trait_method_shapes`.
				_trait = getattr(impl, "trait", None)
				if _trait is not None:
					_is_diagnostic = _is_std_core_diagnostic_trait(
						_trait, module_id=mid, module_aliases=module_aliases,
					)
					_is_debuggable = _is_std_log_debuggable_trait(
						_trait, module_id=mid, module_aliases=module_aliases,
					)
					if _is_diagnostic or _is_debuggable:
						for _mfn in getattr(impl, "methods", []) or []:
							_mname = getattr(_mfn, "name", None)
							_mloc = getattr(_mfn, "loc", None)
							if _is_diagnostic and _mname == "to_diag":
								diagnostics.append(_p_diag(
									message=(
										"`Diagnostic.to_diag(...) -> DiagnosticValue` is removed in "
										"0.31.62; implement `to_json_text(self: &Self) nothrow -> "
										"String` instead and project values via "
										"`core.diagnostic_json_*`"
									),
									severity="error",
									span=_span_in_file(path, _mloc),
									code="E_TO_DIAG_DEPRECATED",
								))
							elif _is_debuggable and _mname == "to_debug":
								diagnostics.append(_p_diag(
									message=(
										"`Debuggable.to_debug(...) -> DiagnosticValue` is removed "
										"in 0.31.62; implement `to_debug_json_text(self: &Self) "
										"nothrow -> String` instead and project values via "
										"`core.diagnostic_json_*`"
									),
									severity="error",
									span=_span_in_file(path, _mloc),
									code="E_TO_DEBUG_DEPRECATED",
								))
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
	# When an external type_table is provided (pre-populated with package types from early
	# linking), use it directly instead of creating a fresh one.  This eliminates the
	# temporal split where the parser resolves types before package data is available.
	if type_table is not None:
		shared_type_table = type_table
	else:
		_tt_kwargs: dict = {}
		if word_bits is not None:
			_tt_kwargs["word_bits"] = word_bits
		shared_type_table = TypeTable(**_tt_kwargs)
	# Slice 5: stash the pre-scanned workspace Diagnostic-impl
	# targets so the per-module synthesizer in
	# `_synthesize_auto_diagnostic_impls` can recognize cross-module
	# explicit `implement core.Diagnostic for T` impls regardless
	# of module visit order.  Also fold in cross-PACKAGE Diagnostic
	# targets gathered by driftc.py from each loaded package's
	# `impl_headers` (the parser-side trait_worlds[external_module]
	# entries are populated AFTER per-module HIR lowering, so an
	# upfront set is needed here).
	combined_diag_targets = set(workspace_diag_targets)
	if external_diagnostic_targets:
		combined_diag_targets |= set(external_diagnostic_targets)
	shared_type_table.workspace_diagnostic_targets = combined_diag_targets
	if package_id is not None:
		shared_type_table.package_id = package_id
	local_pkg = package_id or "__local__"
	has_stdlib = stdlib_root is not None
	if isinstance(external_module_packages, dict):
		for mod, pkg in external_module_packages.items():
			if not isinstance(mod, str) or not isinstance(pkg, str):
				continue
			# Record explicit packaging regardless of whether we apply the
			# module_packages mapping — the boundary provenance matters even
			# when the package mapping is skipped for merged source modules.
			shared_type_table.explicitly_packaged_modules.add(mod)
			if mod in merged_programs and not test_build_only:
				continue
			shared_type_table.module_packages.setdefault(mod, pkg)
	for mod in merged_programs:
		if has_stdlib and _is_stdlib_module(mod):
			shared_type_table.module_packages.setdefault(mod, std_pkg)
		elif mod == "lang.core":
			shared_type_table.module_packages.setdefault(mod, "lang.core")
		else:
			shared_type_table.module_packages.setdefault(mod, local_pkg)
	shared_type_table.module_packages.setdefault("lang.core", "lang.core")
	shared_type_table.module_packages.setdefault("lang.__internal", local_pkg)
	_prime_builtins(shared_type_table)
	# Pre-populate exception schemas from loaded packages so that exception types
	# referenced in function signatures (e.g. `e: err.ResultError`) are resolved
	# as Error rather than FORWARD_NOMINAL during signature resolution.
	if external_exception_schemas:
		prev_exc = getattr(shared_type_table, "exception_schemas", None)
		if not isinstance(prev_exc, dict):
			prev_exc = {}
		prev_exc.update(external_exception_schemas)
		shared_type_table.exception_schemas = prev_exc
	# Slice 6: cross-package manual-Diagnostic merge.
	# Package-defined `pub error E` with a user-owned
	# `implement core.Diagnostic for E` is recorded at producer-side
	# synthesis time and serialized into the package format
	# (`provisional_dmir_v0.py: manual_diagnostic_pub_errors`) and
	# decoded into `DecodedTypeTable.manual_diagnostic_pub_errors`.
	# The package linker (`type_table_link_v0.py`) merges per-package
	# entries into `host.manual_diagnostic_pub_errors`, which is the
	# pre-linked TypeTable passed in via `type_table=` here.  No
	# intersection-with-impl-headers — that approach can't
	# distinguish synthesized impls from manual impls and incorrectly
	# tagged auto-synthesized Diagnostic impls (e.g. on stdlib /
	# package-defined pub errors with all-projectable fields) as
	# manual.  Pinned by
	# `test_ext_cross_package_manual_diagnostic_typed_binder_rejected`.
	# Pre-populate type aliases from loaded packages so that cross-package
	# type references (e.g. web.rest.Request → web.rest.request.Request) resolve
	# correctly during signature resolution in _lower_parsed_program_to_hir.
	if external_type_aliases:
		for a_mid, a_name, a_params, a_target in external_type_aliases:
			if shared_type_table.lookup_type_alias(module_id=a_mid, name=a_name) is None:
				shared_type_table.define_type_alias(
					module_id=a_mid, name=a_name,
					type_params=a_params, target=a_target,
				)

	# Pre-declare all nominal type names across the workspace before lowering any
	# individual module.
	#
	# This prevents cross-module type references (e.g. `import lib as x; val p: x.Point`)
	# from accidentally minting placeholder scalar TypeIds via `ensure_named` when
	# the defining module hasn't been lowered yet. `declare_struct`/`declare_variant`
	# are idempotent when the kind already matches, so later per-module lowering
	# can safely re-run its local declaration passes.
	for _mid, _prog in merged_programs.items():
		# LANGUAGE_BUG follow-up (2026-05-06): apply the same
		# collision dedupe as the per-module lowering pass below
		# (see the longer comment there for the classification +
		# 3-by-3 matrix).  Without this, the workspace pre-scan
		# crashes BEFORE the per-module pass even runs, leaking a
		# raw `field list mismatch` ValueError instead of a clean
		# user diagnostic.  The per-module pass below is the
		# authoritative diagnostic emitter for these classes; the
		# pre-scan dedupes silently so registration completes.
		# LANGUAGE_BUG follow-up (nominal-namespace coherence,
		# 2026-05-06): build a name→first-kind map across all source
		# decl kinds in this module BEFORE per-kind registration so
		# cross-kind collisions can be skipped silently here (the
		# per-module lowerer below is the authoritative diagnostic
		# emitter via `E_DUP_NOMINAL_NAME`).  Without this, the
		# pre-scan's `declare_*` would fail with a raw type-kind
		# mismatch ValueError, which the surrounding `except
		# ValueError` handler would surface as a leaky diagnostic in
		# parallel to the per-module pass's clean diagnostic.
		_pre_kind_by_name: dict[str, str] = {}
		for _s in getattr(_prog, "structs", []) or []:
			if getattr(_s, "is_synthesized_for_error", False):
				_pre_kind_by_name.setdefault(_s.name, "error")
			else:
				_pre_kind_by_name.setdefault(_s.name, "struct")
		for _i in getattr(_prog, "interfaces", []) or []:
			_pre_kind_by_name.setdefault(_i.name, "interface")
		for _v in getattr(_prog, "variants", []) or []:
			_pre_kind_by_name.setdefault(_v.name, "variant")
		for _t in getattr(_prog, "traits", []) or []:
			_pre_kind_by_name.setdefault(_t.name, "trait")

		_pre_seen_source_struct: set[str] = set()
		_pre_seen_synth: set[str] = set()
		for _s in getattr(_prog, "structs", []) or []:
			if _reject_reserved_nominal_type(getattr(_s, "name", ""), loc=getattr(_s, "loc", None), diagnostics=diagnostics):
				continue
			if getattr(_s, "is_synthesized_for_error", False):
				if _s.name in _pre_seen_source_struct:
					# Source struct already occupies this name —
					# per-module lowerer emits
					# `E_DUP_TYPE_NAME_ERROR_VS_STRUCT`.
					continue
				if _s.name in _pre_seen_synth:
					# Duplicate `error` decl; per-module lowerer
					# emits `duplicate exception` via the catalog.
					continue
				_pre_seen_synth.add(_s.name)
			else:
				if _s.name in _pre_seen_source_struct:
					# Duplicate source struct decl; per-module
					# lowerer emits `E_DUP_SOURCE_STRUCT_NAME`.
					continue
				if _s.name in _pre_seen_synth:
					# Synthesized error struct face already used
					# this name in this module; per-module lowerer
					# emits `E_DUP_TYPE_NAME_ERROR_VS_STRUCT`.
					continue
				_pre_seen_source_struct.add(_s.name)
			try:
				struct_id = shared_type_table.declare_struct(
					_mid,
					_s.name,
					[f.name for f in getattr(_s, "fields", []) or []],
					list(getattr(_s, "type_params", []) or []),
					decl_loc=getattr(_s, "loc", None),
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
		_pre_seen_interface: set[str] = set()
		for _i in getattr(_prog, "interfaces", []) or []:
			if _reject_reserved_nominal_type(getattr(_i, "name", ""), loc=getattr(_i, "loc", None), diagnostics=diagnostics):
				continue
			# Cross-kind collision: name was first claimed by a
			# different decl kind; per-module pass emits
			# `E_DUP_NOMINAL_NAME`.  Same-kind duplicate dedupes here.
			if _pre_kind_by_name.get(_i.name) != "interface":
				continue
			if _i.name in _pre_seen_interface:
				continue
			_pre_seen_interface.add(_i.name)
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
		_pre_seen_variant: set[str] = set()
		for _v in getattr(_prog, "variants", []) or []:
			if _reject_reserved_nominal_type(getattr(_v, "name", ""), loc=getattr(_v, "loc", None), diagnostics=diagnostics):
				continue
			# Cross-kind collision dedupe; same-kind dedupe.
			if _pre_kind_by_name.get(_v.name) != "variant":
				continue
			if _v.name in _pre_seen_variant:
				continue
			_pre_seen_variant.add(_v.name)
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
					decl_loc=getattr(_v, "loc", None),
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

	# ── Phase 1: register pub type aliases for ALL modules before lowering. ──
	# This ensures that when module A references module B's pub type alias
	# during signature resolution, the alias is already in the type table —
	# regardless of module processing order. Without this, a module lowered
	# before its dependency creates FORWARD_NOMINALs for aliased types.
	for mid, prog in merged_programs.items():
		for alias in getattr(prog, "type_aliases", []) or []:
			if not getattr(alias, "is_pub", False):
				continue
			alias_name = getattr(alias, "name", None)
			alias_target = getattr(alias, "target", None)
			raw_params = getattr(alias, "type_params", []) or []
			alias_params = [p.name if hasattr(p, "name") else str(p) for p in raw_params]
			if alias_name and alias_target:
				if shared_type_table.lookup_type_alias(module_id=mid, name=alias_name) is None:
					shared_type_table.define_type_alias(
						module_id=mid,
						name=alias_name,
						type_params=alias_params,
						target=alias_target,
					)

	# ── Phase 2: lower each module and qualify its callable symbols. ──
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
			# extern "C" declarations are never exported entrypoints — they have
			# no Drift body to wrap, and callers invoke the bare C symbol directly.
			is_exported = (
				(local_name in local_free_fns)
				and (local_name in exported_values)
				and (local_name != "main")
				and not sig.is_extern_c
			)
			# extern "C" functions must keep their bare C symbol name — do not
			# module-qualify them, or the LLVM declare/call will emit an
			# invalid mangled name like @repro_mod::puts instead of @puts.
			display_name = sig.name if sig.is_extern_c else function_symbol(fn_id)
			updated_sig = replace(sig, name=display_name, is_exported_entrypoint=is_exported)
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

	# Register type aliases for star-re-exported types so that the exporting
	# module’s name is a valid alias for the origin type.  Explicit `pub type`
	# aliases are pre-registered in Phase 1 above; this covers star
	# re-exports (`export { other.module.* }`) which do not create explicit
	# alias declarations but still need alias entries in the type table for
	# correct serialization and consumer-side resolution.
	for exporting_mid, kind_targets in reexported_type_targets_by_module.items():
		for kind, targets in kind_targets.items():
			for local_name, (origin_mid, origin_name) in targets.items():
				if origin_mid == exporting_mid:
					continue
				# Only register if no explicit alias already covers this name.
				if shared_type_table.lookup_type_alias(module_id=exporting_mid, name=local_name) is not None:
					continue
				# Build a TypeExpr-like target pointing to the origin type.
				origin_target = parser_ast.TypeExpr(name=origin_name, args=[], module_id=origin_mid)
				shared_type_table.define_type_alias(
					module_id=exporting_mid,
					name=local_name,
					type_params=[],
					target=origin_target,
				)

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

		def _exported_exception_names(mod: str) -> set[str]:
			if mod in exports_types_by_module:
				return set((exports_types_by_module.get(mod) or {}).get("exceptions") or set())
			if external_module_exports is not None and mod in external_module_exports:
				ext = external_module_exports.get(mod) or {}
				ext_types = ext.get("types")
				if isinstance(ext_types, dict):
					return set(ext_types.get("exceptions") or set())
			return set()

		def _resolve_exported_ctor_target(mod: str, member: str) -> tuple[str, str] | None:
			if member in exported_struct_names(mod):
				return reexported_type_targets_by_module.get(mod, {}).get("structs", {}).get(member, (mod, member))
			# Slice 5 (Path A): a `pub error E` decl carries a parallel
			# StructDef face for value-type machinery (constructor / field
			# access).  Exported under `exceptions` to avoid the package
			# validator's no-overlap-across-kinds rule, but for ctor-call
			# resolution it must behave like a struct ctor.
			if member in _exported_exception_names(mod):
				return (mod, member)
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
			# Slice 7a follow-up (K finding 2, 2026-05-05): direct
			# `core.diagnostic_entry(...)` value-call must surface
			# `E_DV_PUBLIC_REMOVED` rather than the generic "module
			# does not export symbol".  Stdlib retained an internal
			# `diagnostic_entry` for the legacy DV bridge; in user
			# source we want the migration-friendly diagnostic.
			# Stdlib internal use is allowed via module-id prefix
			# (the rewriter doesn't know its source path directly,
			# but the enclosing module_id is in scope).
			if mod == "std.core" and member in ("diagnostic_entry", "diagnostic_value"):
				caller_is_stdlib = (
					isinstance(module_id, str)
					and (module_id.startswith("std.") or module_id.startswith("lang."))
				)
				if not caller_is_stdlib:
					diagnostics.append(_p_diag(
						message=(
							f"`core.{member}(...)` is removed in 0.31.62; user code may not "
							f"name the DV public surface — produce canonical JSON text via "
							f"`core.diagnostic_json_*` and pass `String` instead"
						),
						severity="error",
						span=getattr(receiver, "loc", Span()),
						code="E_DV_PUBLIC_REMOVED",
					))
					return H.HCall(
						fn=H.HVar(name=member, module_id=mod),
						args=args,
						kwargs=kwargs,
						type_args=type_args,
					)
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
						message=f"module-qualified constructor call '{alias}.{member}(...)' is only supported for structs in v1",
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
					# call is not a supported surface construct in v1.
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
						elif hasattr(H, "HMapEntry") and isinstance(it, getattr(H, "HMapEntry")):
							it.key = walk_expr(it.key, bound=bound)
							it.value = walk_expr(it.value, bound=bound)
							new_list.append(it)
						elif hasattr(H, "HKwArg") and isinstance(it, getattr(H, "HKwArg")):
							# HExceptionInit (and any other shape carrying HKwArg
							# items in a list field) reaches the generic recursion
							# arm.  Without this branch, `throw E(field = alias.fn(...))`
							# would leave the kw value as `HMethodCall(receiver=HVar("alias"), ...)`
							# instead of the rewritten module-qualified call, and the
							# type-checker would then report `unknown name 'alias'`.
							if getattr(it, "value", None) is not None:
								it.value = walk_expr(it.value, bound=bound)
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
			diagnostics.append(_p_diag(message=f"exception code collision between '{other}' and '{fqn}' (payload {payload})", severity="error", span=Span(), code="E_EVENT_CODE_DUPLICATE"))
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
	# After all modules are parsed, normalize forward nominals in signatures
	# using the full schema/alias set.
	def _coerce_forward_nominal(tid: TypeId) -> TypeId:
		try:
			td = shared_type_table.get(tid)
		except Exception:
			return tid
		if td.kind is TypeKind.FORWARD_NOMINAL and td.module_id:
			fqn = f"{td.module_id}:{td.name}"
			if fqn in shared_type_table.exception_schemas:
				return shared_type_table.ensure_error()
			alias_def = shared_type_table.lookup_type_alias(module_id=td.module_id, name=td.name)
			if alias_def is not None:
				alias_params, alias_target, _loc = alias_def
				if not alias_params:
					resolved = resolve_opaque_type(alias_target, shared_type_table, module_id=td.module_id, type_params=None, allow_generic_base=True)
					if resolved != tid:
						return _coerce_forward_nominal(resolved)
			resolved_nom = (
				shared_type_table.get_nominal(kind=TypeKind.STRUCT, module_id=td.module_id, name=td.name)
				or shared_type_table.get_nominal(kind=TypeKind.VARIANT, module_id=td.module_id, name=td.name)
				or shared_type_table.get_nominal(kind=TypeKind.INTERFACE, module_id=td.module_id, name=td.name)
			)
			if resolved_nom is not None:
				return resolved_nom
		if td.kind is TypeKind.REF and td.param_types:
			inner = td.param_types[0]
			new_inner = _coerce_forward_nominal(inner)
			if new_inner != inner:
				return shared_type_table.ensure_ref_mut(new_inner) if td.ref_mut else shared_type_table.ensure_ref(new_inner)
		if td.kind is TypeKind.ARRAY and td.param_types:
			elem = td.param_types[0]
			new_elem = _coerce_forward_nominal(elem)
			if new_elem != elem:
				return shared_type_table.new_array(new_elem)
		if td.kind is TypeKind.FNRESULT and len(td.param_types) == 2:
			ok_old = td.param_types[0]
			err_old = td.param_types[1]
			ok_new = _coerce_forward_nominal(ok_old)
			err_new = _coerce_forward_nominal(err_old)
			if ok_new != ok_old or err_new != err_old:
				return shared_type_table.ensure_fnresult(ok_new, err_new)
		if td.kind is TypeKind.FUNCTION and td.param_types:
			new_params = [_coerce_forward_nominal(t) for t in td.param_types]
			changed = any(a != b for a, b in zip(new_params, td.param_types))
			if changed:
				if len(new_params) == 1:
					return shared_type_table.ensure_function([], new_params[0], can_throw=td.fn_throws)
				return shared_type_table.ensure_function(new_params[:-1], new_params[-1], can_throw=td.fn_throws)
		return tid
	for mod in modules.values():
		for sig in mod.signatures_by_id.values():
			if sig.param_type_ids:
				sig.param_type_ids = [_coerce_forward_nominal(t) for t in sig.param_type_ids]
			if sig.return_type_id is not None:
				sig.return_type_id = _coerce_forward_nominal(sig.return_type_id)
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
	# LANGUAGE_BUG follow-ups (2026-05-06): collision classes in the
	# pub-error Path-A struct face AND in plain user-source struct
	# decls.  The classification (per K's review-loop step 1):
	#
	#   - source struct E             (StructDef, is_synthesized_for_error=False)
	#   - source error E              (ExceptionDef, kind="error")
	#   - synthesized struct face for error E
	#                                 (StructDef, is_synthesized_for_error=True)
	#
	# The 3-by-3 collision space (per K's review-loop step 2) has
	# the following crash classes that previously leaked
	# `field list mismatch` ValueError text into user diagnostics:
	#
	#   (a) source struct E + source struct E (different fields)
	#   (b) error E + error E (catalog catches name dup, but synth
	#       struct face dup crashes schema registration)
	#   (c) source struct E + error E (source vs synth struct face,
	#       different fields)
	#
	# Strategy:
	#   1. Walk source structs first; emit `E_DUP_SOURCE_STRUCT_NAME`
	#      at second-and-later occurrences and skip them from
	#      registration so the type table sees only the first
	#      (authoritative) decl.
	#   2. Walk synthesized error struct faces; skip when the name
	#      collides with (i) the surviving source struct (emit
	#      `E_DUP_TYPE_NAME_ERROR_VS_STRUCT`) or (ii) a previously-
	#      seen synthesized face (silent — the catalog surfaces its
	#      own `duplicate exception` diagnostic).
	#
	# Pinned by:
	#   - `test_parser_pub_decls.py::test_parse_pub_top_level_decls`
	#   - `test_parser_exceptions.py::test_duplicate_exception_reports_diagnostic`
	#   - `test_parser_exceptions.py::test_struct_and_error_same_name_reports_clean_diagnostic`
	#   - `test_parser_exceptions.py::test_duplicate_pub_error_does_not_cascade_into_synth_impls`
	#   - `test_parser_structs.py::test_duplicate_source_struct_reports_clean_diagnostic`
	_struct_defs_raw = list(getattr(prog, "structs", []) or [])
	# Track error names whose synthesized face was suppressed because
	# a user-source struct of the same name exists, so downstream
	# Throw / Diagnostic synthesis can also skip them (otherwise the
	# auto-impls reference a target whose struct face no longer
	# matches the error's field schema, producing a cascade of
	# spurious "trait method body type mismatch" diagnostics).
	_blocked_error_names: set[str] = set()
	_seen_source_struct_names: set[str] = set()
	_seen_synth_struct_names: set[str] = set()
	struct_defs = []
	for _s in _struct_defs_raw:
		if getattr(_s, "is_synthesized_for_error", False):
			if _s.name in _seen_source_struct_names:
				diagnostics.append(_p_diag(
					message=(
						f"type name '{_s.name}' is already declared as a struct in "
						f"this module — `error {_s.name}` cannot share the name"
					),
					severity="error",
					span=Span.from_loc(getattr(_s, "loc", None)),
					code="E_DUP_TYPE_NAME_ERROR_VS_STRUCT",
				))
				_blocked_error_names.add(_s.name)
				continue
			if _s.name in _seen_synth_struct_names:
				continue
			_seen_synth_struct_names.add(_s.name)
		else:
			# Source struct decl.  Detect duplicate source struct
			# names AND collisions against an already-registered
			# synthesized error struct face BEFORE registration so
			# the type table's `define_struct_schema_fields` doesn't
			# trip on mismatched schemas with a leaked
			# `field list mismatch` ValueError.  Three cases:
			#   - source struct E + source struct E (same/different
			#     fields): clean `duplicate struct` diagnostic.
			#   - error E (first) + source struct E (second): clean
			#     `E_DUP_TYPE_NAME_ERROR_VS_STRUCT` diagnostic at
			#     the source struct site (the synth face already
			#     occupied the name; the source struct is the
			#     intruder in this declaration order).
			if _s.name in _seen_source_struct_names:
				diagnostics.append(_p_diag(
					message=f"duplicate struct '{_s.name}' in module '{module_id}'",
					severity="error",
					span=Span.from_loc(getattr(_s, "loc", None)),
					code="E_DUP_SOURCE_STRUCT_NAME",
				))
				continue
			if _s.name in _seen_synth_struct_names:
				# A synthesized error struct face was already
				# registered for this name; the source struct
				# cannot share it.  Block the source struct from
				# registration — the user must rename one of them.
				diagnostics.append(_p_diag(
					message=(
						f"type name '{_s.name}' is already declared as an error in "
						f"this module — `struct {_s.name}` cannot share the name"
					),
					severity="error",
					span=Span.from_loc(getattr(_s, "loc", None)),
					code="E_DUP_TYPE_NAME_ERROR_VS_STRUCT",
				))
				continue
			_seen_source_struct_names.add(_s.name)
		struct_defs.append(_s)
	# LANGUAGE_BUG follow-up (nominal-namespace coherence,
	# 2026-05-06): K's review-loop matrix sweep flagged that
	# cross-kind decls sharing a name silently coexist (e.g.
	# `pub struct X` + `pub variant X`, `pub error X` + `pub trait X`),
	# producing two type-table entries under different `TypeKind`s.
	# Downstream resolution then picks one ambiguously.  Fix:
	# detect cross-kind collisions BEFORE per-kind registration
	# runs; emit a clean `E_DUP_NOMINAL_NAME` diagnostic at the
	# second-and-later sites and skip them from registration.
	#
	# Iteration order is source-decl source order (struct, variant,
	# interface, trait, error/pub error).  First-seen kind wins;
	# subsequent decls under any other kind are blocked.  Same-kind
	# duplicates are already handled by per-kind dedupe paths
	# (struct+struct via E_DUP_SOURCE_STRUCT_NAME; trait+trait via
	# the trait-world's "duplicate trait definition" diagnostic).
	# Pinned by `lang/tests/parser/test_parser_nominal_coherence.py`'s
	# 15-cell collision matrix.
	_NOMINAL_KIND_LABELS: dict[str, str] = {
		"struct": "struct",
		"variant": "variant",
		"interface": "interface",
		"trait": "trait",
		"error": "error",
	}
	_nominal_first_kind: dict[str, str] = {}
	_blocked_variant_names: set[str] = set()
	_blocked_interface_names: set[str] = set()
	_blocked_trait_names: set[str] = set()
	_nominal_blocked_error_names_xkind: set[str] = set()

	def _register_nominal_or_diag(_name: str, _kind: str, _loc: object) -> bool:
		"""Returns True if this decl should be allowed to register;
		False if a cross-kind collision was detected and a diagnostic
		was emitted."""
		first = _nominal_first_kind.get(_name)
		if first is None:
			_nominal_first_kind[_name] = _kind
			return True
		if first == _kind:
			# Same-kind duplicate; defer to the per-kind handler
			# (which has already-fixed paths for struct/trait or
			# clean type-table contract checks for variant/interface).
			return True
		first_label = _NOMINAL_KIND_LABELS.get(first, first)
		this_label = _NOMINAL_KIND_LABELS.get(_kind, _kind)
		_article = "an" if first_label[:1] in {"a", "e", "i", "o", "u"} else "a"
		diagnostics.append(_p_diag(
			message=(
				f"type name '{_name}' is already declared as "
				f"{_article} {first_label} in this module — "
				f"`{this_label} {_name}` cannot share the name"
			),
			severity="error",
			span=Span.from_loc(_loc),
			code="E_DUP_NOMINAL_NAME",
		))
		return False

	# Build a unified source-order list of (name, kind, loc) tuples
	# from all source decls.  Source order is the user's declaration
	# order in the file; iterating per-kind (structs first, then
	# variants, etc.) gives wrong "already declared as <X>"
	# diagnostics when a variant/interface/trait was actually
	# declared FIRST and a later struct/error tries to share the
	# name.  Pinned by reverse-order regression cases in
	# `test_parser_nominal_coherence.py` (15 forward + 12 reverse =
	# 27 cross-kind pairs total).
	def _decl_pos(_decl) -> tuple[int, int, int]:
		"""Sort key drawn from the decl's source location.  Falls
		back to large sentinels so any decl missing a usable loc
		still sorts deterministically (after all located decls).
		"""
		_loc = getattr(_decl, "loc", None)
		if _loc is None:
			return (10**9, 10**9, 10**9)
		_sp = getattr(_loc, "start_pos", None)
		if isinstance(_sp, int):
			return (_sp, 0, 0)
		_line = getattr(_loc, "line", None) or 10**9
		_col = getattr(_loc, "column", None) or 10**9
		return (10**9, int(_line), int(_col))

	_decls_in_source_order: list[tuple] = []
	for _s in (getattr(prog, "structs", []) or []):
		# Skip synthesized error struct faces — they share the
		# `loc` of the originating `error` decl (already counted
		# under "error" below).  Counting both would double-emit.
		if getattr(_s, "is_synthesized_for_error", False):
			continue
		# Skip source structs already filtered out by the synth-vs-
		# source dedupe (`struct_defs` is the survivor set).
		if _s not in struct_defs:
			continue
		_decls_in_source_order.append(("struct", _s.name, _s, _decl_pos(_s)))
	for _exc in (getattr(prog, "exceptions", []) or []):
		if getattr(_exc, "kind", "exception") == "error":
			_decls_in_source_order.append(("error", _exc.name, _exc, _decl_pos(_exc)))
	for _v in (getattr(prog, "variants", []) or []):
		_decls_in_source_order.append(("variant", _v.name, _v, _decl_pos(_v)))
	for _i in (getattr(prog, "interfaces", []) or []):
		_decls_in_source_order.append(("interface", _i.name, _i, _decl_pos(_i)))
	for _t in (getattr(prog, "traits", []) or []):
		_decls_in_source_order.append(("trait", _t.name, _t, _decl_pos(_t)))
	_decls_in_source_order.sort(key=lambda _e: _e[3])

	# Single source-ordered walk: same-kind same-name dedupe per
	# kind + cross-kind first-claim-wins.  All diagnostics emit
	# clean codes (no raw "schema mismatch" / "field list mismatch"
	# contract text leaks).
	_seen_variant_names: set[str] = set()
	_seen_interface_names: set[str] = set()
	_seen_trait_names: set[str] = set()
	_blocked_struct_names_xkind: set[str] = set()
	_blocked_error_names_xkind: set[str] = set()
	for _kind, _name, _decl, _ in _decls_in_source_order:
		_loc = getattr(_decl, "loc", None)
		if _kind == "struct":
			# Source struct same-kind dedupe was already done
			# upstream when building `struct_defs` (E_DUP_SOURCE_STRUCT_NAME);
			# here we record the cross-kind first-claim.  If a
			# variant/interface/trait/error already claimed this
			# name (declared earlier in source order), block this
			# struct decl from the registration loops below.
			if not _register_nominal_or_diag(_name, "struct", _loc):
				_blocked_struct_names_xkind.add(_name)
		elif _kind == "error":
			# Source error.  The synth-vs-source dedupe and
			# duplicate-error catalog handle same-kind dups; here
			# we record the cross-kind first-claim.  If a
			# variant/interface/trait/struct already claimed this
			# name, block this error decl AND its synthesized
			# struct face from registration.
			if not _register_nominal_or_diag(_name, "error", _loc):
				_blocked_error_names.add(_name)
				_blocked_error_names_xkind.add(_name)
		elif _kind == "variant":
			if _name in _seen_variant_names:
				diagnostics.append(_p_diag(
					message=f"duplicate variant '{_name}' in module '{module_id}'",
					severity="error",
					span=Span.from_loc(_loc),
					code="E_DUP_SOURCE_VARIANT_NAME",
				))
				_blocked_variant_names.add(_name)
				continue
			if not _register_nominal_or_diag(_name, "variant", _loc):
				_blocked_variant_names.add(_name)
				continue
			_seen_variant_names.add(_name)
		elif _kind == "interface":
			if _name in _seen_interface_names:
				diagnostics.append(_p_diag(
					message=f"duplicate interface '{_name}' in module '{module_id}'",
					severity="error",
					span=Span.from_loc(_loc),
					code="E_DUP_SOURCE_INTERFACE_NAME",
				))
				_blocked_interface_names.add(_name)
				continue
			if not _register_nominal_or_diag(_name, "interface", _loc):
				_blocked_interface_names.add(_name)
				continue
			_seen_interface_names.add(_name)
		elif _kind == "trait":
			if _name in _seen_trait_names:
				diagnostics.append(_p_diag(
					message=f"duplicate trait '{_name}' in module '{module_id}'",
					severity="error",
					span=Span.from_loc(_loc),
					code="E_DUP_SOURCE_TRAIT_NAME",
				))
				_blocked_trait_names.add(_name)
				continue
			if not _register_nominal_or_diag(_name, "trait", _loc):
				_blocked_trait_names.add(_name)
				continue
			_seen_trait_names.add(_name)

	# A struct decl that lost a cross-kind collision must be
	# filtered from `struct_defs` so the registration loops below
	# don't try to register it (which would crash via the type-
	# table contract).  Same for the synthesized error struct face
	# of any error that lost cross-kind.
	if _blocked_struct_names_xkind or _blocked_error_names_xkind:
		struct_defs = [
			_s for _s in struct_defs
			if (
				_s.name not in _blocked_struct_names_xkind
				and not (
					getattr(_s, "is_synthesized_for_error", False)
					and _s.name in _blocked_error_names_xkind
				)
			)
		]

	variant_defs = [
		_v for _v in (getattr(prog, "variants", []) or [])
		if _v.name not in _blocked_variant_names
	]
	# Dedupe by name so two `pub variant X` decls are filtered down
	# to one (the first occurrence) for registration; the duplicate
	# diagnostic was already emitted above.
	_seen_v: set[str] = set()
	variant_defs = [_v for _v in variant_defs if not (_v.name in _seen_v or _seen_v.add(_v.name))]
	interface_defs = [
		_i for _i in (getattr(prog, "interfaces", []) or [])
		if _i.name not in _blocked_interface_names
	]
	_seen_i: set[str] = set()
	interface_defs = [_i for _i in interface_defs if not (_i.name in _seen_i or _seen_i.add(_i.name))]
	type_alias_defs = list(getattr(prog, "type_aliases", []) or [])
	struct_param_maps: dict[TypeKey, dict[str, TypeParamId]] = {}
	exception_catalog: dict[str, int] = _build_exception_catalog(prog.exceptions, module_id, diagnostics)
	# Slice 5: per-event kind ("exception" legacy paren-form vs "error"
	# new `pub error` decl).  Drives catch binder typing — kind="error"
	# binds the typed catch binder to the parallel struct type (Path A).
	exception_kinds: dict[str, str] = {}
	# Slice 5: per-event public-visibility flag.  Drives the visibility
	# coherence check (`pub fn f() throws PrivateError` rejected).
	exception_pub: dict[str, bool] = {}
	for exc in prog.exceptions:
		fqn = f"{module_id}:{exc.name}"
		field_names = [arg.name for arg in getattr(exc, "args", [])]
		exception_schemas[fqn] = (fqn, field_names)
		exception_kinds[fqn] = getattr(exc, "kind", "exception")
		exception_pub[fqn] = bool(getattr(exc, "is_pub", False))
	# Make exception schemas visible before signature resolution so exception
	# types can be used in annotations without minting forward-nominal types.
	prev_exc = getattr(type_table, "exception_schemas", None)
	if not isinstance(prev_exc, dict):
		prev_exc = {}
	prev_exc.update(exception_schemas)
	type_table.exception_schemas = prev_exc
	prev_exc_kinds = getattr(type_table, "exception_kinds", None)
	if not isinstance(prev_exc_kinds, dict):
		prev_exc_kinds = {}
	prev_exc_kinds.update(exception_kinds)
	type_table.exception_kinds = prev_exc_kinds
	prev_exc_pub = getattr(type_table, "exception_pub", None)
	if not isinstance(prev_exc_pub, dict):
		prev_exc_pub = {}
	prev_exc_pub.update(exception_pub)
	type_table.exception_pub = prev_exc_pub
	# Slice 5: synthesize `implement core.Throw for E` for every `pub error E`
	# (idempotent skip when a manual impl exists).  Must run BEFORE
	# `build_trait_world` so the synthesized impls participate in trait-world
	# construction and `Result<T, E>.or_throw()` resolves the `require E is Throw`
	# clause cleanly without forcing users to hand-write boilerplate.
	_synthesize_auto_throw_impls(
		prog, module_id=module_id, module_aliases=module_aliases,
		blocked_error_names=_blocked_error_names,
	)
	# Slice 7a: reject legacy `Diagnostic.to_diag` / `Debuggable.to_debug`
	# impl method shapes BEFORE synthesis runs so the rejection points at
	# the user's offending decl rather than a downstream cascade (e.g.
	# trait-satisfaction failures during synthesis or build_trait_world).
	_reject_deprecated_trait_method_shapes(
		prog,
		module_id=module_id,
		module_aliases=module_aliases,
		diagnostics=diagnostics,
	)
	# Slice 5: synthesize `implement core.Diagnostic for E` for every
	# `pub error E` whose fields are all projectable.  Non-projectable
	# fields surface E_PUB_ERROR_FIELD_NOT_PROJECTABLE here.  Same
	# placement rationale as auto-Throw — must run before
	# `build_trait_world` so the synthesized impls register cleanly.
	_synthesize_auto_diagnostic_impls(
		prog,
		module_id=module_id,
		type_table=type_table,
		module_aliases=module_aliases,
		exception_kinds=type_table.exception_kinds,
		exception_pub=type_table.exception_pub,
		diagnostics=diagnostics,
		blocked_error_names=_blocked_error_names,
	)
	# Build a TypeTable early so we can register user-defined type names (structs)
	# before resolving function signatures. This prevents `resolve_opaque_type`
	# from minting unrelated placeholder TypeIds for struct names.
	if package_id is not None:
		type_table.package_id = package_id
	_prime_builtins(type_table)
	# LANGUAGE_BUG follow-up (nominal-namespace coherence,
	# 2026-05-06): drop traits whose name was blocked above due to
	# a cross-kind collision (e.g. `pub struct X` + `pub trait X`)
	# so `build_trait_world` doesn't register them under the same
	# name as another nominal kind.  The user-facing
	# `E_DUP_NOMINAL_NAME` diagnostic was already emitted above.
	if _blocked_trait_names:
		prog.traits = [
			_t for _t in (getattr(prog, "traits", []) or [])
			if _t.name not in _blocked_trait_names
		]
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
	from lang.driftc.core.types_core import UintConst as _UintConst, Uint64Const as _Uint64Const, validate_const_value as _validate_const

	def _eval_const_value(expr: parser_ast.Expr) -> object | None:
		if isinstance(expr, parser_ast.Literal):
			return expr.value
		if hasattr(parser_ast, "UintLiteral") and isinstance(expr, parser_ast.UintLiteral):
			return _UintConst(expr.value)
		if hasattr(parser_ast, "Uint64Literal") and isinstance(expr, parser_ast.Uint64Literal):
			return _Uint64Const(expr.value)
		if isinstance(expr, parser_ast.Unary) and getattr(expr, "op", None) in ("-", "+"):
			inner = getattr(expr, "operand", None)
			if isinstance(inner, parser_ast.Literal) and isinstance(inner.value, (int, float)):
				if getattr(expr, "op", None) == "-":
					return -inner.value
				return inner.value
		if isinstance(expr, parser_ast.ArrayLiteral):
			vals = []
			for elem in expr.elements:
				v = _eval_const_value(elem)
				if v is None:
					return None
				vals.append(v)
			return vals
		return None

	for c in getattr(prog, "consts", []) or []:
		decl_ty = resolve_opaque_type(c.type_expr, type_table, module_id=module_id)
		val = _eval_const_value(c.value)
		if val is None:
			diagnostics.append(
				_p_diag(
					phase="parser",
					message=(
						f"const '{c.name}' initializer must be a compile-time literal in v1 "
						"(Int/Uint/Bool/String/Float, optionally with unary '+' or '-')"
					),
					severity="error",
					span=Span.from_loc(getattr(c, "loc", None)),
				)
			)
			continue
		ok, val, err = _validate_const(type_table, c.name, decl_ty, val)
		if not ok:
			diagnostics.append(_p_diag(phase="parser", message=err, severity="error", span=Span.from_loc(getattr(c, "loc", None))))
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
				decl_loc=getattr(s, "loc", None),
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
				decl_loc=getattr(v, "loc", None),
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
			trait_lookup_key = impl_meta.trait_key
			if trait_lookup_key is None and getattr(impl, "trait", None) is not None:
				trait_lookup_key = trait_key_from_expr(
					impl.trait,
					default_module=module_id,
					default_package=package_id,
					module_packages=getattr(type_table, "module_packages", None),
				)
			if trait_lookup_key is not None:
				trait_mod = trait_lookup_key.module
				trait_name = trait_lookup_key.name
			else:
				trait_mod = getattr(impl.trait, "module_id", None) or module_id if getattr(impl, "trait", None) is not None else module_id
				trait_name = getattr(impl.trait, "name", None) if getattr(impl, "trait", None) is not None else None
			if getattr(impl, "trait", None) is not None and trait_name is not None:
				trait_worlds_map = getattr(type_table, "trait_worlds", None)
				trait_world = trait_worlds_map.get(trait_mod) if isinstance(trait_worlds_map, dict) else None
				if trait_world is not None:
					trait_defs = getattr(trait_world, "traits", {}) or {}
					for trait_key, trait_def in trait_defs.items():
						if getattr(trait_key, "module", None) != trait_mod:
							continue
						if getattr(trait_key, "name", None) != trait_name:
							continue
						for meth in list(getattr(trait_def, "methods", []) or []):
							if meth.name == fn.name:
								trait_method_declared_nothrow = bool(getattr(meth, "declared_nothrow", False))
								break
						if trait_method_declared_nothrow:
							break
			if getattr(impl, "trait", None) is not None and not trait_method_declared_nothrow:
				trait_base_id = type_table.get_interface_base(module_id=trait_mod, name=trait_name)
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
			impl_method_decl = _FrontendDecl(
				fn_id,
				symbol_name,
				fn.orig_name,
				fn.type_params,
				list(getattr(fn, "type_param_locs", []) or []),
				params,
				fn.return_type,
				getattr(fn, "loc", None),
				declared_nothrow,
				# Phase 1 v2: previously dropped declared_throws because
				# the impl-block path used positional args up through
				# declared_nothrow and then jumped to keyword args.
				bool(getattr(fn, "declared_throws", False)),
				# Phase 1 v3: also pass declared_terminal_throws (the new
				# bare terminal `throws` form). Without this the impl-block
				# path would silently drop it and Phase 2's body-flow check
				# would never trigger on impl-block terminal methods.
				bool(getattr(fn, "declared_terminal_throws", False)),
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
			# Propagate the `@intrinsic` marker from the parser FunctionDef
			# to the frontend decl for implement-block methods (the free-
			# function path does the equivalent assignment in
			# `_decl_from_parser_fn`).  Without this, intrinsic methods on
			# Arc / std.concurrent would be treated as bodied functions
			# and trip the "must return a value on all paths" check.
			impl_method_decl.is_intrinsic = bool(getattr(fn, "is_intrinsic", False))
			# Slice 6: stash trait identity (canonical module + name)
			# on the impl-method decl so FnSignature carries it
			# downstream.  Used by the manual-Diagnostic Site C
			# lowering to disambiguate `to_json_text` providers.
			_impl_trait_expr = getattr(impl, "trait", None)
			if _impl_trait_expr is not None:
				_it_mod = getattr(_impl_trait_expr, "module_id", None)
				if _it_mod is None:
					_it_alias = getattr(_impl_trait_expr, "module_alias", None)
					if _it_alias is not None:
						_it_mod = file_module_aliases.get(_it_alias)
					elif module_id == "std.core":
						_it_mod = "std.core"
				impl_method_decl.impl_trait_module = _it_mod
				impl_method_decl.impl_trait_name = getattr(_impl_trait_expr, "name", None)
			# Arc runtime boundary: when the @intrinsic method lives on
			# `Arc<T>`, tag the decl with the corresponding
			# IntrinsicKind so downstream code (checker call-target,
			# MIR lowering) dispatches through the Arc intrinsic
			# machinery rather than trying to emit a call to a
			# bodyless stub.  Dispatch is keyed on the impl target
			# name + method name + declaration module — a narrow,
			# centralized recognition point per the Stage 2 spec.
			#
			# Two recognized declaration modules:
			#   - `std.core.arc` hosts the type's own intrinsic
			#     methods (`clone`, `get`, `as_interface`) inside an
			#     `implement<T> Arc<T>` block.
			#   - `std.core` hosts the `Destructible` impl for
			#     `Arc<T>` (next to where `Destructible` is declared,
			#     so `std.core.arc` does not need to import
			#     `std.core` and close a cycle).
			# The Arc relocation moved these from `std.concurrent`
			# to `std.core` / `std.core.arc` at ABI 11 (their
			# semantic contract is shared ownership, not concurrency).
			if impl_method_decl.is_intrinsic and module_id in ("std.core", "std.core.arc"):
				_target_name = getattr(getattr(impl, "target", None), "name", None)
				if _target_name == "Arc":
					_meth_name = fn.name
					_trait_is_destructible = (
						trait_lookup_key is not None
						and getattr(trait_lookup_key, "name", None) == "Destructible"
					)
					if _meth_name == "clone" and getattr(impl, "trait", None) is None:
						impl_method_decl.intrinsic_kind = IntrinsicKind.ARC_CLONE
					elif _meth_name == "get" and getattr(impl, "trait", None) is None:
						impl_method_decl.intrinsic_kind = IntrinsicKind.ARC_GET
					elif _meth_name == "destroy" and _trait_is_destructible:
						impl_method_decl.intrinsic_kind = IntrinsicKind.ARC_DESTROY
					elif _meth_name == "as_interface" and getattr(impl, "trait", None) is None:
						impl_method_decl.intrinsic_kind = IntrinsicKind.ARC_AS_INTERFACE
			decls.append(impl_method_decl)
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

	type_table, sigs, ffi_diags = resolve_program_signatures(
		decls, table=type_table, diagnostics=diagnostics,
	)
	signatures.update(sigs)
	for msg in ffi_diags:
		diagnostics.append(_p_diag(message=msg, severity="error"))
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
	except _parser.ParserNestingLimitError as err:
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
	except _parser.ParserIdentifierLengthError as err:
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
