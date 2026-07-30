# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""`&T → T` auto-dup coercion at constructor-arg and field-assign slots.

Two LANGUAGE_BUGs reported by the drift-web app team after the
0.31.75 review of #3/#4 (extended-slots coverage):

**Bug A — Variant constructor positional arg (silent miscompile).**
`Variant::Case(ref_string)` where the payload field is `String` and
the argument is `&String` was accepted by the type-checker without
inserting a HIR-visible `&T → T` conversion.  HIR→MIR lowering then
emitted `load %DriftString, ptr %s` *without* `drift_string_retain`,
so the variant payload aliased the caller's buffer.  Each subsequent
scope-exit release decremented the same refcount → eventual heap
corruption (`malloc(): unaligned tcache chunk detected`).  No
compile-time signal.  HIGH SEVERITY.

**Bug B — Field assignment RHS (codegen ICE).**
`obj.field = ref_string` where the target field is `String` and the
RHS is `&String` was accepted by the type-checker; codegen ICE'd at
`llvm_codegen.py:_lower_instr` with `StoreRef value type mismatch
(have ptr, expected %DriftString)`.  Same root cause: no HIR
deref insertion before lowering.

Both fixes route through the same `_ref_to_value_coerce_applies` +
`_rewrite_ref_to_value` helpers introduced in 0.31.75 for the
let-init / return / binop slot family.  This file pins both
behaviors with compile-and-run + repeated-use checks to catch the
aliasing on Bug A (single-use runs sometimes appear correct because
the heap allocator hasn't reclaimed the freed buffer yet).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]


def _compile_and_run(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out_bin = tmp_path / "bin"
	stdlib = stdlib_root() or (ROOT / "stdlib")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(stdlib),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(60),
	)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[:1500]}"
	return subprocess.run(
		[str(out_bin)], capture_output=True, text=True, timeout=sanitizer_timeout(20),
	)


# ---------------------------------------------------------------------------
# Bug A: variant constructor positional arg (heap corruption pre-fix).
# Runs the ctor in a loop with a heap-allocated source string + per-iteration
# payload access so an aliased buffer manifests as wrong byte_length or
# crashes the allocator.  10 × byte_length("ok") = 20 → exit 20.
# ---------------------------------------------------------------------------


def test_variant_ctor_ref_string_payload_no_aliasing(tmp_path: Path) -> None:
	"""`Node::Tagged(s)` where `s: &String` and the payload field is
	`String`.  Each iteration constructs a fresh variant, reads
	`byte_length()` from the payload, accumulates the sum.  Pre-fix:
	the payload aliases `src`'s buffer; releases on variant drop
	decrement the same refcount; allocator eventually faults.  Post-
	fix: each ctor inserts a deref + Copy (refcount inc) so the
	payload owns its own share.
	"""
	run = _compile_and_run(
		tmp_path,
		"""
module main;

import std.core as core;
import std.format as fmt;

pub variant Node {
	Tagged(s: String),
	Empty
}

fn wrap(s: &String) nothrow -> Node {
	return Node::Tagged(s);
}

pub fn main() nothrow -> Int {
	val src: String = fmt.format_int(7700);
	var sum: Int = 0;
	var i: Int = 0;
	while i < 10 {
		val n = wrap(src);
		match n {
			Node::Tagged(t) => { sum = sum + t.byte_length(); },
			Node::Empty => { sum = sum + 999; }
		}
		i = i + 1;
	}
	return sum;
}
""".lstrip(),
	)
	assert run.returncode == 40, (
		f"variant ctor &String payload aliased the source buffer; "
		f"got exit={run.returncode}, expected 40 (10 iters × 4 bytes of "
		f"\"7700\").  stderr: {run.stderr[:300]}"
	)


def test_variant_ctor_ref_string_payload_survives_caller_drop(tmp_path: Path) -> None:
	"""Stronger aliasing probe: caller's `src` goes out of scope
	before the variant payload is read.  Pre-fix the payload's
	buffer was the caller's freed buffer → use-after-free.
	Post-fix the payload owns a retained share that outlives the
	caller's `src`.
	"""
	run = _compile_and_run(
		tmp_path,
		"""
module main;

import std.format as fmt;

pub variant Node {
	Tagged(s: String),
	Empty
}

fn produce() nothrow -> Node {
	val src: String = fmt.format_int(42);
	return Node::Tagged(&src);
}

pub fn main() nothrow -> Int {
	val n = produce();
	match n {
		Node::Tagged(t) => { return t.byte_length(); },
		Node::Empty => { return 999; }
	}
}
""".lstrip(),
	)
	assert run.returncode == 2, (
		f"variant payload did not survive caller-scope drop; "
		f"got exit={run.returncode}, expected 2 (len \"42\").  "
		f"stderr: {run.stderr[:300]}"
	)


