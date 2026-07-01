# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Phase 1b ConstArc milestone — `ConstArc<T: Frozen>` substrate.

Per `work/constshare-substrate/phase1a-dispositions.md` (revised
2026-04-30): `ConstArc<T>` is the first backing primitive of the
ConstShare track.  This file pins:

  - **Construction**: `const_arc(value)` builds a fresh refcounted
    handle.  The bound `T: Frozen` is enforced both at the struct
    declaration (rejects `var x: ConstArc<NonFrozen>`) and at the
    constructor call (rejects `const_arc(non_frozen_value)`).
  - **Retain**: `.clone()` returns a fresh `ConstArc<T>` over the
    same allocation; both handles are usable independently.
  - **Read-only access**: `.get() -> &T` borrows the inner value;
    no `.get_mut`, no `&mut`-receiver method anywhere.
  - **Drop**: scope-exit on a `ConstArc<T>` decrements the strong
    count via the inner `Arc<T>` field's `Destructible::destroy`.
  - **Rejection**: payloads that do NOT implement `Frozen` (Mutex,
    Arc, Array, HashMap, &T, &mut T, structs containing any of
    these) are rejected at compile time with
    `E_REQUIREMENT_NOT_SATISFIED`.

Out of scope this milestone: implicit `var b = a` duplication
(separate review checkpoint), `ConstShare` auto-derive, fat
`ConstArc<Interface>` views.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main


def _compile(tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str) -> tuple[int, list[dict]]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	rc = driftc_main(["--stdlib-root", "stdlib", "--test-build-only", str(src), "--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	errs = [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	return rc, errs


_PRE = """
module main;

import std.core as core;
import std.core.shareable as shareable;
import std.core.const_arc as ca;
import std.containers as containers;
import std.concurrent as conc;
"""


# ── Positive — construction, retain, read-only access ────────────


def test_const_arc_int_construct_clone_get(tmp_path, capsys):
	"""Smallest end-to-end: `Int` is Frozen, so construction +
	clone + get all type-check; no rejection diagnostic fires."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tvar a = ca.const_arc<type Int>(42);
\tvar b = a.clone();
\tval v: Int = *a.get();
\tval w: Int = *b.get();
\treturn v + w;
}
""")
	assert rc == 0, f"const_arc<Int> must compile: rc={rc}, errs={errs}"


def test_const_arc_string_construct(tmp_path, capsys):
	"""`String` is heap-bearing-but-Frozen; refcount handle on a
	heap String is the canonical use case."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tvar a = ca.const_arc<type String>("hello");
\tvar b = a.clone();
\treturn 0;
}
""")
	assert rc == 0, f"const_arc<String> must compile: rc={rc}, errs={errs}"


def test_const_arc_user_struct_all_frozen(tmp_path, capsys):
	"""User struct whose every owned field is Frozen auto-derives
	Frozen via the prover's structural shortcut, so it is a valid
	`ConstArc<T>` payload."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub struct Config {
\tpub name: String,
\tpub port: Int,
\tpub enabled: Bool
}

pub fn main() nothrow -> Int {
\tvar a = ca.const_arc<type Config>(Config(name = "n", port = 1, enabled = true));
\tvar b = a.clone();
\treturn 0;
}
""")
	assert rc == 0, f"const_arc<all-Frozen-fields struct> must compile: rc={rc}, errs={errs}"


# ── Negative — non-Frozen payload rejection (constructor-level) ──


def _assert_rejected_const_arc_ctor(errs: list[dict], payload_label: str) -> None:
	"""Helper: pin the diagnostic shape so a future regression that
	silently downgrades the require enforcement to a warning, or
	moves it to a different code, fails this test."""
	rejected = any(
		e.get("code") == "E_REQUIREMENT_NOT_SATISFIED"
		and "Frozen" in e.get("message", "")
		and "const_arc" in e.get("message", "")
		for e in errs
	)
	assert rejected, (
		f"`const_arc(<{payload_label}>)` MUST be rejected with "
		f"E_REQUIREMENT_NOT_SATISFIED naming Frozen and the "
		f"`const_arc` function.  Diagnostics:\n"
		+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
	)


