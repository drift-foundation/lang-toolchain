# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Phase 1 ConstShare structural synthesis — driver tests.

Phase 1 scope (per
`work/constshare-substrate/post-link-mandatory-design.md`):
  - structs only (no variants),
  - concrete fields only (no generic typevar fields),
  - same-module nested composition (fixed-point),
  - package serialization included (covered by separate test).

A user struct/variant auto-derives ConstShare iff every owned
field type proves either `ConstShare` (recursive) or `Copy +
Frozen`.  Synthesis registers a real lowering-visible method
body.  No proof-only shortcut.

This file pins the same-module piece.  Cross-module + package
roundtrip lives in `test_const_share_phase1_package.py`.
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
import std.concurrent as conc;
import std.containers as containers;

use trait shareable.ConstShare;

fn assert_cs<T>() nothrow -> Void require T is shareable.ConstShare { }
"""


# ── Positive: same-module synthesis ──────────────────────────────


def test_phase1_struct_with_const_arc_string_field(tmp_path, capsys):
	"""The canonical Phase 1 case: a struct with one
	`core.ConstArc<String>` field auto-derives ConstShare AND
	`holder.const_share()` resolves to the synthesized method."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub struct Holder {
\tpub handle: core.ConstArc<String>
}

pub fn main() nothrow -> Int {
\tval a = core.const_arc<type String>("hello");
\tval h = Holder(handle = a);
\tassert_cs<type Holder>();
\tval h2 = h.const_share();
\treturn 0;
}
""")
	assert rc == 0, f"struct with ConstArc<String> must auto-derive: rc={rc}, errs={errs}"


def test_phase1_struct_mixed_const_arc_string_int(tmp_path, capsys):
	"""Mixed Copy+Frozen + ConstArc fields.  String is Copy+Frozen,
	Int is Copy+Frozen, ConstArc<String> is direct ConstShare."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub struct Mixed {
\tpub handle: core.ConstArc<String>,
\tpub tag: Int,
\tpub label: String
}

pub fn main() nothrow -> Int {
\tval a = core.const_arc<type String>("hi");
\tval m = Mixed(handle = a, tag = 7, label = "x");
\tassert_cs<type Mixed>();
\tval m2 = m.const_share();
\treturn 0;
}
""")
	assert rc == 0, f"Mixed must auto-derive: rc={rc}, errs={errs}"


def test_phase1_nested_same_module(tmp_path, capsys):
	"""Nested same-module composition: `Outer { inner: Inner }`
	where Inner also auto-derives.  Tests that fixed-point
	registration works — Inner must be registered before Outer's
	qualifier sees it."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub struct Inner {
\tpub a: core.ConstArc<Int>
}

pub struct Outer {
\tpub inner: Inner,
\tpub tag: Int
}

pub fn main() nothrow -> Int {
\tval i = Inner(a = core.const_arc<type Int>(7));
\tval o = Outer(inner = i, tag = 1);
\tassert_cs<type Inner>();
\tassert_cs<type Outer>();
\tval o2 = o.const_share();
\treturn 0;
}
""")
	assert rc == 0, f"nested same-module composition must auto-derive: rc={rc}, errs={errs}"


# ── Generic require-clause path ──────────────────────────────────


def test_phase1_generic_dup_with_synthesized_struct(tmp_path, capsys):
	"""Generic function `dup<T>(x: &T) -> T require T is ConstShare`
	must accept a synthesized struct as the type argument.  Pins
	that the synthesized impl is visible through generic dispatch."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub struct Holder {
\tpub handle: core.ConstArc<Int>
}

fn dup<T>(x: &T) nothrow -> T require T is shareable.ConstShare {
\treturn x.const_share();
}

