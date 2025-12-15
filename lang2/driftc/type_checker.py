#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""
Minimal typed checker skeleton for lang2.

This is a real checker scaffold that:
- Allocates ParamId/LocalId/BindingId for bindings.
- Infers types for basic expressions (literals, vars, lets, borrows, calls).
- Produces a TypedFn record with expression TypeIds and binding identity.

It is intentionally small; it will grow to cover full Drift semantics. Borrow
checker integration will consume TypedFn once this matures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Mapping, Tuple

from lang2.driftc import stage1 as H
from lang2.driftc.checker import FnSignature
from lang2.driftc.core.diagnostics import Diagnostic
from lang2.driftc.core.span import Span
from lang2.driftc.core.types_core import TypeId, TypeTable, TypeKind
from lang2.driftc.borrow_checker import (
	DerefProj,
	FieldProj,
	IndexProj,
	Place,
	PlaceBase,
	PlaceKind,
	place_from_expr,
)
from lang2.driftc.method_registry import CallableDecl, CallableRegistry, ModuleId
from lang2.driftc.method_resolver import MethodResolution, ResolutionError, resolve_function_call, resolve_method_call

# Identifier aliases for clarity.
ParamId = int
LocalId = int


@dataclass
class TypedFn:
	"""Typed view of a single function's HIR."""

	name: str
	params: List[ParamId]
	param_bindings: List[int]
	locals: List[LocalId]
	body: H.HBlock
	expr_types: Dict[int, TypeId]  # keyed by id(expr)
	binding_for_var: Dict[int, int]  # keyed by id(HVar)
	binding_types: Dict[int, TypeId]  # binding_id -> TypeId
	binding_names: Dict[int, str]  # binding_id -> name
	call_resolutions: Dict[int, CallableDecl | MethodResolution] = field(default_factory=dict)


@dataclass
class TypeCheckResult:
	"""Result of type checking a function."""

	typed_fn: TypedFn
	diagnostics: List[Diagnostic] = field(default_factory=list)