# ---------------------------------------------------------------------------
# Bug B: field assignment RHS (codegen ICE pre-fix).
# obj.field = &String where field is String must coerce.
# ---------------------------------------------------------------------------


def test_field_assign_ref_string_to_string_field(tmp_path: Path) -> None:
	"""`e.status = ref_string` where `Entry.status: String` and
	the RHS is `&String`.  Pre-fix: codegen ICE'd with `StoreRef
	value type mismatch (have ptr, expected %DriftString)`.  Post-
	fix: deref-and-Copy at the assignment slot; field stores a
	properly retained String.
	"""
	run = _compile_and_run(
		tmp_path,
		"""
module main;

pub struct Entry { pub status: String }

fn set_status(e: &mut Entry, s: &String) nothrow -> Void {
	e.status = s;
	return;
}

pub fn main() nothrow -> Int {
	var e = Entry(status = "init");
	val s: String = "FINISHED";
	set_status(e, s);
	return e.status.byte_length();
}
""".lstrip(),
	)
	assert run.returncode == 8, (
		f"field assign &String → String did not produce the right "
		f"value; got exit={run.returncode}, expected 8 (len "
		f"\"FINISHED\").  stderr: {run.stderr[:300]}"
	)


def test_field_assign_ref_int_to_int_field(tmp_path: Path) -> None:
	"""Plain-Copy scalar path through field assignment."""
	run = _compile_and_run(
		tmp_path,
		"""
module main;

pub struct Cell { pub value: Int }

fn set_value(c: &mut Cell, n: &Int) nothrow -> Void {
	c.value = n;
	return;
}

pub fn main() nothrow -> Int {
	var c = Cell(value = 0);
	val v: Int = 42;
	set_value(c, v);
	return c.value;
}
""".lstrip(),
	)
	assert run.returncode == 42


# ---------------------------------------------------------------------------
# Negative: Destructible (non-Copy non-ConstShare) at the new slots
# must still reject.
# ---------------------------------------------------------------------------


def test_variant_ctor_negative_destructible_still_rejected(tmp_path: Path) -> None:
	"""Variant payload of a `Destructible` (non-Copy non-ConstShare)
	type must not silently auto-dup from `&T`.
	"""
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module main;

import std.core as core;

pub struct Resource { pub tag: Int }

implement core.Destructible for Resource {
	pub fn destroy(var self: Resource) nothrow -> Void { return; }
}

pub variant Holder {
	Just(r: Resource),
	None
}

fn wrap(r: &Resource) nothrow -> Holder {
	return Holder::Just(r);
}

pub fn main() nothrow -> Int {
	return 0;
}
""".lstrip(),
		encoding="utf-8",
	)
	stdlib = stdlib_root() or (ROOT / "stdlib")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(stdlib),
		 str(src), "--entry", "main::main"],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(60),
	)
	assert res.returncode != 0, (
		f"Resource (non-Copy non-ConstShare) must not auto-dup at "
		f"variant ctor payload; compile unexpectedly succeeded.\n"
		f"stderr: {res.stderr[:500]}"
	)


def test_struct_ctor_positional_ref_string_payload(tmp_path: Path) -> None:
	"""Struct ctor with positional `&String` arg into a `String`
	field.  Symmetric with the variant-ctor case — same code path
	in `resolve_struct_ctor`.
	"""
	run = _compile_and_run(
		tmp_path,
		"""
module main;

pub struct Box { pub tag: String }

fn wrap(s: &String) nothrow -> Box {
	return Box(s);
}

pub fn main() nothrow -> Int {
	val src: String = "ok";
	val b = wrap(src);
	return b.tag.byte_length();
}
""".lstrip(),
	)
	assert run.returncode == 2


def test_struct_ctor_named_ref_string_payload(tmp_path: Path) -> None:
	"""Struct ctor with named-arg `&String` into a `String` field.
	Same coercion as positional.
	"""
	run = _compile_and_run(
		tmp_path,
		"""
module main;

pub struct Box { pub tag: String }

fn wrap(s: &String) nothrow -> Box {
	return Box(tag = s);
}

pub fn main() nothrow -> Int {
	val src: String = "ok";
	val b = wrap(src);
	return b.tag.byte_length();
}
""".lstrip(),
	)
	assert run.returncode == 2


def test_local_var_assign_ref_string(tmp_path: Path) -> None:
	"""Local-variable assignment RHS (`x = ref_string` where `x:
	String`).  Same HAssign path as field assignment with the
	target being a bare HVar.
	"""
	run = _compile_and_run(
		tmp_path,
		"""
