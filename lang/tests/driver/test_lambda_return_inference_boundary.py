# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
0.34.2 — value-block lambda return-type inference boundary pins.

The checker's lambda-body typing and MIR lowering must agree on an unannotated
lambda's return type: it is the type of the trailing value expression (an
`HExprStmt`), NOT `Void`.  A stale checker default of `Void` produced a spurious
`E-TRY-ARM-TYPE` (see test_try_expr_immediate_lambda.py) and an `Unknown`
CallInfo boundary.  The fix routes every lambda tail (block trailing value,
statement-form-match/return tail, and body-expression) through ONE shared
return-value authority — the same one `HReturn` uses — so auto-try, `&T->T`
coercion, interface/callback coercion, and mismatch diagnosis are identical for a
lambda tail and an explicit `return`.

These pins fix the observable boundary in the two directions that matter:
  * an unannotated value-block lambda's inferred return flows into an
    explicitly-typed binding (`val r: Int = ...`), which the checker would reject
    if the boundary were `Void`/`Unknown` — a boundary assertion, not a mere
    downstream arithmetic use;
  * an empty / value-less body infers `Void` (never `None`, which would decay to
    `Unknown`), so a stored empty lambda compiles and invokes cleanly.

Both direct `HCall(fn=HLambda)` and the stored/`HInvoke` route are covered.

R4 (0.34.2): the return authority also VERIFIES the implements relation before
recording a concrete->interface return coercion (mirror of the HLet 0.33.77
initializer check) — an unverified record used to surface as a codegen ICE.
The positive (fresh ctor / `move` forms) compiles AND runs; the negative
(non-implementing struct) gets a clean checker diagnostic.  (An earlier note
here claimed the owned positive "isn't demonstrable" — that conflated the
ownership copy-rejection of a bare `return dog;` with a coercion failure; use
`move` or a fresh constructor and the coercion mark lowers fine.)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.driftc.parser import stdlib_root

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

_PRELUDE = "module repro;\n"


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


def test_direct_iife_annotated_boundary_is_int(tmp_path: Path) -> None:
	# Direct HCall(fn=HLambda): the unannotated value-block IIFE's inferred return
	# is bound to `val r: Int`.  A Void/Unknown boundary would fail this binding
	# in the checker, so the annotation IS the boundary assertion.
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\tval r: Int = (|| => { val a = 5; a + 1 })();\n"  # boundary must be Int
		"\treturn r - 6;\n}\n"                               # 6 - 6 = 0
	)
	r = _compile(tmp_path, src, out="direct")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "direct")]).returncode == 0


def test_stored_lambda_annotated_boundary_is_int(tmp_path: Path) -> None:
	# Stored-then-invoked route (pending-lambda / HInvoke): the same inference must
	# reach the CallInfo boundary so `val r: Int = f()` type-checks.
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\tval f = || => { val a = 5; a + 1 };\n"
		"\tval r: Int = f();\n"
		"\treturn r - 6;\n}\n"
	)
	r = _compile(tmp_path, src, out="stored")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "stored")]).returncode == 0


def test_declared_return_matching_tail_compiles(tmp_path: Path) -> None:
	# Positive: an explicit `-> Int` whose trailing value IS Int passes the shared
	# authority's declared/expected comparison (no spurious rejection).
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\tval r = (|| -> Int => { val a = 5; a })();\n"
		"\treturn r - 5;\n}\n"
	)
	r = _compile(tmp_path, src, out="matchtail")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "matchtail")]).returncode == 0


