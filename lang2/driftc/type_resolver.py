# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Minimal type resolver for lang2.

Given a module-like object (or any iterable of function decls) with declared
parameter/return types and throws clauses, build:
  - a shared TypeTable
  - a mapping of function name -> FnSignature with TypeIds populated

This is intentionally shallow: it only resolves declared types (Int/Bool/String/
Error/FnResult<...>) and does not perform expression-level type checking. It
exists to feed real TypeIds into the checker pipeline so legacy string/tuple
type shims can be retired.
"""

from __future__ import annotations

from typing import Iterable, Tuple, Optional

from lang2.driftc.checker import FnSignature
from lang2.driftc.core.type_resolve_common import resolve_opaque_type
from lang2.driftc.core.types_core import TypeId, TypeKind, TypeTable


def resolve_program_signatures(
	func_decls: Iterable[object],
	*,
	table: Optional[TypeTable] = None,
) -> tuple[TypeTable, dict[str, FnSignature]]:
	"""
	Resolve declared types on function declarations into TypeIds.

	Each decl is expected to expose:
	  - name: str
	  - params: iterable with a .type annotation (string/tuple/etc.)
	  - return_type: declared return type
	  - throws or throws_events: optional iterable of event names
	  - loc (optional): carried into FnSignature.loc
	  - is_extern / is_intrinsic (optional): flags
	  - is_method: bool (True when declared inside an `implement Type` block)
	  - self_mode: optional str ("value", "ref", "ref_mut") for methods
	  - impl_target: optional TypeExpr for the nominal type the impl targets
	  - module: optional str module name
	"""
	# Allow callers (parser frontends) to supply a pre-populated TypeTable
	# (e.g., with user-defined struct types already declared).
	table = table or TypeTable()

	# Seed common scalars; unknowns/new scalars are created on demand.
	table.ensure_int()
	table.ensure_bool()
	table.ensure_string()
	table.ensure_error()
	table.ensure_uint()
	table.ensure_void()

	signatures: dict[str, FnSignature] = {}

	for decl in func_decls:
		name = getattr(decl, "name")
		decl_loc = getattr(decl, "loc", None)
		is_extern = bool(getattr(decl, "is_extern", False))
		is_intrinsic = bool(getattr(decl, "is_intrinsic", False))
		module_name = getattr(decl, "module", None)

		# Params
		raw_params = []
		param_names: list[str] = []
		param_type_ids: list[TypeId] = []
		for p in getattr(decl, "params", []):
			raw_ty = getattr(p, "type", None)
			raw_params.append(raw_ty)
			param_names.append(getattr(p, "name", f"p{len(param_names)}"))
			param_type_ids.append(resolve_opaque_type(raw_ty, table))

		# Return
		raw_ret = getattr(decl, "return_type", None)
		return_type_id = resolve_opaque_type(raw_ret, table)
		error_type_id = None
		ret_def = table.get(return_type_id)
		if ret_def.kind is TypeKind.FNRESULT and len(ret_def.param_types) >= 2:
			error_type_id = ret_def.param_types[1]

		throws = _throws_from_decl(decl)
		declared_can_throw = True if throws else None
		if declared_can_throw is None and ret_def.kind is TypeKind.FNRESULT:
			declared_can_throw = True

		is_method = bool(getattr(decl, "is_method", False))
		self_mode = getattr(decl, "self_mode", None)
		impl_target_type_id: TypeId | None = None
		if is_method and getattr(decl, "impl_target", None) is not None:
			impl_target_type_id = resolve_opaque_type(decl.impl_target, table)

		signatures[name] = FnSignature(
			name=name,
			method_name=getattr(decl, "method_name", None) or name,
			loc=decl_loc,
			param_type_ids=param_type_ids,
			return_type_id=return_type_id,
			error_type_id=error_type_id,
			declared_can_throw=declared_can_throw,
			is_extern=is_extern,
			is_intrinsic=is_intrinsic,
			# Legacy/raw fields for compatibility
			param_types=raw_params,
			return_type=raw_ret,
			throws_events=throws,
			param_names=param_names if param_names else None,
			is_method=is_method,
			self_mode=self_mode,
			impl_target_type_id=impl_target_type_id,
			module=module_name,
		)

	return table, signatures


def _throws_from_decl(decl: object) -> Tuple[str, ...]:
	throws = getattr(decl, "throws", None)
	if throws is None:
		throws = getattr(decl, "throws_events", None)
	if throws is None:
		return ()
	return tuple(throws)


__all__ = ["resolve_program_signatures"]
