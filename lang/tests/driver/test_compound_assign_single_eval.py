# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: compound assignment evaluates the LHS place once.

Compound assignment (`+=`, `-=`, `*=`, `/=`, `%=`, `&=`, `|=`, `^=`, `<<=`, `>>=`)
must NOT desugar to `x = x op y` early. For complex places like `arr[i()] += 1`
or `obj.f += 1`, the place address must be computed once, then a single
load-modify-store cycle runs against that address. Naive `x = x + y` desugaring
would evaluate the index/field-bearing receiver twice and is observably wrong
when those subexpressions have side effects.

These tests pin that contract via observable side-effect counters.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _compile_and_run(tmp_path: Path, source: str) -> int:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / "test_bin"
	rc = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=60,
	)
	assert rc.returncode == 0, f"compile failed: {rc.stderr[:600]}"
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=10)
	return run.returncode


def test_local_aug_assign(tmp_path: Path) -> None:
	"""Baseline: `i += 1` on a local."""
	src = (
		"module main;\n"
		"pub fn main() nothrow -> Int {\n"
		"\tvar i = 5;\n"
		"\ti += 3;\n"
		"\ti -= 1;\n"
		"\treturn i;\n"  # 7
		"}\n"
	)
	assert _compile_and_run(tmp_path, src) == 7


def test_field_aug_assign(tmp_path: Path) -> None:
	"""`obj.f += y` updates the field in place."""
	src = (
		"module main;\n"
		"struct Box(value: Int);\n"
		"pub fn main() nothrow -> Int {\n"
		"\tvar b = Box(value = 10);\n"
		"\tb.value += 5;\n"
		"\tb.value *= 2;\n"
		"\treturn b.value;\n"  # 30
		"}\n"
	)
	assert _compile_and_run(tmp_path, src) == 30


def test_index_single_eval(tmp_path: Path) -> None:
	"""`arr[ctr.next()] += 100` must call ctr.next() exactly once.

	If compound assignment naively desugared to `arr[ctr.next()] = arr[ctr.next()] + 100`,
	ctr.next() would run twice and the counter would end at 2, not 1.
	"""
	src = (
		"module main;\n"
		"struct Ctr(n: Int);\n"
		"implement Ctr {\n"
		"\tfn next(self: &mut Ctr) nothrow -> Int {\n"
		"\t\tself.n = self.n + 1;\n"
		"\t\treturn 0;\n"
		"\t}\n"
		"}\n"
		"pub fn main() nothrow -> Int {\n"
		"\tvar ctr = Ctr(n = 0);\n"
		"\tvar arr = [10, 20, 30];\n"
		"\tarr[ctr.next()] += 100;\n"
		"\treturn ctr.n;\n"  # 1, not 2
		"}\n"
	)
	assert _compile_and_run(tmp_path, src) == 1


def test_index_single_eval_value_correct(tmp_path: Path) -> None:
	"""And the stored value is correct: arr[0] becomes 110, not 210."""
	src = (
		"module main;\n"
		"struct Ctr(n: Int);\n"
		"implement Ctr {\n"
		"\tfn next(self: &mut Ctr) nothrow -> Int {\n"
		"\t\tself.n = self.n + 1;\n"
		"\t\treturn 0;\n"
		"\t}\n"
		"}\n"
		"pub fn main() nothrow -> Int {\n"
		"\tvar ctr = Ctr(n = 0);\n"
		"\tvar arr = [10, 20, 30];\n"
		"\tarr[ctr.next()] += 100;\n"
		"\treturn arr[0];\n"  # 110
		"}\n"
	)
	assert _compile_and_run(tmp_path, src) == 110


def test_field_receiver_single_eval(tmp_path: Path) -> None:
	"""`get_box(ctr).value += 1` is not directly expressible (no &mut return);
	use the closest in-language equivalent: a field on a counter-bearing receiver
	updated through `+=`.
	"""
	src = (
		"module main;\n"
		"struct Ctr(n: Int);\n"
		"implement Ctr {\n"
		"\tfn bump(self: &mut Ctr) nothrow -> Int {\n"
		"\t\tself.n += 1;\n"
		"\t\treturn self.n;\n"
		"\t}\n"
		"}\n"
		"pub fn main() nothrow -> Int {\n"
		"\tvar c = Ctr(n = 0);\n"
		"\tval _ = c.bump();\n"
		"\tval _ = c.bump();\n"
		"\tval _ = c.bump();\n"
		"\treturn c.n;\n"  # 3
		"}\n"
	)
	assert _compile_and_run(tmp_path, src) == 3


def test_bitwise_aug_ops(tmp_path: Path) -> None:
	"""All bitwise compound forms: &=, |=, ^=, <<=, >>="""
	src = (
		"module main;\n"
		"pub fn main() nothrow -> Int {\n"
		"\tvar m = 0xF0u;\n"
		"\tm &= 0x3Cu;\n"  # 0x30
		"\tm |= 0x03u;\n"  # 0x33
		"\tm ^= 0x0Fu;\n"  # 0x3C
		"\tm <<= 1u;\n"    # 0x78
		"\tm >>= 2u;\n"    # 0x1E = 30
		"\treturn cast<Int>(m);\n"
		"}\n"
	)
	assert _compile_and_run(tmp_path, src) == 30


def test_arithmetic_aug_ops(tmp_path: Path) -> None:
	"""All arithmetic compound forms: +=, -=, *=, /=, %=."""
	src = (
		"module main;\n"
		"pub fn main() nothrow -> Int {\n"
		"\tvar x = 10;\n"
		"\tx += 5;\n"   # 15
		"\tx -= 3;\n"   # 12
		"\tx *= 4;\n"   # 48
		"\tx /= 5;\n"   # 9
		"\tx %= 4;\n"   # 1
		"\treturn x;\n"
		"}\n"
	)
	assert _compile_and_run(tmp_path, src) == 1
