# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Closure/callback body with a Void-context tail must lower like a named fn.

Regression for the 0.33.10 MIR-lowering ICE reported by the bookkeeper team
(`drift-0.33.10-closure-void-tail-mir-ice`).  `_lower_lambda_block` always
lowered a trailing `HExprStmt` in *value* context, so a Void-producing tail —
a Void free-function call `f(x)`, or a `match` whose arms are Void/empty —
hit the value-context asserts in `_visit_expr_HCall` / `_lower_match`:

    internal: MIR lowering contract failure
        (Void-returning call used in expression context (checker bug))
    internal: MIR lowering contract failure
        (value-producing match arm must yield a value or terminate (checker bug))

The same constructs lower fine as the tail of a named `-> Void` function
(`lower_function_body` discards the trailing expression's value).  The fix
routes a Void-returning lambda/callback tail through `lower_stmt`
(`want_value=False`) so the two shapes behave identically.

These are full compile-and-run checks: the binary must build *and* execute
cleanly, proving the dropped-event branch (`None => {}`) is a valid runtime
path, not just that lowering stopped asserting.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import host_word_bits, sanitizer_timeout
from lang.driftc.parser import stdlib_root


def _compile_and_run(tmp_path: Path, source: str, *, expect_exit: int) -> None:
	src = tmp_path / "repro.drift"
	src.write_text(source.lstrip(), encoding="utf-8")
	out_bin = tmp_path / "bin"
	root_path = Path(__file__).resolve().parents[3]
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc", "--dev",
		"--stdlib-root", str(stdlib_root() or (root_path / "stdlib")),
		"--target-word-bits", str(host_word_bits()),
		"--entry", "repro::main", "-o", str(out_bin), str(src),
	]
	res = subprocess.run(
		cmd, cwd=root_path, capture_output=True, text=True,
		timeout=sanitizer_timeout(60),
	)
	# The pre-fix failure was an internal contract assert, not a user
	# diagnostic — surface it verbatim if it ever returns.
	assert res.returncode == 0, (
		f"compile failed (rc={res.returncode}):\n{res.stderr[-1500:]}"
	)
	run = subprocess.run(
		[str(out_bin)], capture_output=True, text=True,
		timeout=sanitizer_timeout(20),
	)
	assert run.returncode == expect_exit, (
		f"binary exited {run.returncode}, expected {expect_exit}; "
		f"stderr: {run.stderr[:300]}"
	)


# Shared preamble: a Void sink, a Void free-fn, and a classifier that can
# return None so the closure has a genuine drop-the-event branch.
_PREAMBLE = """
module repro;
import std.core as core;

fn classify(x: Int) nothrow -> Optional<Int> {
	if x > 0 { return Optional<Int>::Some(x); }
	return Optional<Int>::None();
}
fn use_it(v: Int) nothrow -> Void {}
fn disp(ev: Int) nothrow -> Void { use_it(ev); }
"""


def test_closure_tail_void_free_call_compiles_and_runs(tmp_path: Path) -> None:
	"""Closure whose tail is a Void free-call `disp(ev)` — pre-fix ICE
	'Void-returning call used in expression context'."""
	_compile_and_run(
		tmp_path,
		_PREAMBLE + """
fn make_b() nothrow -> core.Callback1<Int, Void> {
	return core.callback1(|ev: Int| => { disp(ev); });
}
fn main() nothrow -> Int {
	val cb = make_b();
	cb.call(7);
	return 0;
}
""",
		expect_exit=0,
	)


def test_closure_tail_match_void_arm_compiles_and_runs(tmp_path: Path) -> None:
	"""Closure whose tail is a `match` with a Void/empty arm — pre-fix ICE
	'value-producing match arm must yield a value or terminate'.  Exercises
	both arms at runtime (7 -> Some, -1 -> None drop branch)."""
	_compile_and_run(
		tmp_path,
		_PREAMBLE + """
fn make_a() nothrow -> core.Callback1<Int, Void> {
	return core.callback1(|ev: Int| => {
		match classify(ev) {
			Optional::Some(v) => { use_it(v); },
			Optional::None => {}
		}
	});
}
fn main() nothrow -> Int {
	val cb = make_a();
	cb.call(7);
	cb.call(-1);
	return 0;
}
""",
		expect_exit=0,
	)


def test_named_fn_void_match_tail_control_still_ok(tmp_path: Path) -> None:
	"""Control: the identical Void match tail in a named `-> Void` function
	always lowered fine and must keep working."""
	_compile_and_run(
		tmp_path,
		_PREAMBLE + """
fn handle(ev: Int) nothrow -> Void {
	match classify(ev) {
		Optional::Some(v) => { use_it(v); },
		Optional::None => {}
	}
}
fn main() nothrow -> Int {
	handle(7);
	handle(-1);
	return 0;
}
""",
		expect_exit=0,
	)


def test_closure_tail_void_method_call_control_still_ok(tmp_path: Path) -> None:
	"""Control: a closure whose tail is a Void *method* call already lowered
	fine (the report lists `obj.call(x)` as OK) and must keep working.  The
	inner callback `inner: Callback1<Int, Void>` is captured by the outer
	closure and invoked via its Void-returning `.call(ev)` method as the tail
	— a Void method call, not a free call."""
	_compile_and_run(
		tmp_path,
		_PREAMBLE + """
fn make_m() nothrow -> core.Callback1<Int, Void> {
	val inner = core.callback1(|x: Int| => { disp(x); });
	return core.callback1(|ev: Int| captures(move inner) => { inner.call(ev); });
}
fn main() nothrow -> Int {
	val cb = make_m();
	cb.call(7);
	return 0;
}
""",
		expect_exit=0,
	)


def test_captureless_fnptr_tail_void_free_call_compiles_and_runs(tmp_path: Path) -> None:
	"""Captureless bare lambda coerced to a `Fn(Int) nothrow -> Void` function
	pointer (the canonical fn-ptr type surface), body a block whose tail is a
	Void free-call `disp(x)`.

	This captureless-lambda → fn-pointer coercion is the `LambdaFnSpec` path in
	`compile_stubbed_funcs` (a top-level captureless function — no env, no
	callback vtable), distinct from the callback path.  `_lower_lambda_block`
	now returns None for a Void lambda tail, so the captureless finalizer must
	emit `Return(value=None)` instead of the stale
	'captureless lambda block must end with a value or return' assert."""
	_compile_and_run(
		tmp_path,
		_PREAMBLE + """
fn main() nothrow -> Int {
	val f: Fn(Int) nothrow -> Void = |x: Int| => { disp(x); };
	f(7);
	return 0;
}
""",
		expect_exit=0,
	)


def test_captureless_fnptr_tail_match_void_arm_compiles_and_runs(tmp_path: Path) -> None:
	"""Captureless `Fn(Int) nothrow -> Void` fn-pointer whose lambda tail is a
	`match` with a Void/empty arm — same `LambdaFnSpec` finalizer path,
	exercising both arms at runtime.  Like the free-call sibling, this also
	tripped the stale 'captureless lambda block must end with a value or
	return' assert pre-fix (the match join block falls through unterminated, so
	`_lower_lambda_block` returns None with `builder.block.terminator is None`).
	"""
	_compile_and_run(
		tmp_path,
		_PREAMBLE + """
fn main() nothrow -> Int {
	val f: Fn(Int) nothrow -> Void = |ev: Int| => {
		match classify(ev) {
			Optional::Some(v) => { use_it(v); },
			Optional::None => {}
		}
	};
	f(7);
	f(-1);
	return 0;
}
""",
		expect_exit=0,
	)