def test_const_arc_rejects_mutex_payload(tmp_path, capsys):
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tvar m = conc.mutex<type Int>(0);
\tvar x = ca.const_arc<type conc.Mutex<Int>>(move m);
\treturn 0;
}
""")
	assert rc != 0
	_assert_rejected_const_arc_ctor(errs, "Mutex<Int>")


def test_const_arc_rejects_arc_payload(tmp_path, capsys):
	"""`Arc<T>` is intentionally NOT Frozen — its `&T` may itself
	expose mutation through `BorrowMut`.  The composition is what
	keeps Frozen sound."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tvar inner = conc.arc<type Int>(7);
\tvar x = ca.const_arc<type conc.Arc<Int>>(move inner);
\treturn 0;
}
""")
	assert rc != 0
	_assert_rejected_const_arc_ctor(errs, "Arc<Int>")


def test_const_arc_rejects_array_payload(tmp_path, capsys):
	"""`Array<T>` exposes mutating methods through `&mut`, so it is
	not Frozen."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tvar arr: Array<Int> = [1, 2, 3];
\tvar x = ca.const_arc<type Array<Int>>(move arr);
\treturn 0;
}
""")
	assert rc != 0
	_assert_rejected_const_arc_ctor(errs, "Array<Int>")


def test_const_arc_rejects_hashmap_payload(tmp_path, capsys):
	"""`HashMap<K, V>` exposes mutating methods through `&mut`, so it
	is not Frozen — even when both K and V are Frozen."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tvar m = containers.hash_map<type String, Int>();
\tvar x = ca.const_arc(move m);
\treturn 0;
}
""")
	assert rc != 0
	_assert_rejected_const_arc_ctor(errs, "HashMap<String, Int>")


def test_const_arc_rejects_immutable_ref_payload(tmp_path, capsys):
	"""`&T` is intentionally NOT Frozen even though the reference
	value itself is immutable: the *referent* may be mutable, and
	`Frozen` is a soundness boundary about what becomes observable
	through the inner value's `&Self` API.  See `shareable.drift`'s
	notably-absent list."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn make_ref<T>(self: &T) nothrow -> &T require T is shareable.Frozen { return self; }

pub fn main() nothrow -> Int {
\tvar n: Int = 7;
\tval r: &Int = &n;
\tvar x = ca.const_arc<type &Int>(r);
\treturn 0;
}
""")
	assert rc != 0
	_assert_rejected_const_arc_ctor(errs, "&Int")


def test_const_arc_rejects_mutable_ref_payload(tmp_path, capsys):
	"""`&mut T` is even more clearly not Frozen — the referent is
	mutable through this very reference."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tvar n: Int = 7;
\tval r: &mut Int = &mut n;
\tvar x = ca.const_arc<type &mut Int>(r);
\treturn 0;
}
""")
	assert rc != 0
	_assert_rejected_const_arc_ctor(errs, "&mut Int")


def test_const_arc_rejects_user_struct_with_mutex_field(tmp_path, capsys):
	"""A user struct that LOOKS shareable but contains a Mutex
	field must NOT auto-derive Frozen, and therefore must be
	rejected by `const_arc`'s require."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub struct Bad {
\tpub name: String,
\tpub lock: conc.Mutex<Int>
}

pub fn main() nothrow -> Int {
\tval b = Bad(name = "x", lock = conc.mutex<type Int>(0));
\tvar x = ca.const_arc<type Bad>(move b);
\treturn 0;
}
""")
	assert rc != 0
	_assert_rejected_const_arc_ctor(errs, "Bad{Mutex field}")


# ── Negative — non-Frozen payload rejection (struct-level) ───────


def test_const_arc_struct_require_rejects_non_frozen_type_decl(tmp_path, capsys):
	"""The struct itself carries `require T is Frozen`, so even
	*declaring* a `ConstArc<NonFrozen>` (not just constructing one)
	is a use-site of the struct that must trip
	`E_REQUIREMENT_NOT_SATISFIED` naming the struct.  This is what
	prevents user code from minting an uninhabited
	`var x: ConstArc<Mutex<Int>>` and routing around the
	constructor's check."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn dummy() nothrow -> Void {
\tval x: ca.ConstArc<conc.Mutex<Int>> = ca.const_arc<type Int>(0).clone();
\treturn;
}

