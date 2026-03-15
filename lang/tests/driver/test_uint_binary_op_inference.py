# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Regression: checker must infer Uint result type for binary ops on Uint operands.

Before fix: val r = u >> cast<Uint>(n) infers r as None, causing downstream
bitwise ops like (r | l) to emit spurious "bitwise operators require Uint operands".
"""
from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _compile(tmp_path: Path, content: str):
	mod_root = tmp_path / "mods"
	src = mod_root / "main.drift"
	_write_file(src, content)
	paths = sorted(mod_root.rglob("*.drift"))
	modules, type_table, exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	func_hirs, sigs, _fn_ids = flatten_modules(modules)
	_, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=sigs,
		exc_env=exc_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
	)
	return list(diagnostics) + list(checked.diagnostics)


def _errors(diags):
	return [d for d in diags if getattr(d, "severity", None) == "error"]


def test_uint_shift_result_inferred_for_bitwise(tmp_path: Path) -> None:
	"""val r = u >> cast<Uint>(n); val l = ...; r | l — must not error."""
	diags = _compile(
		tmp_path,
		"""
module m_main;

fn main() nothrow -> Int {
	var u: Uint = cast<Uint>(255);
	val r = u >> cast<Uint>(4);
	val l = u << cast<Uint>(4);
	val combined = r | l;
	if combined == cast<Uint>(0) {
		return 1;
	}
	return 0;
}
""",
	)
	errors = _errors(diags)
	assert errors == [], errors


def test_uint_xor_result_inferred(tmp_path: Path) -> None:
	"""val x = a ^ b; val y = x & c — chained bitwise must not error."""
	diags = _compile(
		tmp_path,
		"""
module m_main;

fn main() nothrow -> Int {
	var a: Uint = cast<Uint>(170);
	var b: Uint = cast<Uint>(85);
	var c: Uint = cast<Uint>(255);
	val x = a ^ b;
	val y = x & c;
	val z = y | a;
	if z == cast<Uint>(0) {
		return 1;
	}
	return 0;
}
""",
	)
	errors = _errors(diags)
	assert errors == [], errors


def test_uint_add_sub_result_inferred(tmp_path: Path) -> None:
	"""val sum = a + b; val diff = a - b — arithmetic on Uint inferred as Uint."""
	diags = _compile(
		tmp_path,
		"""
module m_main;

fn main() nothrow -> Int {
	var a: Uint = cast<Uint>(100);
	var b: Uint = cast<Uint>(50);
	val sum = a + b;
	val diff = a - b;
	val shifted = sum >> cast<Uint>(1);
	val masked = diff & cast<Uint>(255);
	if shifted == cast<Uint>(0) {
		return 1;
	}
	return 0;
}
""",
	)
	errors = _errors(diags)
	assert errors == [], errors


def test_uint_mul_div_mod_result_inferred(tmp_path: Path) -> None:
	"""val p = a * b; val q = a / b; val m = a % b — all Uint-typed."""
	diags = _compile(
		tmp_path,
		"""
module m_main;

fn main() nothrow -> Int {
	var a: Uint = cast<Uint>(200);
	var b: Uint = cast<Uint>(3);
	val p = a * b;
	val q = a / b;
	val m = a % b;
	val check = p ^ q ^ m;
	if check == cast<Uint>(0) {
		return 1;
	}
	return 0;
}
""",
	)
	errors = _errors(diags)
	assert errors == [], errors
