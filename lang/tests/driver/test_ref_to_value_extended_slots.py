# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""`&T → T` auto-dup coercion in non-call value slots.

0.31.68 landed the call-arg auto-dup (`f(s_ref)` where `f` takes
`String`).  This file extends the rule to three sibling value
slot families:

  - **let-init**:        `val name: T = expr_of_ref_T`
  - **return**:          `return expr_of_ref_T` from `fn -> T`
  - **comparison binops**: `lhs_of_ref_T <op> rhs_of_T` (and the
                            symmetric `lhs_of_T <op> rhs_of_ref_T`)
                            for `op ∈ {==, !=, <, <=, >, >=}` —
                            all six share the same type-checker
                            branch so the coercion path is
                            uniform across them.

In each case the type-checker now inserts an explicit `HUnary(DEREF, …)`
HIR node (and, for non-Copy ConstShare T, wraps in
`.const_share()`) so HIR→MIR lowering sees the unwrapped
owned-value shape.  Same mechanism as the call-arg path; the
predicate `_ref_to_value_coerce_applies` and the rewriter
`_rewrite_ref_to_value` live next to `_proves_const_share` in
type_checker.py and are shared across all four slot families.

Tests are compile-and-run with exit-code assertions — proving the
synthesized deref / wrap actually executes correctly, not just
that the type-checker stopped complaining.  The comparison-binop
block pins `==`, `!=`, and orderings in both operand directions
(coverage requested by the app team after the 0.31.75 review
flagged the test file only exercised `==` while implementation
covered all six).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]


def _compile_and_run(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out_bin = tmp_path / "bin"
	stdlib = stdlib_root() or (ROOT / "stdlib")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(stdlib),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(60),
	)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[:1500]}"
	return subprocess.run(
		[str(out_bin)], capture_output=True, text=True, timeout=sanitizer_timeout(20),
	)


# ---------------------------------------------------------------------------
# let-init: `val name: T = expr_of_ref_T`
# ---------------------------------------------------------------------------


def test_let_init_coerces_ref_string_to_string(tmp_path: Path) -> None:
	"""`val owned: String = s_ref` where `s_ref: &String` derefs
	(retain on String's Copy semantics).  Exit = byte_length of
	the resulting owned String.
	"""
	run = _compile_and_run(
		tmp_path,
		"""
module main;

fn capture(s: &String) nothrow -> Int {
	val owned: String = s;
	return owned.byte_length();
}

pub fn main() nothrow -> Int {
	val src: String = "hello";
	return capture(src);
}
""".lstrip(),
	)
	assert run.returncode == 5, (
		f"expected exit 5 (len 'hello'); got {run.returncode}.  "
		f"stderr: {run.stderr[:200]}"
	)


def test_let_init_coerces_ref_int_to_int(tmp_path: Path) -> None:
	"""Plain-Copy scalar (Int) path through let-init."""
	run = _compile_and_run(
		tmp_path,
		"""
module main;

fn capture(n: &Int) nothrow -> Int {
	val v: Int = n;
	return v + 1;
}

pub fn main() nothrow -> Int {
	val src: Int = 41;
	return capture(src);
}
""".lstrip(),
	)
	assert run.returncode == 42


# ---------------------------------------------------------------------------
# return: `return expr_of_ref_T` from `fn -> T`
# ---------------------------------------------------------------------------


def test_return_coerces_ref_string_to_string(tmp_path: Path) -> None:
	"""`return s_ref` from `fn extract(s: &String) -> String`
	derefs the ref so the caller receives an owned String.
	Pre-fix, stage 2 raised `internal: cannot return reference
	as owned 'String'; ... requires explicit 'copy <expr>'`.
	"""
	run = _compile_and_run(
		tmp_path,
		"""
module main;

fn extract(s: &String) nothrow -> String {
	return s;
}

pub fn main() nothrow -> Int {
	val src: String = "ok";
	val out = extract(src);
	return out.byte_length();
}
""".lstrip(),
	)
	assert run.returncode == 2


def test_return_coerces_ref_int_to_int(tmp_path: Path) -> None:
	"""Plain-Copy scalar (Int) path through return."""
	run = _compile_and_run(
		tmp_path,
		"""
module main;

fn extract(n: &Int) nothrow -> Int {
	return n;
}

pub fn main() nothrow -> Int {
	val src: Int = 7;
	return extract(src);
}
""".lstrip(),
	)
	assert run.returncode == 7


