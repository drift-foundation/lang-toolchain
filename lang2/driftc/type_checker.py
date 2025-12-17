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
from lang2.driftc.core.type_resolve_common import resolve_opaque_type
from lang2.driftc.borrow_checker import (
	DerefProj,
	FieldProj,
	IndexProj,
	Place,
	PlaceBase,
	PlaceKind,
	place_from_expr,
	places_overlap,
)
from lang2.driftc.method_registry import CallableDecl, CallableRegistry, ModuleId
from lang2.driftc.method_resolver import MethodResolution, ResolutionError, resolve_function_call, resolve_method_call
from lang2.driftc.core.iter_intrinsics import ensure_array_iter_struct, is_array_iter_struct

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
	binding_mutable: Dict[int, bool]  # binding_id -> declared var?
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
		self._void = self.type_table.ensure_void()
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
		return_type: TypeId | None = None,
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
		# Ref origin tracking (MVP escape policy):
		#
		# When a binding has a reference type, record whether it is ultimately
		# derived from a single reference *parameter* binding. This lets us enforce
		# "return refs only derived from a ref param" without a full lifetime model.
		#
		# Value is the binding_id of the originating ref param, or None when the
		# reference points at local/temporary storage.
		ref_origin_param: Dict[int, Optional[int]] = {}
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

		# Precompute constructor-name visibility for diagnostics.
		#
		# MVP constructor resolution rule (work/variant/work-progress.md):
		# - Constructors are unqualified identifiers.
		# - Constructor calls in expression position require an *expected variant type*.
		# - Without an expected type, the compiler diagnoses instead of guessing.
		ctor_to_variant_bases: dict[str, list[TypeId]] = {}
		for base_id, schema in getattr(self.type_table, "variant_schemas", {}).items():
			for arm in schema.arms:
				ctor_to_variant_bases.setdefault(arm.name, []).append(base_id)

		def type_expr(
			expr: H.HExpr,
			*,
			allow_exception_init: bool = False,
			used_as_value: bool = True,
			expected_type: TypeId | None = None,
		) -> TypeId:
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

			# `match` expression (expression-only in MVP, but may appear in statement
			# position as an ExprStmt where the result is ignored).
			if hasattr(H, "HMatchExpr") and isinstance(expr, getattr(H, "HMatchExpr")):
				scrut_ty = type_expr(expr.scrutinee)
				inst = None
				if scrut_ty is not None:
					try:
						td_scrut = self.type_table.get(scrut_ty)
					except Exception:
						td_scrut = None
					if td_scrut is not None and td_scrut.kind is not TypeKind.VARIANT:
						diagnostics.append(
							Diagnostic(
								message="match scrutinee must be a variant type",
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
					if td_scrut is not None and td_scrut.kind is TypeKind.VARIANT:
						inst = self.type_table.get_variant_instance(scrut_ty)

				seen_default = False
				seen_default_span: Span | None = None
				seen_ctors: set[str] = set()
				result_ty: TypeId | None = None

				for idx, arm in enumerate(expr.arms):
					if arm.ctor is None:
						# default arm
						if seen_default:
							diagnostics.append(
								Diagnostic(
									message="match default arm may appear at most once",
									severity="error",
									span=getattr(arm, "loc", Span()),
								)
							)
						seen_default = True
						seen_default_span = getattr(arm, "loc", Span())
					else:
						if seen_default:
							diagnostics.append(
								Diagnostic(
									message="match arms after default are unreachable",
									severity="error",
									span=getattr(arm, "loc", Span()),
								)
							)
						if arm.ctor in seen_ctors:
							diagnostics.append(
								Diagnostic(
									message=f"duplicate match arm for constructor '{arm.ctor}'",
									severity="error",
									span=getattr(arm, "loc", Span()),
								)
							)
						seen_ctors.add(arm.ctor)

					# Type-check arm body under a scope that includes constructor binders.
					scope_env.append(dict())
					scope_bindings.append(dict())
					try:
						if arm.ctor is not None and inst is not None:
							arm_def = inst.arms_by_name.get(arm.ctor)
							if arm_def is None:
								diagnostics.append(
									Diagnostic(
										message=f"unknown constructor '{arm.ctor}' for this variant",
										severity="error",
										span=getattr(arm, "loc", Span()),
									)
								)
							else:
								if len(arm.binders) != len(arm_def.field_types):
									diagnostics.append(
										Diagnostic(
											message=(
												f"constructor pattern '{arm.ctor}' expects {len(arm_def.field_types)} binders, got {len(arm.binders)}"
											),
											severity="error",
											span=getattr(arm, "loc", Span()),
										)
									)
								for bname, bty in zip(arm.binders, arm_def.field_types):
									bid = self._alloc_local_id()
									locals.append(bid)
									scope_env[-1][bname] = bty
									scope_bindings[-1][bname] = bid
									binding_types[bid] = bty
									binding_names[bid] = bname
									binding_mutable[bid] = False
									binding_place_kind[bid] = PlaceKind.LOCAL

						type_block(arm.block)

						arm_value_ty: TypeId | None = None
						if arm.result is not None:
							arm_value_ty = type_expr(arm.result)
						elif used_as_value:
							# Allow diverging arms to omit a value in MVP. We treat a block as
							# diverging when it ends with a terminator statement.
							last = arm.block.statements[-1] if arm.block.statements else None
							diverges = isinstance(last, (H.HReturn, H.HBreak, H.HContinue, H.HThrow, H.HRethrow))
							if not diverges:
								diagnostics.append(
									Diagnostic(
										message="E-MATCH-ARM-NO-VALUE: match arm must end with an expression when match result is used",
										severity="error",
										span=getattr(arm, "loc", Span()),
									)
								)
						if used_as_value and arm_value_ty is not None:
							if result_ty is None:
								result_ty = arm_value_ty
							elif result_ty != arm_value_ty:
								diagnostics.append(
									Diagnostic(
										message=(
											"E-MATCH-ARM-TYPE: match arms must produce the same type when match result is used "
											f"(have {self.type_table.get(arm_value_ty).name}, expected {self.type_table.get(result_ty).name})"
										),
										severity="error",
										span=getattr(arm, "loc", Span()),
									)
								)
					finally:
						scope_env.pop()
						scope_bindings.pop()

				# Non-exhaustive matches require a default arm (MVP rule).
				if inst is not None and not seen_default:
					all_ctors = set(inst.arms_by_name.keys())
					if seen_ctors != all_ctors:
						missing = ", ".join(sorted(all_ctors - seen_ctors))
						diagnostics.append(
							Diagnostic(
								message=f"E-MATCH-NONEXHAUSTIVE: non-exhaustive match must include default arm (missing: {missing})",
								severity="error",
								span=getattr(expr, "loc", Span()) if seen_default_span is None else seen_default_span,
							)
						)

				if not used_as_value:
					return record_expr(expr, self._void)
				if result_ty is None:
					diagnostics.append(
						Diagnostic(
							message="E-MATCH-NO-VALUE: match result is used but no arm produces a value",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				return record_expr(expr, result_ty)

			# Borrow.
			if isinstance(expr, H.HBorrow):
				# Guardrail: do not materialize `&mut (move x)` into a temp. This would
				# turn an explicit ownership transfer into an implicit "store then
				# borrow" pattern, which is a semantic expansion we want to avoid.
				#
				# Instead, reject at type-check time with a targeted diagnostic.
				def _contains_move(node: H.HExpr) -> bool:
					if hasattr(H, "HMove") and isinstance(node, getattr(H, "HMove")):
						return True
					if isinstance(node, H.HUnary):
						return _contains_move(node.expr)
					if isinstance(node, H.HBinary):
						return _contains_move(node.left) or _contains_move(node.right)
					if isinstance(node, H.HTernary):
						return _contains_move(node.cond) or _contains_move(node.then_expr) or _contains_move(node.else_expr)
					if isinstance(node, H.HCall):
						return (
							_contains_move(node.fn)
							or any(_contains_move(a) for a in node.args)
							or any(_contains_move(k.value) for k in getattr(node, "kwargs", []) or [])
						)
					if isinstance(node, H.HMethodCall):
						return (
							_contains_move(node.receiver)
							or any(_contains_move(a) for a in node.args)
							or any(_contains_move(k.value) for k in getattr(node, "kwargs", []) or [])
						)
					if isinstance(node, H.HField):
						return _contains_move(node.subject)
					if isinstance(node, H.HIndex):
						return _contains_move(node.subject) or _contains_move(node.index)
					if isinstance(node, getattr(H, "HPlaceExpr", ())):
						# Canonical places cannot contain moves in their base/projections.
						return False
					if isinstance(node, H.HArrayLiteral):
						return any(_contains_move(e) for e in node.elements)
					if isinstance(node, H.HDVInit):
						return any(_contains_move(a) for a in node.args)
					if isinstance(node, H.HExceptionInit):
						return any(_contains_move(a) for a in node.pos_args) or any(_contains_move(k.value) for k in node.kw_args)
					if isinstance(node, getattr(H, "HTryExpr", ())):
						if _contains_move(node.attempt):
							return True
						for arm in node.arms:
							if any(_contains_move(s.expr) for s in arm.block.statements if isinstance(s, H.HExprStmt)):
								return True
							if arm.result is not None and _contains_move(arm.result):
								return True
						return False
					return False

				if expr.is_mut and _contains_move(expr.subject):
					diagnostics.append(
						Diagnostic(
							message="cannot take &mut of an expression containing move; assign to a var first",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)

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
					# We do a conservative check:
					#  - For canonical `HPlaceExpr` operands, walk projections and ensure each deref
					#    happens through `&mut`.
					#  - For legacy tree-shaped operands (`HUnary(DEREF, ...)`), walk the tree.
					def _validate_mutable_derefs(node: H.HExpr) -> None:
						if hasattr(H, "HPlaceExpr") and isinstance(node, getattr(H, "HPlaceExpr")):
							cur = type_expr(node.base)
							for pr in node.projections:
								if isinstance(pr, H.HPlaceDeref):
									ptr_def = self.type_table.get(cur)
									if ptr_def.kind is not TypeKind.REF or not ptr_def.ref_mut:
										diagnostics.append(
											Diagnostic(
												message="cannot take &mut through *p unless p is a mutable reference (&mut T)",
												severity="error",
												span=getattr(expr, "loc", Span()),
											)
										)
										return
									if ptr_def.param_types:
										cur = ptr_def.param_types[0]
								elif isinstance(pr, H.HPlaceField):
									td = self.type_table.get(cur)
									if td.kind is TypeKind.STRUCT:
										info = self.type_table.struct_field(cur, pr.name)
										if info is not None:
											_, cur = info
								elif isinstance(pr, H.HPlaceIndex):
									td = self.type_table.get(cur)
									if td.kind is TypeKind.ARRAY and td.param_types:
										cur = td.param_types[0]
							return
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

			# Explicit move.
			#
			# `move <place>` is a surface marker for ownership transfer. For MVP we
			# keep it deliberately strict:
			# - the operand must be an addressable place (same as borrow),
			# - the operand must be a *plain* binding (no projections) to avoid
			#   partial-move semantics before we have a real lifetime/ownership model.
			#
			# The borrow checker enforces:
			# - no moving while borrowed, and
			# - use-after-move until reinitialization.
			if hasattr(H, "HMove") and isinstance(expr, getattr(H, "HMove")):
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
							message="move operand must be an addressable place in MVP (local/param)",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				if place.projections:
					diagnostics.append(
						Diagnostic(
							message="move of a projected place is not supported in MVP; move a local/param or use swap/replace",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				if place.base.local_id is not None and not binding_mutable.get(place.base.local_id, False):
					diagnostics.append(
						Diagnostic(
							message="move requires an owned mutable binding declared with var",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				inner_ty = type_expr(expr.subject)
				if inner_ty is not None:
					td = self.type_table.get(inner_ty)
					if td.kind is TypeKind.REF:
						diagnostics.append(
							Diagnostic(
								message="cannot move from a reference type; move requires owned storage",
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
						return record_expr(expr, self._unknown)
				return record_expr(expr, inner_ty)

			# Calls.
			if isinstance(expr, H.HCall):
				# Variant constructor call in expression position.
				#
				# MVP rule: constructor calls require an *expected* variant type from
				# context (annotation, parameter type, return type, etc.). Without an
				# expected type we do not guess which variant the constructor belongs to.
				if isinstance(expr.fn, H.HVar) and expected_type is not None:
					try:
						exp_def = self.type_table.get(expected_type)
					except Exception:
						exp_def = None
					if exp_def is not None and exp_def.kind is TypeKind.VARIANT:
						inst = self.type_table.get_variant_instance(expected_type)
						if inst is not None and expr.fn.name in inst.arms_by_name:
							arm_def = inst.arms_by_name[expr.fn.name]
							kw_pairs = getattr(expr, "kwargs", []) or []
							if kw_pairs:
								diagnostics.append(
									Diagnostic(
										message="variant constructors do not support keyword arguments in MVP",
										severity="error",
										span=getattr(kw_pairs[0], "loc", getattr(expr, "loc", Span())),
									)
								)
							if len(expr.args) != len(arm_def.field_types):
								diagnostics.append(
									Diagnostic(
										message=(
											f"constructor '{arm_def.name}' expects {len(arm_def.field_types)} arguments, got {len(expr.args)}"
										),
										severity="error",
										span=getattr(expr, "loc", Span()),
									)
								)
							for arg, want in zip(expr.args, arm_def.field_types):
								type_expr(arg, expected_type=want)
							return record_expr(expr, expected_type)

				# Always type fn and args first for side-effects/subexpressions.
				#
				# Special-case struct constructors: `Point(1, 2)` uses a call-like
				# surface form but is not a function call. In that case we must *not*
				# type-check `expr.fn` as a normal expression (`Point` is a type name,
				# not a value), otherwise we'd emit a misleading "unknown variable"
				# diagnostic before the constructor path has a chance to fire.
				should_type_fn = True
				if isinstance(expr.fn, H.HVar):
					# Builtins that look like calls but are not normal function values.
					# We must not type-check `expr.fn` as a variable, otherwise we'd emit
					# misleading "unknown variable" diagnostics before the builtin path
					# fires.
					if expr.fn.name in ("swap", "replace"):
						should_type_fn = False
					is_struct_ctor = (
						expr.fn.name in self.type_table.struct_schemas
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
				kw_pairs = getattr(expr, "kwargs", []) or []
				# When a call signature is known by name, use its parameter types as
				# expected types for arguments. This enables constructor calls inside
				# arguments, e.g. `takes_opt(Some(1))` where `takes_opt` expects
				# `Optional<Int>`.
				arg_types: list[TypeId] = []
				if (
					isinstance(expr.fn, H.HVar)
					and call_signatures
					and expr.fn.name in call_signatures
					and call_signatures[expr.fn.name].param_type_ids is not None
				):
					expected_params = call_signatures[expr.fn.name].param_type_ids or []
					for i, a in enumerate(expr.args):
						want = expected_params[i] if i < len(expected_params) else None
						arg_types.append(type_expr(a, expected_type=want))
				else:
					arg_types = [type_expr(a) for a in expr.args]
				kw_types = [type_expr(k.value) for k in kw_pairs]

				# Builtins: swap/replace operate on *places*.
				#
				# These are part of the borrow/move MVP story: they let users extract or
				# exchange values in-place without creating "moved-from holes" in
				# containers/structs. They are validated here (with spans) and lowered as
				# dedicated MIR patterns later; they are not normal function calls.
				if isinstance(expr.fn, H.HVar) and expr.fn.name in ("swap", "replace"):
					if kw_pairs:
						diagnostics.append(
							Diagnostic(
								message=f"{expr.fn.name} does not support keyword arguments",
								severity="error",
								span=kw_pairs[0].loc if hasattr(kw_pairs[0], "loc") else getattr(expr, "loc", Span()),
							)
						)
					def _base_lookup(hv: object) -> Optional[PlaceBase]:
						bid = getattr(hv, "binding_id", None)
						if bid is None:
							return None
						kind = binding_place_kind.get(bid, PlaceKind.LOCAL)
						name = hv.name if hasattr(hv, "name") else str(hv)
						return PlaceBase(kind=kind, local_id=bid, name=name)

					def _require_writable_place(place_expr: H.HExpr, span: Span) -> None:
						place = place_from_expr(place_expr, base_lookup=_base_lookup)
						if place is None:
							return
						# If the place includes a deref projection, mutability is provided by
						# the reference type (`&mut`) rather than the base binding being `var`.
						has_deref = any(isinstance(p, DerefProj) for p in place.projections)
						if not has_deref and place.base.local_id is not None and not binding_mutable.get(
							place.base.local_id, False
						):
							diagnostics.append(
								Diagnostic(
									message="write requires an owned mutable binding declared with var",
									severity="error",
									span=span,
								)
							)
						# Validate deref projections are through `&mut` refs.
						if has_deref and hasattr(H, "HPlaceExpr") and isinstance(place_expr, getattr(H, "HPlaceExpr")):
							cur = type_expr(place_expr.base)
							for pr in place_expr.projections:
								if isinstance(pr, H.HPlaceDeref):
									if cur is None:
										break
									ptr_def = self.type_table.get(cur)
									if ptr_def.kind is not TypeKind.REF or not ptr_def.ref_mut:
										diagnostics.append(
											Diagnostic(
												message=(
													"cannot write through *p unless p is a mutable reference (&mut T)"
												),
												severity="error",
												span=span,
											)
										)
										return
									if ptr_def.param_types:
										cur = ptr_def.param_types[0]
									continue
								if isinstance(pr, H.HPlaceField):
									if cur is None:
										break
									td = self.type_table.get(cur)
									if td.kind is TypeKind.STRUCT:
										info = self.type_table.struct_field(cur, pr.name)
										if info is not None:
											_, cur = info
									continue
								if isinstance(pr, H.HPlaceIndex):
									if cur is None:
										break
									td = self.type_table.get(cur)
									if td.kind is TypeKind.ARRAY and td.param_types:
										cur = td.param_types[0]
									continue

					name = expr.fn.name
					if name == "swap":
						if len(expr.args) != 2:
							diagnostics.append(
								Diagnostic(
									message="swap expects exactly 2 arguments",
									severity="error",
									span=getattr(expr, "loc", Span()),
								)
							)
							return record_expr(expr, self._void)
						a, b = expr.args
						pa = place_from_expr(a, base_lookup=_base_lookup)
						pb = place_from_expr(b, base_lookup=_base_lookup)
						if pa is None:
							diagnostics.append(
								Diagnostic(
									message="swap argument 0 must be an addressable place",
									severity="error",
									span=getattr(a, "loc", getattr(expr, "loc", Span())),
								)
							)
						if pb is None:
							diagnostics.append(
								Diagnostic(
									message="swap argument 1 must be an addressable place",
									severity="error",
									span=getattr(b, "loc", getattr(expr, "loc", Span())),
								)
							)
						if pa is not None:
							_require_writable_place(a, getattr(a, "loc", getattr(expr, "loc", Span())))
						if pb is not None:
							_require_writable_place(b, getattr(b, "loc", getattr(expr, "loc", Span())))
						if arg_types[0] is not None and arg_types[1] is not None and arg_types[0] != arg_types[1]:
							diagnostics.append(
								Diagnostic(
									message="swap requires both places to have the same type",
									severity="error",
									span=getattr(expr, "loc", Span()),
								)
							)
						if pa is not None and pb is not None and places_overlap(pa, pb):
							diagnostics.append(
								Diagnostic(
									message="swap operands must be distinct non-overlapping places",
									severity="error",
									span=getattr(expr, "loc", Span()),
								)
							)
						return record_expr(expr, self._void)
					# replace(place, new_value) -> old_value
					if name == "replace":
						if len(expr.args) != 2:
							diagnostics.append(
								Diagnostic(
									message="replace expects exactly 2 arguments",
									severity="error",
									span=getattr(expr, "loc", Span()),
								)
							)
							return record_expr(expr, self._unknown)
						place_expr, new_val_expr = expr.args
						place = place_from_expr(place_expr, base_lookup=_base_lookup)
						if place is None:
							diagnostics.append(
								Diagnostic(
									message="replace argument 0 must be an addressable place",
									severity="error",
									span=getattr(place_expr, "loc", getattr(expr, "loc", Span())),
								)
							)
							return record_expr(expr, self._unknown)
						_require_writable_place(place_expr, getattr(place_expr, "loc", getattr(expr, "loc", Span())))
						place_ty = arg_types[0]
						new_ty = arg_types[1]
						if place_ty is not None and new_ty is not None and place_ty != new_ty:
							diagnostics.append(
								Diagnostic(
									message="replace requires the new value to have the same type as the place",
									severity="error",
									span=getattr(new_val_expr, "loc", getattr(expr, "loc", Span())),
								)
							)
						return record_expr(expr, place_ty if place_ty is not None else self._unknown)

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
					field_names = self.type_table.struct_schemas[struct_name][1]
					field_types = list(struct_def.param_types)
					if len(field_names) != len(field_types):
						diagnostics.append(
							Diagnostic(
								message=f"internal: struct '{struct_name}' schema/type mismatch",
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
						return record_expr(expr, struct_id)
					if len(arg_types) > len(field_types):
						diagnostics.append(
							Diagnostic(
								message=f"struct '{struct_name}' constructor expects {len(field_types)} args, got {len(arg_types)}",
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
						return record_expr(expr, struct_id)

					# Map positional + keyword args to fields in declaration order.
					mapped_types: list[Optional[TypeId]] = [None] * len(field_types)
					mapped_spans: list[Span] = [getattr(expr, "loc", Span())] * len(field_types)

					for idx, (ty, arg_expr) in enumerate(zip(arg_types, expr.args)):
						mapped_types[idx] = ty
						mapped_spans[idx] = getattr(arg_expr, "loc", getattr(expr, "loc", Span()))

					for kw, kw_ty in zip(kw_pairs, kw_types):
						try:
							field_idx = field_names.index(kw.name)
						except ValueError:
							diagnostics.append(
								Diagnostic(
									message=f"unknown field '{kw.name}' for struct '{struct_name}'",
									severity="error",
									span=getattr(kw, "loc", getattr(expr, "loc", Span())),
								)
							)
							continue
						if field_idx < len(arg_types):
							diagnostics.append(
								Diagnostic(
									message=f"duplicate field '{kw.name}' for struct '{struct_name}' (already provided positionally)",
									severity="error",
									span=getattr(kw, "loc", getattr(expr, "loc", Span())),
								)
							)
							continue
						if mapped_types[field_idx] is not None:
							diagnostics.append(
								Diagnostic(
									message=f"duplicate field '{kw.name}' for struct '{struct_name}'",
									severity="error",
									span=getattr(kw, "loc", getattr(expr, "loc", Span())),
								)
							)
							continue
						mapped_types[field_idx] = kw_ty
						mapped_spans[field_idx] = getattr(kw.value, "loc", getattr(expr, "loc", Span()))

					for idx, (have, want) in enumerate(zip(mapped_types, field_types)):
						if have is None:
							diagnostics.append(
								Diagnostic(
									message=f"missing field '{field_names[idx]}' for struct '{struct_name}' constructor",
									severity="error",
									span=getattr(expr, "loc", Span()),
								)
							)
							continue
						if have != want:
							diagnostics.append(
								Diagnostic(
									message=(
										f"struct '{struct_name}' field '{field_names[idx]}' type mismatch "
										f"(have {self.type_table.get(have).name}, expected {self.type_table.get(want).name})"
									),
									severity="error",
									span=mapped_spans[idx],
								)
							)
					return record_expr(expr, struct_id)

				if kw_pairs:
					diagnostics.append(
						Diagnostic(
							message="keyword arguments are only supported for struct constructors in MVP",
							severity="error",
							span=getattr(kw_pairs[0], "loc", getattr(expr, "loc", Span())),
						)
					)
					return record_expr(expr, self._unknown)

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
						# If this call looks like a variant constructor invocation (unqualified
						# constructor name) but we have no expected variant type, prefer a
						# targeted diagnostic over a generic "no overload" message.
						#
						# We only do this when there are *no* visible free-function candidates
						# with the same name. If user code declares a real function named
						# `Some`, we should report overload errors for that function instead.
						if expected_type is None and expr.fn.name in ctor_to_variant_bases:
							candidates = callable_registry.get_free_candidates(
								name=expr.fn.name,
								visible_modules=visible_modules or (current_module,),
								include_private_in=current_module,
							)
							if not candidates:
								diagnostics.append(
									Diagnostic(
										message=(
											"E-CTOR-EXPECTED-TYPE: constructor call requires an expected variant type; "
											"add a type annotation or call a function that expects this variant"
										),
										severity="error",
										span=getattr(expr, "loc", Span()),
									)
								)
								return record_expr(expr, self._unknown)
						diagnostics.append(
							Diagnostic(message=str(err), severity="error", span=getattr(expr, "loc", Span()))
						)
						return record_expr(expr, self._unknown)

				# Fallback: signature map by name.
				if call_signatures and isinstance(expr.fn, H.HVar):
					sig = call_signatures.get(expr.fn.name)
					if sig and sig.return_type_id is not None:
						return record_expr(expr, sig.return_type_id)
				# Constructor calls without an expected variant type are rejected in MVP.
				if isinstance(expr.fn, H.HVar) and expected_type is None:
					if expr.fn.name in ctor_to_variant_bases:
						diagnostics.append(
							Diagnostic(
								message=(
									"E-CTOR-EXPECTED-TYPE: constructor call requires an expected variant type; "
									"add a type annotation or call a function that expects this variant"
								),
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
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

				if getattr(expr, "kwargs", None):
					first = (getattr(expr, "kwargs", []) or [None])[0]
					diagnostics.append(
						Diagnostic(
							message="keyword arguments are not supported for method calls in MVP",
							severity="error",
							span=getattr(first, "loc", getattr(expr, "loc", Span())),
						)
					)
					return record_expr(expr, self._unknown)

				recv_ty = type_expr(expr.receiver)
				arg_types = [type_expr(a) for a in expr.args]

				# Iterator protocol intrinsics for `for` desugaring (MVP).
				#
				# The stage1 lowering desugars:
				#   for x in expr { body }
				# into:
				#   let it = expr.iter()
				#   loop { match it.next() { Some(x) => { body } default => { break } } }
				#
				# Modules/traits are not implemented yet, so `.iter()` / `.next()` are
				# compiler intrinsics on arrays.
				if expr.method_name == "iter" and not expr.args:
					recv_def = self.type_table.get(recv_ty)
					if recv_def.kind is TypeKind.ARRAY:
						iter_ty = ensure_array_iter_struct(recv_ty, self.type_table)
						return record_expr(expr, iter_ty)
				if expr.method_name == "next" and not expr.args:
					if is_array_iter_struct(recv_ty, self.type_table):
						iter_def = self.type_table.get(recv_ty)
						if not iter_def.param_types or len(iter_def.param_types) != 2:
							diagnostics.append(
								Diagnostic(
									message="internal array iterator type is malformed (compiler bug)",
									severity="error",
									span=getattr(expr, "loc", Span()),
								)
							)
							return record_expr(expr, self._unknown)
						arr_ty = iter_def.param_types[0]
						arr_def = self.type_table.get(arr_ty)
						if arr_def.kind is not TypeKind.ARRAY or not arr_def.param_types:
							diagnostics.append(
								Diagnostic(
									message="internal array iterator type does not contain Array<T> (compiler bug)",
									severity="error",
									span=getattr(expr, "loc", Span()),
								)
							)
							return record_expr(expr, self._unknown)
						elem_ty = arr_def.param_types[0]
						opt_base = self.type_table.ensure_named("Optional")
						opt_def = self.type_table.get(opt_base)
						if opt_def.kind is not TypeKind.VARIANT:
							diagnostics.append(
								Diagnostic(
									message="Optional<T> variant base is missing (compiler bug)",
									severity="error",
									span=getattr(expr, "loc", Span()),
								)
							)
							return record_expr(expr, self._unknown)
						return record_expr(expr, self.type_table.ensure_instantiated(opt_base, [elem_ty]))

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
			#
			# Canonical place expressions (`HPlaceExpr`) denote addressable storage
			# locations. In expression position they behave like lvalues: their type is
			# the type of the referenced storage location.
			if hasattr(H, "HPlaceExpr") and isinstance(expr, getattr(H, "HPlaceExpr")):
				current_ty = type_expr(expr.base)
				for proj in expr.projections:
					if isinstance(proj, H.HPlaceDeref):
						td = self.type_table.get(current_ty)
						if td.kind is not TypeKind.REF or not td.param_types:
							diagnostics.append(
								Diagnostic(
									message="deref requires a reference value",
									severity="error",
									span=getattr(expr, "loc", Span()),
								)
							)
							return record_expr(expr, self._unknown)
						current_ty = td.param_types[0]
						continue
					if isinstance(proj, H.HPlaceField):
						td = self.type_table.get(current_ty)
						if td.kind is not TypeKind.STRUCT:
							diagnostics.append(
								Diagnostic(
									message="field access requires a struct value",
									severity="error",
									span=getattr(expr, "loc", Span()),
								)
							)
							return record_expr(expr, self._unknown)
						info = self.type_table.struct_field(current_ty, proj.name)
						if info is None:
							diagnostics.append(
								Diagnostic(
									message=f"unknown field '{proj.name}' on struct '{td.name}'",
									severity="error",
									span=getattr(expr, "loc", Span()),
								)
							)
							return record_expr(expr, self._unknown)
						_, field_ty = info
						current_ty = field_ty
						continue
					if isinstance(proj, H.HPlaceIndex):
						idx_ty = type_expr(proj.index)
						if idx_ty is not None and idx_ty not in (self._int, self._uint):
							diagnostics.append(
								Diagnostic(
									message="array index must be an integer type",
									severity="error",
									span=getattr(proj.index, "loc", getattr(expr, "loc", Span())),
								)
							)
							return record_expr(expr, self._unknown)
						td = self.type_table.get(current_ty)
						if td.kind is not TypeKind.ARRAY or not td.param_types:
							diagnostics.append(
								Diagnostic(
									message="indexing requires an Array value",
									severity="error",
									span=getattr(expr, "loc", Span()),
								)
							)
							return record_expr(expr, self._unknown)
						current_ty = td.param_types[0]
						continue
					diagnostics.append(
						Diagnostic(
							message="unsupported place projection",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				return record_expr(expr, current_ty)

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
					return record_expr(expr, sub_ty if sub_ty in (self._uint,) else self._unknown)
				return record_expr(expr, self._unknown)

			if isinstance(expr, H.HBinary):
				left_ty = type_expr(expr.left)
				right_ty = type_expr(expr.right)
				if expr.op in (
					H.BinaryOp.ADD,
					H.BinaryOp.SUB,
					H.BinaryOp.MUL,
					H.BinaryOp.MOD,
				):
					# Arithmetic on Int/Float; MOD also on Uint.
					if left_ty == self._int and right_ty == self._int:
						return record_expr(expr, self._int)
					if left_ty == self._float and right_ty == self._float:
						return record_expr(expr, self._float)
					if expr.op is H.BinaryOp.MOD and left_ty == self._uint and right_ty == self._uint:
						return record_expr(expr, self._uint)
					return record_expr(expr, self._unknown)
				if expr.op in (H.BinaryOp.DIV,):
					if left_ty == self._int and right_ty == self._int:
						return record_expr(expr, self._int)
					if left_ty == self._float and right_ty == self._float:
						return record_expr(expr, self._float)
					return record_expr(expr, self._unknown)
				if expr.op in (
					H.BinaryOp.BIT_AND,
					H.BinaryOp.BIT_OR,
					H.BinaryOp.BIT_XOR,
					H.BinaryOp.SHL,
					H.BinaryOp.SHR,
				):
					if left_ty == self._uint and right_ty == self._uint:
						return record_expr(expr, self._uint)
					diagnostics.append(
						Diagnostic(
							message="bitwise operators require Uint operands",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
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
				declared_ty: TypeId | None = None
				if getattr(stmt, "declared_type_expr", None) is not None:
					try:
						declared_ty = resolve_opaque_type(stmt.declared_type_expr, self.type_table)
					except Exception:
						declared_ty = None
				# If the user provides a type annotation, treat it as the expected type
				# for the initializer. This enables constructor calls like:
				#   val x: Optional<Int> = Some(1)
				inferred_ty = type_expr(stmt.value, expected_type=declared_ty)
				val_ty = inferred_ty
				if declared_ty is not None:
					# MVP: treat the declared type as authoritative for the binding.
					# If the initializer is obviously incompatible, emit a diagnostic.
					# Numeric literals are allowed to flow into Int/Uint without requiring
					# an explicit cast.
					if inferred_ty is not None and inferred_ty != declared_ty:
						is_int_lit = isinstance(stmt.value, H.HLiteralInt)
						decl_name = self.type_table.get(declared_ty).name
						inf_name = self.type_table.get(inferred_ty).name
						if not (is_int_lit and decl_name in ("Int", "Uint") and inf_name == "Int"):
							diagnostics.append(
								Diagnostic(
									message=f"initializer type '{inf_name}' does not match declared type '{decl_name}'",
									severity="error",
									span=getattr(stmt, "loc", Span()),
								)
							)
					val_ty = declared_ty
				scope_env[-1][stmt.name] = val_ty
				scope_bindings[-1][stmt.name] = stmt.binding_id
				binding_types[stmt.binding_id] = val_ty
				binding_names[stmt.binding_id] = stmt.name
				binding_mutable[stmt.binding_id] = bool(getattr(stmt, "is_mutable", False))
				binding_place_kind[stmt.binding_id] = PlaceKind.LOCAL
				# Track origin for ref-typed locals: allow propagation from an existing
				# ref binding, otherwise treat as local/temporary.
				if val_ty is not None and self.type_table.get(val_ty).kind is TypeKind.REF:
					origin: Optional[int] = None
					# val r = p;  (p is a ref param or a local ref derived from param)
					if isinstance(stmt.value, H.HVar) and getattr(stmt.value, "binding_id", None) is not None:
						origin = ref_origin_param.get(stmt.value.binding_id)
					# val r = &(*p).x;  (reborrow through a ref that derives from param)
					if isinstance(stmt.value, H.HBorrow):
						def _base_lookup(hv: object) -> Optional[PlaceBase]:
							bid = getattr(hv, "binding_id", None)
							if bid is None:
								return None
							kind = binding_place_kind.get(bid, PlaceKind.LOCAL)
							name = hv.name if hasattr(hv, "name") else str(hv)
							return PlaceBase(kind=kind, local_id=bid, name=name)

						sub_place = place_from_expr(stmt.value.subject, base_lookup=_base_lookup)
						if sub_place is not None and any(isinstance(p, DerefProj) for p in sub_place.projections):
							origin = ref_origin_param.get(sub_place.base.local_id)
					ref_origin_param[stmt.binding_id] = origin
			elif isinstance(stmt, H.HBlock):
				# Block statements introduce a nested lexical scope.
				#
				# This is used by desugarings like `for` which need to introduce hidden
				# temporaries without leaking them to the surrounding scope.
				scope_env.append(dict())
				scope_bindings.append(dict())
				try:
					for s in stmt.statements:
						type_stmt(s)
				finally:
					scope_env.pop()
					scope_bindings.pop()
			elif isinstance(stmt, H.HAssign):
				type_expr(stmt.value)
				type_expr(stmt.target)
				# Assignment target must be an addressable place.
				def _base_lookup(hv: object) -> Optional[PlaceBase]:
					bid = getattr(hv, "binding_id", None)
					if bid is None:
						return None
					kind = binding_place_kind.get(bid, PlaceKind.LOCAL)
					name = hv.name if hasattr(hv, "name") else str(hv)
					return PlaceBase(kind=kind, local_id=bid, name=name)

				if place_from_expr(stmt.target, base_lookup=_base_lookup) is None:
					diagnostics.append(
						Diagnostic(
							message="assignment target must be an addressable place",
							severity="error",
							span=getattr(stmt, "loc", Span()),
							)
						)
				# If assigning to a ref-typed binding, track origin (simple propagation).
				if isinstance(stmt.target, H.HVar) and getattr(stmt.target, "binding_id", None) is not None:
					tgt_bid = stmt.target.binding_id
					tgt_ty = binding_types.get(tgt_bid)
					if tgt_ty is not None and self.type_table.get(tgt_ty).kind is TypeKind.REF:
						origin: Optional[int] = None
						if isinstance(stmt.value, H.HVar) and getattr(stmt.value, "binding_id", None) is not None:
							origin = ref_origin_param.get(stmt.value.binding_id)
						ref_origin_param[tgt_bid] = origin
			elif hasattr(H, "HAugAssign") and isinstance(stmt, getattr(H, "HAugAssign")):
				"""
				Augmented assignment (`+=`) type rules (MVP).

				- Target must be an addressable place (same as `=`).
				- Operand types must match.
				- Currently supported for numeric scalars only (Int/Float).

				We enforce *writability* here as well:
				- Writes to owned storage require a `var` base binding.
				- Writes through deref require a mutable reference (`&mut`) at each deref.
				"""
				tgt_ty = type_expr(stmt.target)
				val_ty = type_expr(stmt.value)

				def _base_lookup(hv: object) -> Optional[PlaceBase]:
					bid = getattr(hv, "binding_id", None)
					if bid is None:
						return None
					kind = binding_place_kind.get(bid, PlaceKind.LOCAL)
					name = hv.name if hasattr(hv, "name") else str(hv)
					return PlaceBase(kind=kind, local_id=bid, name=name)

				tgt_place = place_from_expr(stmt.target, base_lookup=_base_lookup)
				if tgt_place is None:
					diagnostics.append(
						Diagnostic(
							message="assignment target must be an addressable place",
							severity="error",
							span=getattr(stmt, "loc", Span()),
						)
					)
					return

				# Writability: owned storage requires `var`; reborrow writes require `&mut`.
				has_deref = any(isinstance(p, DerefProj) for p in tgt_place.projections)
				if not has_deref and tgt_place.base.local_id is not None and not binding_mutable.get(tgt_place.base.local_id, False):
					diagnostics.append(
						Diagnostic(
							message="cannot assign through an immutable binding; declare it with `var`",
							severity="error",
							span=getattr(stmt, "loc", Span()),
						)
					)
				if has_deref and hasattr(H, "HPlaceExpr") and isinstance(stmt.target, getattr(H, "HPlaceExpr")):
					cur = type_expr(stmt.target.base)
					for pr in stmt.target.projections:
						if isinstance(pr, H.HPlaceDeref):
							ptr_def = self.type_table.get(cur)
							if ptr_def.kind is not TypeKind.REF or not ptr_def.ref_mut:
								diagnostics.append(
									Diagnostic(
										message="cannot assign through *p unless p is a mutable reference (&mut T)",
										severity="error",
										span=getattr(stmt, "loc", Span()),
									)
								)
								break
							if ptr_def.param_types:
								cur = ptr_def.param_types[0]
						elif isinstance(pr, H.HPlaceField):
							td = self.type_table.get(cur)
							if td.kind is TypeKind.STRUCT:
								info = self.type_table.struct_field(cur, pr.name)
								if info is not None:
									_, cur = info
						elif isinstance(pr, H.HPlaceIndex):
							td = self.type_table.get(cur)
							if td.kind is TypeKind.ARRAY and td.param_types:
								cur = td.param_types[0]

				arith_ops = {"+=", "-=", "*=", "/="}
				bit_ops = {"&=", "|=", "^=", "<<=", ">>="}
				mod_ops = {"%="}
				# Type check: supported augmented assignment operators.
				if stmt.op not in (arith_ops | bit_ops | mod_ops):
					diagnostics.append(
						Diagnostic(
							message=f"unsupported augmented assignment operator '{stmt.op}'",
							severity="error",
							span=getattr(stmt, "loc", Span()),
						)
					)
				if tgt_ty != val_ty:
					diagnostics.append(
						Diagnostic(
							message="augmented assignment requires matching operand types",
							severity="error",
							span=getattr(stmt, "loc", Span()),
						)
					)
				if stmt.op in arith_ops:
					if tgt_ty not in (self._int, self._float):
						pretty = self.type_table.get(tgt_ty).name if tgt_ty is not None else "Unknown"
						diagnostics.append(
							Diagnostic(
								message=f"augmented assignment '{stmt.op}' is not supported for type '{pretty}' in MVP",
								severity="error",
								span=getattr(stmt, "loc", Span()),
							)
						)
				elif stmt.op in mod_ops:
					if tgt_ty not in (self._int, self._uint):
						pretty = self.type_table.get(tgt_ty).name if tgt_ty is not None else "Unknown"
						diagnostics.append(
							Diagnostic(
								message=f"augmented assignment '{stmt.op}' is not supported for type '{pretty}' in MVP",
								severity="error",
								span=getattr(stmt, "loc", Span()),
							)
						)
				elif stmt.op in bit_ops:
					if tgt_ty != self._uint:
						pretty = self.type_table.get(tgt_ty).name if tgt_ty is not None else "Unknown"
						diagnostics.append(
							Diagnostic(
								message=f"bitwise augmented assignment requires Uint operands (have '{pretty}')",
								severity="error",
								span=getattr(stmt, "loc", Span()),
							)
						)
			elif isinstance(stmt, H.HExprStmt):
				type_expr(stmt.expr, used_as_value=False)
			elif isinstance(stmt, H.HReturn):
				if stmt.value is not None:
					type_expr(stmt.value, expected_type=return_type)
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
			binding_mutable=binding_mutable,
			call_resolutions=call_resolutions,
		)

		# MVP escape policy: reference returns must be derived from a single
		# reference parameter.
		if return_type is not None and self.type_table.get(return_type).kind is TypeKind.REF:
			# Seed origin for reference parameters.
			for bid in param_bindings:
				pty = binding_types.get(bid)
				if pty is not None and self.type_table.get(pty).kind is TypeKind.REF:
					ref_origin_param[bid] = bid

			def _return_origin(expr: H.HExpr) -> Optional[int]:
				# Returning an existing reference value (param or local ref).
				if isinstance(expr, H.HVar) and getattr(expr, "binding_id", None) is not None:
					return ref_origin_param.get(expr.binding_id)
				if hasattr(H, "HPlaceExpr") and isinstance(expr, getattr(H, "HPlaceExpr")):
					if isinstance(expr.base, H.HVar) and getattr(expr.base, "binding_id", None) is not None:
						return ref_origin_param.get(expr.base.binding_id)
				# Returning a borrow is only allowed when it reborrows through a ref
				# that originates from a reference parameter (e.g. &(*p).x).
				if isinstance(expr, H.HBorrow):
					def _base_lookup(hv: object) -> Optional[PlaceBase]:
						bid = getattr(hv, "binding_id", None)
						if bid is None:
							return None
						kind = binding_place_kind.get(bid, PlaceKind.LOCAL)
						name = hv.name if hasattr(hv, "name") else str(hv)
						return PlaceBase(kind=kind, local_id=bid, name=name)

					sub_place = place_from_expr(expr.subject, base_lookup=_base_lookup)
					if sub_place is None:
						return None
					if not any(isinstance(p, DerefProj) for p in sub_place.projections):
						return None
					return ref_origin_param.get(sub_place.base.local_id)
				return None

			def _walk_returns(block: H.HBlock, out: List[tuple[Optional[int], Span]]) -> None:
				for s in block.statements:
					if isinstance(s, H.HReturn) and s.value is not None:
						out.append((_return_origin(s.value), getattr(s, "loc", getattr(s.value, "loc", Span()))))
					elif isinstance(s, H.HIf):
						_walk_returns(s.then_block, out)
						if s.else_block:
							_walk_returns(s.else_block, out)
					elif isinstance(s, H.HLoop):
						_walk_returns(s.body, out)
					elif isinstance(s, H.HTry):
						_walk_returns(s.body, out)
						for arm in s.catches:
							_walk_returns(arm.block, out)

			returns: List[tuple[Optional[int], Span]] = []
			_walk_returns(body, returns)

			# Determine the single allowed origin param (if any).
			origin_param: Optional[int] = None
			for origin, span in returns:
				if origin is None:
					diagnostics.append(
						Diagnostic(
							message="reference return must be derived from a reference parameter (MVP escape rule)",
							severity="error",
							span=span,
						)
					)
					continue
				if origin_param is None:
					origin_param = origin
				elif origin != origin_param:
					diagnostics.append(
						Diagnostic(
							message="reference return must derive from a single reference parameter (cannot return from different params)",
							severity="error",
							span=span,
						)
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
