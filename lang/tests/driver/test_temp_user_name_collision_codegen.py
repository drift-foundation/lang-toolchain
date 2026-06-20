# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: user identifiers that shadow compiler-internal names still compile.

End-to-end guard for the codegen CORE_BUG reported by DriftQuery M3.3:

    NotImplementedError: LLVM codegen v1: phi with mixed incoming types
                         {'ptr', 'drift.int'}

Root cause: `MirBuilder.new_temp()` emitted bare `t<N>` names that collided with
user source variables literally named `t1` / `t2`. When such a user `Int` local
was declared INSIDE a loop (so SSA inserts an entry-default `ZeroValue` that reads
`func.local_types[name]`) and a same-named compiler temp had carried a ref/ptr
type (e.g. the receiver copy of a `&mut` param passed to an early call), the
temp's ptr type clobbered the user local's `Int` type — corrupting codegen at the
SSA join.

The fix mints compiler temporaries with a leading `.` (`new_temp()` -> `.t<N>`),
a character the grammar's identifier rule cannot produce, so no source local can
collide. Critically this must hold for EVERY plausible user-name shape, not just
`t<N>`:

  - `t1`/`t2`  — the original bug.
  - `__t1`/`__t2` — proves the fix is NOT a `__` prefix (which the grammar does
    not reserve; `__t1` is a legal source identifier, so a `__`-prefixed temp
    scheme would merely move the collision here).
  - `_t1`/`_t2` — exercises the codegen alloca-name path: a `.t<N>` temp that is
    addr-taken must not be sanitized back onto `_t<N>` and collide there.

Also guards the sibling `_bb` hardening: a user local named like a block label
(`__bb_entry`) must not collide with the LLVM block label namespace.

The loop/`&mut`-param shape below is the minimal standalone reproduction the
original report could not isolate. Pre-fix the `t1`/`t2` form fails to codegen;
post-fix every shape compiles, runs, and returns the correct value.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout


ROOT = Path(__file__).resolve().parents[3]


# Template for the phi-collision shape; `{a}`/`{b}` are the two user sentinel
# names. Each of the 2 outer iterations adds arr[a] + arr[b] = arr[0] + arr[2]
# = 10 + 30 = 40; over two iterations, 80.
def _collision_source(a: str, b: str) -> str:
	return f"""
module m;

struct Acc(items: Array<Int>);

fn note(a: &mut Acc, x: Int) nothrow -> Void {{ a.items.push(x); }}

fn fold(a: &mut Acc, arr: &Array<Int>) nothrow -> Int {{
	note(a, 1);
	var acc = 0;
	var n = 0;
	while n < 2 {{
		var {a} = -1; var {b} = -1;
		var k = 0;
		while k < arr.len() {{
			if arr[k] > 5 {{ if {a} < 0 {{ {a} = k; }} else {{ {b} = k; }} }}
			k = k + 1;
		}}
		if {a} >= 0 {{ acc = acc + arr[{a}]; }}
		if {b} >= 0 {{ acc = acc + arr[{b}]; }}
		n = n + 1;
	}}
	return acc;
}}

pub fn main() nothrow -> Int {{
	var items0: Array<Int> = [];
	var a = Acc(move items0);
	var arr: Array<Int> = [10, 20, 30];
	return fold(&mut a, &arr);
}}
"""


# A user local named like a compiler block label. The old `__bb_<name>` label
# prefix overlapped the source identifier namespace; the `.bb.` prefix does not.
# i in [0,5): i>2 for i in {3,4} -> +__bb_entry twice; else {0,1,2} -> +__bb_if_then thrice.
# 3*2 + 4*3 = 18.
_BB_LABEL_COLLISION = """
module m;

pub fn main() nothrow -> Int {
	var __bb_entry = 3;
	var __bb_if_then = 4;
	var sum = 0;
	var i = 0;
	while i < 5 {
		if i > 2 { sum = sum + __bb_entry; } else { sum = sum + __bb_if_then; }
		i = i + 1;
	}
	return sum;
}
"""


def _compile_and_run(tmp_path: Path, source: str) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str] | None]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out_bin = tmp_path / "test_bin"
	env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
	env["PYTHONPATH"] = str(ROOT)
	build = subprocess.run(
		[
			sys.executable, "-m", "lang.driftc.driftc",
			"--stdlib-root", str(ROOT / "stdlib"),
			"--target-word-bits", "64",
			str(src),
			"--entry", "m::main",
			"-o", str(out_bin),
		],
		cwd=ROOT,
		capture_output=True,
		text=True,
		timeout=sanitizer_timeout(120),
		env=env,
	)
	if build.returncode != 0:
		return (build, None)
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=sanitizer_timeout(30))
	return (build, run)


def _assert_runs(build, run, expected: int) -> None:
	assert build.returncode == 0, (
		"compile failed (compiler/user name-collision regressed):\n"
		f"--- stdout ---\n{build.stdout}\n--- stderr ---\n{build.stderr}"
	)
	assert run is not None
	assert run.returncode == expected, (
		f"program exit {run.returncode} (expected {expected}):\n"
		f"--- stdout ---\n{run.stdout}\n--- stderr ---\n{run.stderr}"
	)


@pytest.mark.parametrize(
	"a,b",
	[
		("t1", "t2"),     # the original bug shape
		("__t1", "__t2"),  # proves the fix is not a `__` prefix
		("_t1", "_t2"),   # exercises the codegen alloca-name path
	],
)
def test_user_var_named_like_compiler_temp_compiles_and_runs(tmp_path: Path, a: str, b: str) -> None:
	build, run = _compile_and_run(tmp_path, _collision_source(a, b))
	_assert_runs(build, run, 80)


def test_user_var_named_like_block_label_compiles_and_runs(tmp_path: Path) -> None:
	build, run = _compile_and_run(tmp_path, _BB_LABEL_COLLISION)
	_assert_runs(build, run, 18)
