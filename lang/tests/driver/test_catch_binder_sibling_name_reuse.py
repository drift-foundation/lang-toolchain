# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG regression (2026-07-24): sequential SIBLING typed
catches reusing the same source binder name failed with a spanless

    error: use of uninitialized 'e' [E-AUTO-77978427]

for every use in the second (and later) handler — each arm alone
compiled; renaming the second binder compiled.  Verified failing on
the pre-fix compiler (this exact fixture shape, plus the minimal
scratch repros): the borrow checker's catch-entry initialization was
NAME-keyed (`_binding_id_by_name` keeps the EARLIEST binding per
name), so handler 2's entry marked handler 1's binding VALID and left
handler 2's own binder — the one its uses actually resolve to —
spuriously uninitialized (bc-debug on the pre-fix compiler: both
entries marked bid 5; the failing use carried bid 6).

Classification (maintainer, 2026-07-24): LANGUAGE_BUG in checker
catch-binder scope/binding identity — LEXICAL identity/alpha-renaming,
not drop, unwind cleanup, or lifetime authority.  Trigger scan
recorded per policy: the creation-site lifetime-registration trigger
in doc/refactor_triggers.md does NOT fire for this defect class.

Fix at the identity owner: `HCatchArm.binder_id` now carries the
binder's binding identity (set by the type checker when it allocates
the STATEMENT-form arm-scoped binding; expression-form arms are
demonstrably unaffected — see pin 3 — and deliberately carry none);
the borrow checker's catch-entry initialization uses THE ARM'S OWN
binding EXCLUSIVELY when present — the name-keyed collection runs
only when no identity was recorded (see the mechanism tooth in
lang/tests/borrow_checker/test_catch_entry_marks_own_binding.py).
Mirrors the HMatchArm.binder_ids design (the 0.33.4/0.33.36
binder-identity family).

Pinned here:
  1. two sequential typed catches BOTH named `e` with DISTINCT
     payload types and field values — full compile AND run, each
     handler proving its `e` resolves to ITS OWN binder (reads its
     own arm's payload values, not the sibling's);
  2. three-arm chain (typed, typed, catch-all) all reusing `e`;
  3. same-name binders in expression-form try arms — expression-form
     is DEMONSTRABLY UNAFFECTED (no statement-style catch-entry
     marking path exists for HTryExpr; this pin proves the property
     and guards it), so HTryExprArm deliberately carries NO
     binder_id and the checker records none for it;
  4. NEGATIVE scope pins: neither binder is visible after its catch
     (use after the try statement still rejects with unknown-name).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]

POSITIVE_SRC = r"""module main;

import std.console as cons;

error AlphaErr { code: Int, tag: Int }
error BetaErr { level: Int }

fn throw_alpha(n: Int) -> Int {
	if n > 0 { throw AlphaErr(code = 41, tag = 7); }
	return n;
}

fn throw_beta(n: Int) -> Int {
	if n > 0 { throw BetaErr(level = 93); }
	return n;
}

pub fn main() nothrow -> Int {
	var got_alpha = 0;
	var got_beta = 0;

	// 1. Sequential sibling typed catches, SAME binder name `e`,
	//    DISTINCT payloads: each use must resolve to its own binder.
	try {
		val x = throw_alpha(1);
		return 1;
	} catch AlphaErr(e) {
		if e.code != 41 { return 2; }
		if e.tag != 7 { return 3; }
		got_alpha = e.code + e.tag;
	} catch {
		return 4;
	}

	try {
		val x = throw_beta(1);
		return 5;
	} catch BetaErr(e) {
		if e.level != 93 { return 6; }
		got_beta = e.level;
	} catch {
		return 7;
	}

	if got_alpha != 48 { return 8; }
	if got_beta != 93 { return 9; }

	// 2. Three-arm chain in ONE function, all reusing `e`.
	var third = 0;
	try {
		val x = throw_alpha(1);
		return 10;
	} catch AlphaErr(e) {
		third = third + e.code;
	} catch BetaErr(e) {
		third = third + e.level;
	} catch e {
		third = third + 1000;
	}
	try {
		val x = throw_beta(1);
		return 11;
	} catch AlphaErr(e) {
		third = third + e.tag;
	} catch BetaErr(e) {
		third = third + e.level;
	} catch e {
		third = third + 1000;
	}
	if third != 41 + 93 { return 12; }

	cons.println("sibling binders OK");
	return 0;
}
"""

