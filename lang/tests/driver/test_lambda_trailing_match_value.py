# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG regression (2026-07-24): a value-producing `match` (or
`try`) expression used DIRECTLY as a lambda's trailing expression ICE'd
in MIR lowering for EVERY lambda —

    internal: MIR lowering contract failure (value-producing match arm
    must yield a value or terminate (checker bug))          [variant]
    ... (value-producing Bool match arm must yield ...)     [Bool]

Root cause (suspected subsystem recorded per policy: stage1 AST→HIR,
`ast_to_hir.py::_visit_expr_Lambda`): the parser parses a lambda-tail
`match`/`try` via the EXPRESSION-form productions (arm blocks must end
with a value — E_EXPR_BLOCK_MISSING_VALUE otherwise), but the lambda
body conversion routed EVERY body statement through the generic
statement visitor, which lowers a statement-position match with
`value_context=False` — so the arm results were never split into
`HMatchArm.result`, and HIR→MIR's value path found `result is None`
and raised the "checker bug" assertion.  Named-function tails never
hit it (they use `return`/`val` forms that take the value-context
path).  `doc/refactor_triggers.md` scan: no registered trigger matches
this failure (confirmed at fix time).

Fix at the common authority: ExprStmt lowering is now ONE method,
`_lower_expr_stmt(stmt, *, value_context)` — the ordinary statement
visitor calls it with value_context=False, the lambda-tail conversion
calls it with value_context=True for EVERY trailing ExprStmt (no
per-shape branches at the call sites); the authority routes match/try
payloads to their expression lowerings with the position's
value_context.  Void lambdas are unaffected: HIR→MIR's lambda-tail
statement path evaluates-and-discards arm results.

Follow-up regression (caught by test_reload_coordinator in the full
suite): the parser has TWO match productions — `match_expr`
(value_block arms, every arm ends with a bare trailing expression) and
`match_stmt` (plain block arms, typically exiting via `return`) — but
both built the SAME ExprStmt(MatchExpr) AST, erasing the
classification.  Forcing value context onto a statement-form tail
match misread its arms as valueless results (E-MATCH-NO-VALUE).  The
parser now records `MatchExpr.statement_form`, carried through the
stage0 conversion, and the authority never applies value context to a
statement-form match.  Statement-form `try` is a distinct TryStmt node
and needs no flag.

Pinned here — the positive program is full compile-AND-run; the two
negatives are compile-and-REJECT (exact-diagnostic pins):
  1. Bool match as callback-lambda tail (first sighting's shape);
  2. Bool match preceded by statements (guard-teeth shape);
  3. variant match over a CALL-RESULT scrutinee (docs shape);
  4. variant match over a PARAM scrutinee;
  5. `try ... catch` value form as lambda tail (same family);
  6. the callback/coerced path AND behavior parity between a named-fn
     tail and the lambda tail (same inputs, same outputs).

Negative companions (compile-and-reject):
  7. `return` inside an expression-form lambda-tail match arm is
     rejected with EXACTLY E_EXPECTED_SEMICOLON (the parse diagnostic
     whose message spells out the match-as-value rules);
  8. unannotated non-callback lambdas still infer Void for ANY
     trailing expression (plain or match alike — pre-existing,
     deliberately unchanged inference), surfacing as EXACTLY the
     use-site arithmetic mismatch E-AUTO-5a90687a (Void vs Int),
     NOT an ICE.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]

