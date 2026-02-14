from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def test_noncopy_field_projection_from_borrow_is_rejected(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module m

struct Payload {
	xs: Array<Int>
}

struct Wrapper {
	p: Payload
}

fn take(w: &Wrapper) nothrow -> Payload {
	return w.p;
}

fn main() nothrow -> Int {
	var xs: Array<Int> = [];
	xs.push(1);
	val w = Wrapper(p = Payload(xs = move xs));
	val y = take(&w);
	return y.xs.len;
}
"""
	)
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
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert any(d.phase == "typecheck" for d in errors), errors
	assert any("cannot copy value of type" in d.message for d in errors), errors