EXPR_FORM_SRC = r"""module main;

error AlphaErr { code: Int, tag: Int }
error BetaErr { level: Int }

fn ta(n: Int) -> Int {
	if n > 0 { throw AlphaErr(code = 41, tag = 7); }
	return n;
}
fn tb(n: Int) -> Int {
	if n > 0 { throw BetaErr(level = 93); }
	return n;
}

pub fn main() nothrow -> Int {
	// expression-form try arms, sequential, BOTH binders named `e`,
	// distinct payloads: each use must read its own arm's payload.
	val a = try ta(1) catch AlphaErr(e) { e.code + e.tag } catch { 0 - 1 };
	val b = try tb(1) catch BetaErr(e) { e.level } catch { 0 - 1 };
	if a != 48 { return 1; }
	if b != 93 { return 2; }
	return 0;
}
"""

SCOPE_NEG_SRC = r"""module main;

error AlphaErr { code: Int, tag: Int }

fn boom() -> Int {
	throw AlphaErr(code = 1, tag = 2);
}

pub fn main() nothrow -> Int {
	try {
		val x = boom();
		return 1;
	} catch AlphaErr(e) {
		if e.code != 1 { return 2; }
	} catch {
		return 3;
	}
	// The binder must NOT leak out of its catch arm.
	return e.code;
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


def test_sibling_catch_binders_same_name(tmp_path: Path) -> None:
	res, out_bin = _compile(tmp_path, POSITIVE_SRC, "siblings")
	err = res.stdout + res.stderr
	assert "use of uninitialized" not in err, f"the sibling-binder defect is back:\n{err[:1200]}"
	assert res.returncode == 0, f"compile failed:\n{err[:2000]}"
	run = subprocess.run([str(out_bin)], capture_output=True, text=True,
		timeout=sanitizer_timeout(60))
	assert run.returncode == 0, (
		f"exit={run.returncode} (failing check # — a nonzero here means a use "
		f"resolved to the WRONG arm's binder)\n{run.stderr[:400]}"
	)


def test_expression_form_sibling_binders_unaffected(tmp_path: Path) -> None:
	res, out_bin = _compile(tmp_path, EXPR_FORM_SRC, "exprform")
	err = res.stdout + res.stderr
	assert "use of uninitialized" not in err, err[:800]
	assert res.returncode == 0, f"compile failed:\n{err[:1500]}"
	run = subprocess.run([str(out_bin)], capture_output=True, text=True,
		timeout=sanitizer_timeout(60))
	assert run.returncode == 0, f"exit={run.returncode}\n{run.stderr[:400]}"


def test_catch_binder_not_visible_after_arm(tmp_path: Path) -> None:
	res, _ = _compile(tmp_path, SCOPE_NEG_SRC, "scope_neg")
	assert res.returncode != 0, "binder leaked out of its catch arm"
	err = res.stdout + res.stderr
	# EXACT diagnostic: the out-of-scope use is an unknown-NAME error —
	# the binder does not exist after its arm.  It must NOT surface as
	# the uninitialized-binder diagnostic (that would mean the binding
	# leaked into function scope and merely lacked a value).
	assert "E-UNKNOWN-NAME" in err and "unknown name 'e'" in err, err[:800]
	assert "use of uninitialized" not in err, (
		f"binder leaked into function scope (visible but uninitialized):\n{err[:800]}"
	)
	assert "MIR lowering contract failure" not in err