# ---------------------------------------------------------------------------
# Comparison binops: `&T <op> T` and the symmetric `T <op> &T`.
# All six ops (`==`, `!=`, `<`, `<=`, `>`, `>=`) share the same
# type-checker branch — the deref is inserted on whichever side
# is a ref.  Tests below pin `==` (positive + negative-value +
# both directions), `!=`, and orderings in both operand
# directions.
# ---------------------------------------------------------------------------


def test_eq_coerces_ref_string_lhs_to_string(tmp_path: Path) -> None:
	"""`s_ref == "literal"` — lhs is `&String`, rhs is `String`.
	The lhs derefs so both operands are `String` for the
	equality.  Pre-fix: `comparison requires matching operand
	types (have Ref<String> vs String)`.
	"""
	run = _compile_and_run(
		tmp_path,
		"""
module main;

fn match_lit(s: &String) nothrow -> Int {
	var tag: Int = 0;
	if s == "hello" { tag = 1; }
	return tag;
}

pub fn main() nothrow -> Int {
	val src: String = "hello";
	return match_lit(src);
}
""".lstrip(),
	)
	assert run.returncode == 1


def test_eq_coerces_ref_string_rhs_to_string(tmp_path: Path) -> None:
	"""Symmetric: `"literal" == s_ref`.  rhs is `&String`, lhs is
	`String`.  The rhs derefs.
	"""
	run = _compile_and_run(
		tmp_path,
		"""
module main;

fn match_lit(s: &String) nothrow -> Int {
	var tag: Int = 0;
	if "world" == s { tag = 1; }
	return tag;
}

pub fn main() nothrow -> Int {
	val src: String = "world";
	return match_lit(src);
}
""".lstrip(),
	)
	assert run.returncode == 1


def test_ne_coerces_ref_string(tmp_path: Path) -> None:
	"""`s_ref != "other"` — exercises the `!=` operand of the
	comparison-binop family.  When the values differ, returns
	true (tag = 1).
	"""
	run = _compile_and_run(
		tmp_path,
		"""
module main;

fn differs(s: &String) nothrow -> Int {
	var tag: Int = 0;
	if s != "expected" { tag = 1; }
	return tag;
}

pub fn main() nothrow -> Int {
	val src: String = "different";
	return differs(src);
}
""".lstrip(),
	)
	assert run.returncode == 1


def test_lt_coerces_ref_int_lhs(tmp_path: Path) -> None:
	"""`n_ref < 100` — exercises an *ordering* op with the ref
	on the lhs.  `42 < 100` → true → tag = 1.
	"""
	run = _compile_and_run(
		tmp_path,
		"""
module main;

fn under_limit(n: &Int) nothrow -> Int {
	var tag: Int = 0;
	if n < 100 { tag = 1; }
	return tag;
}

pub fn main() nothrow -> Int {
	val src: Int = 42;
	return under_limit(src);
}
""".lstrip(),
	)
	assert run.returncode == 1


def test_gt_coerces_ref_int_rhs(tmp_path: Path) -> None:
	"""`100 > n_ref` — exercises an ordering op with the ref on
	the rhs (symmetric direction to the `lt` test above).
	`100 > 42` → true → tag = 1.
	"""
	run = _compile_and_run(
		tmp_path,
		"""
module main;

fn under_limit(n: &Int) nothrow -> Int {
	var tag: Int = 0;
	if 100 > n { tag = 1; }
	return tag;
}

pub fn main() nothrow -> Int {
	val src: Int = 42;
	return under_limit(src);
}
""".lstrip(),
	)
	assert run.returncode == 1


def test_le_coerces_ref_int_lhs(tmp_path: Path) -> None:
	"""`n_ref <= 42` — exercises the `<=` ordering op with the ref
	on the lhs.  `42 <= 42` → true → tag = 1.
	"""
	run = _compile_and_run(
		tmp_path,
		"""
module main;

fn at_or_below(n: &Int) nothrow -> Int {
	var tag: Int = 0;
	if n <= 42 { tag = 1; }
	return tag;
}

pub fn main() nothrow -> Int {
	val src: Int = 42;
	return at_or_below(src);
}
""".lstrip(),
	)
	assert run.returncode == 1


def test_ge_coerces_ref_int_rhs(tmp_path: Path) -> None:
	"""`100 >= n_ref` — exercises the `>=` ordering op with the
	ref on the rhs (complementary direction to the `<=` probe).
	`100 >= 42` → true → tag = 1.
	"""
	run = _compile_and_run(
		tmp_path,
		"""
module main;

fn ceiling_ok(n: &Int) nothrow -> Int {
	var tag: Int = 0;
	if 100 >= n { tag = 1; }
	return tag;
}

pub fn main() nothrow -> Int {
	val src: Int = 42;
	return ceiling_ok(src);
}
""".lstrip(),
	)
	assert run.returncode == 1