def test_declared_return_mismatch_tail_rejected(tmp_path: Path) -> None:
	# Negative: an explicit `-> Int` whose trailing value is a String must be
	# rejected (compile fails).  The shared authority diagnoses any type still
	# incompatible after its coercion ladder — the same contract HReturn uses —
	# so this fails exactly as `fn f() nothrow -> Int { return "x"; }` does; the
	# fix does NOT quietly accept an incompatible declared/tail pairing.
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\tval g = (|| -> Int => { val s = \"x\"; s })();\n"
		"\treturn 0;\n}\n"
	)
	r = _compile(tmp_path, src, out="mismatch")
	assert r.returncode != 0, "declared -> Int with a String tail must be rejected"
	assert "does not match declared type" in r.stderr, r.stderr
	# R3.P1 dedup pin: ONE authority diagnoses this — the checker's direct-IIFE
	# branch consumes CallInfo instead of re-inferring the body, so the mismatch
	# is reported exactly once (it used to double via the raw-equality fallback).
	assert r.stderr.count("does not match declared type") == 1, r.stderr


def test_named_fn_return_literal_mismatch_rejected(tmp_path: Path) -> None:
	# R3.P1: `type_expr(..., expected_type=...)` only shapes inference — a String
	# literal at `return` ignores the Int expectation, so without the authority's
	# post-coercion diagnosis this reached codegen and died with a ConstructResultOk
	# payload-mismatch ICE (reproduced on certified 0.33.90).  Must now be a clean
	# diagnostic, not a traceback.
	src = _PRELUDE + (
		"fn f() -> Int {\n"
		"\treturn \"x\";\n}\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval r = f();\n"
		"\treturn 0;\n}\n"
	)
	r = _compile(tmp_path, src, out="namedlit")
	assert r.returncode != 0, "named fn returning String where Int declared must be rejected"
	assert "does not match declared type" in r.stderr, r.stderr
	assert "Traceback" not in r.stderr, r.stderr


def test_named_fn_return_variable_mismatch_rejected(tmp_path: Path) -> None:
	# R3.P1: same gap through an ordinary variable (variables ignore
	# expected_type entirely); previously the same codegen ICE.
	src = _PRELUDE + (
		"fn g() -> Int {\n"
		"\tval s = \"hello\";\n"
		"\treturn s;\n}\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval r = g();\n"
		"\treturn 0;\n}\n"
	)
	r = _compile(tmp_path, src, out="namedvar")
	assert r.returncode != 0, "named fn returning a String variable where Int declared must be rejected"
	assert "does not match declared type" in r.stderr, r.stderr
	assert "Traceback" not in r.stderr, r.stderr


def test_stored_lambda_declared_mismatch_rejected(tmp_path: Path) -> None:
	# R3.P1 (the reviewer's bypass): a STORED annotated lambda with a mismatched
	# value tail.  The stored/HInvoke route consumes the declared CallInfo return
	# (Int), so no downstream pass ever re-checks the body — before the authority
	# diagnosed lambda tails, this compiled AND linked silently (miscompile,
	# reproduced on certified 0.33.90).
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\tval f = || -> Int => { \"x\" };\n"
		"\tval x = f();\n"
		"\treturn 0;\n}\n"
	)
	r = _compile(tmp_path, src, out="storedmm")
	assert r.returncode != 0, "stored `|| -> Int` with a String tail must be rejected"
	assert "does not match declared type" in r.stderr, r.stderr


def test_named_fn_return_ok_wrapped_rejected(tmp_path: Path) -> None:
	# `return Ok(5)` in a can-throw `-> Int` fn: the expected surface type is
	# Int, not a variant — §10.3 does not let the compiler guess Result<Int,E>
	# and auto-try it.  Unqualified `Ok(...)` is an ordinary contextual
	# variant-constructor spelling (the legacy internal-result-node source
	# seam was deleted, Slawomir-approved 2026-08-03; see doc/history.md),
	# so this gets ONE clean constructor-context rejection — no duplicate,
	# no traceback.
	src = _PRELUDE + (
		"fn f() -> Int {\n"
		"\treturn Ok(5);\n}\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval r = try f() catch { 0 };\n"
		"\treturn 0;\n}\n"
	)
	r = _compile(tmp_path, src, out="okwrap")
	assert r.returncode != 0, "Ok(...) at a non-variant return expectation must be rejected"
	assert r.stderr.count("E-CTOR-EXPECTED-TYPE") >= 1, r.stderr
	assert r.stderr.count("error:") == 1, r.stderr
	assert "Traceback" not in r.stderr, r.stderr


def test_local_unannotated_ok_rejected_cleanly(tmp_path: Path) -> None:
	# The child repro's local form: `val r = Ok(a)` with no expected variant
	# type.  Previously this passed checking and ICEd in LLVM codegen
	# ("ok payload type mismatch for ConstructResultOk"); now it is the same
	# clean constructor-context rejection as the return form.
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\tval r = Ok(1);\n"
		"\treturn 0;\n}\n"
	)
	r = _compile(tmp_path, src, out="oklocal")
	assert r.returncode != 0, "unannotated local Ok(...) must be rejected"
	assert "E-CTOR-EXPECTED-TYPE" in r.stderr, r.stderr
	assert "Traceback" not in r.stderr, r.stderr
	assert "NotImplementedError" not in r.stderr, r.stderr


