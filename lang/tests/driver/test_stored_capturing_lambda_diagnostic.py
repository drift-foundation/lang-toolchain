# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
LANGUAGE_BUG (0.34.2): stored capturing lambda produced an invisible rejection
plus a garbage cascade.

`val g = || => { t + x }; val y = g();` is (correctly) rejected in v1 — a bare
stored lambda cannot carry captures; the callback interface is the supported
vehicle.  But the rejection was unusable, three stacked defects deep:

  1. HLambda was the only HExpr without a `loc` field, so the real diagnostic
     ("capturing lambdas cannot be coerced to function pointers") rendered as
     "<unknown location>" and was dropped from display entirely;
  2. the binding then decayed to Unknown and `_require_copy_value` piled on
     "cannot copy 'g': type 'Unknown' Copy is unknown" noise;
  3. the call site repeated "call target is not a function value" over the
     same poisoned binding.

Fixed: HLambda carries a stamped `loc`; the Unknown-cascade diagnostics are
suppressed by EXACT causal provenance — the rejection primary is recorded as
the poisoned binding's cause (`unknown_cause_by_binding`), and only consumers
of that specific binding/expression suppress (unrelated Unknowns still trip
E-COPY-UNKNOWN; see test_causal_unknown_provenance.py).  The result is
exactly ONE actionable, properly-spanned diagnostic.

Companion LANGUAGE_BUG (same slice): DIVERGENT lambda bodies.  A lambda whose
body throws on every path was only accepted in the flat trailing-throw form;
non-flat shapes were rejected "must return a value" — a checker rejection
masking a `_lambda_can_throw` walker gap (no descent into bare nested HBlock
statements or HMatchExpr arms), which classified such lambdas NOTHROW and
lowered their call sites without error dispatch (runtime SIGABRT).  Fixed:
the value-less-body guard uses semantic divergence analysis, and the walker
descends into nested blocks / match arms / casts.  All divergent shapes are
now compile/run POSITIVES, pinned below.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.driftc.parser import stdlib_root

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

_PRELUDE = "module repro;\n"


def _compile(tmp_path: Path, src: str, *, out: str, allow_unsafe: bool = False) -> subprocess.CompletedProcess:
	p = tmp_path / "main.drift"
	p.write_text(src, encoding="utf-8")
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		str(p), "--entry", "repro::main", "--target-word-bits", "64",
		"-o", str(tmp_path / out),
	]
	if allow_unsafe:
		cmd.append("--allow-unsafe")
	stdlib = stdlib_root()
	if stdlib is not None:
		cmd.extend(["--stdlib-root", str(stdlib)])
	return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(180))


def test_stored_capturing_lambda_single_spanned_diagnostic(tmp_path: Path) -> None:
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\tval x = 4;\n"
		"\tval g = || => { val t = 3; t + x };\n"
		"\tval y = g();\n"
		"\treturn y;\n}\n"
	)
	r = _compile(tmp_path, src, out="cap")
	assert r.returncode != 0, "bare stored capturing lambda must be rejected"
	# An implicit READ capture is a shared borrow (HCaptureKind.REF), so the
	# borrowed-capture variant fires (the capture-kind normalization fix —
	# comparing enum kinds against strings had made this variant unreachable
	# for implicit captures, falling back to the generic fn-pointer message).
	assert r.stderr.count("closures with borrowed captures are non-escaping in v0") == 1, r.stderr
	# The diagnostic must carry the lambda's real span (line 4, the
	# `val g = ||...` line), not "<unknown location>" (the pre-fix rendering
	# that hid it entirely).
	assert "<unknown location>" not in r.stderr, r.stderr
	assert "main.drift:4:" in r.stderr, r.stderr
	# Cascade suppression: no Unknown-copy noise, no repeated call-target
	# complaint over the same poisoned binding.
	assert "E-COPY-UNKNOWN" not in r.stderr, r.stderr
	assert "call target is not a function value" not in r.stderr, r.stderr


def test_nonflat_ifelse_divergent_lambda_runs_both_branches(tmp_path: Path) -> None:
	# Divergent if/else body: BOTH branches throw; both are exercised at
	# runtime via two calls.  Previously rejected "must return a value" (the
	# masking); a mislowered branch would abort instead of reaching the catch.
	src = _PRELUDE + (
		"pub error ExcA { kind: Int }\n"
		"pub error ExcB { kind: Int }\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval f = |n: Int| -> Int => { if n > 0 { throw ExcA(kind = 1); } else { throw ExcB(kind = 2); } };\n"
		"\tval a = try f(1) catch { 10 };\n"
		"\tval b = try f(-1) catch { 20 };\n"
		"\treturn a + b - 30;\n}\n"
	)
	r = _compile(tmp_path, src, out="nonflat_if")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "nonflat_if")]).returncode == 0


