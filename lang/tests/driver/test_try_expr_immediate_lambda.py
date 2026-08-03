# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
v1 classification for "block-form try expression" (bug #2): NOT supported
syntax; use an immediately-invoked lambda.

The native block form

    val x = try { val a = work(); a + 1 } catch { 0 };

is intentionally unsupported in v1 (it would require threading a statement-scope
attempt body through every HIR walker).  Multi-statement throwing work in
expression position is instead written as an immediately-invoked lambda, which
the compiler already supports fully — captures, success value, and caught
failure all work:

    val x = try (|| => { val a = work(); a + 1 })() catch { 0 };

This test pins BOTH facts so neither regresses and future maintainers don't
rediscover the idiom: the immediate-lambda form compiles and runs correctly,
and the native block form is still rejected at parse time.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.driftc.parser import stdlib_root

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]


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


_PRELUDE = (
	"module repro;\n"
	"pub error MyExc { kind: Int }\n"
	"fn risky(n: Int) -> Int { if n > 0 { throw MyExc(kind = n); } return n; }\n"
)


def test_immediate_lambda_value_block_body_runs(tmp_path: Path) -> None:
	# Trailing-value (value-block) lambda body, caught failure: risky(1) throws → 0.
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\tval x = try (|| => { val a = risky(1); a + 1 })() catch { 0 };\n"
		"\treturn x;\n}\n"
	)
	r = _compile(tmp_path, src, out="vb")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "vb")]).returncode == 0


def test_immediate_lambda_plain_value_block_no_try(tmp_path: Path) -> None:
	# 0.34.2: an unannotated value-block IIFE (no try, no callback) infers its
	# return type from the trailing expression — `a + 1` (Int) — matching MIR
	# lowering; the arithmetic use `r - 6` only type-checks if the boundary is
	# Int (a Void boundary would fail E-AUTO), so this pins the CallInfo boundary.
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\tval r = (|| => { val a = 5; a + 1 })();\n"   # a+1 = 6
		"\treturn r - 6;\n}\n"                            # 6 - 6 = 0
	)
	r = _compile(tmp_path, src, out="plain")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "plain")]).returncode == 0


def test_immediate_lambda_return_body_with_capture_runs(tmp_path: Path) -> None:
	# Explicit-return body capturing an outer local; caught failure path.
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\tval base = 10;\n"
		"\tval x = try (|| => { val a = risky(base); return a + base; })() catch { -1 };\n"
		"\treturn x + 1;\n}\n"  # risky(10) throws → x = -1 → 0
	)
	r = _compile(tmp_path, src, out="cap")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "cap")]).returncode == 0


def test_immediate_lambda_success_value_flows(tmp_path: Path) -> None:
	# Success path: no throw, the lambda's value flows out of the try.
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\tval x = try (|| => { val a = risky(0); return a * 2 + 10; })() catch { 0 };\n"
		"\treturn x - 10;\n}\n"  # risky(0)=0 → 0*2+10 = 10 → 0
	)
	r = _compile(tmp_path, src, out="ok")
	assert r.returncode == 0, r.stderr
	assert subprocess.run([str(tmp_path / "ok")]).returncode == 0


def test_native_block_form_try_expr_is_unsupported(tmp_path: Path) -> None:
	# The native block form stays a parse error in v1 (use the immediate lambda).
	src = _PRELUDE + (
		"pub fn main() nothrow -> Int {\n"
		"\tval x = try { val a = risky(1); a + 1 } catch { 0 };\n"
		"\treturn x;\n}\n"
	)
	r = _compile(tmp_path, src, out="block")
	assert r.returncode != 0, "native block-form try expression must remain unsupported in v1"
