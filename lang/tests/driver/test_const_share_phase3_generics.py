# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""ConstShare structural synthesis — Phase 3 generic structs.

Phase 3 scope (per `work/constshare-substrate/post-link-mandatory-design.md`):
  - Generic struct types with EXPLICIT user require clauses
    that already prove every field qualifies for ConstShare or
    Copy+Frozen.
  - No implicit constraint strengthening — `Box<T> { value: T }`
    (no require) does NOT auto-derive.
  - Synthesized impl carries the declared type_params and the
    same require clause verbatim.

Phase 3 limitations carried forward:
  - Structs only (variants in a later phase).
  - No `var b = a` value-flow synthesis.

This file pins:
  - Positive: `Box<T> require T is ConstShare { value: T }`
    derives, and `Box<core.ConstArc<String>>` instantiations
    work.
  - Positive: `Box<T> require T is Copy, T is Frozen { value: T }`
    derives, and `Box<Int>` works.
  - Negative: `Box<T> { value: T }` (no require) doesn't
    derive.
  - Negative: `Box<T> require T is Frozen { value: T }` alone
    isn't enough.
  - Negative: `Box<T> require T is Copy { value: T }` alone
    isn't enough.
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

use trait shareable.ConstShare;

fn assert_cs<T>() nothrow -> Void require T is shareable.ConstShare { }
"""


# ── Positive — explicit require clauses ──────────────────────────


def test_phase3_box_with_require_const_share_derives(tmp_path, capsys):
	"""`Box<T> require T is ConstShare { value: T }` auto-derives.
	Instantiation `Box<ConstArc<String>>` proves ConstShare AND
	`box.const_share()` resolves."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub struct Box<T> require T is shareable.ConstShare {
\tpub value: T
}

pub fn main() nothrow -> Int {
\tval inner = core.const_arc<type String>("hi");
\tval b = Box<type core.ConstArc<String>>(value = inner);
\tassert_cs<type Box<core.ConstArc<String>>>();
\tval b2 = b.const_share();
\treturn 0;
}
""")
	assert rc == 0, (
		f"Box<T> require T:ConstShare must auto-derive: rc={rc}, errs:\n"
		+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
	)


def test_phase3_box_with_require_copy_frozen_derives(tmp_path, capsys):
	"""`Box<T> require T is Copy, T is Frozen { value: T }`
	auto-derives via the Copy+Frozen field path.  Instantiation
	with `Int` (which is Copy+Frozen) works."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub struct Box<T> require T is core.Copy, T is shareable.Frozen {
\tpub value: T
}

pub fn main() nothrow -> Int {
\tval b = Box<type Int>(value = 42);
\tassert_cs<type Box<Int>>();
\tval b2 = b.const_share();
\treturn 0;
}
""")
	assert rc == 0, (
		f"Box<T> require T:Copy, T:Frozen must auto-derive: rc={rc}, errs:\n"
		+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
	)


# ── Negative — missing or insufficient require clauses ───────────


def _assert_does_not_derive(rc: int, errs: list[dict], label: str) -> None:
	assert rc != 0, f"{label}: must not auto-derive (no implicit strengthening)"
	rejected = any(
		e.get("code") == "E_REQUIREMENT_NOT_SATISFIED"
		and "ConstShare" in e.get("message", "")
		for e in errs
	)
	assert rejected, (
		f"{label}: expected E_REQUIREMENT_NOT_SATISFIED naming "
		f"ConstShare; got:\n"
		+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
	)


def test_phase3_box_without_require_does_not_derive(tmp_path, capsys):
	"""`Box<T> { value: T }` with NO require clause must not
	auto-derive — `T`'s ConstShare-ness is unknown without an
	explicit user opt-in.  Phase 3 does NOT strengthen
	user-declared constraints."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub struct Box<T> {
\tpub value: T
}

pub fn main() nothrow -> Int {
\tassert_cs<type Box<Int>>();
\treturn 0;
}
""")
	_assert_does_not_derive(rc, errs, "Box<T> with no require")


def test_phase3_box_with_require_frozen_only_does_not_derive(tmp_path, capsys):
	"""`Box<T> require T is Frozen { value: T }` alone is
	insufficient — Frozen does not imply ConstShare or
	Copy+Frozen.  No derivation."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub struct Box<T> require T is shareable.Frozen {
\tpub value: T
}

pub fn main() nothrow -> Int {
\tassert_cs<type Box<Int>>();
\treturn 0;
}
""")
	_assert_does_not_derive(rc, errs, "Box<T> require T:Frozen only")


def test_phase3_box_with_require_copy_only_does_not_derive(tmp_path, capsys):
	"""`Box<T> require T is Copy { value: T }` alone is
	insufficient — Copy does not imply Frozen.  No derivation."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub struct Box<T> require T is core.Copy {
\tpub value: T
}

pub fn main() nothrow -> Int {
\tassert_cs<type Box<Int>>();
\treturn 0;
}
""")
	_assert_does_not_derive(rc, errs, "Box<T> require T:Copy only")
