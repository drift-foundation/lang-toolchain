# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""End-to-end regression: recursive value-type declarations must be rejected
with a clean front-end diagnostic.

Closes `issues/recursive-value-struct-accepted/`. Drift previously accepted
struct declarations whose field-type transitive closure formed a cycle in
which every edge was by-value (no `Arc`/`Array`/`&`/etc. indirection),
producing an uninstantiable type with no diagnostic. The fix is a
kind-based cycle detector that runs after monomorphization on the type
table.

This file pins both the rejected and accepted shapes by running real
Drift sources through the compile pipeline and asserting the diagnostic
shape (or its absence).

Diagnostic contract (per the spec on the issue dir):
- error code: `E_RECURSIVE_VALUE_TYPE`
- the message names the offending struct/variant
- the primary indirection suggestion is `Box<Self>` — the unique-ownership
  value indirection (`core.Box`); `Arc<Self>` is the shared-ownership alternative
- when the offending field is `Optional<Self>`, the suggestion preserves
  the `Optional` wrapper: `Optional<Box<Self>>`, not bare `Box<Self>`
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]


def _compile(tmp_path: Path, source: str) -> subprocess.CompletedProcess:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / "test_bin"
	return subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)


def _has_recursive_value_type_error(stderr: str) -> bool:
	low = stderr.lower()
	return (
		"recursive value type" in low
		or "infinitely recursive" in low
		or "e_recursive_value_type" in low
	)


def _recursive_value_type_diag_count(stderr: str) -> int:
	"""Count the number of recursive-value-type diagnostic lines in stderr.

	The contract is **one diagnostic per cycle**, not per type in the cycle,
	so this should always equal the number of distinct cycles in the input.
	Pinning the count keeps the contract from drifting back to "one per type"
	silently.
	"""
	count = 0
	for line in stderr.splitlines():
		low = line.lower()
		if "recursive value type" in low or "infinitely recursive" in low:
			count += 1
	return count


_DIAG_LINE_RE = None


def _diag_has_real_span(stderr: str) -> bool:
	"""True iff at least one recursive-value-type diagnostic in stderr is
	anchored at a real source location (file:line:col), not the
	`<source>:?:?:` or `<source>:None:None:` sentinel that means "no span".

	Pre-fix shape: every diagnostic was emitted with `span=Span()` and
	rendered as `<source>:None:None:`. Post-fix: the diagnostic is anchored
	at the containing struct/variant declaration loc.
	"""
	import re
	pattern = re.compile(r"<source>:(\d+):(\d+):\s+error:\s+recursive value type")
	# Also accept "infinitely recursive" wording for the single-node case.
	pattern2 = re.compile(r"<source>:(\d+):(\d+):\s+error:.*infinitely recursive")
	for line in stderr.splitlines():
		if pattern.search(line) or pattern2.search(line):
			return True
	return False


# ── Rejected shapes ─────────────────────────────────────────────────


def test_direct_self_reference_struct_rejected(tmp_path: Path) -> None:
	"""`struct Node(child: Node, value: Int)` must be rejected with a clean
	front-end diagnostic, not silently accepted.

	Pins:
	- exactly one recursive-value-type diagnostic (not one per cycle member)
	- the diagnostic has a real source span (`<source>:N:M:`), not the
	  unanchored `<source>:None:None:` sentinel
	"""
	src = (
		"module main;\n"
		"struct Node(child: Node, value: Int);\n"
		"pub fn main() nothrow -> Int { return 0; }\n"
	)
	res = _compile(tmp_path, src)
	assert res.returncode != 0, (
		f"compile must reject infinitely-recursive struct, got rc={res.returncode}\n"
		f"stderr: {res.stderr[-800:]}"
	)
	assert "Traceback" not in res.stderr
	assert _has_recursive_value_type_error(res.stderr), (
		f"expected recursive-value-type diagnostic, got:\n{res.stderr[-800:]}"
	)
	assert "Node" in res.stderr
	# One diagnostic per cycle (not per type).
	assert _recursive_value_type_diag_count(res.stderr) == 1, (
		f"expected exactly 1 diagnostic for self-cycle, got "
		f"{_recursive_value_type_diag_count(res.stderr)}:\n{res.stderr[-800:]}"
	)
	# Must be anchored at a real source span.
	assert _diag_has_real_span(res.stderr), (
		f"diagnostic missing real source span (got <source>:None:None or "
		f"<source>:?:?):\n{res.stderr[-800:]}"
	)


