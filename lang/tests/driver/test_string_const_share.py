# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Load-bearing String + ConstShare regressions.

Pins three claims about `String`'s position in the trait surface:

  1. `assert_cs<type String>()` — `String` proves
     `shareable.ConstShare` directly via the stdlib impl at
     `stdlib/std/core/shareable.drift` (landed at 0.31.53,
     2026-05-03 — closed the diagnostics-context branch-
     completion gate).
  2. `val b = a` over a `String` binding — implicit duplication
     keeps both bindings live and value-equal (Copy semantics
     for static-flagged literals; refcount inc for heap
     strings via `string_arc`).
  3. A synthesized struct with a `String` field — proves
     `ConstShare` via the structural composition rule and
     duplicates cleanly.

`String` is `Copy + Frozen + ConstShare` — all three facts hold
simultaneously.  The trait-method body for `ConstShare for String`
is a thin surface over the existing Copy path
(`return *self;` triggers `M.CopyValue → drift_string_retain`),
so the runtime behavior is identical to plain Copy duplication;
the trait identity is what's new.
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


def _ok(rc: int, errs: list[dict], label: str) -> None:
	assert rc == 0, (
		f"{label}: rc={rc}, errs:\n"
		+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
	)


_PRE = """
module main;

import std.core as core;
import std.core.shareable as shareable;

use trait shareable.ConstShare;
use trait shareable.Frozen;

// Test-only witness: requires the type-arg to satisfy
// `T: shareable.ConstShare`.
fn assert_cs<T>() nothrow -> Void require T is shareable.ConstShare { }

// Test-only witness: requires the type-arg to satisfy
// `T: shareable.Frozen`.
fn assert_frozen<T>() nothrow -> Void require T is shareable.Frozen { }
"""


# ── Probe 1 ─ direct String : ConstShare proof ───────────────────


def test_string_proves_const_share(tmp_path, capsys):
	"""`assert_cs<type String>()` must compile — `String` is a
	first-class `ConstShare` adopter via the direct stdlib impl
	at `stdlib/std/core/shareable.drift`.  This test was the
	branch-completion gate for the diagnostics-context migration;
	flipped from strict-xfail to live when the direct impl
	landed."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tassert_cs<type String>();
\treturn 0;
}
""")
	_ok(rc, errs, "String proves ConstShare")


def test_string_proves_frozen(tmp_path, capsys):
	"""Sanity baseline: `String` proves `Frozen` (longstanding;
	included alongside the ConstShare probe so a regression on
	either is easy to localize)."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tassert_frozen<type String>();
\treturn 0;
}
""")
	_ok(rc, errs, "String proves Frozen")


# ── Probe 2 ─ implicit duplication on a String binding ───────────


def test_string_implicit_duplication_let_binding(tmp_path, capsys):
	"""`val b = a;` over a `String` binding produces two
	independently-usable values — pins the implicit-duplication
	contract for String at value-flow sites."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tval a: String = "hello";
\tval b = a;
\tval la: Int = a.byte_length();
\tval lb: Int = b.byte_length();
\treturn la + lb;
}
""")
	_ok(rc, errs, "String let-binding implicit duplication")


def test_string_implicit_duplication_owned_arg(tmp_path, capsys):
	"""Owned-arg passing of a `String` to two consecutive calls
	must compile; pins the call-boundary implicit-duplication
	surface for the throw-time canonical-params builder."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn use_str(s: String) nothrow -> Int {
\treturn s.byte_length();
}

pub fn main() nothrow -> Int {
\tval a: String = "world";
\tval n1 = use_str(a);
\tval n2 = use_str(a);
\treturn n1 + n2;
}
""")
	_ok(rc, errs, "String owned-arg implicit duplication")


# ── Probe 3 ─ synthesized struct containing String ──────────────


def test_struct_with_string_field_proves_const_share(tmp_path, capsys):
	"""A user struct whose only field is `String` must prove
	`ConstShare` via the Phase 1 synthesis path (all fields Frozen
	+ ConstShare-able)."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
struct Carrier {
\tname: String,
}

pub fn main() nothrow -> Int {
\tassert_cs<type Carrier>();
\treturn 0;
}
""")
	_ok(rc, errs, "struct with String field proves ConstShare")


def test_struct_with_string_field_implicit_duplication(tmp_path, capsys):
	"""`val b = a;` over a synthesized-ConstShare struct that
	carries a String field must compile and produce two
	independently-usable values."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
struct Carrier {
\tname: String,
}

pub fn main() nothrow -> Int {
\tval a: Carrier = Carrier(name = "Alice");
\tval b = a;
\tval la: Int = a.name.byte_length();
\tval lb: Int = b.name.byte_length();
\treturn la + lb;
}
""")
	_ok(rc, errs, "struct-with-String implicit duplication")