module main;

fn update(s: &String) nothrow -> Int {
	var x: String = "initial";
	x = s;
	return x.byte_length();
}

pub fn main() nothrow -> Int {
	val src: String = "after";
	return update(src);
}
""".lstrip(),
	)
	assert run.returncode == 5


def test_indexed_assign_ref_string_element(tmp_path: Path) -> None:
	"""Indexed assignment RHS (`arr[i] = ref_string` where `arr:
	Array<String>`).  Same HAssign path as field/local assignment
	with the target being an HPlaceExpr with an index projection.
	"""
	run = _compile_and_run(
		tmp_path,
		"""
module main;

fn update_at(arr: &mut Array<String>, s: &String) nothrow -> Void {
	arr[0] = s;
	return;
}

pub fn main() nothrow -> Int {
	var arr: Array<String> = ["a", "b", "c"];
	val src: String = "REPLACED";
	update_at(arr, src);
	return arr[0].byte_length();
}
""".lstrip(),
	)
	assert run.returncode == 8


def test_struct_ctor_positional_negative_destructible_still_rejected(tmp_path: Path) -> None:
	"""Struct ctor with a positional `&Resource` arg where
	`Resource` has a user `Destructible` impl (non-Copy, non-
	ConstShare).  Pinned independently from the variant-ctor
	negative case because the struct-ctor coercion is wired at a
	separate site in `call_resolver.py` (positional and named arg
	loops).
	"""
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module main;

import std.core as core;

pub struct Resource { pub tag: Int }

implement core.Destructible for Resource {
	pub fn destroy(var self: Resource) nothrow -> Void { return; }
}

pub struct Holder { pub item: Resource }

fn wrap(r: &Resource) nothrow -> Holder {
	return Holder(r);
}

pub fn main() nothrow -> Int {
	return 0;
}
""".lstrip(),
		encoding="utf-8",
	)
	stdlib = stdlib_root() or (ROOT / "stdlib")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(stdlib),
		 str(src), "--entry", "main::main"],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(60),
	)
	assert res.returncode != 0, (
		f"Resource (non-Copy non-ConstShare) must not auto-dup at "
		f"struct ctor positional slot; compile unexpectedly succeeded.\n"
		f"stderr: {res.stderr[:500]}"
	)


def test_struct_ctor_named_negative_destructible_still_rejected(tmp_path: Path) -> None:
	"""Named-arg sibling of the positional struct-ctor negative
	case — covers the second wiring site in `call_resolver.py`
	(named-arg branch of `resolve_struct_ctor`).
	"""
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module main;

import std.core as core;

pub struct Resource { pub tag: Int }

implement core.Destructible for Resource {
	pub fn destroy(var self: Resource) nothrow -> Void { return; }
}

pub struct Holder { pub item: Resource }

fn wrap(r: &Resource) nothrow -> Holder {
	return Holder(item = r);
}

pub fn main() nothrow -> Int {
	return 0;
}
""".lstrip(),
		encoding="utf-8",
	)
	stdlib = stdlib_root() or (ROOT / "stdlib")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(stdlib),
		 str(src), "--entry", "main::main"],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(60),
	)
	assert res.returncode != 0, (
		f"Resource (non-Copy non-ConstShare) must not auto-dup at "
		f"struct ctor named slot; compile unexpectedly succeeded.\n"
		f"stderr: {res.stderr[:500]}"
	)


def test_field_assign_negative_destructible_still_rejected(tmp_path: Path) -> None:
	"""Field assignment of `&Resource` into a `Resource` field
	(non-Copy non-ConstShare) must reject — symmetric with the
	let-init negative case in test_ref_to_value_extended_slots.py.
	"""
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module main;

import std.core as core;

pub struct Resource { pub tag: Int }

implement core.Destructible for Resource {
	pub fn destroy(var self: Resource) nothrow -> Void { return; }
}

pub struct Holder { pub item: Resource }

fn set_item(h: &mut Holder, r: &Resource) nothrow -> Void {
	h.item = r;
	return;
}

pub fn main() nothrow -> Int {
	return 0;
}
""".lstrip(),
		encoding="utf-8",
	)
	stdlib = stdlib_root() or (ROOT / "stdlib")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(stdlib),
		 str(src), "--entry", "main::main"],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(60),
	)
	assert res.returncode != 0, (
		f"Resource (non-Copy non-ConstShare) must not auto-dup at "
		f"field assignment RHS; compile unexpectedly succeeded.\n"
		f"stderr: {res.stderr[:500]}"
	)
