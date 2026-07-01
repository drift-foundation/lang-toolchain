from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _compile(tmp_path: Path, source: str):
	src = tmp_path / "main.drift"
	src.write_text(source)
	modules, type_table, exception_catalog, module_exports, module_deps, parse_diags = parse_drift_workspace_to_hir(
		[src],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	assert parse_diags == []
	func_hirs, signatures, _ = flatten_modules(modules)
	_ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		enforce_entrypoint=True,
		entry="m::main",
	)
	return checked


def test_noncall_instantiation_slots_do_not_clobber_callsite_callinfo(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module m;

import std.containers as containers;

fn close(x: Int) nothrow -> Int {
	return x;
}

fn id<T>(x: T) nothrow -> T {
	return x;
}

pub fn main() nothrow -> Int {
	val m1: containers.HashMap<Int, Int> = {1: 2};
	val f = id<type Int>;
	val y = close(1);
	val z = f(2);
	if m1.len() < 0 {
		return 99;
	}
	return y + z;
}
"""
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert not any("internal: SSA return type does not match declared signature" in d.message for d in errors), errors
	assert not any("CallInfo param layout mismatch" in d.message for d in errors), errors
	assert errors == []
