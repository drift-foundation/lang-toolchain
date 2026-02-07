#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-

from __future__ import annotations

from pathlib import Path

from lang.driftc.method_registry import CallableRegistry, CallableSignature, Visibility
from lang.driftc.parser import parse_drift_to_hir
from lang.driftc.type_checker import TypeChecker


def _check_main(src: str, tmp_path: Path) -> list:
	src_path = tmp_path / "infer_nested_types.drift"
	src_path.write_text(src)
	module, type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src_path)
	assert diagnostics == []
	registry = CallableRegistry()
	signatures_by_id = module.signatures_by_id
	module_id = 0
	callable_id = 1
	for fn_id, sig in module.signatures_by_id.items():
		if sig.is_method:
			continue
		if sig.param_type_ids is None or sig.return_type_id is None:
			continue
		registry.register_free_function(
			callable_id=callable_id,
			name=fn_id.name,
			module_id=module_id,
			visibility=Visibility.public(),
			signature=CallableSignature(param_types=tuple(sig.param_type_ids), result_type=sig.return_type_id),
			fn_id=fn_id,
		)
		callable_id += 1
	fn_ids = module.fn_ids_by_name.get("main") or []
	assert len(fn_ids) == 1
	main_id = fn_ids[0]
	main_block = module.func_hirs[main_id]
	main_sig = module.signatures_by_id.get(main_id)
	tc = TypeChecker(type_table=type_table)
	result = tc.check_function(
		main_id,
		main_block,
		param_types=None,
		return_type=main_sig.return_type_id if main_sig is not None else None,
		signatures_by_id=signatures_by_id,
		callable_registry=registry,
		visible_modules=(module_id,),
		current_module=module_id,
	)
	return result.diagnostics


def test_infer_type_args_from_nested_struct_arg(tmp_path: Path) -> None:
	diags = _check_main(
		"""
struct Box<T> { value: T }

fn id<T>(x: Box<T>) -> T { return x.value; }

fn main() -> Int {
	val b: Box<Int> = Box<type Int>(1);
	return id(b);
}
""",
		tmp_path,
	)
	assert diags == []


def test_infer_type_args_from_double_nested_struct(tmp_path: Path) -> None:
	diags = _check_main(
		"""
struct Box<T> { value: T }

fn inner<T>(x: Box<Box<T>>) -> T { return x.value.value; }

fn main() -> Int {
	val inner_box: Box<Int> = Box<type Int>(1);
	val outer_box: Box<Box<Int>> = Box<type Box<Int>>(inner_box);
	return inner(outer_box);
}
""",
		tmp_path,
	)
	assert diags == []
