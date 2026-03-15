# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.core.function_id import fn_name_key
from lang.driftc.parser import parse_drift_to_hir
from lang.driftc.test_helpers import build_linked_world
from lang.driftc.type_checker import TypeChecker
from lang.driftc.method_registry import CallableRegistry, CallableSignature, Visibility


def _typecheck_main_with_registry(src: Path):
	module, type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert diagnostics == []
	registry = CallableRegistry()
	next_id = 1
	for fn_id, sig in module.signatures_by_id.items():
		if sig.is_method:
			continue
		if sig.param_type_ids is None or sig.return_type_id is None:
			continue
		name = fn_id.name
		registry.register_free_function(
			callable_id=next_id,
			name=name,
			module_id=0,
			visibility=Visibility.public(),
			signature=CallableSignature(param_types=tuple(sig.param_type_ids), result_type=sig.return_type_id),
			fn_id=fn_id,
			is_generic=False,
		)
		next_id += 1
	linked_world, require_env = build_linked_world(type_table)
	tc = TypeChecker(type_table=type_table)
	fn_ids = module.fn_ids_by_name.get("main") or []
	assert len(fn_ids) == 1
	fn_id = fn_ids[0]
	main_block = module.func_hirs[fn_id]
	sig = module.signatures_by_id.get(fn_id)
	param_types = {}
	if sig and sig.param_names and sig.param_type_ids:
		param_types = {pname: pty for pname, pty in zip(sig.param_names, sig.param_type_ids) if pty is not None}
	call_signatures = {
		fn_name_key(fid.module, fid.name): s
		for fid, s in module.signatures_by_id.items()
		if not s.is_method
	}
	result = tc.check_function(
		fn_id,
		main_block,
		param_types=param_types,
		return_type=sig.return_type_id if sig is not None else None,
		callable_registry=registry,
		linked_world=linked_world,
		require_env=require_env,
		visible_modules=(0,),
		current_module=0,
	)
	return result


def test_require_filters_out_unmet_overload(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
trait A { fn a(self: Int) -> Int }
struct S { }
fn f<T>(x: T) -> Int require T is A { return 1; }
fn main(x: S) -> Int { return f(x); }
	"""
	)
	res = _typecheck_main_with_registry(src)
	# Type checker now enforces require clauses during overload resolution:
	# unmet require eliminates the overload, producing "no matching overload".
	assert any("no matching overload" in d.message for d in res.diagnostics)
