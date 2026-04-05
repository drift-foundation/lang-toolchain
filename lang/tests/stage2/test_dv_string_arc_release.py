# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: string_arc must release owned string temps consumed by
ConstructDV(String), but must NOT release borrowed locals.

Verifies at the IR level that:
- DiagnosticValue::String(fmt.format_int(...)) emits drift_string_release
  for the format_int temp (owned creator → last-use release)
- throw Info(s, ...) where s is a local does NOT emit an extra release
  for the string arg to drift_dv_string (borrowed → scope-exit handles it)
"""
from __future__ import annotations

import re
from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.tests.support.module_packages import mk_module


def _compile_ir(tmp_path: Path, source: str, entry: str = "main") -> str:
	mod_root = tmp_path / "mods"
	mod_root.mkdir(parents=True, exist_ok=True)
	(mod_root / "main.drift").write_text(source)
	module_packages: dict = {}
	mk_module(module_packages, "main", "main")
	paths = sorted(mod_root.rglob("*.drift"))
	modules, type_table, exc_catalog, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		paths, module_paths=[mod_root], external_module_packages=module_packages,
		stdlib_root=stdlib_root(), test_build_only=True,
	)
	assert not diags, diags
	func_hirs, signatures, _ = flatten_modules(modules)
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs, signatures=signatures, exc_env=exc_catalog,
		entry=entry, type_table=type_table, module_exports=module_exports,
		module_deps=module_deps,
	)
	assert not checked.diagnostics, checked.diagnostics
	assert ir
	return ir


def _extract_func(ir: str, name: str) -> str:
	"""Extract one function body from LLVM IR."""
	pattern = rf'define [^\n]*@"?{re.escape(name)}"?\([^\n]*\{{(.*?)\n\}}'
	m = re.search(pattern, ir, re.DOTALL)
	assert m, f"function {name!r} not found in IR"
	return m.group(1)


def test_owned_temp_gets_string_release(tmp_path: Path) -> None:
	"""format_int temp passed to DV::String must get drift_string_release."""
	source = (
		"module main;\n"
		"import std.core as core;\n"
		"import std.format as fmt;\n"
		"\n"
		"fn do_work(code: Int) nothrow -> Int {\n"
		"\tval dv = DiagnosticValue::String(fmt.format_int(code));\n"
		"\treturn code;\n"
		"}\n"
		"\n"
		"fn main() nothrow -> Int { return do_work(0); }\n"
	)
	ir = _compile_ir(tmp_path, source)
	do_work = _extract_func(ir, "do_work")
	# The format_int result is an owned temp. After drift_dv_string retains,
	# string_arc must release the original via last-use machinery.
	assert "drift_dv_string(" in do_work, "ConstructDV(String) should call drift_dv_string"
	assert "drift_string_release(" in do_work, (
		"owned string temp consumed by ConstructDV(String) must get "
		"drift_string_release from string_arc last-use tracking"
	)


def test_borrowed_local_no_extra_release(tmp_path: Path) -> None:
	"""Exception field from local var must NOT get extra drift_string_release."""
	source = (
		"module main;\n"
		"import std.core as core;\n"
		"\n"
		"exception Info(msg: String)\n"
		"\n"
		"fn do_throw(s: String) -> Int {\n"
		"\tthrow Info(s);\n"
		"}\n"
		"\n"
		"fn main() nothrow -> Int {\n"
		"\tval r = try do_throw(\"hello\") catch { 0 };\n"
		"\treturn r;\n"
		"}\n"
	)
	ir = _compile_ir(tmp_path, source)
	do_throw = _extract_func(ir, "do_throw")
	# Count drift_string_release calls in do_throw.  The function has
	# one string param (s) which gets exactly one scope-exit release.
	# If ConstructDV(String) incorrectly adds another, there would be
	# two releases for the same string → double-free.
	releases = do_throw.count("drift_string_release(")
	dv_calls = do_throw.count("drift_dv_string(")
	# There should be at most one release per string local (scope-exit).
	# The DV construction must NOT add an extra release for the borrowed arg.
	assert dv_calls >= 1, "do_throw should call drift_dv_string for Info(s)"
	# One release for param s at scope-exit is expected.
	# More than one would indicate a double-release bug.
	assert releases <= 1, (
		f"do_throw has {releases} drift_string_release calls but only one "
		f"string param — borrowed DV string arg should NOT get extra release"
	)
