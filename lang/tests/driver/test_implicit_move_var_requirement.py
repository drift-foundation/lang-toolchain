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
		entry="main",
	)
	return checked


def test_noncopy_byvalue_direct_call_from_val_is_allowed(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module main;

struct Blob {
	xs: Array<Int>
}

fn take(v: Blob) nothrow -> Int {
	return 0;
}

	pub fn main() nothrow -> Int {
		val b = Blob(xs = [1]);
		return take(move b);
	}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == []


def test_noncopy_byvalue_indirect_call_from_val_is_allowed(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module main;

struct Blob {
	xs: Array<Int>
}

fn take(v: Blob) nothrow -> Int {
	return 0;
}

pub fn main() nothrow -> Int {
	val b = Blob(xs = [1]);
	var f = take;
	return f(move b);
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == []


def test_noncopy_byvalue_interface_call_from_val_is_allowed(tmp_path: Path) -> None:
	checked = _compile(
		tmp_path,
		"""
module main;

struct Blob {
	xs: Array<Int>
}

interface Take {
	fn take(self: &Self, v: Blob) nothrow -> Int;
}

struct Impl {}

implement Take for Impl {
	fn take(self: &Impl, v: Blob) nothrow -> Int {
		return 8;
	}
}

pub fn main() nothrow -> Int {
	var x = Impl();
	var t: Take = x;
	val b = Blob(xs = [1]);
	return t.take(move b);
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == []
