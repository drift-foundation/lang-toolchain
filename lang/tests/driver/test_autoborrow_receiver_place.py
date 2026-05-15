# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import stdlib_root
from lang.codegen.llvm.test_utils import sanitizer_timeout


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	root = stdlib_root()
	args = list(argv)
	if root:
		args += ["--stdlib-root", str(root)]
	args += ["--dev"]
	args += ["--json"]
	rc = driftc_main(args)
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def test_autoborrow_shared_receiver_allows_rvalue_place_chain(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main;

struct Inner { value: Int }

implement Inner {
	pub fn get(self: &Inner) nothrow -> Int { return self.value; }
}

struct Wrap { inner: Inner }

fn make() -> Wrap {
	return Wrap(inner = Inner(value = 1));
}

fn main() nothrow -> Int {
	return make().inner.get();
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc == 0, payload


def test_autoborrow_shared_receiver_allows_ref_returning_rvalue_chain(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Shared receiver chain where the intermediate rvalue is *already
	a `&T`* (not an owned value): `node() -> &Inner` then
	`.get(&self: &Inner)`.  No autoborrow is needed — the
	intermediate ref already matches the method's `&self` — but
	the pre-fix check at type_checker.py:8489-8499 required an
	addressable place and rejected the rvalue ref-returning call
	with "borrow requires an addressable place; bind to a local
	first".

	The sibling test `..._allows_rvalue_place_chain` covers the
	`make() -> Wrap` (owned rvalue) → `.field` (place) → `.get()`
	shape; this test covers the distinct `f() -> &T` (rvalue ref)
	→ `.method()` shape, which surfaced against std.json's
	`payload.node().get_string_at_path(...)` idiom in the
	bookkeeper tree.
	"""
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main;

struct Inner { value: Int }

implement Inner {
	pub fn get(self: &Inner) nothrow -> Int { return self.value; }
}

struct Outer { inner: Inner }

implement Outer {
	pub fn node(self: &Outer) nothrow -> &Inner { return &self.inner; }
}

fn main() nothrow -> Int {
	val o = Outer(inner = Inner(value = 42));
	return o.node().get();
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc == 0, payload


def test_autoborrow_mut_rvalue_chain_terminates_without_resolver_recursion(tmp_path: Path) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main;

struct Builder { x: Int }

implement Builder {
	pub fn step(self: &Builder) nothrow -> Builder {
		return Builder(x = self.x);
	}

	pub fn finish(self: &mut Builder) nothrow -> Int {
		self.x = self.x + 1;
		return self.x;
	}
}

fn make() nothrow -> Builder {
	return Builder(x = 0);
}

fn main() nothrow -> Int {
	val _ = make().step().step().finish();
	return 0;
}
""".lstrip(),
	)
	main_path = mod_root / "main" / "main.drift"
	cmd = [sys.executable, "-m", "lang.driftc", "-M", str(mod_root), str(main_path), "--dev", "--json"]
	root = stdlib_root()
	if root:
		cmd.insert(3, "--stdlib-root")
		cmd.insert(4, str(root))
	try:
		res = subprocess.run(cmd, cwd=Path(__file__).parents[3], capture_output=True, text=True, timeout=sanitizer_timeout(20))
	except subprocess.TimeoutExpired:
		pytest.fail("driftc compile timed out (possible resolver recursion on rvalue mut receiver chain)")
	payload = json.loads(res.stdout) if res.stdout.strip() else {}
	assert res.returncode != 0
	diags = payload.get("diagnostics", [])
	assert any("borrow requires an addressable place" in str(d.get("message", "")) for d in diags)


# ─────────────────────────────────────────────────────────────────────────────
# Regressions for the maria-v1 chained-method-call autoborrow shape.
#
# 0.31.87 fixed the address-of form `&w.get().handle` (stage1 borrow
# materialize split-lift place-chain).  0.31.88 fixes the method-receiver
# autoborrow form `w.get().handle.peek()` and adds a companion soundness
# rejection for value-self consumption through a borrowed projection
# (`w.get().handle.consume()` and the named-intermediate equivalent
# `val r = w.get(); r.handle.consume();`).  See `docs/history.md`
# 2026-05-15 entries for the cumulative picture.
# ─────────────────────────────────────────────────────────────────────────────


_MARIA_HANDLE_PROLOGUE = """
module main;
import std.core as core;

pub struct Handle { pub raw: Int }

implement core.Destructible for Handle {
	pub fn destroy(var self: Handle) nothrow -> Void { return; }
}

implement Handle {
	pub fn peek(self: &Handle) nothrow -> Int { return self.raw; }
	pub fn consume(self: Handle) nothrow -> Int { return self.raw; }
}

pub struct Inner { pub handle: Handle }
pub struct Wrapper { pub inner: Inner }

implement Wrapper {
	pub fn get(self: &Wrapper) nothrow -> &Inner { return &self.inner; }
}
""".lstrip()


def test_autoborrow_shared_receiver_through_ref_rvalue_field_projection(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""maria-v1 Issue 1 — customer-critical regression.

	`w.get().handle.peek()` where get() -> &Inner, peek(self: &Handle),
	Handle is non-Copy. Method-receiver autoborrow must lift the
	ref-returning rvalue base into a hidden temp so the field
	projection is borrowed, not copied.

	Fix: type_checker.py HBorrow rvalue-subject branch types its
	subject with used_as_value=False (was unspecified, defaulting
	True), suppressing the spurious value-copy diagnostic on the
	non-Copy projected field. Stage1 borrow materialize's
	`_split_lift_place_chain` then lifts the rvalue base.
	"""
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		_MARIA_HANDLE_PROLOGUE + """
pub fn main() nothrow -> Int {
	val w = Wrapper(inner = Inner(handle = Handle(raw = 42)));
	return w.get().handle.peek();
}
""",
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc == 0, payload


def test_address_of_field_through_ref_rvalue_still_compiles(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Pin the 0.31.87 partial fix: `&w.get().handle` (address-of form)
	must keep compiling even after the chained-method-call autoborrow
	fix lands."""
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		_MARIA_HANDLE_PROLOGUE + """
pub fn main() nothrow -> Int {
	val w = Wrapper(inner = Inner(handle = Handle(raw = 42)));
	val r = &w.get().handle;
	return r.peek();
}
""",
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc == 0, payload


def test_autoborrow_via_named_ref_intermediate_compiles(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""The lowering-equivalent baseline the chained form must reach:
	bind the &Inner rvalue to a local, then chain `.handle.peek()`.
	This must keep compiling; the fix target is to make the inline
	form lower identically."""
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		_MARIA_HANDLE_PROLOGUE + """
pub fn main() nothrow -> Int {
	val w = Wrapper(inner = Inner(handle = Handle(raw = 42)));
	val r = w.get();
	return r.handle.peek();
}
""",
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc == 0, payload


def test_autoborrow_through_bare_ref_returning_function_chain(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Stack-only variant — proves the fix covers any 'ref-returning
	rvalue base + field projection + &self auto-borrow', not just
	Wrapper/Arc."""
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		_MARIA_HANDLE_PROLOGUE + """
pub fn get_inner_ref(i: &Inner) nothrow -> &Inner { return i; }

pub fn main() nothrow -> Int {
	val inner = Inner(handle = Handle(raw = 42));
	return get_inner_ref(&inner).handle.peek();
}
""",
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc == 0, payload


def test_owned_receiver_through_ref_rvalue_field_projection_rejects_non_copy(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Negative regression: `consume(self: Handle)` called as
	`w.get().handle.consume()` must be rejected for non-Copy Handle.

	The receiver `w.get().handle` is a non-Copy field projection through
	a borrowed `&Inner`.  A value-self call would have to move `Handle`
	out of `w.inner.handle` — a location read through a borrow, which
	cannot be moved from.  Pre-0.31.88 the compiler silently accepted
	this and emitted an implicit move plus drop-flag rescue (sound at
	runtime but left `w` partially-moved without any user signal).
	0.31.88 surfaces this as `E_CONSUME_THROUGH_BORROWED_PROJECTION` at
	the method-call dispatch site.
	"""
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		_MARIA_HANDLE_PROLOGUE + """
pub fn main() nothrow -> Int {
	val w = Wrapper(inner = Inner(handle = Handle(raw = 42)));
	return w.get().handle.consume();
}
""",
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc != 0
	diags = payload.get("diagnostics", [])
	assert any(
		d.get("code") == "E_CONSUME_THROUGH_BORROWED_PROJECTION"
		or "consume non-Copy value" in str(d.get("message", ""))
		for d in diags
	), payload


def test_owned_receiver_through_named_ref_intermediate_rejects_non_copy(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Negative regression: the named-intermediate lowering-equivalent of
	`w.get().handle.consume()` must ALSO reject.  The guard predicate
	cannot key on "ultimate base is an rvalue call" because

	    val r = w.get();      // r: &Inner (HVar typed as &Inner)
	    r.handle.consume();   // HField(HVar(r), "handle").consume()

	is the same ownership violation as the inline form — `Handle` would
	have to move out of `*r` which is a borrow.  Pre-fix this rejected
	the inline form (`_ultimate_base_is_rvalue_call` keyed on the
	HMethodCall base) but silently accepted the named form (HVar base).
	The 0.31.88 guard keys on `_expr_reads_through_ref_projection` —
	walks the projection chain checking whether any subject types as
	`&T` — so both shapes reject identically.
	"""
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		_MARIA_HANDLE_PROLOGUE + """
pub fn main() nothrow -> Int {
	val w = Wrapper(inner = Inner(handle = Handle(raw = 42)));
	val r = w.get();
	return r.handle.consume();
}
""",
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc != 0
	diags = payload.get("diagnostics", [])
	assert any(
		d.get("code") == "E_CONSUME_THROUGH_BORROWED_PROJECTION"
		for d in diags
	), payload


def test_method_receiver_autoborrow_through_ref_rvalue_hindex_rejects(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Negative regression: `w.handles_ref()[i].peek()` where
	`handles_ref(self: &Wrapper) -> &Array<Handle>` and
	`peek(self: &Handle)` must reject at type-check.

	MIR lowering for borrowed-array-element-through-rvalue-base is not
	supported in v1 (`_visit_expr_HIndex` raises
	NotImplementedError("array index read requires Copy element type;
	borrow not supported in v1")).  The 0.31.88 widened
	`_ultimate_base_is_rvalue_call` predicate intentionally walks only
	HField chains, NOT HIndex — admitting the HIndex case at type-check
	would push the error from a clean type-check diagnostic to an
	ICE-shaped NotImplementedError.  Workaround: bind the array
	reference to a local first or use the explicit-borrow form
	`&w.handles_ref()[i]` (sibling e2e
	`borrow_chained_ref_projection_noncopy` pins that).

	When MIR support lands, widen `_ultimate_base_is_rvalue_call` to
	walk HIndex too and flip this test to accept.
	"""
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main;
import std.core as core;

pub struct Handle { pub raw: Int }
implement core.Destructible for Handle {
	pub fn destroy(var self: Handle) nothrow -> Void { return; }
}
implement Handle {
	pub fn peek(self: &Handle) nothrow -> Int { return self.raw; }
}

pub struct Wrapper { pub handles: Array<Handle> }
implement core.Destructible for Wrapper {
	pub fn destroy(var self: Wrapper) nothrow -> Void { return; }
}
implement Wrapper {
	pub fn handles_ref(self: &Wrapper) nothrow -> &Array<Handle> { return &self.handles; }
}

pub fn main() nothrow -> Int {
	var handles: Array<Handle> = [];
	handles.push(Handle(raw = 10));
	val w = Wrapper(handles = move handles);
	return w.handles_ref()[0].peek();
}
""",
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc != 0
	diags = payload.get("diagnostics", [])
	# The current diagnostic is the pre-existing "cannot copy" from the
	# index-element gate; what matters here is that we get a real type-
	# check diagnostic, NOT a downstream NotImplementedError.
	assert any(
		"cannot copy value of type 'Handle'" in str(d.get("message", ""))
		for d in diags
	), payload