def test_mutually_recursive_value_structs_rejected(tmp_path: Path) -> None:
	"""`struct A(b: B); struct B(a: A)` must be rejected; the diagnostic
	must name both A and B in the cycle.

	Pins one diagnostic for the cycle (not two), and a real source span.
	"""
	src = (
		"module main;\n"
		"struct A(b: B);\n"
		"struct B(a: A);\n"
		"pub fn main() nothrow -> Int { return 0; }\n"
	)
	res = _compile(tmp_path, src)
	assert res.returncode != 0
	assert "Traceback" not in res.stderr
	assert _has_recursive_value_type_error(res.stderr)
	assert "A" in res.stderr and "B" in res.stderr
	assert _recursive_value_type_diag_count(res.stderr) == 1, (
		f"expected exactly 1 diagnostic for the A↔B cycle, got "
		f"{_recursive_value_type_diag_count(res.stderr)}:\n{res.stderr[-800:]}"
	)
	assert _diag_has_real_span(res.stderr)


@pytest.mark.heavy
def test_three_cycle_value_structs_rejected(tmp_path: Path) -> None:
	"""`A → B → C → A` must be rejected.

	Pins one diagnostic for the 3-cycle (not three), and a real source span.
	"""
	src = (
		"module main;\n"
		"struct A(b: B);\n"
		"struct B(c: C);\n"
		"struct C(a: A);\n"
		"pub fn main() nothrow -> Int { return 0; }\n"
	)
	res = _compile(tmp_path, src)
	assert res.returncode != 0
	assert "Traceback" not in res.stderr
	assert _has_recursive_value_type_error(res.stderr)
	assert _recursive_value_type_diag_count(res.stderr) == 1
	assert _diag_has_real_span(res.stderr)


def test_self_recursive_variant_rejected(tmp_path: Path) -> None:
	"""`variant Tree { Leaf, Branch(next: Tree) }` must be rejected — the
	Branch payload embeds Tree by value.

	Pre-fix shape was actually a Python `RecursionError` deep in
	`has_drop`, not silent acceptance — see the 0.27.168 history entry.

	Pins one diagnostic for the self-cycle and a real source span.
	"""
	src = (
		"module main;\n"
		"variant Tree { Leaf, Branch(next: Tree) }\n"
		"pub fn main() nothrow -> Int { return 0; }\n"
	)
	res = _compile(tmp_path, src)
	assert res.returncode != 0
	assert "Traceback" not in res.stderr
	assert _has_recursive_value_type_error(res.stderr)
	assert "Tree" in res.stderr
	assert _recursive_value_type_diag_count(res.stderr) == 1
	assert _diag_has_real_span(res.stderr)


def test_optional_recursive_field_rejected_and_suggests_optional_box(tmp_path: Path) -> None:
	"""`struct Node(next: Optional<Node>, value: Int)` must be rejected,
	and the suggestion must preserve the user's Optional wrapper:
	`Optional<Box<Node>>` rather than bare `Box<Node>`.

	The primary indirection suggestion is `Box<Self>` — the unique-ownership
	value indirection (`core.Box`) — not `Arc<Self>` (shared ownership).

	Pins one diagnostic, the Optional<Box<...>> suggestion, and a real
	source span.
	"""
	src = (
		"module main;\n"
		"struct Node(next: Optional<Node>, value: Int);\n"
		"pub fn main() nothrow -> Int { return 0; }\n"
	)
	res = _compile(tmp_path, src)
	assert res.returncode != 0
	assert "Traceback" not in res.stderr
	assert _has_recursive_value_type_error(res.stderr)
	# Diagnostic suggestion must mention Optional<Box<...>>, preserving
	# the user's Optional wrapper around the Box indirection (Box primary,
	# not Arc).
	assert "Optional<Box<" in res.stderr, (
		f"expected diagnostic to suggest Optional<Box<...>>, got:\n{res.stderr[-1200:]}"
	)
	assert "Optional<Arc<" not in res.stderr, (
		f"suggestion should be Box, not Arc, for value recursion:\n{res.stderr[-1200:]}"
	)
	assert _recursive_value_type_diag_count(res.stderr) == 1
	assert _diag_has_real_span(res.stderr)


