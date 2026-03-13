# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Minimal type resolver for lang.

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

from lang.driftc.checker import FnSignature, TypeParam
from lang.driftc.stage1.call_info import IntrinsicKind
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.type_resolve_common import resolve_opaque_type
from lang.driftc.core.types_core import TypeId, TypeKind, TypeParamId, TypeTable
from lang.driftc.core.span import Span


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
	func_decls_list = list(func_decls)

	def _intrinsic_kind_for_name(name: str) -> IntrinsicKind | None:
		try:
			return IntrinsicKind(name)
		except ValueError:
			return None

	name_ord: dict[tuple[str, str], int] = {}
	for decl in func_decls_list:
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
		is_extern_c = bool(getattr(decl, "is_extern_c", False))
		is_intrinsic = bool(getattr(decl, "is_intrinsic", False))
		intrinsic_kind = None
		if is_intrinsic:
			intrinsic_kind = getattr(decl, "intrinsic_kind", None)
			if intrinsic_kind is None and isinstance(name, str):
				intrinsic_kind = _intrinsic_kind_for_name(name)

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
		param_mutable: list[bool] = []
		param_type_ids: list[TypeId] = []

		def _coerce_exception_nominal(ty: TypeId) -> TypeId:
			"""
			Map forward-nominal exception types to Error, preserving &/&mut wrappers.
			This allows signatures to use exception names in type positions.
			"""
			try:
				td = table.get(ty)
			except Exception:
				return ty
			if td.kind is TypeKind.FORWARD_NOMINAL and td.module_id:
				fqn = f"{td.module_id}:{td.name}"
				if fqn in table.exception_schemas:
					return table.ensure_error()
			if td.kind is TypeKind.REF and td.param_types:
				inner = td.param_types[0]
				new_inner = _coerce_exception_nominal(inner)
				if new_inner != inner:
					return table.ensure_ref_mut(new_inner) if td.ref_mut else table.ensure_ref(new_inner)
			return ty
		local_type_params = dict(impl_type_param_map)
		local_type_params.update(type_param_map)
		for idx, p in enumerate(getattr(decl, "params", [])):
			raw_ty = getattr(p, "type", None)
			raw_params.append(raw_ty)
			param_names.append(getattr(p, "name", f"p{len(param_names)}"))
			param_mutable.append(bool(getattr(p, "mutable", False)))
			resolved_param: TypeId | None = None
			if resolved_param is None:
				resolved_param = resolve_opaque_type(raw_ty, table, module_id=module_name, type_params=local_type_params)
			resolved_param = _coerce_exception_nominal(resolved_param)
			param_type_ids.append(resolved_param)

		# Return
		raw_ret = getattr(decl, "return_type", None)
		return_type_id = resolve_opaque_type(raw_ret, table, module_id=module_name, type_params=local_type_params)
		error_type_id = None
		ret_def = table.get(return_type_id)
		if ret_def.kind is TypeKind.FNRESULT and len(ret_def.param_types) >= 2:
			error_type_id = ret_def.param_types[1]

		throws = _throws_from_decl(decl)
		declared_nothrow = bool(getattr(decl, "declared_nothrow", False))
		declared_throws = bool(getattr(decl, "declared_throws", False))
		declared_unsafe = bool(getattr(decl, "is_unsafe", False)) or is_extern_c
		# Surface ABI rule: nothrow is the only way to force a non-throwing ABI.
		declared_can_throw = not declared_nothrow
		# Note: throws_events are for validation only; they do not change ABI.
		if declared_can_throw and error_type_id is None:
			error_type_id = table.ensure_error()

		is_method = bool(getattr(decl, "is_method", False))
		self_mode = getattr(decl, "self_mode", None)
		impl_target_type_id: TypeId | None = None
		impl_target_type_args: list[TypeId] | None = None
		if getattr(decl, "impl_target", None) is not None:
			target_expr = decl.impl_target
			origin_mod = getattr(target_expr, "module_id", None) or module_name
			target_base_expr = target_expr
			if target_expr.name in {"&", "&mut"} and getattr(target_expr, "args", None):
				target_base_expr = target_expr.args[0]
			base_id = None
			if origin_mod is not None:
				base_id = table.get_struct_base(module_id=origin_mod, name=target_base_expr.name)
			if base_id is None and origin_mod is not None:
				base_id = table.get_variant_base(module_id=origin_mod, name=target_base_expr.name)
			if base_id is None:
				impl_target_type_id = resolve_opaque_type(
					target_base_expr,
					table,
					module_id=origin_mod,
					type_params=impl_type_param_map,
				)
			else:
				impl_target_type_id = base_id
			if impl_target_type_id is not None:
				td = table.get(impl_target_type_id)
				if td.kind is TypeKind.ARRAY:
					impl_target_type_id = table.array_base_id()
			target_for_args = target_base_expr
			if getattr(target_for_args, "args", None):
				arg_mod = getattr(target_for_args, "module_id", None) or origin_mod
				impl_target_type_args = [
					resolve_opaque_type(a, table, module_id=arg_mod, type_params=impl_type_param_map)
					for a in list(getattr(target_for_args, "args", []) or [])
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
			declared_throws=declared_throws,
			declared_unsafe=declared_unsafe,
			is_extern=is_extern,
			is_extern_c=is_extern_c,
			is_intrinsic=is_intrinsic,
			intrinsic_kind=intrinsic_kind,
			# Legacy/raw fields for compatibility
			param_types=raw_params,
			return_type=raw_ret,
			throws_events=throws,
			param_names=param_names if param_names else None,
			param_mutable=param_mutable if param_mutable else None,
			is_method=is_method,
			self_mode=self_mode,
			impl_target_type_id=impl_target_type_id,
			impl_target_type_args=impl_target_type_args,
			impl_type_params=impl_type_params,
			is_pub=bool(getattr(decl, "is_pub", False)),
			module=module_name,
		)

	# Validate FFI-safe types on extern "C" signatures.
	ffi_diagnostics: list[str] = []
	for fn_id, sig in signatures.items():
		if not sig.is_extern_c:
			continue
		# Find the original decl for raw TypeExpr access.
		decl_match = None
		for d in func_decls_list:
			d_name = getattr(d, "name", None)
			if d_name == sig.name and bool(getattr(d, "is_extern_c", False)):
				decl_match = d
				break
		_validate_ffi_safe_signature(sig, table, ffi_diagnostics, decl=decl_match)
	return table, signatures, ffi_diagnostics


