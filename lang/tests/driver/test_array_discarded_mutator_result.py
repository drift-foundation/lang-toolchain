# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Value-returning array mutators (`pop`/`remove`/`swap_remove`) must still
mutate the array when their returned value is DISCARDED as a statement
(CORE_BUG).

Previously `a.pop();` in statement position was a silent no-op: the
HIR→MIR statement-discard path called `_lower_array_intrinsic_method(...,
want_value=False)`, and the `pop`/`remove`/`swap_remove` lowerings bailed out
with `if not want_value: return True, None` BEFORE emitting the
`ArraySetLen`/`StoreRef` that actually removes the element.  So the element
was never removed, yet the call compiled and "succeeded" — exactly the
"erroneously accepted as a no-op" defect the app team reported.

The mutation must always happen; only the Optional<T> result production is
skipped when discarded.  The popped/removed element is moved out and must be
DROPPED in the discard path (it is no longer owned by the array), so element
destructors still run — no leak.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]


def _compile_and_run(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out_bin = tmp_path / "bin"
	stdlib = stdlib_root() or (ROOT / "stdlib")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev", "--target-word-bits", "64",
		 "--stdlib-root", str(stdlib), str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(60),
	)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[-1500:]}"
	return subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=sanitizer_timeout(20))


def test_discarded_pop_mutates(tmp_path: Path) -> None:
	"""`a.pop();` with the result discarded still removes the last element."""
	src = """\
module main;
pub fn main() nothrow -> Int {
	var a = [10, 20, 30];
	a.pop();
	return a.len();
}
"""
	assert _compile_and_run(tmp_path, src).returncode == 2


def test_discarded_remove_mutates(tmp_path: Path) -> None:
	"""`a.remove(0);` with the result discarded still removes the element."""
	src = """\
module main;
pub fn main() nothrow -> Int {
	var a = [10, 20, 30];
	a.remove(0);
	return a.len();
}
"""
	assert _compile_and_run(tmp_path, src).returncode == 2


def test_discarded_swap_remove_mutates(tmp_path: Path) -> None:
	"""`a.swap_remove(0);` with the result discarded still removes the element."""
	src = """\
module main;
pub fn main() nothrow -> Int {
	var a = [10, 20, 30];
	a.swap_remove(0);
	return a.len();
}
"""
	assert _compile_and_run(tmp_path, src).returncode == 2


def test_discarded_pop_repeated_drains(tmp_path: Path) -> None:
	"""Repeated discarded pops drain the array to empty."""
	src = """\
module main;
pub fn main() nothrow -> Int {
	var a = [1, 2, 3, 4, 5];
	a.pop();
	a.pop();
	a.pop();
	return a.len();
}
"""
	assert _compile_and_run(tmp_path, src).returncode == 2


def test_discarded_pop_drops_string_element(tmp_path: Path) -> None:
	"""A discarded pop of a refcounted (String) element still runs and the
	program exits cleanly — the moved-out element is dropped, not leaked."""
	src = """\
module main;
pub fn main() nothrow -> Int {
	var a = ["alpha", "beta", "gamma"];
	a.pop();
	return a.len();
}
"""
	assert _compile_and_run(tmp_path, src).returncode == 2
