# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""By-value reads of interface-typed struct fields are rejected (CORE_BUG).

`val cb = h.cb` (an INTERFACE-typed field read from an OWNED subject) used to
compile and then corrupt memory: owned-subject field reads lower as a SEMANTIC
deep copy (struct/array/String recursion in codegen's `_emit_copy_value`), but
INTERFACE has no copy arm — the payload is dynamic (a boxed-callback env, a
flag-2 heap block) with a drop hook and no clone hook. A bare interface field
slipped through as a raw aliased extract → both the holder and the alias
dropped the same env (double-free / UAF / heap corruption; segfault for boxed
callbacks). An interface nested inside a struct/variant/array field ICE'd in
`_emit_copy_value` ("copy not supported for INTERFACE"). Both pre-existing in
certified 0.33.69.

Interfaces were ALREADY non-Copy everywhere else — whole-local copies,
ref-subject field reads, and array-element reads all rejected. The fix closes
the one leaky path with the same contract: the checker's owned-subject HField
gate now rejects when the field type is or transitively contains an interface
value (`_contains_interface_value`, mirroring `_emit_copy_value`'s recursion),
with E_IFACE_FIELD_COPY and guidance. Sound patterns are unchanged: borrow the
field, call through it directly, or move the whole struct.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]


def _stdlib() -> Path:
	return stdlib_root() or (ROOT / "stdlib")


def _compile(tmp_path: Path, source: str, entry: str = "main::main", sanitize: str | None = None) -> subprocess.CompletedProcess[str]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out_bin = tmp_path / "bin"
	cmd = [sys.executable, "-m", "lang.driftc.driftc", "--dev",
	       "--stdlib-root", str(_stdlib())]
	if sanitize:
		cmd.append(f"--sanitize={sanitize}")
	cmd += [str(src), "--entry", entry, "-o", str(out_bin)]
	return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(90))


def _compile_and_run(tmp_path: Path, source: str, sanitize: str | None = None) -> subprocess.CompletedProcess[str]:
	res = _compile(tmp_path, source, sanitize=sanitize)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[-1500:]}"
	return subprocess.run([str(tmp_path / "bin")], capture_output=True, text=True, timeout=sanitizer_timeout(30))


def _error_diags(tmp_path: Path, source: str) -> list[dict]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--stdlib-root",
		 str(_stdlib()), "--test-build-only", str(src), "--json"],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(60),
	)
	payload = json.loads(out.stdout) if out.stdout.strip() else {}
	return [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]


_CB_PRELUDE = """\
module main;
import std.core as core;

struct Holder {
	cb: core.Callback0<String>
}

fn build() -> Holder {
	val note = "note-" + "n";
	return Holder(cb = core.callback0(| | captures(move note) => {
		return note.clone();
	}));
}
"""

_IFACE_PRELUDE = """\
module main;

interface Greeter {
	fn greet(self: &Self) -> String;
}

struct Loud {
	name: String
}

implement Greeter for Loud {
	fn greet(self: &Loud) -> String {
		return self.name.clone() + "!";
	}
}

struct Holder {
	g: Greeter
}

fn build() -> Holder {
	val l = Loud(name = "hi-" + "there");
	return Holder(g = move l);
}
"""


def test_boxed_callback_field_copy_rejected(tmp_path: Path) -> None:
	"""`val cb = h.cb` on a boxed-callback field — was a segfault/double-free
	(the alias and the holder both dropped the same env)."""
	src = _CB_PRELUDE + """
pub fn main() nothrow -> Int {
	val h = build();
	val cb = h.cb;
	val s = cb.call();
	if s == "note-n" { return 0; }
	return 1;
}
"""
	diags = _error_diags(tmp_path, src)
	codes = [d.get("code") for d in diags]
	assert "E_IFACE_FIELD_COPY" in codes, codes


def test_plain_interface_field_copy_rejected(tmp_path: Path) -> None:
	"""`val g = h.g` on a user-interface field — was heap corruption
	(tcache_thread_shutdown abort). Not callback-specific."""
	src = _IFACE_PRELUDE + """
pub fn main() nothrow -> Int {
	val h = build();
	val g = h.g;
	val s = try g.greet() catch { "err" };
	if s == "hi-there!" { return 0; }
	return 1;
}
"""
	diags = _error_diags(tmp_path, src)
	codes = [d.get("code") for d in diags]
	assert "E_IFACE_FIELD_COPY" in codes, codes


def test_struct_containing_interface_field_copy_rejected(tmp_path: Path) -> None:
	"""A struct field that transitively CONTAINS an interface — was a codegen
	ICE (`copy not supported for INTERFACE`); now the same clean rejection."""
	src = _CB_PRELUDE + """
struct Outer {
	h: Holder
}

pub fn main() nothrow -> Int {
	val o = Outer(h = build());
	val h2 = o.h;
	val s = h2.cb.call();
	if s == "note-n" { return 0; }
	return 1;
}
"""
	diags = _error_diags(tmp_path, src)
	codes = [d.get("code") for d in diags]
	assert "E_IFACE_FIELD_COPY" in codes, codes