pub fn main() nothrow -> Int {
\tval h = Holder(handle = core.const_arc<type Int>(42));
\tval h2 = dup<type Holder>(h);
\treturn 0;
}
""")
	assert rc == 0, f"generic dup<T:ConstShare> with synthesized struct must compile: rc={rc}, errs={errs}"


# ── Negative: blocking field types ───────────────────────────────


def _assert_blocked(rc: int, errs: list[dict], label: str) -> None:
	assert rc != 0, f"{label}: expected compile failure but compile succeeded"
	# Either E_REQUIREMENT_NOT_SATISFIED on assert_cs OR no matching
	# method on .const_share().
	blocked = any(
		e.get("code") == "E_REQUIREMENT_NOT_SATISFIED"
		or "no matching method 'const_share'" in e.get("message", "")
		or "ConstShare" in e.get("message", "")
		for e in errs
	)
	assert blocked, (
		f"{label}: expected ConstShare-related rejection.  "
		f"Diagnostics:\n"
		+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
	)


def test_phase1_struct_with_arc_field_blocks(tmp_path, capsys):
	"""`core.Arc<T>` is intentionally NOT ConstShare (it's
	mutable-shared via Share, not value-immutable-shared).  A
	struct with an Arc field does NOT auto-derive."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub struct Bad {
\tpub handle: core.Arc<Int>
}

pub fn main() nothrow -> Int {
\tassert_cs<type Bad>();
\treturn 0;
}
""")
	_assert_blocked(rc, errs, "Bad{Arc field}")


def test_phase1_struct_with_mutex_field_blocks(tmp_path, capsys):
	"""Mutex<T> is not ConstShare and not Copy+Frozen.  Blocks."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub struct Bad {
\tpub handle: core.ConstArc<Int>,
\tpub lock: conc.Mutex<Int>
}

pub fn main() nothrow -> Int {
\tassert_cs<type Bad>();
\treturn 0;
}
""")
	_assert_blocked(rc, errs, "Bad{ConstArc + Mutex}")


def test_phase1_struct_with_array_field_blocks(tmp_path, capsys):
	"""Array<T> exposes mutating methods → not ConstShare."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub struct Bad {
\tpub handle: core.ConstArc<Int>,
\tpub items: Array<Int>
}

pub fn main() nothrow -> Int {
\tassert_cs<type Bad>();
\treturn 0;
}
""")
	_assert_blocked(rc, errs, "Bad{ConstArc + Array}")


def test_phase1_struct_with_hashmap_field_blocks(tmp_path, capsys):
	"""HashMap<K,V> exposes mutating methods → not ConstShare."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub struct Bad {
\tpub handle: core.ConstArc<Int>,
\tpub m: HashMap<String, Int>
}

pub fn main() nothrow -> Int {
\tassert_cs<type Bad>();
\treturn 0;
}
""")
	_assert_blocked(rc, errs, "Bad{ConstArc + HashMap}")


def test_phase1_struct_with_immutable_ref_field_blocks(tmp_path, capsys):
	"""`&T` is not ConstShare — referent may be mutable."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub struct Bad {
\tpub handle: core.ConstArc<Int>,
\tpub borrowed: &Int
}

pub fn main() nothrow -> Int {
\tassert_cs<type Bad>();
\treturn 0;
}
""")
	_assert_blocked(rc, errs, "Bad{ConstArc + &Int}")


def test_phase1_struct_with_mutable_ref_field_blocks(tmp_path, capsys):
	"""`&mut T` is even more clearly not ConstShare."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub struct Bad {
\tpub handle: core.ConstArc<Int>,
\tpub borrowed: &mut Int
}

pub fn main() nothrow -> Int {
\tassert_cs<type Bad>();
\treturn 0;
}
""")
	_assert_blocked(rc, errs, "Bad{ConstArc + &mut Int}")


# ── Sealed direct user impl rejection (regression) ───────────────


def test_phase1_direct_user_impl_still_rejected(tmp_path, capsys):
	"""Phase 1 doesn't change the user-impl gate — direct
	`implement ConstShare for X` is rejected even if X qualifies
	for auto-derive.  The synthesizer must NOT fire when a user
	impl is present (would conflict)."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub struct Wrap {
\tpub n: Int
}

implement shareable.ConstShare for Wrap {
\tpub fn const_share(self: &Wrap) nothrow -> Wrap {
\t\treturn Wrap(n = self.n);
\t}
}

pub fn main() nothrow -> Int { return 0; }
""")
	assert rc != 0
	assert any(
		e.get("code") == "E_CONST_SHARE_USER_IMPL_REJECTED"
		for e in errs
	), (
		"user-written `implement ConstShare for Wrap` must remain "
		"rejected with E_CONST_SHARE_USER_IMPL_REJECTED.  "
		f"Diagnostics:\n"
		+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
	)