def test_capturing_iife_divergent_body_runs(tmp_path: Path) -> None:
	# Same divergent if/else shape through the capture-carrying immediate
	# invocation route (captures are legal for IIFEs).
	src = _PRELUDE + (
		"pub error ExcA { kind: Int }\n"
		"pub error ExcB { kind: Int }\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval n = 3;\n"
		"\tval c = try (|| -> Int => { if n > 2 { throw ExcA(kind = 3); } else { throw ExcB(kind = 4); } })() catch { 30 };\n"
		"\treturn c - 30;\n}\n"
	)
	r = _compile(tmp_path, src, out="nonflat_iife")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "nonflat_iife")]).returncode == 0


def test_statement_form_match_all_throw_lambda_runs(tmp_path: Path) -> None:
	# Statement-form match whose arms ALL throw: previously classified nothrow
	# (the can-throw walker had no HMatchExpr case) → the call site lowered
	# without error dispatch → SIGABRT at runtime.  Both arms exercised.
	src = _PRELUDE + (
		"pub error ExcA { kind: Int }\n"
		"pub error ExcB { kind: Int }\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval g = |k: Int| -> Int => {\n"
		"\t\tmatch k {\n"
		"\t\t\t0 => { throw ExcA(kind = 5); },\n"
		"\t\t\tdefault => { throw ExcB(kind = 6); }\n"
		"\t\t}\n"
		"\t};\n"
		"\tval d = try g(0) catch { 40 };\n"
		"\tval e = try g(9) catch { 50 };\n"
		"\treturn d + e - 90;\n}\n"
	)
	r = _compile(tmp_path, src, out="nonflat_match")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "nonflat_match")]).returncode == 0


def test_nested_block_throw_lambda_runs(tmp_path: Path) -> None:
	# Bare nested block ending in a throw: previously classified nothrow (the
	# can-throw walker had no HBlock statement case) → SIGABRT at runtime.
	src = _PRELUDE + (
		"pub error ExcA { kind: Int }\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval h = || -> Int => { { throw ExcA(kind = 7); } };\n"
		"\tval i = try h() catch { 60 };\n"
		"\treturn i - 60;\n}\n"
	)
	r = _compile(tmp_path, src, out="nonflat_block")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "nonflat_block")]).returncode == 0


def test_try_catch_all_terminal_lambda_runs(tmp_path: Path) -> None:
	# Divergence via try/catch: body throws, catch-all arm rethrows a
	# different event — every exit throws, so the value-less `-> Int` body is
	# accepted and runs (shared terminal-flow authority, HTry case).
	src = _PRELUDE + (
		"pub error ExcA { kind: Int }\n"
		"pub error ExcB { kind: Int }\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval t = |n: Int| -> Int => { try { throw ExcA(kind = n); } catch { throw ExcB(kind = 2); } };\n"
		"\tval a = try t(1) catch { 10 };\n"
		"\treturn a - 10;\n}\n"
	)
	r = _compile(tmp_path, src, out="div_try")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "div_try")]).returncode == 0


def test_unsafe_terminal_block_lambda_runs(tmp_path: Path) -> None:
	# Divergence via an unsafe block whose body throws (HUnsafeBlock case).
	src = _PRELUDE + (
		"pub error ExcA { kind: Int }\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval t = || -> Int => { unsafe { throw ExcA(kind = 3); } };\n"
		"\tval b = try t() catch { 20 };\n"
		"\treturn b - 20;\n}\n"
	)
	r = _compile(tmp_path, src, out="div_unsafe", allow_unsafe=True)
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "div_unsafe")]).returncode == 0


def test_nonbreaking_loop_terminal_lambda_runs(tmp_path: Path) -> None:
	# Divergence via `while true` with no break (the literal-cond fold makes
	# the desugared else-break dead): the only exit is the throw.
	src = _PRELUDE + (
		"pub error ExcA { kind: Int }\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval t = |n: Int| -> Int => { while true { if n > 0 { throw ExcA(kind = 4); } } };\n"
		"\tval c = try t(5) catch { 30 };\n"
		"\treturn c - 30;\n}\n"
	)
	r = _compile(tmp_path, src, out="div_loop")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "div_loop")]).returncode == 0


