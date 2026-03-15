# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.borrow_checker_pass import BorrowChecker
from lang.driftc.parser import parse_drift_to_hir
from lang.driftc.stage1.normalize import normalize_hir
from lang.driftc.type_checker import TypeChecker


def _borrow_diags(src: str, *, tmp_path: Path) -> list[object]:
	path = tmp_path / "main.drift"
	path.write_text(src)
	module, type_table, _exc_env, diagnostics = parse_drift_to_hir(path)
	assert diagnostics == []
	fn_ids = module.fn_ids_by_name.get("main") or []
	assert len(fn_ids) == 1
	fn_id = fn_ids[0]
	block = normalize_hir(module.func_hirs[fn_id])
	tc = TypeChecker(type_table)
	sig = module.signatures_by_id.get(fn_id)
	param_types = {}
	if sig and sig.param_names and sig.param_type_ids:
		param_types = {name: ty for name, ty in zip(sig.param_names, sig.param_type_ids)}
	res = tc.check_function(
		fn_id,
		block,
		param_types=param_types,
		return_type=sig.return_type_id if sig is not None else None,
	)
	assert res.diagnostics == []
	bc = BorrowChecker.from_typed_fn(res.typed_fn, type_table=type_table, signatures_by_id=None, enable_auto_borrow=True)
	return bc.check_block(res.typed_fn.body)


def test_capture_shared_conflicts_with_mut_arg(tmp_path: Path) -> None:
	diags = _borrow_diags(
		"""
module main;

fn main() -> Int {
	var x: Int = 1;
	return (|y: &mut Int| => { return x; })(&mut x);
}
""",
		tmp_path=tmp_path,
	)
	assert any("mutable borrow" in d.message for d in diags)


def test_capture_mut_conflicts_with_shared_arg(tmp_path: Path) -> None:
	diags = _borrow_diags(
		"""
module main;

fn main() -> Int {
	var x: Int = 1;
	return (|y: &Int| => { x = 2; return 0; })(&x);
}
""",
		tmp_path=tmp_path,
	)
	assert any("shared borrow" in d.message for d in diags)


def test_capture_field_conflicts_with_mut_arg(tmp_path: Path) -> None:
	diags = _borrow_diags(
		"""
module main;

struct S(f: Int, g: Int);
fn main() -> Int {
	var s: S = S(1, 2);
	return (|y: &mut Int| => { return s.f; })(&mut s.f);
}
""",
		tmp_path=tmp_path,
	)
	assert any("mutable borrow" in d.message for d in diags)


def test_capture_field_disjoint_mut_arg_ok(tmp_path: Path) -> None:
	diags = _borrow_diags(
		"""
module main;

struct S(f: Int, g: Int);
fn main() -> Int {
	var s: S = S(1, 2);
	return (|y: &mut Int| => { return s.f; })(&mut s.g);
}
""",
		tmp_path=tmp_path,
	)
	assert diags == []