# ── Accepted shapes ─────────────────────────────────────────────────


def test_arc_wrapped_recursive_struct_accepted(tmp_path: Path) -> None:
	"""`struct Node(child: Arc<Node>, value: Int)` must be accepted —
	Arc<T> contains a RawPtr<T> via its buf field, the by-value path
	stops at the RAW_PTR indirection.

	Asserts the absence of the recursive-value-type diagnostic. We do
	not assert `rc == 0` here because the surrounding code might need
	additional constructor synthesis support that is unrelated to row
	#13/#14; the contract this test pins is "the cycle detector accepts
	this shape." The companion test below pins `rc == 0` for the Array
	form (which is the simpler indirection).
	"""
	src = (
		"module main;\n"
		"import std.concurrent as conc;\n"
		"struct Node(child: conc.Arc<Node>, value: Int);\n"
		"pub fn main() nothrow -> Int { return 0; }\n"
	)
	res = _compile(tmp_path, src)
	assert "Traceback" not in res.stderr
	assert not _has_recursive_value_type_error(res.stderr), (
		f"unexpected recursive-value-type diagnostic for Arc-wrapped:\n{res.stderr[-1200:]}"
	)


def test_array_wrapped_recursive_struct_accepted(tmp_path: Path) -> None:
	"""`struct Node(children: Array<Node>, value: Int)` must be accepted —
	Array is heap-backed (ARRAY kind, indirected) — and must compile
	cleanly through the full pipeline (`rc == 0`).

	This is the strong accepted-shape contract: not just "no recursive-
	value-type diagnostic" but "actually compiles through the full
	pipeline." If a future change to type checking, codegen, or any
	other phase regresses the accepted Array<Self> path, this catches
	it.
	"""
	src = (
		"module main;\n"
		"struct Node(children: Array<Node>, value: Int);\n"
		"pub fn main() nothrow -> Int { return 0; }\n"
	)
	res = _compile(tmp_path, src)
	assert "Traceback" not in res.stderr
	assert not _has_recursive_value_type_error(res.stderr), (
		f"unexpected recursive-value-type diagnostic for Array-wrapped:\n{res.stderr[-1200:]}"
	)
	assert res.returncode == 0, (
		f"Array<Node> recursive struct must compile cleanly, got rc={res.returncode}\n"
		f"stderr: {res.stderr[-1200:]}"
	)


def test_non_recursive_struct_accepted(tmp_path: Path) -> None:
	"""Sanity: a plain struct with no recursion must compile cleanly and
	produce no recursive-value-type diagnostic."""
	src = (
		"module main;\n"
		"struct Point(x: Int, y: Int);\n"
		"pub fn main() nothrow -> Int { return 0; }\n"
	)
	res = _compile(tmp_path, src)
	assert res.returncode == 0, (
		f"plain non-recursive struct must compile, got rc={res.returncode}\n"
		f"stderr: {res.stderr[-800:]}"
	)
	assert not _has_recursive_value_type_error(res.stderr)


def test_unrelated_struct_in_same_module_does_not_trigger(tmp_path: Path) -> None:
	"""Two unrelated structs in the same module — one recursive, one not.
	The non-recursive one must not be reported."""
	src = (
		"module main;\n"
		"struct Bad(self_ref: Bad);\n"
		"struct Good(x: Int);\n"
		"pub fn main() nothrow -> Int { return 0; }\n"
	)
	res = _compile(tmp_path, src)
	assert res.returncode != 0
	assert "Traceback" not in res.stderr
	assert _has_recursive_value_type_error(res.stderr)
	# The diagnostic must name Bad as the recursive type.
	assert "Bad" in res.stderr
	# Exactly one diagnostic for the Bad cycle; Good is silent.
	assert _recursive_value_type_diag_count(res.stderr) == 1
	assert _diag_has_real_span(res.stderr)
