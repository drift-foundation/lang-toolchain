#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-

from __future__ import annotations

from pathlib import Path

from lang2.driftc.parser import parse_drift_to_hir
from lang2.driftc.type_checker import TypeChecker


def _check_main(src: str, tmp_path: Path) -> list:
	src_path = tmp_path / "struct_generics.drift"
	src_path.write_text(src)
	func_hirs, sigs, fn_ids_by_name, type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src_path)
	assert diagnostics == []
	fn_ids = fn_ids_by_name.get("main") or []
	assert len(fn_ids) == 1
	main_id = fn_ids[0]
	main_block = func_hirs[main_id]
	main_sig = sigs.get(main_id)
	tc = TypeChecker(type_table=type_table)
	result = tc.check_function(
		main_id,
		main_block,
		param_types=None,
		return_type=main_sig.return_type_id if main_sig is not None else None,
		signatures_by_id=sigs,
		callable_registry=None,
	)
	return result.diagnostics


def test_struct_generic_ctor_and_field_access(tmp_path: Path) -> None:
	diags = _check_main(
		"""
struct Box<T> { value: T }

fn main() returns Int {
	val b: Box<Int> = Box<type Int>(1);
	return b.value;
}
""",
		tmp_path,
	)
	assert diags == []


def test_generic_impl_method_substitutes_receiver_params(tmp_path: Path) -> None:
	diags = _check_main(
		"""
struct Box<T> { value: T }

implement<T> Box<T> {
	fn get(self: Box<T>) returns T { return self.value; }
}

fn main() returns Int {
	val b: Box<Int> = Box<type Int>(1);
	return b.get();
}
""",
		tmp_path,
	)
	assert diags == []
