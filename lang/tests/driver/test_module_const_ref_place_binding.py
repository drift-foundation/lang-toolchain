from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def test_module_const_ref_place_does_not_hit_binding_id_contract(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module main;

const S: String = "abc";

fn f(s: &String) nothrow -> Int {
	return 0;
}

pub fn main() nothrow -> Int {
	return f(&S);
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
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		enforce_entrypoint=True,
		entry="main::main",
	)
	assert ir != ""
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert not any("missing binding_id for place base" in d.message for d in errors), errors
	assert errors == []


def test_module_const_and_borrowed_field_in_constructor_args_compile(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module main;

const USER: String = "root";

struct Hello {
	plugin: String
}

struct Req {
	user: String,
	plugin: String
}

fn mk(h: &Hello) nothrow -> Req {
	return Req(user = USER, plugin = h.plugin);
}

pub fn main() nothrow -> Int {
	val h = Hello(plugin = "mysql_native_password");
	val r = mk(&h);
	if r.user == "root" and r.plugin == "mysql_native_password" {
		return 0;
	}
	return 1;
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
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		enforce_entrypoint=True,
		entry="main::main",
	)
	assert ir != ""
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert not any("missing binding_id for place base" in d.message for d in errors), errors
	assert errors == []


def test_mut_borrow_of_module_const_reports_checker_error_not_internal(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module main;

const S: String = "abc";

fn touch(s: &mut String) nothrow -> Int {
	return s.byte_length();
}

pub fn main() nothrow -> Int {
	return touch(&mut S);
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
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		enforce_entrypoint=True,
		entry="main::main",
	)
	assert ir == ""
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors, checked.diagnostics
	assert any(d.phase == "typecheck" for d in errors), errors
	assert not any("MIR lowering contract failure" in d.message for d in errors), errors
	assert not any("checker bug" in d.message for d in errors), errors
