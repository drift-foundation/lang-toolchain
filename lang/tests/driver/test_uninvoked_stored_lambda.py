# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
LANGUAGE_BUG (0.34.2): an UNINVOKED stored lambda had no lowering route.

`val t = || -> Int => { throw e; };` with no call: pending-lambda resolution
only fired at the first call, so the binding stayed Unknown and the raw
`HLambda` reached MIR lowering ("No MIR lowering for expr HLambda" ICE).
The annotated form (`val t: Fn() -> Int = ...`) worked in named functions
but not inside a lambda body: the captureless-lambda worklist was a single
snapshot, so a spec registered while rechecking another spec's body (lambda
stored inside a lambda) was never lowered — the emitted fat fn-ptr
referenced a hidden symbol clang could not find.

Previously MASKED: the old throw-effect walkers recursed into uninvoked
nested lambda bodies, so the enclosing `nothrow` lambda was misclassified
may-throw and rejected before lowering ran.  The corrected effect boundary
(construction does not execute) made the checker accept these forms, which
made the lowering gap a live checker/lowering contract violation.

Fixes: unresolved pending lambdas are flushed through the ordinary
no-expectation typing at end-of-function (captureless -> LambdaFnSpec +
fnptr const; capturing -> the standard capture rejection), and the
captureless worklist drains until no new specs appear.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.driftc.parser import stdlib_root

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

_PRELUDE = "module repro;\npub error ExcA { kind: Int }\n"


def _compile(tmp_path: Path, src: str, *, out: str) -> subprocess.CompletedProcess:
	p = tmp_path / "main.drift"
	p.write_text(src, encoding="utf-8")
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		str(p), "--entry", "repro::main", "--target-word-bits", "64",
		"-o", str(tmp_path / out),
	]
	stdlib = stdlib_root()
	if stdlib is not None:
		cmd.extend(["--stdlib-root", str(stdlib)])
	return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(180))


def _compile_and_run(tmp_path: Path, src: str, *, out: str) -> None:
	r = _compile(tmp_path, src, out=out)
	assert r.returncode == 0, r.stderr
	assert "No MIR lowering" not in r.stderr, r.stderr
	assert subprocess.run([str(tmp_path / out)]).returncode == 0


def test_uninvoked_unannotated_lambda_in_named_fn_runs(tmp_path: Path) -> None:
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\tval t = || -> Int => { throw ExcA(kind = 1); };\n"
		"\treturn 0;\n}\n"
	)
	_compile_and_run(tmp_path, src, out="unann_named")


def test_uninvoked_annotated_lambda_in_named_fn_runs(tmp_path: Path) -> None:
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\tval t: Fn() -> Int = || -> Int => { throw ExcA(kind = 1); };\n"
		"\treturn 0;\n}\n"
	)
	_compile_and_run(tmp_path, src, out="ann_named")


def test_uninvoked_unannotated_lambda_in_nothrow_lambda_runs(tmp_path: Path) -> None:
	# The nested-lambda effect-boundary driver POSITIVE: constructing (not
	# invoking) a throwing lambda inside a `nothrow` lambda is accepted AND
	# the accepted form lowers and runs (checker/lowering contract).
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\tval outer = | | nothrow => {\n"
		"\t\tval t = || -> Int => { throw ExcA(kind = 1); };\n"
		"\t\t0\n"
		"\t};\n"
		"\tval x = outer();\n"
		"\treturn x;\n}\n"
	)
	_compile_and_run(tmp_path, src, out="unann_lam")


def test_uninvoked_annotated_lambda_in_nothrow_lambda_runs(tmp_path: Path) -> None:
	# Nested-spec drain: the inner lambda's spec is registered while the
	# OUTER lambda's standalone body is rechecked — a snapshot worklist left
	# its hidden symbol unemitted (clang undefined-reference).
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\tval outer = | | nothrow => {\n"
		"\t\tval t: Fn() -> Int = || -> Int => { throw ExcA(kind = 1); };\n"
		"\t\t0\n"
		"\t};\n"
		"\tval x = outer();\n"
		"\treturn x;\n}\n"
	)
	_compile_and_run(tmp_path, src, out="ann_lam")