def test_terminal_throws_tail_call_lambda_runs(tmp_path: Path) -> None:
	# Divergence via a tail call to a terminal-`throws` function.  The IIFE
	# form pins the lambda route end-to-end (a trailing terminal call is a
	# STATEMENT, not the value tail — value-context lowering ICEd with an
	# Unknown-layout MoveOut); the named-fn wrapper pins route parity.
	# The STORED form of the same body is pinned green in
	# test_hidden_lambda_return_boundary.py (0.35.0 hidden-return fix).
	src = _PRELUDE + (
		"pub error ExcA { kind: Int }\n"
		"fn fail(code: Int) throws {\n"
		"\tthrow ExcA(kind = code);\n}\n"
		"fn wrap(n: Int) -> Int {\n"
		"\tfail(n);\n}\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval n = 7;\n"
		"\tval d = try (|| -> Int => { fail(n); })() catch { 40 };\n"
		"\tval e = try wrap(9) catch { 50 };\n"
		"\treturn d + e - 90;\n}\n"
	)
	r = _compile(tmp_path, src, out="div_termcall")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "div_termcall")]).returncode == 0


def test_dead_literal_false_throw_in_nothrow_lambda_runs(tmp_path: Path) -> None:
	# Effect-side CFG reachability: the `if false` arm can never execute, so
	# its throw contributes no effect and the `nothrow` lambda is accepted
	# AND runs (the structural walk used to count the dead throw and reject).
	src = _PRELUDE + (
		"pub error ExcA { kind: Int }\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval f = | | nothrow => {\n"
		"\t\tif false {\n"
		"\t\t\tthrow ExcA(kind = 1);\n"
		"\t\t}\n"
		"\t\t0\n"
		"\t};\n"
		"\tval x = f();\n"
		"\treturn x;\n}\n"
	)
	r = _compile(tmp_path, src, out="deadfalse")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "deadfalse")]).returncode == 0


def test_dead_throw_after_continue_lambda_runs(tmp_path: Path) -> None:
	# Effect+flow agreement on dead tails: the throw AFTER `continue` is
	# unreachable, so the try's catch arm (and its `break`) stays dead — the
	# loop is still permanently divergent via the reachable `if n > 0` throw,
	# and the lambda compiles and runs.
	src = _PRELUDE + (
		"pub error ExcA { kind: Int }\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval f = |n: Int| -> Int => { while true { if n > 0 { throw ExcA(kind = 9); } try { continue; throw ExcA(kind = 1); } catch { break; } } };\n"
		"\tval a = try f(1) catch { 10 };\n"
		"\treturn a - 10;\n}\n"
	)
	r = _compile(tmp_path, src, out="deadtailthrow")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "deadtailthrow")]).returncode == 0


def test_dead_break_after_continue_lambda_runs(tmp_path: Path) -> None:
	# Review finding 1 boundary pin: `continue; break;` — the break is dead
	# (unreachable), so the loop never falls through and the lambda is
	# permanently divergent.  When break/continue were classified FALLTHROUGH
	# the reachable-break scan walked past the `continue`, counted the dead
	# break, gave the loop a fallthrough exit, and falsely rejected this
	# valid non-Void lambda "must return a value".  Full compile/run
	# companion per AGENTS.md Rule 3 (the acceptance is lowering-visible).
	src = _PRELUDE + (
		"pub error ExcA { kind: Int }\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval t = |n: Int| -> Int => { while true { if n > 0 { throw ExcA(kind = 1); } continue; break; } };\n"
		"\tval a = try t(3) catch { 10 };\n"
		"\treturn a - 10;\n}\n"
	)
	r = _compile(tmp_path, src, out="deadbrk")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "deadbrk")]).returncode == 0


def test_dead_catch_break_divergent_lambda_runs(tmp_path: Path) -> None:
	# Unreachable-catch gating (flow side): `try { continue; } catch
	# { break; }` — the attempt cannot throw, so the catch `break` is dead
	# and must not give the loop a fallthrough exit.  Counting it falsely
	# rejected this genuinely divergent lambda "must return a value".
	src = _PRELUDE + (
		"pub error ExcA { kind: Int }\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval t = |n: Int| -> Int => { while true { if n > 0 { throw ExcA(kind = 1); } try { continue; } catch { break; } } };\n"
		"\tval a = try t(1) catch { 10 };\n"
		"\treturn a - 10;\n}\n"
	)
	r = _compile(tmp_path, src, out="deadcatchbrk")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "deadcatchbrk")]).returncode == 0


