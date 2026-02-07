# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.test_helpers import build_linked_world
from lang.driftc.type_checker import TypeChecker
from lang.driftc.impl_index import GlobalImplIndex
from lang.driftc.trait_index import GlobalTraitImplIndex, GlobalTraitIndex
from lang.driftc.method_registry import CallableRegistry, CallableSignature, SelfMode, Visibility
from lang.driftc.core.function_id import FunctionId


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _build_registry(signatures: dict[FunctionId, object]) -> tuple[CallableRegistry, dict[object, int]]:
	registry = CallableRegistry()
	module_ids: dict[object, int] = {None: 0}
	next_id = 1
	for fn_id, sig in signatures.items():
		if sig.param_type_ids is None or sig.return_type_id is None:
			continue
		module_id = module_ids.setdefault(sig.module, len(module_ids))
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
				visibility=Visibility.public() if sig.is_pub else Visibility.private(),
				signature=CallableSignature(param_types=tuple(sig.param_type_ids), result_type=sig.return_type_id),
				fn_id=fn_id,
				impl_id=next_id,
				impl_target_type_id=sig.impl_target_type_id,
				self_mode=self_mode,
				is_generic=bool(sig.type_params or getattr(sig, "impl_type_params", [])),
			)
		else:
			callable_name = fn_id.name if fn_id.module == "main" else f"{fn_id.module}::{fn_id.name}"
			registry.register_free_function(
				callable_id=next_id,
				name=callable_name,
				module_id=module_id,
				visibility=Visibility.public(),
				signature=CallableSignature(param_types=tuple(sig.param_type_ids), result_type=sig.return_type_id),
				fn_id=fn_id,
				is_generic=bool(sig.type_params),
			)
		next_id += 1
	return registry, module_ids


def _typecheck_main(src: str, tmp_path: Path) -> list[object]:
	path = tmp_path / "main.drift"
	_write_file(path, src)
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		[path],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == []
	func_hirs, signatures, fn_ids_by_name = flatten_modules(modules)
	registry, module_ids = _build_registry(signatures)
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
	trait_scope_by_module: dict[str, list[object]] = {}
	for _mod, exports in module_exports.items():
		if isinstance(exports, dict):
			scope = exports.get("trait_scope", [])
			if isinstance(scope, list):
				trait_scope_by_module[_mod] = list(scope)
	linked_world, require_env = build_linked_world(type_table)
	main_ids = fn_ids_by_name.get("main") or []
	assert len(main_ids) == 1
	main_id = main_ids[0]
	main_block = func_hirs[main_id]
	main_sig = signatures.get(main_id)
	param_types = {}
	if main_sig and main_sig.param_names and main_sig.param_type_ids:
		param_types = {pname: pty for pname, pty in zip(main_sig.param_names, main_sig.param_type_ids)}
	current_mod = module_ids.setdefault(main_sig.module, len(module_ids)) if main_sig else 0
	visible_mods = tuple(sorted(module_ids.setdefault(mod, len(module_ids)) for mod in module_deps.get("main", {"main"})))
	tc = TypeChecker(type_table=type_table)
	result = tc.check_function(
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
	)
	return result.diagnostics


def test_require_commas_enforced(tmp_path: Path) -> None:
	diags = _typecheck_main(
		"""
module main

trait Show { fn show(self: Self) -> Int }
trait Debug { fn debug(self: Self) -> Int }

implement Show for Int { pub fn show(self: Int) -> Int { return 0; } }

fn id<T>(var x: T) -> T require T is Show, T is Debug { return move x; }

fn main() nothrow -> Int{
	val x: Int = id(1);
	return x;
}
""",
		tmp_path,
	)
	assert diags
	assert any(d.code == "E_REQUIREMENT_NOT_SATISFIED" for d in diags)