def test_uninvoked_capturing_lambda_clean_rejection(tmp_path: Path) -> None:
	# Flush totality (implicit shared-borrow capture): a bare stored
	# CAPTURING lambda is unsupported in v1; the flush must produce the SAME
	# single, spanned rejection as the invoked form — no Unknown cascade, no
	# raw-HLambda lowering traceback.  An implicit READ capture is a shared
	# borrow (HCaptureKind.REF), so the borrowed-capture variant fires.
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\tval x = 1;\n"
		"\tval t = || => { x };\n"
		"\treturn 0;\n}\n"
	)
	r = _compile(tmp_path, src, out="capimpl")
	assert r.returncode != 0, "uninvoked stored capturing lambda must be rejected"
	assert r.stderr.count("closures with borrowed captures are non-escaping in v0") == 1, r.stderr
	assert "main.drift:5:" in r.stderr, r.stderr
	assert "No MIR lowering" not in r.stderr, r.stderr
	assert "E-COPY-UNKNOWN" not in r.stderr, r.stderr


def test_uninvoked_mut_borrow_capture_lambda_clean_rejection(tmp_path: Path) -> None:
	# Implicit MUTABLE-borrow capture (a write to the outer var).
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\tvar x = 1;\n"
		"\tval t = || => { x = 5; };\n"
		"\treturn 0;\n}\n"
	)
	r = _compile(tmp_path, src, out="capmut")
	assert r.returncode != 0, "uninvoked stored mut-borrow-capture lambda must be rejected"
	assert "closures with borrowed captures are non-escaping in v0" in r.stderr, r.stderr
	assert "No MIR lowering" not in r.stderr, r.stderr


def test_uninvoked_explicit_ref_capture_lambda_clean_rejection(tmp_path: Path) -> None:
	# Explicit `captures(&x)` companion for the borrowed variant.
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\tval x = 1;\n"
		"\tval t = | | captures(&x) => { x };\n"
		"\treturn 0;\n}\n"
	)
	r = _compile(tmp_path, src, out="capref")
	assert r.returncode != 0, "uninvoked stored ref-capture lambda must be rejected"
	assert "closures with borrowed captures are non-escaping in v0" in r.stderr, r.stderr
	assert "No MIR lowering" not in r.stderr, r.stderr


def test_uninvoked_value_capture_lambda_bare_storage_rejected(tmp_path: Path) -> None:
	# v1 ruling: NO bare stored capturing lambda, even uninvoked and even
	# with value-only captures — there is no closure-value type to bind.
	# One clear diagnostic; no raw-HLambda lowering ICE; no move-of-
	# uninitialized-binding hole (the binding never exists).
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\tval x = 1;\n"
		"\tval t = | | captures(copy x) => { x };\n"
		"\treturn 0;\n}\n"
	)
	r = _compile(tmp_path, src, out="capvalue")
	assert r.returncode != 0, "bare stored value-capture lambda must be rejected"
	assert "bare capturing lambdas cannot be stored in v1" in r.stderr, r.stderr
	assert "No MIR lowering" not in r.stderr, r.stderr


def test_uninvoked_move_of_bare_stored_capture_binding_rejected(tmp_path: Path) -> None:
	# The rev-5 review's 1b probe: `move f` of the (now-rejected) binding
	# must be a clean rejection path, never an uninitialized-local MoveOut
	# MIR ICE.
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\tval x = 1;\n"
		"\tval f = | | captures(copy x) => { x };\n"
		"\tval g = move f;\n"
		"\treturn 0;\n}\n"
	)
	r = _compile(tmp_path, src, out="capmove")
	assert r.returncode != 0
	assert "bare capturing lambdas cannot be stored in v1" in r.stderr, r.stderr
	assert "MIR invariant violation" not in r.stderr, r.stderr
	assert "Traceback" not in r.stderr, r.stderr


def test_uninvoked_unchecked_body_now_rejected(tmp_path: Path) -> None:
	# The rev-5 review's 1a probe: the bare-storage rejection also closes the
	# skipped-body-check hole (an undefined name in the body no longer
	# compiles silently — the binding itself is rejected first).
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\tval x = 1;\n"
		"\tval f = | | captures(copy x) => { unknown_name };\n"
		"\treturn 0;\n}\n"
	)
	r = _compile(tmp_path, src, out="capbody")
	assert r.returncode != 0, "bare storage must reject before the unchecked body can slip through"


def test_value_captures_escape_via_callback_runs(tmp_path: Path) -> None:
	# The SUPPORTED escape representation: core.callback0 with copy + move
	# captures compiles AND runs even though the callback is never invoked —
	# capture effects (the move consuming `s`) occur at callback
	# construction.
	src = (
		"module repro;\n"
		"import std.core as core;\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval x = 41;\n"
		"\tval s = \"owned-string-payload\";\n"
		"\tval cb: core.Callback0<Int> = core.callback0(| | captures(copy x, move s) nothrow => { (x + 1) });\n"
		"\treturn 0;\n}\n"
	)
	_compile_and_run(tmp_path, src, out="capcb")


