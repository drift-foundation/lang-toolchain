# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression pins: `Arc<T>.get()` where T is (or transitively holds) a
self-referential struct via `Array<Self>` — the only shape recursive
value types take.  `call_resolver._has_owner_typevar` walked the
`param_types` graph with no visited set; that graph is legitimately
CYCLIC for such types, so resolving the generic method crashed with a
raw Python RecursionError (no diagnostic, no source pointer).  Reported
2026-07-10: issues/arc-get-recursive-struct-owner-typevar-recursion.

Pinned end-to-end (compile AND run): the checker walk is only half the
contract — the instantiated `get()` must also lower and execute.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

# T shared one level removed through a non-recursive wrapper.
_HOLDER_SOURCE = """\
module main;

import std.core as core;
import std.concurrent as conc;

struct Node { kind: Int, children: Array<Node> }
struct Holder { root: Node }

fn mknode() nothrow -> Node {
	var kids: Array<Node> = [];
	return Node(kind = 1, children = move kids);
}

pub fn main() nothrow -> Int {
	val h = Holder(root = mknode());
	val a = conc.arc(h);
	val p = a.get();
	if p.root.kind == 1 { return 0; }
	return 1;
}
"""

# Arc wrapping the self-referential struct directly.
_DIRECT_SOURCE = """\
module main;

import std.core as core;
import std.concurrent as conc;

struct Node { kind: Int, children: Array<Node> }

fn mknode() nothrow -> Node {
	var kids: Array<Node> = [];
	return Node(kind = 7, children = move kids);
}

pub fn main() nothrow -> Int {
	val a = conc.arc(mknode());
	val p = a.get();
	if p.kind == 7 { return 0; }
	return 1;
}
"""


def _compile_and_run(tmp_path: Path, source: str) -> None:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / "test_bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(240), env=os.environ.copy(),
	)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[-1800:]}"
	assert "RecursionError" not in res.stderr, res.stderr[-1800:]
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(10))
	assert run.returncode == 0, f"expected exit 0, got {run.returncode}; stderr:\n{run.stderr[-800:]}"


def test_arc_get_recursive_struct_via_wrapper(tmp_path: Path) -> None:
	_compile_and_run(tmp_path, _HOLDER_SOURCE)


def test_arc_get_recursive_struct_direct(tmp_path: Path) -> None:
	_compile_and_run(tmp_path, _DIRECT_SOURCE)
