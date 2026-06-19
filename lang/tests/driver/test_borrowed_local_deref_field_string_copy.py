# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: explicit-deref place borrows preserve value-copy semantics.

Sibling to `test_borrowed_local_field_string_copy.py` (the 0.33.43 index fix).
Here the borrowed local is taken through an EXPLICIT deref place — `&(*p)` and
`&(*p).children[0]` — which normalizes to an `HPlaceExpr` whose projection list
starts with an `HPlaceDeref`. Before the deref arm was added to the shallow place
walker (`Checker._TypingContext._infer_expr_type`), such a borrow degraded to
`Unknown`, so the field read off the borrowed local typed as `Unknown` and the
SECOND string op on the derived local mis-fired

    error: string binary ops require String operands ... [E-AUTO-f6706407]

The deref-place value-copy must behave exactly like the index-place case: the
field read is a value-copy/dup of the String, not a leaked `Ref<String>`.

(Plain value reads `p->text` / `(*p).text` were always fine — they go through the
HUnary(DEREF)+HField path, not the HPlaceExpr place walker — so the gap only
showed through a deref *place* in a borrow/assign/move target.)
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout


ROOT = Path(__file__).resolve().parents[3]


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


# Bare deref reborrow: `&(*p)` -> place [Deref]. p: &Node, so *p: Node, &(*p): &Node.
_DEREF_BARE_REBORROW = """
module m;

struct Node(text: String, children: Array<Node>);

fn leaf(text: String) nothrow -> Node {
	var kids: Array<Node> = [];
	return Node(text, move kids);
}

fn parent() nothrow -> Node {
	var kids: Array<Node> = [];
	kids.push(leaf("head"));
	return Node("parent", move kids);
}

fn check(p: &Node) nothrow -> Int {
	val head = &(*p);
	val name = head.text + "";
	val again = name + "";
	if again.byte_length() != 6 { return 1; }
	return 0;
}

pub fn main() nothrow -> Int {
	val pl = parent();
	return check(&pl);
}
"""


# Deref + field + index: `&(*p).children[0]` -> place [Deref, Field, Index].
_DEREF_FIELD_INDEX = """
module m;

struct Node(text: String, children: Array<Node>);

fn leaf(text: String) nothrow -> Node {
	var kids: Array<Node> = [];
	return Node(text, move kids);
}

fn parent() nothrow -> Node {
	var kids: Array<Node> = [];
	kids.push(leaf("head"));
	return Node("parent", move kids);
}

fn check(p: &Node) nothrow -> Int {
	val head = &(*p).children[0];
	val name = head.text + "";
	val again = name + "";
	if again.byte_length() != 4 { return 1; }
	return 0;
}

pub fn main() nothrow -> Int {
	val pl = parent();
	return check(&pl);
}
"""


# `->` member-through-reference sugar inside a borrowed place chain. `p->children`
# normalizes to `(*p).children`, so `&p->children[0]` is place [Deref, Field, Index].
_ARROW_FIELD_INDEX = """
module m;

struct Node(text: String, children: Array<Node>);

fn leaf(text: String) nothrow -> Node {
	var kids: Array<Node> = [];
	return Node(text, move kids);
}

fn parent() nothrow -> Node {
	var kids: Array<Node> = [];
	kids.push(leaf("head"));
	return Node("parent", move kids);
}

fn check(p: &Node) nothrow -> Int {
	val head = &p->children[0];
	val name = head.text + "";
	val again = name + "";
	if again.byte_length() != 4 { return 1; }
	return 0;
}

pub fn main() nothrow -> Int {
	val pl = parent();
	return check(&pl);
}
"""


def _assert_clean(build, run) -> None:
	assert build.returncode == 0, (
		f"compile failed:\n--- stdout ---\n{build.stdout}\n--- stderr ---\n{build.stderr}"
	)
	assert run is not None
	assert run.returncode == 0, (
		f"program exit {run.returncode}:\n--- stdout ---\n{run.stdout}\n--- stderr ---\n{run.stderr}"
	)


def test_deref_bare_reborrow_field_string_copies(tmp_path: Path) -> None:
	build, run = _compile_and_run(tmp_path, _DEREF_BARE_REBORROW)
	_assert_clean(build, run)


def test_deref_field_index_borrow_field_string_copies(tmp_path: Path) -> None:
	build, run = _compile_and_run(tmp_path, _DEREF_FIELD_INDEX)
	_assert_clean(build, run)


def test_arrow_sugar_deref_field_index_borrow_string_copies(tmp_path: Path) -> None:
	build, run = _compile_and_run(tmp_path, _ARROW_FIELD_INDEX)
	_assert_clean(build, run)