def test_uninvoked_unconstrained_lambda_clean_inference_rejection(tmp_path: Path) -> None:
	# Flush totality (unresolved ABI): `val id = |x| => { x };` has no call
	# site to constrain `x`; a LambdaFnSpec with Unknown parameter/return
	# types is not lowerable, so the flush must emit a clean source
	# diagnostic instead (no traceback, no internal contract failure).
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\tval id = |x| => { x };\n"
		"\treturn 0;\n}\n"
	)
	r = _compile(tmp_path, src, out="uncons")
	assert r.returncode != 0, "unconstrained uninvoked lambda must be rejected"
	assert "cannot infer type for lambda parameter(s) 'x'" in r.stderr, r.stderr
	assert "Traceback" not in r.stderr, r.stderr
	assert "internal" not in r.stderr, r.stderr


def test_annotated_nested_generic_lambda_runs(tmp_path: Path) -> None:
	# Nested-generic compile/run positive (both specs known before the drain:
	# outer resolves at its call, the annotated inner via the HLet path).
	src = _PRELUDE + (
		"fn ident<T>(x: T) -> T {\n"
		"\treturn x;\n}\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval outer = | | nothrow => {\n"
		"\t\tval t: Fn() -> Int = || -> Int => { (ident(41) + 1) };\n"
		"\t\t0\n"
		"\t};\n"
		"\tval x = outer();\n"
		"\treturn x;\n}\n"
	)
	_compile_and_run(tmp_path, src, out="draingen")


def test_spec_with_generic_call_registered_during_drain_runs(tmp_path: Path) -> None:
	# TRUE during-drain producer: `outer` is never invoked, so only the
	# end-of-check flush emits it; `middle` is unannotated AND uninvoked, so
	# it is first flushed while the captureless drain RECHECKS `outer` — a
	# spec registered mid-drain.  Its body's generic call queues an
	# instantiation after the main lowering loop; the drain's quiescence
	# must type, lower, and emit `ident__inst__*` (previously: clang
	# undefined-value) and classify its can-throw entry.
	src = _PRELUDE + (
		"fn ident<T>(x: T) nothrow -> T {\n"
		"\treturn x;\n}\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval outer = | | nothrow => {\n"
		"\t\tval middle = | | nothrow => { (ident(41) + 1) };\n"
		"\t\t0\n"
		"\t};\n"
		"\treturn 0;\n}\n"
	)
	_compile_and_run(tmp_path, src, out="draingen2")


def test_thunk_registered_during_drain_runs(tmp_path: Path) -> None:
	# Late-thunk producer: the fn-reference coercion (`nothrow` named fn to a
	# throwing `Fn() -> Int` value) is first typed while the drain rechecks a
	# spec, registering a thunk AFTER the one-shot thunk pass — the
	# quiescence step must synthesize it.
	src = _PRELUDE + (
		"fn target() nothrow -> Int {\n"
		"\treturn 1;\n}\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval outer = | | nothrow => {\n"
		"\t\tval middle = | | nothrow => {\n"
		"\t\t\tval fp: Fn() -> Int = target;\n"
		"\t\t\t0\n"
		"\t\t};\n"
		"\t\t0\n"
		"\t};\n"
		"\treturn 0;\n}\n"
	)
	_compile_and_run(tmp_path, src, out="drainthunk")


def test_hidden_lambda_registered_during_drain_runs(tmp_path: Path) -> None:
	# Late hidden-lambda producer: `outer` is lowered only in the captureless
	# worklist (after the hidden-lambda drain); its inner IIFE registers a
	# hidden spec on that worklist's local lowering object — it must be
	# harvested and drained, else the emitted M.Call references a hidden fn
	# that was never built.
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\tval outer = | | nothrow => {\n"
		"\t\t(| | nothrow => { 42 })()\n"
		"\t};\n"
		"\treturn 0;\n}\n"
	)
	_compile_and_run(tmp_path, src, out="drainhidden")


def test_uninvoked_then_invoked_later_still_works(tmp_path: Path) -> None:
	# Control: the flush must not break the normal store-then-call flow.
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\tval t = || -> Int => { throw ExcA(kind = 1); };\n"
		"\tval x = try t() catch { 5 };\n"
		"\treturn x - 5;\n}\n"
	)
	_compile_and_run(tmp_path, src, out="stored_called")