pub fn main() nothrow -> Int { return 0; }
""")
	assert rc != 0
	rejected = any(
		e.get("code") == "E_REQUIREMENT_NOT_SATISFIED"
		and "Frozen" in e.get("message", "")
		and "ConstArc" in e.get("message", "")
		for e in errs
	)
	assert rejected, (
		"declaring a `var: ConstArc<Mutex<Int>>` MUST be rejected "
		"with E_REQUIREMENT_NOT_SATISFIED naming `ConstArc`.  "
		f"Diagnostics:\n"
		+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
	)


# ── Import boundary — `std.core.const_arc` is self-sufficient ───


def test_const_arc_works_without_explicit_std_core_import(tmp_path, capsys):
	"""`import std.core.const_arc as ca;` alone MUST be sufficient
	to call `ca.const_arc<type Int>(42)` — the user must NOT need a
	separate `import std.core` to bring `Frozen` impls into scope.

	This pins the API boundary of `std.core.const_arc`: it
	re-exports `std.core.shareable.*` (where the primitive Frozen
	impls live), so the visibility BFS at
	`lang/driftc/driftc.py::_collect_reexport_targets` reaches them
	through the const_arc import alone.

	If this test fails, either:
	  - the re-export was removed from `const_arc.drift`, or
	  - primitive Frozen impls were moved out of
	    `shareable.drift` back into a non-re-exported module
	    (e.g. `core.drift`).

	Either regression silently breaks every downstream user who
	imports only `std.core.const_arc`, with a confusing
	`E_REQUIREMENT_NOT_SATISFIED` at the `const_arc(...)` call.
	"""
	# Note: this test does NOT use `_PRE` because `_PRE` itself
	# imports `std.core` — that would defeat the regression.  We
	# write the source from scratch with the minimum imports.
	src = tmp_path / "main.drift"
	src.write_text("""\
module main;

import std.core.const_arc as ca;

pub fn main() nothrow -> Int {
\tvar a = ca.const_arc<type Int>(42);
\tvar b = ca.const_arc<type String>("hi");
\tvar c = a.clone();
\tval v: Int = *a.get();
\treturn v;
}
""", encoding="utf-8")
	rc = driftc_main(["--stdlib-root", "stdlib", "--test-build-only", str(src), "--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	errs = [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	assert rc == 0, (
		"const_arc<Int> + const_arc<String> + .clone() + .get() must "
		"compile with ONLY `import std.core.const_arc` (no separate "
		"`import std.core`).  Diagnostics:\n"
		+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
	)


# ── Canonical user surface — `core.const_arc` via std.core ───────


def test_const_arc_via_root_std_core(tmp_path, capsys):
	"""The canonical user-facing path is `import std.core as core;
	core.const_arc<type T>(...)` — `std.core` re-exports
	`std.core.const_arc.*`, so the submodule's `ConstArc` type and
	`const_arc()` constructor surface directly under the `core.`
	prefix.

	If this fails, the `export { std.core.const_arc.* }` line in
	`stdlib/std/core/core.drift` regressed."""
	src = tmp_path / "main.drift"
	src.write_text("""\
module main;

import std.core as core;

pub fn main() nothrow -> Int {
\tval a = core.const_arc<type Int>(42);
\tval b = a.clone();
\tval h: core.ConstArc<Int> = a.clone();
\tval v: Int = *b.get();
\treturn v + *h.get();
}
""", encoding="utf-8")
	rc = driftc_main(["--stdlib-root", "stdlib", "--test-build-only", str(src), "--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	errs = [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	assert rc == 0, (
		"`core.const_arc<T>(...)` and `core.ConstArc<T>` MUST work "
		"with only `import std.core as core` (no submodule import).  "
		f"Diagnostics:\n"
		+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
	)


# ── Negative — read-only seal (no &mut method) ───────────────────


def test_const_arc_has_no_get_mut(tmp_path, capsys):
	"""ConstArc must NOT expose `get_mut` (or any other
	&mut-receiver method).  This is the immutability seal: if a
	future patch accidentally re-exports `Arc.get_mut` through the
	wrapper, mutation through `&mut ConstArc<T>` would silently
	become possible.  Calling a non-existent method must produce a
	method-resolution error."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tvar a = ca.const_arc<type Int>(42);
\tval r = a.get_mut();
\treturn 0;
}
""")
	assert rc != 0
	no_method = any(
		"no matching method" in e.get("message", "")
		and "get_mut" in e.get("message", "")
		for e in errs
	)
	assert no_method, (
		"ConstArc must not expose `get_mut`.  Diagnostics:\n"
		+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
	)
