# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Regression (bug #7, bookkeeper team): a bare tail `match` (or any bare tail
expression) is NOT an implicit function return in Drift v1.  `match` used as an
expression must be returned, bound, or passed; a bare tail expression at the end
of a function body is a parse error, and its diagnostic must say so clearly
(no implicit return) instead of the misleading "top-level statements like
import, export, const require a trailing semicolon" cascade.

Must keep working:
  - `return match e { ... };`
  - `val x = match e { ... }; return x;`
  - statement-form `match` whose arms `return`.

`match e { ... } - 7` (a match combined with operators) falls under the same
parser shape; the diagnostic points at the parentheses/binding remedy, and both
`(match e { ... }) - 7` and bind-then-operate compile.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.driftc.parser import stdlib_root

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]
_HDR = "module repro;\nvariant Opt { Some(v: Int), None }\n"
_MAIN = "fn main() nothrow -> Int { val o = Opt::Some(5); return f(o) - 5; }\n"
_ARMS = "{ Opt::Some(v) => { v }, Opt::None => { 0 } }"


def _compile(tmp_path: Path, fn_src: str, *, out: str) -> subprocess.CompletedProcess:
	(tmp_path / "main.drift").write_text(_HDR + fn_src + "\n" + _MAIN, encoding="utf-8")
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc",
		str(tmp_path / "main.drift"), "--entry", "repro::main",
		"--target-word-bits", "64", "-o", str(tmp_path / out),
	]
	stdlib = stdlib_root()
	if stdlib is not None:
		cmd.extend(["--stdlib-root", str(stdlib)])
	return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(180))


def test_bare_tail_match_rejected_with_clear_diagnostic(tmp_path: Path) -> None:
	r = _compile(tmp_path, f"fn f(o: Opt) nothrow -> Int {{ match o {_ARMS} }}", out="bare")
	assert r.returncode != 0
	# Clear about the real problem: no implicit return / not a function return.
	assert "no implicit return" in r.stderr and "is NOT a function return" in r.stderr, r.stderr
	# Must NOT lead with the misleading top-level-decl emphasis.
	assert "top-level statements like import, export, and const require" not in r.stderr, r.stderr


def test_return_match_runs(tmp_path: Path) -> None:
	r = _compile(tmp_path, f"fn f(o: Opt) nothrow -> Int {{ return match o {_ARMS}; }}", out="ret")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "ret")]).returncode == 0


def test_bind_then_return_runs(tmp_path: Path) -> None:
	r = _compile(tmp_path, f"fn f(o: Opt) nothrow -> Int {{ val x = match o {_ARMS}; return x; }}", out="bind")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "bind")]).returncode == 0


def test_statement_form_match_return_arms_runs(tmp_path: Path) -> None:
	r = _compile(
		tmp_path,
		"fn f(o: Opt) nothrow -> Int { match o { Opt::Some(v) => { return v; }, Opt::None => { return 0; } } }",
		out="stmt",
	)
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "stmt")]).returncode == 0


def test_match_with_trailing_operator_diagnostic_and_remedies(tmp_path: Path) -> None:
	# `val x = match o { ... } - 7;` is rejected; message names the remedy.
	r = _compile(tmp_path, f"fn f(o: Opt) nothrow -> Int {{ val x = match o {_ARMS} - 7; return x; }}", out="op")
	assert r.returncode != 0
	assert "parentheses or a binding" in r.stderr, r.stderr
	# Both remedies compile.
	rp = _compile(tmp_path, f"fn f(o: Opt) nothrow -> Int {{ val x = (match o {_ARMS}) - 7; return x; }}", out="par")
	assert rp.returncode == 0, rp.stderr
	rb = _compile(tmp_path, f"fn f(o: Opt) nothrow -> Int {{ val m = match o {_ARMS}; return m - 7; }}", out="bnd")
	assert rb.returncode == 0, rb.stderr
