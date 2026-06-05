# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Regression (LANGUAGE_BUG): a lambda bound to a `val` inside a value-producing
match arm must be type-inferred the same as at function-body / if-branch scope.

Bug: when the call to the lambda binding is the arm's TRAILING-RESULT expression
(`{ val g = || -> Int => { 7 }; g() }`), the reference in `g()` was not linked to
the arm-local `HLet` binding, so the deferred-lambda resolution never fired and
the binding stayed typed `Unknown` — surfacing as a misleading `E-COPY-UNKNOWN`
("cannot copy 'g': type 'Unknown'") instead of compiling (non-capturing) or
giving the clear "capturing lambdas cannot be coerced to function pointers"
diagnostic (capturing).  The same binding works at top level, in an if-branch,
when the call is in an inner `val`, when the call is a non-trailing statement,
and when the lambda is immediately invoked (IIFE).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]
_HDR = "module repro;\nvariant R { Ok(v: Int), Err(e: Int) }\n"


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
	return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=180)


def test_noncapturing_stored_lambda_trailing_result_compiles_and_runs(tmp_path: Path) -> None:
	# THE bug: g() is the arm's trailing-result expression.
	src = _HDR + (
		"fn f(r: R) -> Int {\n"
		"\tval x = match r { R::Ok(v) => { v }, R::Err(e) => { val g = || -> Int => { 7 }; g() } };\n"
		"\treturn x;\n}\n"
		"fn main() nothrow -> Int { return f(R::Err(4)) - 7; }\n"
	)
	r = _compile(tmp_path, src, out="trail")
	assert r.returncode == 0, r.stderr
	assert "E-COPY-UNKNOWN" not in r.stderr, r.stderr
	assert subprocess.run([str(tmp_path / "trail")]).returncode == 0


def test_capturing_stored_lambda_in_arm_reports_clear_diagnostic(tmp_path: Path) -> None:
	# A capturing stored lambda is rejected — and must surface the SAME clear,
	# actionable primary diagnostic as at top-level / if-branch.  Before the fix
	# the arm case was MISSING this message entirely (it produced only the
	# misleading `Unknown`/`E-COPY-UNKNOWN` cascade); now it has parity.  (The
	# secondary copy-unknown / not-a-fn-value cascade is the same at top level,
	# so we assert on the actionable message, not the cascade's absence.)
	src = _HDR + (
		"fn f(r: R) -> Int {\n"
		"\tval k = 4;\n"
		"\tval x = match r { R::Ok(v) => { v }, R::Err(e) => { val g = || -> Int => { k + 1 }; g() } };\n"
		"\treturn x;\n}\n"
		"fn main() nothrow -> Int { return f(R::Err(9)); }\n"
	)
	r = _compile(tmp_path, src, out="cap")
	assert r.returncode != 0
	assert "capturing lambdas cannot be coerced to function pointers" in r.stderr, r.stderr


def test_iife_lambda_in_arm_still_works(tmp_path: Path) -> None:
	# Control: an immediately-invoked lambda as the arm value is unaffected.
	src = _HDR + (
		"fn f(r: R) -> Int {\n"
		"\tval x = match r { R::Ok(v) => { v }, R::Err(e) => { (|| -> Int => { 7 })() } };\n"
		"\treturn x;\n}\n"
		"fn main() nothrow -> Int { return f(R::Err(4)) - 7; }\n"
	)
	r = _compile(tmp_path, src, out="iife")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "iife")]).returncode == 0