POSITIVE_SRC = r"""module main;

import std.core as core;

error Boom { code: Int }

fn may_throw(n: Int) -> Int {
	if n < 0 { throw Boom(code = n); }
	return n * 2;
}

fn probe(n: Int) nothrow -> Optional<Int> {
	if n > 0 { return Optional::Some(n); }
	return Optional<Int>::None();
}

// Behavior-parity reference: the SAME match as a named-fn value.
fn named_ref(n: Int) nothrow -> Int {
	val r = match n > 0 { true => { 1 }, false => { 0 }, };
	return r;
}

pub fn main() nothrow -> Int {
	// 1. Bool match as the lambda tail (sole expression).
	val c1: core.Callback1<Int, Int> = core.callback1(|n: Int| => {
		match n > 0 { true => { 1 }, false => { 0 }, }
	});
	if c1.call(5) != 1 { return 1; }

	// 2. Bool match preceded by statements.
	val c2: core.Callback1<Int, Int> = core.callback1(|n: Int| => {
		val b = n > 0;
		match b { true => { 1 }, false => { 0 }, }
	});
	if c2.call(-3) != 0 { return 2; }

	// 3. Variant match over a CALL-RESULT scrutinee.
	val c3: core.Callback1<Int, Int> = core.callback1(|n: Int| => {
		match probe(n) { Some(v) => { v }, None() => { 0 }, }
	});
	if c3.call(7) != 7 { return 3; }

	// 4. Variant match over a PARAM scrutinee.
	val c4: core.Callback1<Optional<Int>, Int> = core.callback1(|o: Optional<Int>| => {
		match o { Some(v) => { v }, None() => { 0 }, }
	});
	if c4.call(Optional::Some(3)) != 3 { return 4; }

	// 5. try/catch value form as the lambda tail (same family).
	val c5: core.Callback1<Int, Int> = core.callback1(|n: Int| => {
		try may_throw(n) catch { -1 }
	});
	if c5.call(4) != 8 { return 5; }
	if c5.call(-1) != -1 { return 6; }

	// 6. Behavior parity with the named-fn form on both arms.
	val c6: core.Callback1<Int, Int> = core.callback1(|n: Int| => {
		match n > 0 { true => { 1 }, false => { 0 }, }
	});
	if c6.call(9) != named_ref(9) { return 7; }
	val c6b: core.Callback1<Int, Int> = core.callback1(|n: Int| => {
		match n > 0 { true => { 1 }, false => { 0 }, }
	});
	if c6b.call(-9) != named_ref(-9) { return 8; }

	// 7. STATEMENT-form match (plain block arms exiting via `return`)
	//    as the lambda tail — must STAY statement-lowered even though
	//    the position is a value position (the reload-coordinator
	//    regression shape), including a NESTED statement-form match as
	//    an arm's final statement.
	val c7: core.Callback1<Int, Int> = core.callback1(|n: Int| => {
		match probe(n) {
			Some(v) => {
				match v > 5 {
					true => { return v; },
					false => { return 0 - v; }
				}
			},
			None() => { return 0; }
		}
	});
	if c7.call(7) != 7 { return 9; }
	if c7.call(3) != -3 { return 10; }
	if c7.call(-1) != 0 { return 11; }

	return 0;
}
"""

NEG_RETURN_IN_ARM = r"""module main;

import std.core as core;

pub fn main() nothrow -> Int {
	val cb: core.Callback1<Int, Int> = core.callback1(|n: Int| => {
		match n > 0 { true => { return 1; }, false => { 0 }, }
	});
	return cb.call(1);
}
"""

NEG_VOID_INFERENCE = r"""module main;

pub fn main() nothrow -> Int {
	val r = (|n: Int| => {
		match n > 2 { true => { 10 }, false => { 20 }, }
	})(5);
	return r - 10;
}
"""


def _compile(tmp_path: Path, src_text: str, name: str):
	src = tmp_path / f"{name}.drift"
	src.write_text(src_text)
	out_bin = tmp_path / f"{name}.bin"
	stdlib = stdlib_root() or (ROOT / "stdlib")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(stdlib), str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	return res, out_bin


def test_lambda_trailing_match_value_family(tmp_path: Path) -> None:
	res, out_bin = _compile(tmp_path, POSITIVE_SRC, "positive")
	assert res.returncode == 0, f"{res.stdout}\n---\n{res.stderr[:2500]}"
	assert "MIR lowering contract failure" not in (res.stdout + res.stderr)
	run = subprocess.run([str(out_bin)], capture_output=True, text=True,
		timeout=sanitizer_timeout(60))
	assert run.returncode == 0, f"exit={run.returncode} (failing pin #)\n{run.stderr[:500]}"


def test_return_in_expression_form_arm_still_rejected(tmp_path: Path) -> None:
	res, _ = _compile(tmp_path, NEG_RETURN_IN_ARM, "neg_return")
	assert res.returncode != 0
	err = res.stdout + res.stderr
	# EXACT diagnostic pin for THIS source: `return` after the arm's `{`
	# ends expression-form arm parsing, and the parser rejects the
	# statement with E_EXPECTED_SEMICOLON — whose message spells out the
	# match-as-value rules ("return match e { ... };").
	assert "E_EXPECTED_SEMICOLON" in err, (
		f"the exact parse rejection for this shape must be preserved:\n{err[:1200]}"
	)
	assert "no implicit return" in err, "the guidance message must be preserved"
	assert "MIR lowering contract failure" not in err, "must never reach the MIR ICE"


def test_unannotated_lambda_void_inference_unchanged(tmp_path: Path) -> None:
	"""Pre-existing, deliberately unchanged: a lambda with no callback/
	annotation context infers Void regardless of its trailing expression
	(plain `n + 5` behaves identically) — for THIS source the exact
	surface is the use-site arithmetic mismatch (`r - 10` with r: Void),
	never an ICE."""
	res, _ = _compile(tmp_path, NEG_VOID_INFERENCE, "neg_void")
	assert res.returncode != 0
	err = res.stdout + res.stderr
	assert "MIR lowering contract failure" not in err, "must never ICE"
	assert "E-AUTO-5a90687a" in err, (
		f"expected the exact use-site diagnostic for this shape:\n{err[:1200]}"
	)
	assert "Void vs Int" in err, f"expected the Void operand mismatch:\n{err[:1200]}"
