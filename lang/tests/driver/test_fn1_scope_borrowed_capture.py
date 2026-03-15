# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Fn1 SCOPED borrowed-capture regression: a lambda with captures(&x) passed to
a generic ``F is Fn1<A,R>`` param must not be rejected by the type checker.

Before the fix:  type checker emits "closures with borrowed captures are
non-escaping in v0" because call_resolver.py unconditionally overrides
allow_capture_invoke = False for Fn-trait-bounded params (TP4/TP5).

After the fix:   type checker accepts the lambda (allow_capture_invoke stays
True for Fn-bounded capturing lambdas) and the borrow checker validates escape
level via the SCOPED promotion path.
"""
from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _compile(tmp_path: Path, content: str):
	mod_root = tmp_path / "mods"
	src = mod_root / "main.drift"
	_write_file(src, content)
	paths = sorted(mod_root.rglob("*.drift"))
	modules, type_table, exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	func_hirs, sigs, _fn_ids = flatten_modules(modules)
	_, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=sigs,
		exc_env=exc_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
	)
	return list(diagnostics) + list(checked.diagnostics)


def test_fn1_bounded_scope_borrowed_capture_accepted(tmp_path: Path) -> None:
	"""Lambda with captures(&x) passed to generic F is Fn1<A,R> must be accepted.

	This is the minimal regression for the Fn1 SCOPED borrowed-capture fix.
	The type checker must NOT reject the lambda with "closures with borrowed
	captures are non-escaping in v0".
	"""
	diags = _compile(tmp_path, """\
module m;

import std.core as core;

fn apply<F>(f: F) nothrow -> Void require F is core.Fn1<Int, Void> {
	f.call(42);
}

fn main() nothrow -> Int {
	var x: Int = 10;
	apply(|_a| captures(&x) => {});
	return 0;
}
""")
	borrowed_errors = [d for d in diags if "closures with borrowed captures" in (d.message or "")]
	assert borrowed_errors == [], f"Type checker must not reject Fn1-bounded borrowed capture: {borrowed_errors}"


def test_non_fn_bounded_generic_borrowed_capture_still_rejected(tmp_path: Path) -> None:
	"""Lambda with captures(&x) passed to a generic param WITHOUT Fn* bound must still be rejected.

	This guards against the TP4 relaxation being too broad: only Fn*-bounded
	params get the allow_capture_invoke=True treatment.
	"""
	diags = _compile(tmp_path, """\
module m;

import std.core as core;

trait Marker { fn mark(self: &Self) nothrow -> Void; }

fn apply<F>(f: F) nothrow -> Void require F is Marker {
	f.mark();
}

fn main() nothrow -> Int {
	var x: Int = 10;
	apply(|_a| captures(&x) => {});
	return 0;
}
""")
	# Non-Fn-bounded generic: borrowed captures should still be rejected
	# (or the call should fail for other reasons — the lambda doesn't implement Marker).
	errors = [d for d in diags if d.severity == "error"]
	assert len(errors) > 0, "Non-Fn-bounded generic with borrowed capture must produce an error"


def test_callback1_satisfies_fn1_require(tmp_path: Path) -> None:
	"""Callback1<A,R> passed to generic F is Fn1<A,R> must be accepted.

	B1 regression: the trait solver must structurally recognise that
	Callback1<A,R> satisfies the Fn1<A,R> require bound.
	"""
	diags = _compile(tmp_path, """\
module m;

import std.core as core;

fn apply<F>(f: F) nothrow -> Void require F is core.Fn1<Int, Void> {
	f.call(42);
}

fn main() nothrow -> Int {
	val cb: core.Callback1<Int, Void> = core.callback1(|_a: Int| nothrow => {});
	apply(move cb);
	return 0;
}
""")
	requirement_errors = [d for d in diags if d.severity == "error" and "E_REQUIREMENT_NOT_SATISFIED" in (d.code or "")]
	assert requirement_errors == [], f"Callback1 must satisfy Fn1 require bound: {requirement_errors}"
	errors = [d for d in diags if d.severity == "error"]
	assert errors == [], f"Unexpected errors: {[(d.code, d.message) for d in errors]}"


def test_copy_capture_lambda_to_fn_bounded_generic_accepted(tmp_path: Path) -> None:
	"""Lambda with captures(copy x) passed to generic F is Fn1<A,R> must be accepted.

	B2 regression: the checker must auto-wrap the capturing lambda in callback1()
	so that F is instantiated as Callback1<A,R> (not fn ptr). The copy capture
	avoids the borrowed-capture MIR limitation.
	"""
	diags = _compile(tmp_path, """\
module m;

import std.core as core;

fn apply<F>(f: F) nothrow -> Void require F is core.Fn1<Int, Void> {
	f.call(42);
}

fn main() nothrow -> Int {
	val x: Int = 10;
	apply(|_a| captures(copy x) nothrow => {});
	return 0;
}
""")
	# B2 targets: no "capturing lambdas" rejection; no E_REQUIREMENT_NOT_SATISFIED.
	capture_errors = [d for d in diags if d.severity == "error" and "capturing lambdas" in (d.message or "")]
	assert capture_errors == [], f"Copy-capture lambda must not be rejected: {capture_errors}"
	requirement_errors = [d for d in diags if d.severity == "error" and "E_REQUIREMENT_NOT_SATISFIED" in (d.code or "")]
	assert requirement_errors == [], f"Callback must satisfy Fn1 require: {requirement_errors}"
	errors = [d for d in diags if d.severity == "error"]
	assert errors == [], f"Unexpected errors: {[(d.code, d.message) for d in errors]}"


def test_borrowed_capture_lambda_to_fn_bounded_generic_accepted(tmp_path: Path) -> None:
	"""Lambda with captures(&x) passed to generic F is Fn1<A,R> must be accepted.

	B4 regression: borrowed-capture lambdas are auto-wrapped in callback_N()
	(same as B2 copy captures). The MIR callback env stores &T fields.
	The borrow checker validates escape levels before MIR lowering.
	"""
	diags = _compile(tmp_path, """\
module m;

import std.core as core;

fn apply<F>(f: F) nothrow -> Void require F is core.Fn1<Int, Void> {
	f.call(42);
}

fn main() nothrow -> Int {
	var x: Int = 10;
	apply(|_a| captures(&x) nothrow => {});
	return 0;
}
""")
	# B4 targets: no "closures with borrowed captures" rejection;
	# no "capturing lambdas" rejection; no E_REQUIREMENT_NOT_SATISFIED;
	# no AssertionError from MIR lowering; zero errors.
	borrowed_errors = [d for d in diags if "closures with borrowed captures" in (d.message or "")]
	assert borrowed_errors == [], f"Borrowed-capture lambda must not be rejected: {borrowed_errors}"
	capture_errors = [d for d in diags if d.severity == "error" and "capturing lambdas" in (d.message or "")]
	assert capture_errors == [], f"Capturing lambda must not be rejected: {capture_errors}"
	requirement_errors = [d for d in diags if d.severity == "error" and "E_REQUIREMENT_NOT_SATISFIED" in (d.code or "")]
	assert requirement_errors == [], f"Callback must satisfy Fn1 require: {requirement_errors}"
	assertion_errors = [d for d in diags if "borrowed capture in owned callback env" in (d.message or "")]
	assert assertion_errors == [], f"MIR lowering must not reject borrowed callback env: {assertion_errors}"
	errors = [d for d in diags if d.severity == "error"]
	assert errors == [], f"Unexpected errors: {[(d.code, d.message) for d in errors]}"
