from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _extract_llvm_function(ir: str, name: str) -> str:
	lines = ir.splitlines()
	out: list[str] = []
	in_fn = False
	for line in lines:
		if line.startswith("define ") and (f"@\"{name}\"" in line or f"@{name}" in line):
			in_fn = True
		if in_fn:
			out.append(line)
			if line.strip() == "}":
				break
	return "\n".join(out)


def test_throw_emits_captured_locals_into_error_frames(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module main;

error E { code: Int }
fn fail() -> Int {
	val ^count: Int as "step.count" = 7;
	val ^msg: String = "boom";
	throw E(code = 1);
}

pub fn main() nothrow -> Int {
	return try fail() catch E(_e) { 0 } catch { 1 };
}
"""
	)
	modules, type_table, exc_catalog, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		[src],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert not diags
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exc_catalog,
		entry="main",
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
	)
	assert not checked.diagnostics
	fail_ir = _extract_llvm_function(ir, "fail")
	# Slice 7b: legacy DV captures path retired.  Captured locals are
	# now projected directly to JSON text via per-scalar
	# `core.diagnostic_json_*` helpers and concatenated into a frame
	# JSON appended via `drift_error_append_context_frame`.  No more
	# `drift_error_add_local_dv` emission.
	assert "drift_error_add_local_dv" not in fail_ir
	assert "drift_error_append_context_frame" in fail_ir
	assert "step.count" in ir
	assert "msg" in ir