class TypeChecker:
	"""
	Minimal HIR type checker that assigns binding IDs and basic types.

	This is a skeleton: it understands literals, vars, lets, borrows, calls, and
	a small set of builtin constructs (f-strings, exceptions, DiagnosticValue
	helpers).
	"""

	def __init__(self, type_table: Optional[TypeTable] = None):
		self.type_table = type_table or TypeTable()
		self._uint = self.type_table.ensure_uint()
		self._int = self.type_table.ensure_int()
		self._float = self.type_table.ensure_float()
		self._bool = self.type_table.ensure_bool()
		self._string = self.type_table.ensure_string()
		self._dv = self.type_table.ensure_diagnostic_value()
		self._opt_int = self.type_table.new_optional(self._int)
		self._opt_bool = self.type_table.new_optional(self._bool)
		self._opt_string = self.type_table.new_optional(self._string)
		self._unknown = self.type_table.ensure_unknown()
		# Binding ids (params and locals) share a single id-space.
		#
		# This is critical for correctness: many downstream passes (including the
		# borrow checker) treat `binding_id` as a stable identity. If ParamId and
		# LocalId were allocated from separate counters, their numeric ids could
		# collide (e.g. param 1 and local 1), silently corrupting identity-based
		# maps like `binding_types`.
		self._next_binding_id: int = 1

	def check_function(
		self,
		name: str,
		body: H.HBlock,
		param_types: Mapping[str, TypeId] | None = None,
		call_signatures: Mapping[str, FnSignature] | None = None,
		callable_registry: CallableRegistry | None = None,
		visible_modules: Optional[Tuple[ModuleId, ...]] = None,
		current_module: ModuleId = 0,
	) -> TypeCheckResult:
		scope_env: List[Dict[str, TypeId]] = [dict()]
		scope_bindings: List[Dict[str, int]] = [dict()]
		expr_types: Dict[int, TypeId] = {}
		binding_for_var: Dict[int, int] = {}
		binding_types: Dict[int, TypeId] = {}
		binding_names: Dict[int, str] = {}
		# Binding mutability (val/var) keyed by binding id.
		#
		# MVP borrow rules depend on this:
		#   - `&mut x` requires `x` to be declared mutable (`var`).
		binding_mutable: Dict[int, bool] = {}
		# Binding identity kind (param vs local). This avoids accidental collisions:
		# ParamId and LocalId are allocated from separate counters, so a param and
		# local can share the same numeric id.
		binding_place_kind: Dict[int, PlaceKind] = {}
		# Borrow exclusivity (MVP): tracked within a single statement/expression.
		#
		# Key by Place (not binding id) so this mechanism naturally extends to
		# projections once we support borrowing from `x.field`, `arr[i]`, `*p`.
		#
		# Value is "shared" or "mut". This is intentionally shallow (no lifetimes)
		# but prevents the worst footguns:
		#   - multiple `&x` in a statement is OK
		#   - `&mut x` conflicts with any other borrow of `x` in the same statement
		#   - `&x` conflicts with a prior `&mut x` in the same statement
		borrows_in_stmt: Dict[Place, str] = {}
		diagnostics: List[Diagnostic] = []
		call_resolutions: Dict[int, CallableDecl | MethodResolution] = {}

		params: List[ParamId] = []
		param_bindings: List[int] = []
		locals: List[LocalId] = []

		# Seed parameters if provided.
		for pname, pty in (param_types or {}).items():
			pid = self._alloc_param_id()
			params.append(pid)
			param_bindings.append(pid)
			scope_env[-1][pname] = pty
			scope_bindings[-1][pname] = pid
			binding_types[pid] = pty
			binding_names[pid] = pname
			binding_mutable[pid] = False
			binding_place_kind[pid] = PlaceKind.PARAM

		def record_expr(expr: H.HExpr, ty: TypeId) -> TypeId:
			expr_id = id(expr)
			expr_types[expr_id] = ty
			return ty

		def type_expr(expr: H.HExpr, *, allow_exception_init: bool = False) -> TypeId:
			# Literals.
			if isinstance(expr, H.HLiteralInt):
				return record_expr(expr, self._int)
			if hasattr(H, "HLiteralFloat") and isinstance(expr, getattr(H, "HLiteralFloat")):
				return record_expr(expr, self._float)
			if isinstance(expr, H.HLiteralBool):
				return record_expr(expr, self._bool)
			if isinstance(expr, H.HLiteralString):
				return record_expr(expr, self._string)
			if isinstance(expr, H.HFString):
				# f-strings are sugar that ultimately produce a String.
				#
				# MVP rules (from spec-change request):
				# - Each hole expression must be one of {Bool, Int, Uint, Float, String}.
				# - `:spec` is supported syntactically, but only the empty spec is
				#   accepted for now (future work will validate a richer subset).
				for hole in expr.holes:
					hole_ty = type_expr(hole.expr)
					if hole.spec:
						diagnostics.append(
							Diagnostic(
								message="E-FSTR-BAD-SPEC: non-empty :spec is not supported yet (MVP: empty only)",
								severity="error",
								span=getattr(hole, "loc", Span()),
							)
						)
					if hole_ty not in (self._bool, self._int, self._uint, self._float, self._string):
						pretty = self.type_table.get(hole_ty).name if hole_ty is not None else "Unknown"
						diagnostics.append(
							Diagnostic(
								message=f"E-FSTR-UNSUPPORTED-TYPE: f-string hole value is not formattable in MVP (have {pretty})",
								severity="error",
								span=getattr(hole, "loc", Span()),
							)
						)
				return record_expr(expr, self._string)

			# Names and bindings.
			if isinstance(expr, H.HVar):
				if expr.binding_id is None:
					for scope in reversed(scope_bindings):
						if expr.name in scope:
							expr.binding_id = scope[expr.name]
							break
				for scope in reversed(scope_env):
					if expr.name in scope:
						if expr.binding_id is not None:
							binding_for_var[id(expr)] = expr.binding_id
						return record_expr(expr, scope[expr.name])
				diagnostics.append(
					Diagnostic(
						message=f"unknown variable '{expr.name}'",
						severity="error",
						span=getattr(expr, "loc", Span()),
					)
				)
				return record_expr(expr, self._unknown)

			# Borrow.
			if isinstance(expr, H.HBorrow):
				inner_ty = type_expr(expr.subject)
				# MVP: borrowing is only supported from addressable places.
				#
				# Current support:
				# - locals/params: `&x`, `&mut x`
				# - reborrow through a reference: `&*p`, `&mut *p`
				#
				# Future work: field/index borrows and temporary materialization of rvalues.
				def _base_lookup(hv: object) -> Optional[PlaceBase]:
					bid = getattr(hv, "binding_id", None)
					if bid is None:
						return None
					kind = binding_place_kind.get(bid, PlaceKind.LOCAL)
					name = hv.name if hasattr(hv, "name") else str(hv)
					return PlaceBase(kind=kind, local_id=bid, name=name)

				place = place_from_expr(expr.subject, base_lookup=_base_lookup)
				if place is None:
					diagnostics.append(
						Diagnostic(
							message="borrow operand must be an addressable place in MVP (local/param or deref place)",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)

				# MVP: we accept borrowing from nested projections (`x.field`, `arr[i]`,
				# `(*p).field`, etc.) as long as the operand is a real place.
				#
				# Note: rvalues are rejected above by `place_from_expr` returning None.
				# We intentionally do not allow autoref: callers must write `&x`.

				if expr.is_mut:
					# `&mut x` requires `x` to be `var`.
					#
					# We enforce two invariants:
					#  - If the borrow is from owned storage (no deref projections), the base
					#    binding must be `var`. (Example: `&mut p.x` where `p` is a local.)
					#  - If the borrow goes through a deref projection (reborrow), mutability
					#    comes from the reference being dereferenced (Example: `&mut (*p).x`
					#    where `p: &mut Point`). In that case, the base binding does not need
					#    to be `var` (params are effectively `val`), but the dereferenced
					#    reference must be `&mut`.
					#  - If the place includes a deref projection, the reference being dereferenced
					#    must itself be mutable (`&mut`), i.e. a mutable reborrow.
					has_deref = any(isinstance(p, DerefProj) for p in place.projections)
					if (not has_deref) and place.base.local_id is not None and not binding_mutable.get(
						place.base.local_id, False
					):
						diagnostics.append(
							Diagnostic(
								message="cannot take &mut of an immutable binding; declare it with `var`",
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
					# Detect a deref projection anywhere in the place and validate the corresponding
					# reference expression is `&mut`.
					#
					# We do a conservative check by inspecting the surface operand tree: if the borrow
					# operand contains any unary deref, the pointer being dereferenced must type as `&mut _`.
					def _validate_mutable_derefs(node: H.HExpr) -> None:
						if isinstance(node, H.HUnary) and node.op is H.UnaryOp.DEREF:
							ptr_ty = type_expr(node.expr)
							ptr_def = self.type_table.get(ptr_ty)
							if ptr_def.kind is not TypeKind.REF or not ptr_def.ref_mut:
								diagnostics.append(
									Diagnostic(
										message="cannot take &mut through *p unless p is a mutable reference (&mut T)",
										severity="error",
										span=getattr(expr, "loc", Span()),
									)
								)
							_validate_mutable_derefs(node.expr)
						elif isinstance(node, H.HField):
							_validate_mutable_derefs(node.subject)
						elif isinstance(node, H.HIndex):
							_validate_mutable_derefs(node.subject)
							_validate_mutable_derefs(node.index)

					_validate_mutable_derefs(expr.subject)
					if place in borrows_in_stmt:
						diagnostics.append(
							Diagnostic(
								message="conflicting borrows in the same statement: cannot take &mut while borrowed",
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
					borrows_in_stmt[place] = "mut"
				else:
					if borrows_in_stmt.get(place) == "mut":
						diagnostics.append(
							Diagnostic(
								message="conflicting borrows in the same statement: cannot take & while mutably borrowed",
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
					borrows_in_stmt.setdefault(place, "shared")

				ref_ty = self.type_table.ensure_ref_mut(inner_ty) if expr.is_mut else self.type_table.ensure_ref(inner_ty)
				return record_expr(expr, ref_ty)

			# Calls.
			if isinstance(expr, H.HCall):
				# Always type fn and args first for side-effects/subexpressions.
				#
				# Special-case struct constructors: `Point(1, 2)` uses a call-like
				# surface form but is not a function call. In that case we must *not*
				# type-check `expr.fn` as a normal expression (`Point` is a type name,
				# not a value), otherwise we'd emit a misleading "unknown variable"
				# diagnostic before the constructor path has a chance to fire.
				should_type_fn = True
				if isinstance(expr.fn, H.HVar):
					is_struct_ctor = (
						expr.fn.name in self.type_table.struct_schemas
						and callable_registry is None
						and (call_signatures is None or expr.fn.name not in call_signatures)
					)
					if is_struct_ctor:
						should_type_fn = False
					if callable_registry is not None:
						should_type_fn = False
					elif call_signatures and expr.fn.name in call_signatures:
						should_type_fn = False
				if should_type_fn:
					type_expr(expr.fn)
				arg_types = [type_expr(a) for a in expr.args]

				# Struct constructor: `Point(1, 2)` constructs a `struct Point`.
				#
				# In v1, struct initialization uses a call-like surface form. This is a
				# language-level construct (not a function call) and must work even when
				# a callable registry is present.
				#
				# We only treat the call as a constructor when there is no known callable
				# signature for the same name (to avoid ambiguity if user code later
				# allows a free function named `Point`).
				if (
					(call_signatures is None or not (isinstance(expr.fn, H.HVar) and expr.fn.name in call_signatures))
					and isinstance(expr.fn, H.HVar)
					and expr.fn.name in self.type_table.struct_schemas
				):
					struct_name = expr.fn.name
					struct_id = self.type_table.ensure_named(struct_name)
					struct_def = self.type_table.get(struct_id)
					if struct_def.kind is not TypeKind.STRUCT:
						diagnostics.append(
							Diagnostic(
								message=f"internal: struct schema '{struct_name}' is not a STRUCT TypeId",
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
						return record_expr(expr, self._unknown)
					field_types = list(struct_def.param_types)
					if len(arg_types) != len(field_types):
						diagnostics.append(
							Diagnostic(
								message=f"struct '{struct_name}' constructor expects {len(field_types)} args, got {len(arg_types)}",
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
						return record_expr(expr, struct_id)
					for idx, (have, want) in enumerate(zip(arg_types, field_types)):
						if have != want:
							diagnostics.append(
								Diagnostic(
									message=f"struct '{struct_name}' constructor arg {idx} type mismatch (have {self.type_table.get(have).name}, expected {self.type_table.get(want).name})",
									severity="error",
									span=getattr(expr.args[idx], "loc", Span()),
								)
							)
					return record_expr(expr, struct_id)

				# Try registry-based resolution when available.
				if callable_registry and isinstance(expr.fn, H.HVar):
					try:
						decl = resolve_function_call(
							callable_registry,
							self.type_table,
							name=expr.fn.name,
							arg_types=arg_types,
							visible_modules=visible_modules or (current_module,),
							current_module=current_module,
						)
						call_resolutions[id(expr)] = decl
						return record_expr(expr, decl.signature.result_type)
					except ResolutionError as err:
						diagnostics.append(
							Diagnostic(message=str(err), severity="error", span=getattr(expr, "loc", Span()))
						)
						return record_expr(expr, self._unknown)

				# Fallback: signature map by name.
				if call_signatures and isinstance(expr.fn, H.HVar):
					sig = call_signatures.get(expr.fn.name)
					if sig and sig.return_type_id is not None:
						return record_expr(expr, sig.return_type_id)
				return record_expr(expr, self._unknown)

			if isinstance(expr, H.HMethodCall):
				# Built-in DiagnosticValue helpers are reserved method names and take precedence.
				if expr.method_name in ("as_int", "as_bool", "as_string"):
					recv_ty = type_expr(expr.receiver)
					recv_def = self.type_table.get(recv_ty)
					if recv_def.kind is not TypeKind.DIAGNOSTICVALUE:
						diagnostics.append(
							Diagnostic(
								message=f"{expr.method_name} is only valid on DiagnosticValue",
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
						return record_expr(expr, self._unknown)
					if expr.method_name == "as_int":
						return record_expr(expr, self._opt_int)
					if expr.method_name == "as_bool":
						return record_expr(expr, self._opt_bool)
					if expr.method_name == "as_string":
						return record_expr(expr, self._opt_string)
					return record_expr(expr, self._unknown)

				recv_ty = type_expr(expr.receiver)
				arg_types = [type_expr(a) for a in expr.args]

				if callable_registry:
					try:
						resolution = resolve_method_call(
							callable_registry,
							self.type_table,
							receiver_type=recv_ty,
							method_name=expr.method_name,
							arg_types=arg_types,
							visible_modules=visible_modules or (current_module,),
							current_module=current_module,
						)
						call_resolutions[id(expr)] = resolution
						return record_expr(expr, resolution.decl.signature.result_type)
					except ResolutionError as err:
						diagnostics.append(
							Diagnostic(message=str(err), severity="error", span=getattr(expr, "loc", Span()))
						)
						return record_expr(expr, self._unknown)

				if call_signatures:
					sig = call_signatures.get(expr.method_name)
					if sig and sig.return_type_id is not None:
						return record_expr(expr, sig.return_type_id)
				return record_expr(expr, self._unknown)

			# Field access and indexing.
			if isinstance(expr, H.HField):
				sub_ty = type_expr(expr.subject)
				if expr.name == "attrs":
					diagnostics.append(
						Diagnostic(
							message='attrs must be indexed: use error.attrs["key"]',
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				# Struct fields: `x.field`
				sub_def = self.type_table.get(sub_ty)
				if sub_def.kind is TypeKind.STRUCT:
					info = self.type_table.struct_field(sub_ty, expr.name)
					if info is None:
						diagnostics.append(
							Diagnostic(
								message=f"unknown field '{expr.name}' on struct '{sub_def.name}'",
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
						return record_expr(expr, self._unknown)
					_, field_ty = info
					return record_expr(expr, field_ty)
				return record_expr(expr, self._unknown)

			if isinstance(expr, H.HIndex):
				# Special-case Error.attrs["key"] → DiagnosticValue.
				if isinstance(expr.subject, H.HField) and expr.subject.name == "attrs":
					sub_ty = type_expr(expr.subject.subject)
					key_ty = type_expr(expr.index)
					if self.type_table.get(sub_ty).kind is not TypeKind.ERROR:
						diagnostics.append(
							Diagnostic(
								message="attrs access is only supported on Error values",
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
						return record_expr(expr, self._unknown)
					if self.type_table.get(key_ty).name != "String":
						diagnostics.append(
							Diagnostic(
								message="Error.attrs expects a String key",
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
					return record_expr(expr, self._dv)

				sub_ty = type_expr(expr.subject)
				idx_ty = type_expr(expr.index)
				td = self.type_table.get(sub_ty)
				if idx_ty is not None and idx_ty not in (self._int, self._uint):
					diagnostics.append(
						Diagnostic(
							message="array index must be an integer type",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				if td.kind is TypeKind.ARRAY and td.param_types:
					return record_expr(expr, td.param_types[0])
				diagnostics.append(
					Diagnostic(
						message="indexing requires an Array value",
						severity="error",
						span=getattr(expr, "loc", Span()),
					)
				)
				return record_expr(expr, self._unknown)

			# Disallow implicit setters; attrs require explicit runtime helpers in MIR.
			if isinstance(expr, H.HCall) and isinstance(expr.fn, H.HField) and expr.fn.name == "attrs":
				diagnostics.append(
					Diagnostic(
						message="attrs values must be DiagnosticValue; implicit setters are not supported",
						severity="error",
						span=getattr(expr, "loc", Span()),
					)
				)
				return record_expr(expr, self._unknown)

			# Unary/binary ops (MVP).
			if isinstance(expr, H.HUnary):
				sub_ty = type_expr(expr.expr)
				if expr.op is H.UnaryOp.NEG:
					return record_expr(expr, sub_ty if sub_ty in (self._int, self._float) else self._unknown)
				if expr.op in (H.UnaryOp.NOT,):
					return record_expr(expr, self._bool)
				if expr.op is H.UnaryOp.BIT_NOT:
					return record_expr(expr, sub_ty if sub_ty == self._int else self._unknown)
				return record_expr(expr, self._unknown)

			if isinstance(expr, H.HBinary):
				left_ty = type_expr(expr.left)
				right_ty = type_expr(expr.right)
				if expr.op in (
					H.BinaryOp.ADD,
					H.BinaryOp.SUB,
					H.BinaryOp.MUL,
					H.BinaryOp.DIV,
					H.BinaryOp.MOD,
					H.BinaryOp.BIT_AND,
					H.BinaryOp.BIT_OR,
					H.BinaryOp.BIT_XOR,
					H.BinaryOp.SHL,
					H.BinaryOp.SHR,
				):
					if left_ty == self._int and right_ty == self._int:
						return record_expr(expr, self._int)
					return record_expr(expr, self._unknown)
				if expr.op in (
					H.BinaryOp.EQ,
					H.BinaryOp.NE,
					H.BinaryOp.LT,
					H.BinaryOp.LE,
					H.BinaryOp.GT,
					H.BinaryOp.GE,
				):
					return record_expr(expr, self._bool)
				if expr.op in (H.BinaryOp.AND, H.BinaryOp.OR):
					return record_expr(expr, self._bool)
				return record_expr(expr, self._unknown)

			# Arrays/ternary.
			if isinstance(expr, H.HArrayLiteral):
				elem_types = [type_expr(e) for e in expr.elements]
				if elem_types and all(t == elem_types[0] for t in elem_types):
					return record_expr(expr, self.type_table.new_array(elem_types[0]))
				return record_expr(expr, self._unknown)

			if isinstance(expr, H.HTernary):
				type_expr(expr.cond)
				then_ty = type_expr(expr.then_expr)
				else_ty = type_expr(expr.else_expr)
				return record_expr(expr, then_ty if then_ty == else_ty else self._unknown)

			# Exception constructors are only legal as throw payloads.
			if isinstance(expr, H.HExceptionInit):
				if not allow_exception_init:
					diagnostics.append(
						Diagnostic(
							message="exception constructors are only valid as throw payloads",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				from lang2.driftc.core.exception_ctor_args import KwArg as _KwArg, resolve_exception_ctor_args

				schemas: dict[str, tuple[str, list[str]]] = getattr(self.type_table, "exception_schemas", {}) or {}
				schema = schemas.get(expr.event_fqn)
				decl_fields: list[str] | None
				if schema is None:
					decl_fields = None
				else:
					_decl_fqn, decl_fields = schema

				resolved, diags = resolve_exception_ctor_args(
					event_fqn=expr.event_fqn,
					declared_fields=decl_fields,
					pos_args=[(a, getattr(a, "loc", Span())) for a in expr.pos_args],
					kw_args=[
						_KwArg(name=kw.name, value=kw.value, name_span=getattr(kw, "loc", Span()))
						for kw in expr.kw_args
					],
					span=getattr(expr, "loc", Span()),
				)
				diagnostics.extend(diags)

				values_to_validate = [v for _name, v in resolved]
				if decl_fields is None:
					# Unknown schema: we cannot map positional args to names, but we
					# still validate that provided values are DV or supported literals.
					values_to_validate = list(expr.pos_args) + [kw.value for kw in expr.kw_args]

				for val_expr in values_to_validate:
					val_ty = type_expr(val_expr)
					is_primitive_literal = isinstance(val_expr, (H.HLiteralInt, H.HLiteralBool, H.HLiteralString))
					if val_ty != self._dv and not is_primitive_literal:
						diagnostics.append(
							Diagnostic(
								message=(
									"exception field value must be a DiagnosticValue or a primitive literal "
									"(Int/Bool/String)"
								),
								severity="error",
								span=getattr(val_expr, "loc", Span()),
							)
						)
				return record_expr(expr, self._dv)

			# DiagnosticValue constructors.
			if isinstance(expr, H.HDVInit):
				arg_types = [type_expr(a) for a in expr.args]
				if expr.args:
					# Only zero-arg (missing) or single-arg primitive DV ctors are supported in v1.
					if len(expr.args) > 1:
						diagnostics.append(
							Diagnostic(
								message="DiagnosticValue constructors support at most one argument in v1",
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
						return record_expr(expr, self._unknown)
					inner_ty = arg_types[0]
					if inner_ty not in (self._int, self._bool, self._string):
						diagnostics.append(
							Diagnostic(
								message="unsupported DiagnosticValue constructor argument type",
								severity="error",
								span=getattr(expr.args[0], "loc", Span()),
							)
						)
						return record_expr(expr, self._unknown)
				return record_expr(expr, self._dv)

			# Result/try sugar.
			if isinstance(expr, H.HResultOk):
				ok_ty = type_expr(expr.value)
				err_ty = self._unknown
				return record_expr(expr, self.type_table.new_fnresult(ok_ty, err_ty))
			if isinstance(expr, H.HTryResult):
				return record_expr(expr, type_expr(expr.expr))

			# Fallback: unknown type.
			return record_expr(expr, self._unknown)

		catch_depth = 0

		def type_stmt(stmt: H.HStmt) -> None:
			nonlocal catch_depth
			# Borrow conflicts are diagnosed within a single statement.
			borrows_in_stmt.clear()
			if isinstance(stmt, H.HLet):
				if stmt.binding_id is None:
					stmt.binding_id = self._alloc_local_id()
				locals.append(stmt.binding_id)
				val_ty = type_expr(stmt.value)
				scope_env[-1][stmt.name] = val_ty
				scope_bindings[-1][stmt.name] = stmt.binding_id
				binding_types[stmt.binding_id] = val_ty
				binding_names[stmt.binding_id] = stmt.name
				binding_mutable[stmt.binding_id] = bool(getattr(stmt, "is_mutable", False))
				binding_place_kind[stmt.binding_id] = PlaceKind.LOCAL
			elif isinstance(stmt, H.HAssign):
				type_expr(stmt.value)
				type_expr(stmt.target)
			elif isinstance(stmt, H.HExprStmt):
				type_expr(stmt.expr)
			elif isinstance(stmt, H.HReturn):
				if stmt.value is not None:
					type_expr(stmt.value)
			elif isinstance(stmt, H.HIf):
				type_expr(stmt.cond)
				type_block(stmt.then_block)
				if stmt.else_block:
					type_block(stmt.else_block)
			elif isinstance(stmt, H.HLoop):
				type_block(stmt.body)
			elif isinstance(stmt, H.HTry):
				type_block(stmt.body)
				for arm in stmt.catches:
					catch_depth += 1
					type_block(arm.block)
					catch_depth -= 1
			elif isinstance(stmt, H.HThrow):
				if isinstance(stmt.value, H.HMethodCall) and stmt.value.method_name == "unwrap_err":
					type_expr(stmt.value)
				elif not isinstance(stmt.value, H.HExceptionInit):
					diagnostics.append(
						Diagnostic(
							message="throw payload must be an exception constructor",
							severity="error",
							span=getattr(stmt, "loc", Span()),
						)
					)
					type_expr(stmt.value)
				else:
					type_expr(stmt.value, allow_exception_init=True)
			elif isinstance(stmt, H.HRethrow):
				# Valid only inside a catch; outside catches it is reported here.
				if catch_depth == 0:
					diagnostics.append(
						Diagnostic(
							message="rethrow is only valid inside a catch block",
							severity="error",
							span=getattr(stmt, "loc", Span()),
						)
					)
			# HBreak/HContinue are typeless here.

		def type_block(block: H.HBlock) -> None:
			scope_env.append(dict())
			scope_bindings.append(dict())
			try:
				for s in block.statements:
					type_stmt(s)
			finally:
				scope_env.pop()
				scope_bindings.pop()

		type_block(body)

		typed = TypedFn(
			name=name,
			params=params,
			param_bindings=param_bindings,
			locals=locals,
			body=body,
			expr_types={ref: ty for ref, ty in expr_types.items()},
			binding_for_var=binding_for_var,
			binding_types=binding_types,
			binding_names=binding_names,
			call_resolutions=call_resolutions,
		)
		return TypeCheckResult(typed_fn=typed, diagnostics=diagnostics)

	def _alloc_param_id(self) -> ParamId:
		pid = self._next_binding_id
		self._next_binding_id += 1
		return pid

	def _alloc_local_id(self) -> LocalId:
		lid = self._next_binding_id
		self._next_binding_id += 1
		return lid
