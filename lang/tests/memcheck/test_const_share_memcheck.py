# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""`ConstShare` substrate — memcheck carriers for the explicit
`a.const_share()` lifecycle on `core.ConstArc<T>`.

ConstShare's first stdlib impl lives on
`core.ConstArc<T:Frozen>` (see `stdlib/std/core/const_arc.drift`).
The body delegates to `Arc::clone`, so the refcount path is the
existing fat-Arc machinery.  These carriers exercise that path
through the new `const_share()` entry point and pin that:

  - construct + const_share + drop both → no leak (refcount goes
    1 → 2 → 1 → 0 across the lifecycle);
  - chained const_share calls → no leak (each owner releases
    independently);
  - const_share inside a loop → no leak (refcount monotone).

If any test in this file fails, the regression is in either:
  - the `implement<T> shareable.ConstShare for ConstArc<T>` body
    (`stdlib/std/core/const_arc.drift`), or
  - the underlying `Arc::clone` / `Arc::destroy` path
    (`stdlib/std/core/arc.drift` + the compiler intrinsic dispatch
    in `lang/driftc/parser/__init__.py` /
    `lang/driftc/stage2/hir_to_mir.py`).

This file is the memcheck companion to
`lang/tests/driver/test_const_share_substrate.py` (which proves
typing only).  Per the testing-discipline memory:
`feedback_memcheck_in_gate.md`, ConstShare/Arc-substrate work
must include memcheck in the verification gate from the start.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from lang.codegen.llvm.test_utils import valgrind_cmd

import pytest

ROOT = Path(__file__).resolve().parents[3]


CONST_SHARE_INT_LIFECYCLE_SOURCE = """\
module main;

import std.core as core;
import std.core.shareable as shareable;

use trait shareable.ConstShare;

pub fn main() nothrow -> Int {
\tval a = core.const_arc<type Int>(42);
\tval b = a.const_share();
\tval va: Int = *a.get();
\tval vb: Int = *b.get();
\treturn va + vb;
\t// `b` drops first (LIFO) -> count 2 -> 1, no free.
\t// `a` drops next            -> count 1 -> 0, free ArcBox<Int>.
}
"""


CONST_SHARE_STRING_LIFECYCLE_SOURCE = """\
module main;

import std.core as core;
import std.core.shareable as shareable;
import std.format as fmt;

use trait shareable.ConstShare;

pub fn main() nothrow -> Int {
\tval s: String = fmt.format_int(700);
\tval a = core.const_arc<type String>(move s);
\tval b = a.const_share();
\tval ref_a: &String = a.get();
\tval ref_b: &String = b.get();
\treturn ref_a.byte_length() + ref_b.byte_length();
\t// Two ConstArc<String> handles over the same allocation;
\t// each release decrements the strong count by 1.  Last
\t// drop frees ArcBox + the inner String allocation.
}
"""


CONST_SHARE_CHAINED_SOURCE = """\
module main;

import std.core as core;
import std.core.shareable as shareable;
import std.format as fmt;

use trait shareable.ConstShare;

pub fn main() nothrow -> Int {
\tval s: String = fmt.format_int(700);
\tval a = core.const_arc<type String>(move s);
\tval b = a.const_share();
\tval c = b.const_share();
\tval d = c.const_share();
\tval r = d.get();
\treturn r.byte_length();
\t// Four owners over the same allocation (counts 1 -> 4).  All
\t// four drop at scope exit; only the last one's release runs
\t// the structural drop (count 4 -> 3 -> 2 -> 1 -> 0).
}
"""


CONST_SHARE_LOOP_SOURCE = """\
module main;

import std.core as core;
import std.core.shareable as shareable;
import std.format as fmt;

use trait shareable.ConstShare;

pub fn main() nothrow -> Int {
\tval s: String = fmt.format_int(700);
\tval a = core.const_arc<type String>(move s);
\tvar i = 0;
\tvar last = 0;
\twhile i < 16 {
\t\tval c = a.const_share();
\t\tval ref_c: &String = c.get();
\t\tlast = ref_c.byte_length();
\t\ti = i + 1;
\t\t// `c` drops here -> count N+1 -> N (no free until i == 16
\t\t//   AND `a` drops at function exit).
\t}
\treturn last;
\t// `a` drops -> count 1 -> 0 -> free ArcBox + inner String.
}
"""


