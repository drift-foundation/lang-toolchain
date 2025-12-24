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

from lang2.driftc.checker import FnSignature, TypeParam
from lang2.driftc.core.function_id import FunctionId
from lang2.driftc.core.type_resolve_common import resolve_opaque_type
from lang2.driftc.core.types_core import TypeId, TypeKind, TypeParamId, TypeTable
from lang2.driftc.core.span import Span


def resolve_program_signatures(
	func_decls: Iterable[object],
	*,
	table: Optional[TypeTable] = None,
) -> tuple[TypeTable, dict[FunctionId, FnSignature]]:
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

	signatures: dict[FunctionId, FnSignature] = {}

	name_ord: dict[tuple[str, str], int] = {}
	for decl in func_decls:
		name = getattr(decl, "name")
		module_name = getattr(decl, "module", None)
		fn_id = getattr(decl, "fn_id", None)
		if not isinstance(fn_id, FunctionId):
			ord_key = (module_name or "main", name)
			ordinal = name_ord.get(ord_key, 0)
			name_ord[ord_key] = ordinal + 1
			fn_id = FunctionId(module=module_name or "main", name=name, ordinal=ordinal)
		decl_loc = getattr(decl, "loc", None)
		is_extern = bool(getattr(decl, "is_extern", False))
		is_intrinsic = bool(getattr(decl, "is_intrinsic", False))

		raw_type_params = list(getattr(decl, "type_params", []) or [])
		raw_type_param_locs = list(getattr(decl, "type_param_locs", []) or [])
		type_params: list[TypeParam] = []
		type_param_map: dict[str, TypeParamId] = {}
		for idx, tp_name in enumerate(raw_type_params):
			param_id = TypeParamId(owner=fn_id, index=idx)
			span = None
			if idx < len(raw_type_param_locs):
				span = Span.from_loc(raw_type_param_locs[idx])
			type_params.append(TypeParam(id=param_id, name=tp_name, span=span))
			type_param_map[tp_name] = param_id
		raw_impl_type_params = list(getattr(decl, "impl_type_params", []) or [])
		raw_impl_type_param_locs = list(getattr(decl, "impl_type_param_locs", []) or [])
		impl_type_params: list[TypeParam] = []
		impl_type_param_map: dict[str, TypeParamId] = {}
		impl_owner = getattr(decl, "impl_owner", None)
		if raw_impl_type_params:
			if not isinstance(impl_owner, FunctionId):
				impl_owner = FunctionId(module="lang.__internal", name=f"__impl_{module_name or 'main'}::{name}", ordinal=0)
			for idx, tp_name in enumerate(raw_impl_type_params):
				param_id = TypeParamId(owner=impl_owner, index=idx)
				span = None
				if idx < len(raw_impl_type_param_locs):
					span = Span.from_loc(raw_impl_type_param_locs[idx])
				impl_type_params.append(TypeParam(id=param_id, name=tp_name, span=span))
				impl_type_param_map[tp_name] = param_id

		# Params
		raw_params = []
		param_names: list[str] = []
		param_type_ids: list[TypeId] = []
		param_nonescaping: list[bool] = []
		local_type_params = dict(impl_type_param_map)
		local_type_params.update(type_param_map)
		for p in getattr(decl, "params", []):
			raw_ty = getattr(p, "type", None)
			raw_params.append(raw_ty)
			param_names.append(getattr(p, "name", f"p{len(param_names)}"))
			param_type_ids.append(
				resolve_opaque_type(raw_ty, table, module_id=module_name, type_params=local_type_params)
			)
			param_nonescaping.append(bool(getattr(p, "non_escaping", False)))

		# Return
		raw_ret = getattr(decl, "return_type", None)
		return_type_id = resolve_opaque_type(raw_ret, table, module_id=module_name, type_params=local_type_params)
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
		impl_target_type_args: list[TypeId] | None = None
		if is_method and getattr(decl, "impl_target", None) is not None:
			origin_mod = getattr(decl.impl_target, "module_id", None) or module_name
			base_id = None
			if origin_mod is not None:
				base_id = table.get_struct_base(module_id=origin_mod, name=decl.impl_target.name)
			if base_id is None and origin_mod is not None:
				base_id = table.get_variant_base(module_id=origin_mod, name=decl.impl_target.name)
			if base_id is None:
				impl_target_type_id = resolve_opaque_type(decl.impl_target, table, module_id=module_name)
			else:
				impl_target_type_id = base_id
			if getattr(decl.impl_target, "args", None):
				impl_target_type_args = [
					resolve_opaque_type(a, table, module_id=origin_mod, type_params=impl_type_param_map)
					for a in list(getattr(decl.impl_target, "args", []) or [])
				]

		signatures[fn_id] = FnSignature(
			name=name,
			method_name=getattr(decl, "method_name", None) or name,
			type_params=type_params,
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
			param_nonescaping=param_nonescaping if param_nonescaping else None,
			is_method=is_method,
			self_mode=self_mode,
			impl_target_type_id=impl_target_type_id,
			impl_target_type_args=impl_target_type_args,
			impl_type_params=impl_type_params,
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
