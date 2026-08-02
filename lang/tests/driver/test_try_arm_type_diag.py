# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""try/catch result-type contract — exact-CODE pin
(work/control-flow-rvalue-ownership, P5 finding 2).

A try-expression whose attempt and catch arm produce DIFFERENT result types
used to reach lowering and crash as an LLVM `phi with mixed incoming types`
(a MIR/LLVM contract failure).  The checker now rejects it upstream with
`E-TRY-ARM-TYPE`.  The e2e fixture can only match message text; this driver pin
asserts the exact CODE — exactly ONE error, code `E-TRY-ARM-TYPE` — and that NO
MIR/LLVM contract failure surfaces (neither a raised NotImplementedError from
lowering nor a `phi with mixed` diagnostic).
"""
from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


_SRC = """
module m;
fn mkStr_t() throws -> String { return "aa" + ""; }
fn mkInt() nothrow -> Int { return 7; }

pub fn main() nothrow -> Int {
	val v = try mkStr_t() catch { mkInt() };
	return 0;
}
"""


def _compile(tmp_path: Path, source: str):
	src = tmp_path / "main.drift"
	src.write_text(source)
	modules, tt, exc, mexp, mdeps, pd = parse_drift_workspace_to_hir(
		[src], module_paths=[tmp_path], stdlib_root=stdlib_root(), test_build_only=True)
	assert pd == [], [str(d) for d in pd]
	fh, sig, _ = flatten_modules(modules)
	# Must NOT raise: the mismatch is rejected in the checker, before lowering can
	# hit the `phi with mixed incoming types` NotImplementedError.
	_ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=fh, signatures=sig, exc_env=exc, type_table=tt,
		module_exports=mexp, module_deps=mdeps, enforce_entrypoint=True, entry="m::main")
	return checked


def test_try_arm_type_mismatch_exact_code_no_contract_failure(tmp_path: Path) -> None:
	checked = _compile(tmp_path, _SRC)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	codes = [d.code for d in errors]
	# Exactly one error, exactly the E-TRY-ARM-TYPE code.
	assert codes == ["E-TRY-ARM-TYPE"], [(d.code, d.message) for d in errors]
	# No MIR/LLVM contract failure leaked into the diagnostics.
	blob = " ".join(d.message for d in checked.diagnostics).lower()
	assert "phi with mixed" not in blob, [d.message for d in checked.diagnostics]
	assert "notimplemented" not in blob, [d.message for d in checked.diagnostics]
	assert not any("contract" in (d.code or "").lower() for d in errors), codes