def _compile_and_valgrind(tmp_path: Path, source: str, *, label: str) -> tuple[int, str, int]:
	"""Compile under raw stdlib and run under valgrind.  Returns
	(definitely_lost_bytes, valgrind_log_text, error_count)."""
	assert shutil.which("valgrind") is not None, "valgrind required"

	src = tmp_path / "main.drift"
	src.write_text(source)
	out_bin = tmp_path / f"bin_{label}"

	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	assert res.returncode == 0, f"[{label}] compile failed: {res.stderr[:1500]}"
	assert out_bin.exists(), f"[{label}] binary not produced"

	vg_log = tmp_path / f"valgrind_{label}.log"
	subprocess.run(
		valgrind_cmd("--leak-check=full",
			"--show-leak-kinds=definite,indirect",
			"--errors-for-leak-kinds=definite,indirect",
			"--error-exitcode=97",
			f"--log-file={vg_log}",
			str(out_bin)),
		capture_output=True, text=True, timeout=120,
	)
	vg_output = vg_log.read_text() if vg_log.exists() else ""
	lost_match = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	definitely_lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0
	err_match = re.search(r"ERROR SUMMARY: (\d+) errors", vg_output)
	error_count = int(err_match.group(1)) if err_match else 0
	return definitely_lost, vg_output, error_count


def _assert_clean(lost: int, vg_log: str, errors: int, *, label: str, hint: str) -> None:
	assert lost == 0, (
		f"[{label}] {lost} bytes definitely lost. {hint}\n"
		f"Valgrind log tail:\n{vg_log[-1500:]}"
	)
	if "Invalid read" in vg_log or "Invalid write" in vg_log or "Invalid free" in vg_log:
		raise AssertionError(
			f"[{label}] valgrind reported invalid memory access. {hint}\n"
			f"Valgrind error count: {errors}\n\n"
			f"Valgrind log tail:\n{vg_log[-2000:]}"
		)


def test_const_share_int_lifecycle_no_leak(tmp_path: Path) -> None:
	lost, vg, errors = _compile_and_valgrind(
		tmp_path, CONST_SHARE_INT_LIFECYCLE_SOURCE, label="cs_int_lifecycle"
	)
	_assert_clean(
		lost, vg, errors,
		label="cs_int_lifecycle",
		hint="`const_share()` on ConstArc<Int> must produce one retain + "
		     "two releases; an imbalance leaks or double-frees the "
		     "ArcBox<Int>.",
	)


def test_const_share_string_lifecycle_no_leak(tmp_path: Path) -> None:
	lost, vg, errors = _compile_and_valgrind(
		tmp_path, CONST_SHARE_STRING_LIFECYCLE_SOURCE, label="cs_string_lifecycle"
	)
	_assert_clean(
		lost, vg, errors,
		label="cs_string_lifecycle",
		hint="heap-bearing payload — last release must run the per-T "
		     "drop thunk and free both ArcBox<String> and the inner "
		     "String allocation.",
	)


def test_const_share_chained_no_leak(tmp_path: Path) -> None:
	lost, vg, errors = _compile_and_valgrind(
		tmp_path, CONST_SHARE_CHAINED_SOURCE, label="cs_chained"
	)
	_assert_clean(
		lost, vg, errors,
		label="cs_chained",
		hint="four chained const_share() calls produce three retains; "
		     "all four owners must release independently at scope exit.",
	)


def test_const_share_loop_no_leak(tmp_path: Path) -> None:
	lost, vg, errors = _compile_and_valgrind(
		tmp_path, CONST_SHARE_LOOP_SOURCE, label="cs_loop"
	)
	_assert_clean(
		lost, vg, errors,
		label="cs_loop",
		hint="N const_share() calls in a loop must be matched by N "
		     "releases (each at iteration-end); the original's drop at "
		     "function exit takes the count to 0.",
	)