def test_eq_negative_case_compiles_and_returns_false(tmp_path: Path) -> None:
	"""Sanity: `s_ref == "different"` compiles + returns false
	when the values genuinely differ.  Guards against a
	pathological "coerce always passes through True" failure
	mode.
	"""
	run = _compile_and_run(
		tmp_path,
		"""
module main;

fn match_lit(s: &String) nothrow -> Int {
	var tag: Int = 0;
	if s == "expected" { tag = 1; }
	return tag;
}

pub fn main() nothrow -> Int {
	val src: String = "different";
	return match_lit(src);
}
""".lstrip(),
	)
	assert run.returncode == 0


# ---------------------------------------------------------------------------
# Negative: non-Copy non-ConstShare type still rejects in let-init.
# Pins that the coercion gate (Copy or ConstShare) is enforced and
# the fix doesn't silently widen to all `&T → T` shapes.
# ---------------------------------------------------------------------------


def test_let_init_negative_destructible_still_rejected(tmp_path: Path) -> None:
	"""`val owned: Resource = r_ref` where `Resource` has a user
	`Destructible` (not Copy, not ConstShare) must still reject.
	"""
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module main;

import std.core as core;

pub struct Resource { pub tag: Int }

implement core.Destructible for Resource {
	pub fn destroy(var self: Resource) nothrow -> Void { return; }
}

fn capture(r: &Resource) nothrow -> Int {
	val owned: Resource = r;
	return owned.tag;
}

pub fn main() nothrow -> Int {
	return 0;
}
""".lstrip(),
		encoding="utf-8",
	)
	stdlib = stdlib_root() or (ROOT / "stdlib")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(stdlib),
		 str(src), "--entry", "main::main"],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(60),
	)
	assert res.returncode != 0, (
		f"Resource (non-Copy non-ConstShare) must not auto-dup; "
		f"compile unexpectedly succeeded.\nstderr: {res.stderr[:500]}"
	)


def test_return_negative_destructible_still_rejected(tmp_path: Path) -> None:
	"""Companion to `test_let_init_negative_destructible_still_rejected`
	covering the RETURN-position slot.  `return r` where `r: &Resource`
	and the function returns owned `Resource` must reject — the
	implicit ref-to-value coercion is gated on Copy / ConstShare for T,
	and a user-`Destructible` struct is neither.  Without this guard,
	the auto-deref would silently bitcopy the struct and run the
	user destructor twice on the same heap state.

	Pinned alongside the now-passing `ref_variant_binder_return_owned`
	codegen e2e (which exercises the positive Copy-T return path that
	the variant-binder shape produces) to lock in the asymmetry:
	return slot accepts &T → T for Copy/ConstShare T, rejects for any
	other T.

	Asserts both a nonzero exit AND the specific Copy-gate diagnostic so
	the test can't pass for an unrelated parser / import / entry failure
	— the rejection MUST come from the non-Copy / non-ConstShare gate
	this test is meant to protect.  Both the stable error code
	(`E-AUTO-9cc3ceb9`) and the human-readable shape
	(`cannot copy value of type 'Resource'`) are checked.
	"""
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module main;

import std.core as core;

pub struct Resource { pub tag: Int }

implement core.Destructible for Resource {
	pub fn destroy(var self: Resource) nothrow -> Void { return; }
}

fn extract(r: &Resource) nothrow -> Resource {
	return r;
}

pub fn main() nothrow -> Int {
	return 0;
}
""".lstrip(),
		encoding="utf-8",
	)
	stdlib = stdlib_root() or (ROOT / "stdlib")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(stdlib),
		 str(src), "--entry", "main::main"],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(60),
	)
	assert res.returncode != 0, (
		f"return of &Resource as owned Resource (non-Copy "
		f"non-ConstShare) must not auto-dup; compile unexpectedly "
		f"succeeded.\nstderr: {res.stderr[:500]}"
	)
	diag = res.stderr or ""
	assert "cannot copy value of type 'Resource'" in diag, (
		"compile rejected, but not via the non-Copy / non-ConstShare "
		"return-slot gate this test pins.  Expected diagnostic text "
		"\"cannot copy value of type 'Resource'\" was not in stderr — "
		"the failure may be from an unrelated parser / import / entry "
		f"error.\nstderr: {diag[:1000]}"
	)
	assert "[E-AUTO-9cc3ceb9]" in diag, (
		"compile rejected with the right human-readable message but the "
		"stable error code `E-AUTO-9cc3ceb9` was missing — the "
		"diagnostic identity may have shifted.\nstderr: {diag[:1000]}"
	)
