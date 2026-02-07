# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.method_registry import CallableRegistry, CallableSignature, Visibility
from lang.driftc.type_checker import TypeChecker
from lang.driftc.test_helpers import build_linked_world
from lang.driftc.impl_index import GlobalImplIndex
from lang.driftc.traits.enforce import enforce_fn_requires


def _write(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _build_registry(signatures: dict) -> tuple[CallableRegistry, dict[object, int]]:
	registry = CallableRegistry()
	module_ids: dict[object, int] = {None: 0}
	next_id = 1
	for fn_id, sig in signatures.items():
		if getattr(sig, "is_wrapper", False):
			continue
		if sig.param_type_ids is None or sig.return_type_id is None:
			continue
		module_id = module_ids.setdefault(sig.module, len(module_ids))
		if sig.is_method:
			continue
		registry.register_free_function(
			callable_id=next_id,
			name=fn_id.name,
			module_id=module_id,
			visibility=Visibility.public(),
			signature=CallableSignature(param_types=tuple(sig.param_type_ids), result_type=sig.return_type_id),
			fn_id=fn_id,
			is_generic=bool(sig.type_params),
		)
		next_id += 1
	return registry, module_ids


def test_trait_enforce_lambda_arg_infers_fn0(tmp_path: Path) -> None:
	files = {
		Path("m_main/main.drift"): """
module m_main

import std.concurrent as conc;

fn main() nothrow -> Int {
	var t = conc.spawn(| | => { return 1; });
	val _ = t.join_timeout(conc.Duration(millis = 0));
	return 0;
}
""",
	}
	for rel, content in files.items():
		_write(tmp_path / rel, content)
	paths = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, _exc_catalog, _exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == []
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	registry, module_ids = _build_registry(signatures)
	visible_mods = tuple(sorted(module_ids.setdefault(m, len(module_ids)) for m in (module_deps.get("m_main", set()) | {"m_main"})))
	linked_world, require_env = build_linked_world(type_table)
	main_id = [fid for fid in func_hirs if fid.module == "m_main" and fid.name == "main"][0]
	tc = TypeChecker(type_table=type_table)
	result = tc.check_function(
		main_id,
		func_hirs[main_id],
		param_types=[],
		return_type=signatures[main_id].return_type_id,
		signatures_by_id=signatures,
		callable_registry=registry,
		impl_index=GlobalImplIndex(),
		linked_world=linked_world,
		require_env=require_env,
		visible_modules=visible_mods,
		current_module="m_main",
	)
	assert not any(d.code == "E_REQUIREMENT_NOT_SATISFIED" for d in result.diagnostics)
	enforce_res = enforce_fn_requires(
		linked_world,
		require_env,
		result.typed_fn,
		type_table,
		module_name="m_main",
		signatures=signatures,
		visible_modules=module_deps.get("m_main", set()) | {"m_main"},
	)
	assert not any(d.code == "E_REQUIREMENT_NOT_SATISFIED" for d in enforce_res.diagnostics)
