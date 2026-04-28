# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc import stage1 as H
from lang.driftc.impl_index import GlobalImplIndex
from lang.driftc.method_registry import CallableRegistry, CallableSignature, SelfMode, Visibility
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.driftc.test_helpers import build_linked_world
from lang.driftc.trait_index import GlobalTraitImplIndex, GlobalTraitIndex
from lang.driftc.type_checker import TypeChecker


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content, encoding="utf-8")


def _collect_calls(block: H.HBlock) -> list[H.HCall]:
	calls: list[H.HCall] = []

	def walk(obj: object) -> None:
		if isinstance(obj, H.HCall):
			calls.append(obj)
		for field in getattr(obj, "__dataclass_fields__", {}) or {}:
			val = getattr(obj, field, None)
			if isinstance(val, list):
				for item in val:
					if isinstance(item, H.HNode):
						walk(item)
			elif isinstance(val, H.HNode):
				walk(val)

	walk(block)
	return calls


def test_lambda_return_inferred_in_callback0(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	_write_file(
		src,
		"""
module m_main;

import std.core as core;

fn main() nothrow -> Int {
	var cb = core.callback0(| | => {
		val r: Optional<Int> = Optional::Some(7);
		match r {
			Some(v) => { return v; },
			None => { return 8; }
		}
	});
	val _ = move cb;
	return 0;
}
""",
	)
	paths = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == []
	func_hirs, signatures, fn_ids_by_name = flatten_modules(modules)
	registry = CallableRegistry()
	module_ids: dict[object, int] = {None: 0}
	next_id = 1
	for fn_id, sig in signatures.items():
		if sig.return_type_id is None or getattr(sig, "is_wrapper", False):
			continue
		module_name = getattr(fn_id, "module", None) or getattr(sig, "module", None)
		module_id = module_ids.setdefault(module_name, len(module_ids))
		if sig.is_method:
			if sig.impl_target_type_id is None or sig.self_mode is None:
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
				signature=CallableSignature(param_types=tuple(sig.param_type_ids or []), result_type=sig.return_type_id),
				fn_id=fn_id,
				impl_id=next_id,
				impl_target_type_id=sig.impl_target_type_id,
				self_mode=self_mode,
				is_generic=bool(sig.type_params or getattr(sig, "impl_type_params", [])),
			)
		else:
			registry.register_free_function(
				callable_id=next_id,
				name=fn_id.name,
				module_id=module_id,
				visibility=Visibility.public(),
				signature=CallableSignature(param_types=tuple(sig.param_type_ids or []), result_type=sig.return_type_id),
				fn_id=fn_id,
				is_generic=bool(sig.type_params),
			)
		next_id += 1
	impl_index = GlobalImplIndex.from_module_exports(
		module_exports=module_exports,
		type_table=type_table,
		module_ids=module_ids,
	)
	trait_index = GlobalTraitIndex.from_trait_worlds(getattr(type_table, "trait_worlds", None))
	trait_impl_index = GlobalTraitImplIndex.from_module_exports(
		module_exports=module_exports,
		type_table=type_table,
		module_ids=module_ids,
	)
	trait_scope_by_module: dict[str, list] = {}
	if isinstance(module_exports, dict):
		for _mod_name, exp in module_exports.items():
			if isinstance(exp, dict):
				scope = exp.get("trait_scope", [])
				if isinstance(scope, list):
					trait_scope_by_module[_mod_name] = list(scope)
	linked_world, require_env = build_linked_world(type_table)
	main_id = None
	for fn_id in signatures.keys():
		if fn_id.name == "main" and fn_id.module == "m_main":
			main_id = fn_id
			break
	assert main_id is not None
	main_block = func_hirs[main_id]
	main_sig = signatures.get(main_id)
	param_types = {}
	if main_sig and main_sig.param_names and main_sig.param_type_ids:
		param_types = {pname: pty for pname, pty in zip(main_sig.param_names, main_sig.param_type_ids)}
	current_mod = module_ids.setdefault(main_sig.module, len(module_ids))
	visible_mods = tuple(sorted(module_ids.values()))
	visibility_provenance = {mid: (name if name is not None else "<unknown>",) for name, mid in module_ids.items()}
	result = TypeChecker(type_table=type_table).check_function(
		main_id,
		main_block,
		param_types=param_types,
		return_type=main_sig.return_type_id if main_sig is not None else None,
		signatures_by_id=signatures,
		callable_registry=registry,
		impl_index=impl_index,
		trait_index=trait_index,
		trait_impl_index=trait_impl_index,
		trait_scope_by_module=trait_scope_by_module,
		linked_world=linked_world,
		require_env=require_env,
		visible_modules=visible_mods,
		current_module=current_mod,
		visibility_provenance=visibility_provenance,
	)
	assert result.diagnostics == []
	cb_call = None
	for call in _collect_calls(main_block):
		fn = call.fn
		if isinstance(fn, H.HVar) and fn.module_id == "std.core" and fn.name == "callback0":
			cb_call = call
			break
	assert cb_call is not None
	csid = getattr(cb_call, "callsite_id", None)
	assert isinstance(csid, int)
	info = result.typed_fn.call_info_by_callsite_id.get(csid)
	assert info is not None
	cb_ty = info.sig.user_ret_type
	inst = type_table.get_interface_instance(cb_ty)
	assert inst is not None
	base = type_table.interface_bases.get(inst.base_id)
	assert base is not None
	assert base.name == "Callback0"
	assert len(inst.type_args) == 1
	assert inst.type_args[0] == type_table.ensure_int()
