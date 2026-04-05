# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: deref of &DiagnosticValue into an owned return context
must emit CopyValue (drift_dv_clone), not a raw LoadRef alias.

Without CopyValue, the returned DV and the original share the same
inner string pointer without a balancing retain — leading to
double-free when both are destroyed.

This test pins the lowering behavior via emitted IR: the function
that returns *self on &DiagnosticValue must contain drift_dv_clone.
"""
from __future__ import annotations

import re
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


def test_dv_deref_emits_clone(tmp_path: Path) -> None:
	"""*self on &DiagnosticValue in a return context must emit drift_dv_clone."""
	source = (
		"module main;\n"
		"import std.core as core;\n"
		"import std.format as fmt;\n"
		"\n"
		"fn identity(dv: &DiagnosticValue) nothrow -> DiagnosticValue {\n"
		"\treturn *dv;\n"
		"}\n"
		"\n"
		"fn main() nothrow -> Int {\n"
		"\tval dv = DiagnosticValue::String(fmt.format_int(42));\n"
		"\tval copy = identity(&dv);\n"
		"\treturn 0;\n"
		"}\n"
	)
	ir = _compile_ir(tmp_path, source)
	# Extract the identity function's IR.
	match = re.search(r'define [^\n]*@"?identity"?[^\n]*\{(.*?)\n\}', ir, re.DOTALL)
	assert match, "identity function not found in IR"
	identity_ir = match.group(1)
	assert "drift_dv_clone" in identity_ir, (
		"deref of &DiagnosticValue in identity() must emit drift_dv_clone, "
		"not a raw LoadRef alias"
	)
