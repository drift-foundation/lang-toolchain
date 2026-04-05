# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: ConstructDV(String) must set owns_string_arg based on
whether the inner expression yields a fresh owned value or references
an existing binding.

- Call-temporary DV string: owns_string_arg=True → codegen emits drift_dv_string_move
- HVar-based DV string: owns_string_arg=False → codegen emits drift_dv_string (retain)

Without this distinction, codegen either leaks (missing release for owned
temps) or double-frees (releasing borrowed bindings).

Tested via IR output: the emitted LLVM call target proves which ownership
mode the lowering chose.
"""
from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.tests.support.module_packages import mk_module


def _compile_ir(tmp_path: Path, source: str) -> str:
	mod_root = tmp_path / "mods"
	mod_root.mkdir(parents=True, exist_ok=True)
	(mod_root / "main.drift").write_text(source)
	module_packages: dict = {}
	mk_module(module_packages, "main", "main")
	paths = sorted(mod_root.rglob("*.drift"))
	modules, type_table, exc_catalog, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		external_module_packages=module_packages,
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	assert not diags, diags
	func_hirs, signatures, _ = flatten_modules(modules)
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exc_catalog,
		entry="main",
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
	)
	assert not checked.diagnostics, checked.diagnostics
	assert ir
	return ir


def test_call_temporary_dv_string_uses_move(tmp_path: Path) -> None:
	"""DiagnosticValue::String(fn_call()) must emit drift_dv_string_move."""
	source = (
		"module main;\n"
		"import std.core as core;\n"
		"import std.format as fmt;\n"
		"\n"
		"fn main() nothrow -> Int {\n"
		"\tval dv = DiagnosticValue::String(fmt.format_int(42));\n"
		"\treturn 0;\n"
		"}\n"
	)
	ir = _compile_ir(tmp_path, source)
	assert "call %DriftDiagnosticValue @drift_dv_string_move(" in ir, (
		"call-temporary DV string should use drift_dv_string_move"
	)


def test_var_reference_dv_string_uses_retain(tmp_path: Path) -> None:
	"""DiagnosticValue::String(local_var) must emit drift_dv_string (retain)."""
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
	# The exception field for 's' (HVar) must use drift_dv_string (retain),
	# NOT drift_dv_string_move.  The function body for do_throw should
	# contain a retain call, not a move call, for the variable binding.
	# Extract only the do_throw function's IR to avoid matching stdlib usage.
	import re
	match = re.search(r'define [^\n]*@"?do_throw"?[^\n]*\{(.*?)\n\}', ir, re.DOTALL)
	assert match, "do_throw function not found in IR"
	do_throw_ir = match.group(1)
	assert "drift_dv_string_move" not in do_throw_ir, (
		"var-reference DV string in do_throw should NOT use drift_dv_string_move"
	)
	assert "drift_dv_string(" in do_throw_ir, (
		"var-reference DV string in do_throw should use drift_dv_string (retain)"
	)
