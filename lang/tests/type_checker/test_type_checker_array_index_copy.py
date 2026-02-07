#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-

from __future__ import annotations

from pathlib import Path

from lang.driftc.parser import parse_drift_to_hir
from lang.driftc.type_checker import TypeChecker


def _check_fn(src: str, fn_name: str, tmp_path: Path) -> list:
	src_path = tmp_path / "array_index_copy.drift"
	src_path.write_text(src)
	module, type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src_path)
	assert diagnostics == []
	fn_ids = module.fn_ids_by_name.get(fn_name) or []
	assert len(fn_ids) == 1
	fn_id = fn_ids[0]
	fn_block = module.func_hirs[fn_id]
	fn_sig = module.signatures_by_id.get(fn_id)
	tc = TypeChecker(type_table=type_table)
	param_types = None
	if fn_sig is not None and fn_sig.param_names and fn_sig.param_type_ids:
		param_types = dict(zip(fn_sig.param_names, fn_sig.param_type_ids))
	result = tc.check_function(
		fn_id,
		fn_block,
		param_types=param_types,
		return_type=fn_sig.return_type_id if fn_sig is not None else None,
		signatures_by_id=module.signatures_by_id,
		callable_registry=None,
	)
	return result.diagnostics


def test_array_index_non_copy_rejected(tmp_path: Path) -> None:
	diags = _check_fn(
		"""
struct File { data: Array<Int> }

fn get(xs: Array<File>) -> Int {
	val f = xs[0];
	return 0;
}
""",
		"get",
		tmp_path,
	)
	assert any("cannot copy value of type 'File" in d.message for d in diags)


def test_array_pop_non_copy_allowed(tmp_path: Path) -> None:
	diags = _check_fn(
		"""
struct File(data: Array<Int>);

fn pop_ok() -> Int {
	var inner = [1];
	var arr: Array<File> = [];
	arr.push(File(data = move inner));
	val _v = arr.pop();
	return 0;
}
""",
		"pop_ok",
		tmp_path,
	)
	assert diags == []


def test_array_literal_non_copy_rejected(tmp_path: Path) -> None:
	diags = _check_fn(
		"""
struct File { data: Array<Int> }

fn make() -> Int {
	var inner = [1];
	val f = File(data = move inner);
	val xs = [f];
	return 0;
}
""",
		"make",
		tmp_path,
	)
	assert any("array literals require Copy element type" in d.message for d in diags)


def test_array_index_borrow_non_copy_allowed(tmp_path: Path) -> None:
	diags = _check_fn(
		"""
struct File { data: Array<Int> }

fn borrow_ok(xs: &Array<File>) -> Int {
	val _p = &xs[0];
	return 0;
}
""",
		"borrow_ok",
		tmp_path,
	)
	assert diags == []


def test_array_index_borrow_mut_non_copy_allowed(tmp_path: Path) -> None:
	diags = _check_fn(
		"""
struct File { data: Array<Int> }

fn borrow_mut_ok(xs: &mut Array<File>) -> Int {
	val _p = &mut xs[0];
	return 0;
}
""",
		"borrow_mut_ok",
		tmp_path,
	)
	assert diags == []
