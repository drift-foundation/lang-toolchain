# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
LANGUAGE_BUG (0.34.2): a direct IIFE in STATEMENT position ICEd in MIR.

`(|| => {})();` routed HCall→`_lower_call`→INDIRECT→`_lower_indirect_call`→
`lower_expr(HLambda)` → "No MIR lowering for expr HLambda".  Expression
position (`val x = (...)()`) already routed through
`_lower_lambda_immediate_call`; the statement fast path did not.  Two fixes:

  * `_visit_stmt_HExprStmt`'s HCall/HInvoke fast paths exclude lambda callees,
    so statement IIFEs take the generic expression tail (immediate-call
    lowering + owned-result drop);
  * the immediate-call lowering's re-derived return type now falls back to
    Void for value-less/empty block bodies, matching the checker's 0.34.2
    `_lambda_body_result` contract (the Unknown default tripped the
    hidden-lambda "must end with a value or return" assertion).
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


def test_empty_iife_statement_compiles_and_runs(tmp_path: Path) -> None:
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\t(|| => {})();\n"
		"\treturn 0;\n}\n"
	)
	r = _compile(tmp_path, src, out="empty")
	assert r.returncode == 0, r.stderr
	assert "No MIR lowering" not in r.stderr, r.stderr
	assert subprocess.run([str(tmp_path / "empty")]).returncode == 0


def test_valueless_iife_statement_compiles_and_runs(tmp_path: Path) -> None:
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\t(|| => { val a = 1; })();\n"
		"\treturn 0;\n}\n"
	)
	r = _compile(tmp_path, src, out="valueless")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "valueless")]).returncode == 0


def test_owned_result_iife_statement_discard_runs(tmp_path: Path) -> None:
	# Discarded owned (String) IIFE result: the generic statement tail must
	# emit the DropValue — this exercises the fall-through route end-to-end.
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\t(|| => { val s = \"own\"; s + \"ed\" })();\n"
		"\treturn 0;\n}\n"
	)
	r = _compile(tmp_path, src, out="owned")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "owned")]).returncode == 0


def test_throwing_iife_value_in_try_runs(tmp_path: Path) -> None:
	# Can-throw IIFE as the VALUE operand of a try expression (`val x =
	# try (...)() catch { 7 }`): this is EXPRESSION lowering — the IIFE is
	# not an HExprStmt — and the immediate-call lowering checks and
	# propagates internally (no double-wrapped throw checking on the value
	# route).  The true statement-position twin is pinned separately below.
	src = _PRELUDE + (
		"pub error MyExc { kind: Int }\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval x = try (|| -> Int => { throw MyExc(kind = 1); })() catch { 7 };\n"
		"\treturn x - 7;\n}\n"
	)
	r = _compile(tmp_path, src, out="throwing")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "throwing")]).returncode == 0


def test_throwing_iife_true_statement_position_runs(tmp_path: Path) -> None:
	# TRUE statement position: the throwing IIFE is a discarded HExprStmt
	# inside a throwing fn.  Pins the statement fast-path EXCLUSION in
	# `_visit_stmt_HExprStmt` (lambda callees fall through to the generic
	# expression tail → `_lower_lambda_immediate_call`, which owns the
	# throw check/unwrap); the indirect statement path would ICE on the
	# raw HLambda ("No MIR lowering") and double-wrap throw checking.  On
	# Err, `fire()` propagates and main's try observes the thrown error.
	src = _PRELUDE + (
		"pub error MyExc { kind: Int }\n"
		"fn fire() -> Int {\n"
		"\t(|| -> Int => { throw MyExc(kind = 1); })();\n"
		"\treturn 99;\n}\n"
		"pub fn main() nothrow -> Int {\n"
		"\tval x = try fire() catch { 7 };\n"
		"\treturn x - 7;\n}\n"
	)
	r = _compile(tmp_path, src, out="throwing_stmt")
	assert r.returncode == 0, r.stderr
	assert "No MIR lowering" not in r.stderr, r.stderr
	assert subprocess.run([str(tmp_path / "throwing_stmt")]).returncode == 0