def test_return_ok_into_public_result_runs(tmp_path: Path) -> None:
	# Contrasting positive: when the return expectation IS a public Result,
	# `return Ok(5)` builds the public inner variant and receives exactly the
	# normal outer throwing-ABI wrap — the two layers are distinct and no
	# double-wrap payload mismatch remains.  The caller successfully tries
	# the call and matches the public result.
	src = (
		"module repro;\n"
		"import std.core as core;\n"
		"fn g() -> core.Result<Int, String> {\n"
		"\treturn Ok(5);\n}\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval r = try g() catch { core.Result<Int, String>::Err(err = \"boom\") };\n"
		"\tval x = match r {\n"
		"\t\tOk(v) => { (v + 1) },\n"
		"\t\tErr(e) => { 0 },\n"
		"\t};\n"
		"\treturn x - 6;\n}\n"
	)
	r = _compile(tmp_path, src, out="okresult")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "okresult")]).returncode == 0


_IFACE_PRELUDE = _PRELUDE + (
	"pub interface Speaker {\n"
	"\tfn speak(self: &Self) nothrow -> Int;\n"
	"}\n"
	"pub struct Dog {\n"
	"\tpub n: Int\n"
	"}\n"
	"implement Speaker for Dog {\n"
	"\tpub fn speak(self: &Dog) nothrow -> Int {\n"
	"\t\treturn self.n;\n\t}\n"
	"}\n"
)


def test_return_interface_coercion_positive_runs(tmp_path: Path) -> None:
	# R4.1 positive: implementing concrete -> interface RETURN, both fresh-ctor
	# and `move` forms — the recorded coercion mark must lower and run (an
	# unverified/failed record would ICE in codegen or crash at dispatch).
	src = _IFACE_PRELUDE + (
		"fn make_ctor() nothrow -> Speaker {\n"
		"\treturn Dog(n = 7);\n}\n"
		"fn make_move() nothrow -> Speaker {\n"
		"\tval dog = Dog(n = 35);\n"
		"\treturn move dog;\n}\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval a = make_ctor();\n"
		"\tval b = make_move();\n"
		"\treturn a.speak() + b.speak() - 42;\n}\n"
	)
	r = _compile(tmp_path, src, out="ifacepos")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "ifacepos")]).returncode == 0


def test_return_interface_non_implementing_rejected(tmp_path: Path) -> None:
	# R4.1 negative: a struct that does NOT implement the declared interface
	# must get a clean checker diagnostic (previously the unverified coercion
	# record deferred the failure to codegen).
	src = _IFACE_PRELUDE + (
		"pub struct Cat {\n"
		"\tpub n: Int\n"
		"}\n"
		"fn make() nothrow -> Speaker {\n"
		"\treturn Cat(n = 7);\n}\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval a = make();\n"
		"\treturn a.speak() - 7;\n}\n"
	)
	r = _compile(tmp_path, src, out="ifaceneg")
	assert r.returncode != 0, "non-implementing struct at interface return must be rejected"
	assert "'Cat' does not implement interface 'Speaker'" in r.stderr, r.stderr
	assert "Traceback" not in r.stderr, r.stderr


def test_declared_ret_valueless_body_rejected(tmp_path: Path) -> None:
	# R4.2: a declared non-Void return over a value-less (or empty) body is an
	# undefined-value hole — certified 0.33.90 silently compiled these to
	# garbage.  The authority's guard (moved from the removed checker fallback)
	# rejects both; the flat trailing-throw exemption is pinned separately in
	# test_stored_capturing_lambda_diagnostic.py.
	for body, out in (("{ val a = 5; }", "vlss"), ("{}", "vempty")):
		src = _PRELUDE + (
			"pub fn main() nothrow -> Int {\n"
			f"\tval f = || -> Int => {body};\n"
			"\tval x = f();\n"
			"\treturn 0;\n}\n"
		)
		r = _compile(tmp_path, src, out=out)
		assert r.returncode != 0, f"declared -> Int with value-less body {body!r} must be rejected"
		assert "lambda with explicit return type must return a value" in r.stderr, r.stderr


def test_empty_lambda_infers_void(tmp_path: Path) -> None:
	# Void fallback: an empty body infers Void (never None -> Unknown).  A stored
	# empty lambda invoked as a statement compiles and runs.
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\tval f = || => {};\n"
		"\tf();\n"
		"\treturn 0;\n}\n"
	)
	r = _compile(tmp_path, src, out="empty")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "empty")]).returncode == 0


def test_value_less_return_lambda_infers_void(tmp_path: Path) -> None:
	# Void fallback: a body whose only exit is a value-less `return;` infers Void.
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\tval f = || => { return; };\n"
		"\tf();\n"
		"\treturn 0;\n}\n"
	)
	r = _compile(tmp_path, src, out="retvoid")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "retvoid")]).returncode == 0