def test_variant_containing_interface_field_copy_rejected(tmp_path: Path) -> None:
	"""A VARIANT field whose arm payload carries an interface — pins the
	variant arm of `_contains_interface_value`'s recursion. (Uses a local
	interface: a variant payload naming a qualified generic like
	`core.Callback0<String>` trips an unrelated E-TYPE-UNKNOWN wart.)"""
	src = _IFACE_PRELUDE + """
variant MaybeGreeter {
	Has(g: Greeter),
	Nothing
}

struct VHolder {
	v: MaybeGreeter
}

fn build_v() -> VHolder {
	val l = Loud(name = "hi-" + "there");
	return VHolder(v = MaybeGreeter::Has(move l));
}

pub fn main() nothrow -> Int {
	val h = build_v();
	val v2 = h.v;
	return 0;
}
"""
	diags = _error_diags(tmp_path, src)
	codes = [d.get("code") for d in diags]
	assert "E_IFACE_FIELD_COPY" in codes, codes


def test_array_of_interface_field_copy_rejected(tmp_path: Path) -> None:
	"""An `Array<Callback0<…>>` field — pins the array-element arm of
	`_contains_interface_value`'s recursion."""
	src = """\
module main;
import std.core as core;

struct Holder {
	cbs: Array<core.Callback0<String>>
}

fn build() -> Holder {
	var cbs: Array<core.Callback0<String>> = [];
	cbs.push(core.callback0(| | => { return "a"; }));
	return Holder(cbs = move cbs);
}

pub fn main() nothrow -> Int {
	val h = build();
	val a2 = h.cbs;
	return 0;
}
"""
	diags = _error_diags(tmp_path, src)
	codes = [d.get("code") for d in diags]
	assert "E_IFACE_FIELD_COPY" in codes, codes


def test_direct_call_through_field_still_works_asan(tmp_path: Path) -> None:
	"""The sound access pattern: call through the field without extracting it.
	Runs clean under ASAN."""
	src = _CB_PRELUDE + """
pub fn main() nothrow -> Int {
	val h = build();
	val s = h.cb.call();
	if s == "note-n" { return 0; }
	return 1;
}
"""
	run = _compile_and_run(tmp_path, src, sanitize="address,undefined")
	assert run.returncode == 0, f"expected 0, got {run.returncode}: {run.stderr[-600:]}"
	assert "ERROR: AddressSanitizer" not in run.stderr, run.stderr[-600:]


def test_borrowed_field_receiver_still_works(tmp_path: Path) -> None:
	"""Method dispatch through the borrowed field keeps working."""
	src = _IFACE_PRELUDE + """
pub fn main() nothrow -> Int {
	val h = build();
	val s = try h.g.greet() catch { "err" };
	if s == "hi-there!" { return 0; }
	return 1;
}
"""
	run = _compile_and_run(tmp_path, src)
	assert run.returncode == 0, f"expected 0, got {run.returncode}: {run.stderr[-600:]}"


def test_whole_struct_move_still_works(tmp_path: Path) -> None:
	"""Moving the CONTAINING struct transfers the interface soundly."""
	src = _CB_PRELUDE + """
fn consume(h: Holder) -> String {
	return h.cb.call();
}

pub fn main() nothrow -> Int {
	val h = build();
	val s = consume(move h);
	if s == "note-n" { return 0; }
	return 1;
}
"""
	run = _compile_and_run(tmp_path, src)
	assert run.returncode == 0, f"expected 0, got {run.returncode}: {run.stderr[-600:]}"


def test_noncopy_struct_field_without_interface_still_deep_copies(tmp_path: Path) -> None:
	"""Over-rejection control: an owned-subject read of a non-Copy struct
	field WITHOUT interface content keeps its existing deep-copy behavior."""
	src = """\
module main;

struct Inner {
	items: Array<Int>
}

struct Outer {
	inner: Inner
}

fn build() -> Outer {
	var items: Array<Int> = [];
	items.push(41);
	return Outer(inner = Inner(items = move items));
}

pub fn main() nothrow -> Int {
	val o = build();
	val i2 = o.inner;
	if i2.items.len() == 1 { return 0; }
	return 1;
}
"""
	run = _compile_and_run(tmp_path, src)
	assert run.returncode == 0, f"expected 0, got {run.returncode}: {run.stderr[-600:]}"


def test_ref_subject_and_array_elem_rejections_unchanged(tmp_path: Path) -> None:
	"""The neighboring gates that were already correct stay pinned: iface
	field read through `&Holder`, and `Array<Callback0>` element read."""
	src_ref = _CB_PRELUDE + """
fn read_via_ref(h: &Holder) -> Int {
	val cb = h.cb;
	return 0;
}

pub fn main() nothrow -> Int {
	val h = build();
	return read_via_ref(h);
}
"""
	diags = _error_diags(tmp_path, src_ref)
	assert any("cannot copy" in (d.get("message") or "") for d in diags), diags

	src_arr = """\
module main;
import std.core as core;

pub fn main() nothrow -> Int {
	var cbs: Array<core.Callback0<String>> = [];
	cbs.push(core.callback0(| | => { return "a"; }));
	val c = cbs[0];
	return 0;
}
"""
	diags = _error_diags(tmp_path, src_arr)
	assert any("cannot copy" in (d.get("message") or "") for d in diags), diags