def test_dead_throwing_handler_in_nothrow_lambda_runs(tmp_path: Path) -> None:
	# Unreachable-catch gating (effect side): the attempt is effectless, so
	# the throwing catch-all handler can never run — the `nothrow` lambda is
	# accepted and runs (previously rejected "may throw").
	src = _PRELUDE + (
		"pub error ExcA { kind: Int }\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval outer = | | nothrow => {\n"
		"\t\ttry { val a = 1; } catch { throw ExcA(kind = 1); }\n"
		"\t\t0\n"
		"\t};\n"
		"\tval x = outer();\n"
		"\treturn x;\n}\n"
	)
	r = _compile(tmp_path, src, out="deadhandler")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "deadhandler")]).returncode == 0


def test_lazy_dead_operands_in_nothrow_lambdas_run(tmp_path: Path) -> None:
	# Lazy-evaluation effect agreement: `false && throwing()`, a literal-true
	# ternary's untaken throwing branch, and a passing assert's throwing
	# message can never evaluate — the `nothrow` lambdas are accepted AND run.
	src = _PRELUDE + (
		"pub error ExcA { kind: Int }\n"
		"fn might(k: Int) -> Int {\n"
		"\tif k == 0 {\n"
		"\t\tthrow ExcA(kind = 1);\n\t}\n"
		"\treturn k;\n}\n"
		"fn mightb(k: Int) -> Bool {\n"
		"\tif k == 0 {\n"
		"\t\tthrow ExcA(kind = 1);\n\t}\n"
		"\treturn true;\n}\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval a = | | nothrow => { val d = false and mightb(0); 0 };\n"
		"\tval b = | | nothrow => { (true ? 1 : might(0)) };\n"
		"\tval c = | | nothrow => { assert(true, \"never-built\"); 0 };\n"
		"\treturn a() + b() + c() - 1;\n}\n"
	)
	r = _compile(tmp_path, src, out="lazydead")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "lazydead")]).returncode == 0


def test_lazy_live_operands_still_reject_nothrow(tmp_path: Path) -> None:
	# Live companions: when the throwing side IS reachable, the nothrow
	# declaration still rejects.
	src = _PRELUDE + (
		"pub error ExcA { kind: Int }\n"
		"fn mightb(k: Int) -> Bool {\n"
		"\tif k == 0 {\n"
		"\t\tthrow ExcA(kind = 1);\n\t}\n"
		"\treturn true;\n}\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval a = | | nothrow => { val d = true and mightb(0); 0 };\n"
		"\treturn a();\n}\n"
	)
	r = _compile(tmp_path, src, out="lazylive")
	assert r.returncode != 0, "reachable throwing RHS must still reject a nothrow lambda"
	assert "declared nothrow but may throw" in r.stderr, r.stderr


def test_bare_return_lambda_still_rejected(tmp_path: Path) -> None:
	# A bare `return;` exits WITHOUT a value — divergence acceptance must not
	# admit it for a declared non-Void return (RETURNS_BARE is not THROWS).
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\tval f = || -> Int => { return; };\n"
		"\tval x = f();\n"
		"\treturn 0;\n}\n"
	)
	r = _compile(tmp_path, src, out="bareret")
	assert r.returncode != 0, "bare `return;` in a `-> Int` lambda must be rejected"


def test_value_match_throwing_arm_result_both_paths_run(tmp_path: Path) -> None:
	# Effect-walker isolation (value position): a VALUE match whose one arm
	# RESULT is a can-throw call and whose other arm is a normal value.  Both
	# arms exercised — the throwing arm must reach the catch (the old walker
	# had no HMatchExpr case: the lambda was classified nothrow and the call
	# aborted), and the value arm must produce the real value (not a zeroed
	# discard).  IIFE form; the stored form is pinned green in
	# test_hidden_lambda_return_boundary.py (0.35.0 hidden-return fix).
	src = _PRELUDE + (
		"pub error ExcA { kind: Int }\n"
		"fn might(k: Int) -> Int {\n"
		"\tif k == 0 {\n"
		"\t\tthrow ExcA(kind = 1);\n\t}\n"
		"\treturn k;\n}\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval k0 = 0;\n"
		"\tval a = try (|| -> Int => { match k0 == 0 { true => { might(0) }, false => { (k0 + 1) }, } })() catch { 10 };\n"
		"\tval k4 = 4;\n"
		"\tval b = try (|| -> Int => { match k4 == 0 { true => { might(0) }, false => { (k4 + 1) }, } })() catch { 99 };\n"
		"\treturn a + b - 15;\n}\n"
	)
	r = _compile(tmp_path, src, out="valmatch")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "valmatch")]).returncode == 0


