# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.core.function_id import FunctionId
from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.method_registry import CallableRegistry, CallableSignature, SelfMode, Visibility
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.driftc.test_helpers import build_linked_world
from lang.driftc.trait_index import GlobalTraitIndex
from lang.driftc.type_checker import TypeChecker


def _build_registry(sigs: dict[FunctionId, object]) -> tuple[CallableRegistry, dict[object, int]]:
	registry = CallableRegistry()
	module_ids: dict[object, int] = {None: 0}
	next_id = 1
	for fn_id, sig in sigs.items():
		if getattr(sig, "param_type_ids", None) is None or getattr(sig, "return_type_id", None) is None:
			continue
		module_id = module_ids.setdefault(getattr(sig, "module", None), len(module_ids))
		param_types_tuple = tuple(sig.param_type_ids)
		if getattr(sig, "is_method", False):
			if getattr(sig, "impl_target_type_id", None) is None or getattr(sig, "self_mode", None) is None:
				continue
			self_mode = {
				"value": SelfMode.SELF_BY_VALUE,
				"ref": SelfMode.SELF_BY_REF,
				"ref_mut": SelfMode.SELF_BY_REF_MUT,
			}.get(sig.self_mode)
			if self_mode is None:
				continue
			registry.register_inherent_method(
				callable_id=next_id,
				name=sig.method_name or sig.name,
				module_id=module_id,
				visibility=Visibility.public(),
				signature=CallableSignature(param_types=param_types_tuple, result_type=sig.return_type_id),
				fn_id=fn_id,
				impl_id=next_id,
				impl_target_type_id=sig.impl_target_type_id,
				self_mode=self_mode,
				is_generic=bool(getattr(sig, "type_params", None) or getattr(sig, "impl_type_params", [])),
			)
		else:
			registry.register_free_function(
				callable_id=next_id,
				name=fn_id.name,
				module_id=module_id,
				visibility=Visibility.public(),
				signature=CallableSignature(param_types=param_types_tuple, result_type=sig.return_type_id),
				fn_id=fn_id,
				is_generic=bool(getattr(sig, "type_params", None)),
			)
		next_id += 1
	return registry, module_ids


def test_stdlib_sort_in_place_requires_resolve(tmp_path: Path) -> None:
	modules, type_table, _exc, module_exports, _deps, diagnostics = parse_drift_workspace_to_hir(
		[],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == []
	func_hirs, sigs, _fn_ids = flatten_modules(modules)
	fn_id = None
	for fid in func_hirs.keys():
		if getattr(fid, "module", None) == "std.algo" and getattr(fid, "name", None) == "sort_in_place":
			fn_id = fid
			break
	assert fn_id is not None
	registry, module_ids = _build_registry(sigs)
	linked_world, require_env = build_linked_world(type_table)
	trait_index = GlobalTraitIndex.from_trait_worlds(getattr(type_table, "trait_worlds", None))
	trait_scope_by_module = {mid: (exports.get("trait_scope") or []) for mid, exports in module_exports.items()}
	current_mod = module_ids.setdefault(getattr(fn_id, "module", None), len(module_ids))
	tc = TypeChecker(type_table=type_table)
	param_types: dict[str, object] = {}
	param_mutable: dict[str, bool] | None = None
	sig = sigs.get(fn_id)
	if sig is not None and sig.param_names is not None and sig.param_type_ids is not None:
		param_types = {pname: pty for pname, pty in zip(sig.param_names, sig.param_type_ids)}
	if sig is not None and sig.param_names is not None and sig.param_mutable is not None:
		if len(sig.param_names) == len(sig.param_mutable):
			param_mutable = {pname: bool(flag) for pname, flag in zip(sig.param_names, sig.param_mutable)}
	result = tc.check_function(
		fn_id,
		func_hirs[fn_id],
		param_types=param_types,
		param_mutable=param_mutable,
		return_type=sigs[fn_id].return_type_id if fn_id in sigs else None,
		signatures_by_id=sigs,
		callable_registry=registry,
		trait_index=trait_index,
		trait_scope_by_module=trait_scope_by_module,
		linked_world=linked_world,
		require_env=require_env,
		visible_modules=(current_mod,),
		current_module=current_mod,
	)
	assert result.diagnostics == []
