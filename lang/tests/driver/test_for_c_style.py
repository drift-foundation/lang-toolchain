# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: C-style for loop syntax sugar.

  for (init?; cond?; step?) { body }

Lowers to existing while/break/continue with correct semantics:
- continue runs step before re-checking cond
- break exits immediately, skipping step
- init binding scoped to the for loop
- all three clauses optional
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]


def _compile_and_run(tmp_path: Path, source: str) -> int:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / "test_bin"
	rc = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True,
		# 60s solo would be fine for these small compiles, but under
		# high parallel pytest load CPU contention can push any compile
		# past 60s. sanitizer_timeout(180) gives 3x headroom and absorbs
		# parallel slowdown the same way the row #5 / row #11 driver
		# tests do.
		timeout=sanitizer_timeout(180),
	)
	assert rc.returncode == 0, f"compile failed: {rc.stderr[:400]}"
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=10)
	return run.returncode


def _compile_expect_fail(tmp_path: Path, source: str) -> str:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / "test_bin"
	rc = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(180),
	)
	assert rc.returncode != 0, "expected compile to fail"
	return rc.stderr


def test_all_clauses(tmp_path: Path) -> None:
	"""for (init; cond; step) — sum 0..4 = 10"""
	src = (
		"module main;\n"
		"import std.core as core;\n"
		"pub fn main() nothrow -> Int {\n"
		"\tvar sum = 0;\n"
		"\tfor (var i = 0; i < 5; i = i + 1) { sum = sum + i; }\n"
		"\treturn sum;\n"
		"}\n"
	)
	assert _compile_and_run(tmp_path, src) == 10


def test_missing_init(tmp_path: Path) -> None:
	"""for (; cond; step) — uses outer variable"""
	src = (
		"module main;\n"
		"import std.core as core;\n"
		"pub fn main() nothrow -> Int {\n"
		"\tvar i = 0;\n"
		"\tvar sum = 0;\n"
		"\tfor (; i < 5; i = i + 1) { sum = sum + i; }\n"
		"\treturn sum;\n"
		"}\n"
	)
	assert _compile_and_run(tmp_path, src) == 10


def test_missing_cond(tmp_path: Path) -> None:
	"""for (init; ; step) — infinite loop with break"""
	src = (
		"module main;\n"
		"import std.core as core;\n"
		"pub fn main() nothrow -> Int {\n"
		"\tvar sum = 0;\n"
		"\tfor (var i = 0; ; i = i + 1) {\n"
		"\t\tif i >= 5 { break; }\n"
		"\t\tsum = sum + i;\n"
		"\t}\n"
		"\treturn sum;\n"
		"}\n"
	)
	assert _compile_and_run(tmp_path, src) == 10


def test_missing_step(tmp_path: Path) -> None:
	"""for (init; cond; ) — manual increment in body"""
	src = (
		"module main;\n"
		"import std.core as core;\n"
		"pub fn main() nothrow -> Int {\n"
		"\tvar sum = 0;\n"
		"\tfor (var i = 0; i < 5; ) {\n"
		"\t\tsum = sum + i;\n"
		"\t\ti = i + 1;\n"
		"\t}\n"
		"\treturn sum;\n"
		"}\n"
	)
	assert _compile_and_run(tmp_path, src) == 10


def test_init_scope(tmp_path: Path) -> None:
	"""init binding must not be visible after the loop"""
	src = (
		"module main;\n"
		"import std.core as core;\n"
		"pub fn main() nothrow -> Int {\n"
		"\tfor (var i = 0; i < 3; i = i + 1) { }\n"
		"\treturn i;\n"  # i should not be in scope here
		"}\n"
	)
	stderr = _compile_expect_fail(tmp_path, src)
	# Some "not found" or "undefined" diagnostic for i
	assert "i" in stderr.lower(), f"expected diagnostic about 'i': {stderr[:300]}"


def test_continue_runs_step(tmp_path: Path) -> None:
	"""continue must execute step before re-checking cond.
	Sum even numbers 0..9 — if continue skipped step, this would loop forever."""
	src = (
		"module main;\n"
		"import std.core as core;\n"
		"pub fn main() nothrow -> Int {\n"
		"\tvar sum = 0;\n"
		"\tfor (var i = 0; i < 10; i = i + 1) {\n"
		"\t\tif i % 2 == 1 { continue; }\n"
		"\t\tsum = sum + i;\n"
		"\t}\n"
		"\treturn sum;\n"  # 0+2+4+6+8 = 20
		"}\n"
	)
	assert _compile_and_run(tmp_path, src) == 20


def test_break_skips_step(tmp_path: Path) -> None:
	"""break exits immediately, does not run step.
	Verify by checking that i has the value at break, not after step."""
	src = (
		"module main;\n"
		"import std.core as core;\n"
		"pub fn main() nothrow -> Int {\n"
		"\tvar last = -1;\n"
		"\tfor (var i = 0; i < 100; i = i + 1) {\n"
		"\t\tif i == 7 { last = i; break; }\n"
		"\t}\n"
		"\treturn last;\n"  # 7, not 8
		"}\n"
	)
	assert _compile_and_run(tmp_path, src) == 7


def test_nested_continue_targets_inner(tmp_path: Path) -> None:
	"""continue in inner for must target the inner loop's step."""
	src = (
		"module main;\n"
		"import std.core as core;\n"
		"pub fn main() nothrow -> Int {\n"
		"\tvar sum = 0;\n"
		"\tfor (var i = 0; i < 3; i = i + 1) {\n"
		"\t\tfor (var j = 0; j < 3; j = j + 1) {\n"
		"\t\t\tif j == 1 { continue; }\n"
		"\t\t\tsum = sum + j;\n"
		"\t\t}\n"
		"\t}\n"
		"\treturn sum;\n"  # 3 iterations of (0+2) = 6
		"}\n"
	)
	assert _compile_and_run(tmp_path, src) == 6


def test_nested_break_targets_inner(tmp_path: Path) -> None:
	"""break in inner for must target only the inner loop."""
	src = (
		"module main;\n"
		"import std.core as core;\n"
		"pub fn main() nothrow -> Int {\n"
		"\tvar count = 0;\n"
		"\tfor (var i = 0; i < 3; i = i + 1) {\n"
		"\t\tfor (var j = 0; j < 100; j = j + 1) {\n"
		"\t\t\tif j == 2 { break; }\n"
		"\t\t\tcount = count + 1;\n"
		"\t\t}\n"
		"\t}\n"
		"\treturn count;\n"  # 3 outer * 2 inner = 6
		"}\n"
	)
	assert _compile_and_run(tmp_path, src) == 6
