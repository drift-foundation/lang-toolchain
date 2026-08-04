# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""or_throw match-receiver diagnostic-CODE pin
(control-flow-rvalue ownership; doc/history.md 2026-08-02).

The e2e fixture `cfrv_match_receiver_or_throw_not_error_rejected` can only match
diagnostic MESSAGE text (the e2e runner does not compare codes).  This driver
pin asserts the exact CODE:

  * the invalid `(match … Result<Int,Int> …).or_throw()` emits
    `E_OR_THROW_NOT_ERROR_TYPE` (the or_throw preflight sees the match's real
    Result type — a value-position receiver — and rejects the non-`pub error`
    Err type), and
  * it does NOT regress to the old `E_REQUIREMENT_NOT_SATISFIED` "Int is Throw"
    cascade that appeared when the preflight typed its receiver
    `used_as_value=False` and the match collapsed to Void.
"""
from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


_SRC = """
module m;
import std.core as core;

fn a() nothrow -> core.Result<Int, Int> { return core.Result::Err(1); }
fn b() nothrow -> core.Result<Int, Int> { return core.Result::Err(2); }

pub fn main() throws -> Int {
	return (match true {
		true => { a() },
		false => { b() }
	}).or_throw();
}
"""


def _compile(tmp_path: Path, source: str):
	src = tmp_path / "main.drift"
	src.write_text(source)
	modules, type_table, exc, module_exports, module_deps, parse_diags = parse_drift_workspace_to_hir(
		[src], module_paths=[tmp_path], stdlib_root=stdlib_root(), test_build_only=True)
	assert parse_diags == []
	func_hirs, signatures, _ = flatten_modules(modules)
	_ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs, signatures=signatures, exc_env=exc, type_table=type_table,
		module_exports=module_exports, module_deps=module_deps,
		enforce_entrypoint=True, entry="m::main")
	return checked


def test_or_throw_match_receiver_emits_exact_not_error_code(tmp_path: Path) -> None:
	checked = _compile(tmp_path, _SRC)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	codes = {d.code for d in errors}
	assert "E_OR_THROW_NOT_ERROR_TYPE" in codes, [(d.code, d.message) for d in errors]
	# The whole point of the fix: the match receiver is typed as a value, so the
	# preflight fires the precise error rather than cascading to the weaker
	# require diagnostic.
	assert "E_REQUIREMENT_NOT_SATISFIED" not in codes, [(d.code, d.message) for d in errors]