def test_ctor_kwarg_and_exception_init_throw_effect_runs(tmp_path: Path) -> None:
	# Effect-walker wrapper coverage: the only escaping throws sit inside a
	# struct-ctor KEYWORD argument and an exception-initializer keyword value
	# — slots the old walkers never visited (`.args`-only descent), so these
	# lambdas were classified nothrow and aborted at runtime.
	src = _PRELUDE + (
		"pub error ExcA { kind: Int }\n"
		"pub struct Pt {\n"
		"\tpub x: Int\n"
		"}\n"
		"fn might(k: Int) -> Int {\n"
		"\tif k == 0 {\n"
		"\t\tthrow ExcA(kind = 1);\n\t}\n"
		"\treturn k;\n}\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval g = |k: Int| -> Int => { val p = Pt(x = might(k)); p.x };\n"
		"\tval c = try g(0) catch { 7 };\n"
		"\tval d = try g(3) catch { 99 };\n"
		"\tval h = |k: Int| -> Int => { val e = ExcA(kind = might(k)); e.kind };\n"
		"\tval i = try h(0) catch { 2 };\n"
		"\tval j = try h(6) catch { 99 };\n"
		"\treturn c + d + i + j - 18;\n}\n"
	)
	r = _compile(tmp_path, src, out="kwargeff")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "kwargeff")]).returncode == 0


def test_nested_lambda_iife_in_nothrow_lambda_rejected(tmp_path: Path) -> None:
	# Nested-lambda boundary, executing half: an IIFE inside a `nothrow`
	# lambda EXECUTES its body, so the throw escapes and the outer lambda
	# must be rejected.  (The construction half — an uninvoked nested lambda
	# must NOT make the outer lambda throwing — has its driver compile/run
	# positive in test_uninvoked_stored_lambda.py and unit pins in
	# lang/tests/checker/test_hir_flow.py.)
	src = _PRELUDE + (
		"pub error ExcA { kind: Int }\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval outer = | | nothrow => {\n"
		"\t\t(|| -> Int => { throw ExcA(kind = 1); })();\n"
		"\t\t0\n"
		"\t};\n"
		"\tval x = outer();\n"
		"\treturn x;\n}\n"
	)
	r = _compile(tmp_path, src, out="nestneg")
	assert r.returncode != 0, "IIFE of a throwing lambda inside a nothrow lambda must be rejected"
	assert "declared nothrow but may throw" in r.stderr, r.stderr


def test_valueless_fallthrough_lambda_still_rejected(tmp_path: Path) -> None:
	# The guard still rejects a genuinely value-less body that CAN fall
	# through — divergence acceptance must not have opened this hole.
	src = _PRELUDE + (
		"pub error MyExc { kind: Int }\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval f = |n: Int| -> Int => { if n > 0 { throw MyExc(kind = 1); } };\n"
		"\tval x = try f(1) catch { 3 };\n"
		"\treturn x - 3;\n}\n"
	)
	r = _compile(tmp_path, src, out="fallthrough")
	assert r.returncode != 0, "if-without-else divergence must still reject (can fall through)"
	assert "lambda with explicit return type must return a value" in r.stderr, r.stderr


def test_stored_flat_throw_only_lambda_still_runs(tmp_path: Path) -> None:
	# Certified-parity guard for the exemption: the FLAT trailing-throw form is
	# the one divergent shape codegen lowers correctly — it ran on certified
	# 0.33.90 and must keep compiling and running.
	src = _PRELUDE + (
		"pub error MyExc { kind: Int }\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval f = || -> Int => { throw MyExc(kind = 1); };\n"
		"\tval x = try f() catch { 3 };\n"
		"\treturn x - 3;\n}\n"
	)
	r = _compile(tmp_path, src, out="flat")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "flat")]).returncode == 0
