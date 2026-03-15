# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.core.function_id import FunctionId
from lang.driftc.method_registry import CallableRegistry
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir
from lang.driftc.test_helpers import build_linked_world
from lang.driftc.trait_index import GlobalTraitIndex
from lang.driftc.type_checker import TypeChecker


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def test_type_param_receiver_trait_method_uses_require_type_args(tmp_path: Path) -> None:
	mod_root = tmp_path / "mods"
	src = mod_root / "main.drift"
	_write_file(
		src,
		"""
module m_main;

trait BinarySearchable<T> { fn compare_key(self: &Self, i: Int, key: &T) -> Int; }
use trait BinarySearchable;

fn f<R, T>(r: &R, key: &T) -> Int require R is BinarySearchable<T> {
	return r.compare_key(0, key);
}
""",
	)
	paths = sorted(mod_root.rglob("*.drift"))
	modules, type_table, _exc_catalog, module_exports, _module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=None,
	)
	assert diagnostics == []
	func_hirs, sigs, _fn_ids = flatten_modules(modules)
	linked_world, _require_env = build_linked_world(type_table)
	trait_index = GlobalTraitIndex.from_trait_worlds(getattr(type_table, "trait_worlds", None))
	trait_scope_by_module = {mid: (exports.get("trait_scope") or []) for mid, exports in module_exports.items()}
	module_ids: dict[object, int] = {None: 0}
	for sig in sigs.values():
		module_ids.setdefault(getattr(sig, "module", None), len(module_ids))
	fn_id: FunctionId | None = None
	for fid in func_hirs.keys():
		if getattr(fid, "name", None) == "f":
			fn_id = fid
			break
	assert fn_id is not None
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
		callable_registry=CallableRegistry(),
		trait_index=trait_index,
		trait_scope_by_module=trait_scope_by_module,
		linked_world=linked_world,
		require_env=None,
		visible_modules=(current_mod,),
		current_module=current_mod,
	)
	assert result.diagnostics == []