# Types considered FFI-safe for extern "C" signatures.
_FFI_SAFE_SCALAR_NAMES: frozenset[str] = frozenset({
	"Int", "UInt", "Uint", "Uint64", "Byte", "Bool", "Float",
	"Int32", "Uint32",
})

# Type names NOT safe for FFI (regardless of TypeId resolution).
_FFI_UNSAFE_NAMES: frozenset[str] = frozenset({
	"String", "Array", "Fn", "FnResult", "Optional",
})


def _is_ffi_safe_type_name(name: str) -> bool:
	"""Return True if a raw type name is FFI-safe."""
	if name in _FFI_SAFE_SCALAR_NAMES:
		return True
	if name in ("Void", "RawPtr"):
		return True
	return False


def _is_ffi_safe_type(tid: TypeId, table: TypeTable) -> bool:
	"""Return True if *tid* is allowed in an extern C signature."""
	td = table.get(tid)
	if td is None:
		return False
	name = td.name
	kind = td.kind
	# Skip Unknown — those are unresolved and will be validated elsewhere.
	if kind is TypeKind.UNKNOWN:
		return True
	if name in _FFI_SAFE_SCALAR_NAMES:
		return True
	if name == "Void" or kind is TypeKind.VOID:
		return True
	if name == "RawPtr" or kind is TypeKind.RAW_PTR:
		return True
	return False


def _validate_ffi_safe_signature(
	sig: FnSignature,
	table: TypeTable,
	diagnostics: list[str],
	*,
	decl: object | None = None,
) -> None:
	"""Reject non-FFI-safe param/return types on an extern C function."""
	# Validate param types using raw TypeExpr names when available.
	raw_params = list(getattr(decl, "params", []) or []) if decl is not None else []
	if sig.param_type_ids:
		param_names = sig.param_names or [f"param{i}" for i in range(len(sig.param_type_ids))]
		for i, tid in enumerate(sig.param_type_ids):
			pname = param_names[i] if i < len(param_names) else f"param{i}"
			# Void is only valid as a return type, not a parameter type.
			if i < len(raw_params):
				raw_p = raw_params[i]
				te = getattr(raw_p, "type_expr", None)
				if te is not None:
					type_name = getattr(te, "name", "")
					if type_name == "Void":
						diagnostics.append(
							f"type 'Void' is not valid as a parameter type in extern C signature of '{sig.name}' (parameter '{pname}')"
						)
						continue
			if tid is not None:
				td = table.get(tid)
				if td is not None and (td.name == "Void" or td.kind is TypeKind.VOID):
					diagnostics.append(
						f"type 'Void' is not valid as a parameter type in extern C signature of '{sig.name}' (parameter '{pname}')"
					)
					continue
			# Check TypeExpr name directly for more reliable validation.
			if i < len(raw_params):
				raw_p = raw_params[i]
				te = getattr(raw_p, "type_expr", None)
				if te is not None:
					type_name = getattr(te, "name", "")
					if type_name and not _is_ffi_safe_type_name(type_name):
						diagnostics.append(
							f"type '{type_name}' is not FFI-safe in extern C signature of '{sig.name}' (parameter '{pname}')"
						)
						continue
			# Fallback: check resolved TypeId.
			if tid is not None and not _is_ffi_safe_type(tid, table):
				td = table.get(tid)
				type_name = td.name if td is not None else str(tid)
				diagnostics.append(
					f"type '{type_name}' is not FFI-safe in extern C signature of '{sig.name}' (parameter '{pname}')"
				)
	# Validate return type.
	raw_ret = getattr(decl, "return_type", None) if decl is not None else None
	if raw_ret is not None:
		ret_name = getattr(raw_ret, "name", "")
		if ret_name and not _is_ffi_safe_type_name(ret_name):
			diagnostics.append(
				f"type '{ret_name}' is not FFI-safe in extern C signature of '{sig.name}' (return type)"
			)
			return
	if sig.return_type_id is not None and not _is_ffi_safe_type(sig.return_type_id, table):
		td = table.get(sig.return_type_id)
		type_name = td.name if td is not None else str(sig.return_type_id)
		diagnostics.append(
			f"type '{type_name}' is not FFI-safe in extern C signature of '{sig.name}' (return type)"
		)


def _throws_from_decl(decl: object) -> Tuple[str, ...]:
	throws = getattr(decl, "throws", None)
	if throws is None:
		throws = getattr(decl, "throws_events", None)
	if throws is None:
		return ()
	return tuple(throws)


__all__ = ["resolve_program_signatures"]
