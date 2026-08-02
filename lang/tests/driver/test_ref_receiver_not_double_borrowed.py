# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: a reference-typed method receiver must NOT be double-borrowed
(work/control-flow-rvalue-ownership).

P3 routed method receivers through `_type_user_arg` (used_as_value=True) so a
value-control-flow receiver (`(match …).m()`) types to its result instead of
Void.  Applied BLANKET, that autoborrowed an ALREADY-`&`-typed receiver a second
time — `count.get()` on a captured `&Cell<Int>` resolved against
`Ref<Ref<Cell<Int>>>` ("no matching method 'get'"), regressing `cell_counter_fn0`
(compile-only corpus fixture, invisible to the focused pytest gates).

Fix: value-type the receiver ONLY when it is a DIRECT control-flow expression
(the shape that actually collapses to Void); place / reference / call / field
receivers keep place semantics.  This pins the reference-receiver shape directly.
"""
from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


# A captured `&Cell<Int>` receiver in an IIFE body — `count` is `&Cell`, and
# `count.get()` / `count.set(..)` must resolve on `&Cell`, not `&&Cell`.
_SRC = """
module m;
import std.core as core;

pub fn main() nothrow -> Int {
	var count = core.cell(0);
	(| | captures(&count) => {
		count.set(count.get() + 1);
		return 0;
	})();
	return count.get();
}
"""


def test_reference_receiver_method_resolves_without_double_borrow(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(_SRC)
	modules, tt, exc, mexp, mdeps, pd = parse_drift_workspace_to_hir(
		[src], module_paths=[tmp_path], stdlib_root=stdlib_root(), test_build_only=True)
	assert pd == [], [str(d) for d in pd]
	fh, sig, _ = flatten_modules(modules)
	_ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=fh, signatures=sig, exc_env=exc, type_table=tt,
		module_exports=mexp, module_deps=mdeps, enforce_entrypoint=True, entry="m::main")
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], [(d.code, d.message) for d in errors]
	# The specific regression signature: a double-ref receiver.
	assert not any("Ref<Ref<" in d.message for d in checked.diagnostics), [
		d.message for d in checked.diagnostics]
