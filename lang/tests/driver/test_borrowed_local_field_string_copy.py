# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression for borrowed-local field reads preserving value-copy semantics.

App code commonly takes a shared borrow of an array child and then reads a
String field from that borrowed local:

    val head = &pl.children[0];
    val name = head.text + "";

This must behave like the direct projection `pl.children[0].text + ""`: the
field read is a value-copy/dup of the String, not a leaked `Ref<String>` at the
string operator or call boundary.
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


_BORROWED_CHILD_FIELD_STRING = """
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

pub fn main() nothrow -> Int {
	val pl = parent();
	val head = &pl.children[0];
	val name = head.text + "";
	val again = name + "";
	if again.byte_length() != 4 { return 1; }
	return 0;
}
"""


_BORROWED_PARENT_PARAM_CHILD_FIELD_STRING = """
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

fn take_name(name: String) nothrow -> Int {
	return name.byte_length();
}

fn check(pl: &Node) nothrow -> Int {
	val head = &pl.children[0];
	val name = head.text + "";
	val again = name + "";
	val inferred = head.text;
	if again.byte_length() != 4 { return 1; }
	if take_name(head.text) != 4 { return 2; }
	if take_name(inferred) != 4 { return 3; }
	return 0;
}

pub fn main() nothrow -> Int {
	val pl = parent();
	return check(&pl);
}
"""


def test_borrowed_array_child_local_string_field_copies_for_string_ops(tmp_path: Path) -> None:
	build, run = _compile_and_run(tmp_path, _BORROWED_CHILD_FIELD_STRING)
	assert build.returncode == 0, (
		f"compile failed:\n--- stdout ---\n{build.stdout}\n--- stderr ---\n{build.stderr}"
	)
	assert run is not None
	assert run.returncode == 0, (
		f"program exit {run.returncode}:\n"
		f"--- stdout ---\n{run.stdout}\n--- stderr ---\n{run.stderr}"
	)


def test_borrowed_parent_param_child_local_string_field_copies_for_string_slots(tmp_path: Path) -> None:
	build, run = _compile_and_run(tmp_path, _BORROWED_PARENT_PARAM_CHILD_FIELD_STRING)
	assert build.returncode == 0, (
		f"compile failed:\n--- stdout ---\n{build.stdout}\n--- stderr ---\n{build.stderr}"
	)
	assert run is not None
	assert run.returncode == 0, (
		f"program exit {run.returncode}:\n"
		f"--- stdout ---\n{run.stdout}\n--- stderr ---\n{run.stderr}"
	)
