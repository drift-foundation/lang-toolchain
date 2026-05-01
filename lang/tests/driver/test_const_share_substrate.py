# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Phase 1c ConstShare milestone — direct stdlib capability impl.

This file pins the minimum-viable ConstShare layer:

  - **Direct stdlib impl**: `core.ConstArc<T:Frozen>` implements
    `ConstShare`; `a.const_share()` returns a fresh `ConstArc<T>`
    over the same allocation (delegates to `Arc::clone`).
  - **User-impl rejection**: user-written `implement ConstShare for X`
    is rejected outside trusted stdlib with
    `E_CONST_SHARE_USER_IMPL_REJECTED`, mirror of the `Frozen`
    gate.  Spoofed `module std.evil;` files outside `--stdlib-root`
    are rejected via the same path-vetted package check.

**Out of scope this milestone:** structural ConstShare auto-derive
for user struct / variant types.  ConstShare is NOT like Frozen
— Frozen has no methods, so a prover-only structural shortcut is
sound.  ConstShare has

    fn const_share(self: &Self) nothrow -> Self

so allowing a struct to PROVE `ConstShare` while
`holder.const_share()` cannot resolve a method body would be an
incomplete trait state.  The next milestone lands proof AND
method-body synthesis together for auto-derived types; until
then, the only types that prove ConstShare are those with
stdlib-baked direct impls (currently just `core.ConstArc<T:Frozen>`).

**Out of scope (later milestones):** implicit `var b = a`
duplication, owned-param / return synthesis, liveness-gated
HCall insertion.
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

// Test-only witness: requires the type-arg to satisfy `T: ConstShare`.
fn assert_cs<T>() nothrow -> Void require T is shareable.ConstShare { }
"""


# ── Direct stdlib impl on ConstArc ───────────────────────────────


def test_const_arc_int_const_share_call(tmp_path, capsys):
	"""`a.const_share()` on `ConstArc<Int>` returns a fresh
	`ConstArc<Int>` sharing the same allocation.  Body delegates
	to `Arc::clone`."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn main() nothrow -> Int {
\tval a = core.const_arc<type Int>(42);
\tval b = a.const_share();
\treturn *a.get() + *b.get();
}
""")
	assert rc == 0, f"const_share on ConstArc<Int> must compile: rc={rc}, errs={errs}"


def test_const_arc_string_const_share_call(tmp_path, capsys):
	"""Heap-bearing payload (String, Frozen) — exercises
	const_share on a refcounted inner type."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn main() nothrow -> Int {
\tval a = core.const_arc<type String>("hello");
\tval b = a.const_share();
\tval s: &String = b.get();
\treturn s.byte_length();
}
""")
	assert rc == 0, f"const_share on ConstArc<String> must compile: rc={rc}, errs={errs}"


def test_const_arc_proves_const_share_bound(tmp_path, capsys):
	"""The bound `T: ConstShare` is provable for `ConstArc<T:Frozen>`
	via the direct stdlib impl."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn main() nothrow -> Int {
\tassert_cs<type core.ConstArc<Int>>();
\tassert_cs<type core.ConstArc<String>>();
\treturn 0;
}
""")
	assert rc == 0, f"ConstArc must prove ConstShare directly: rc={rc}, errs={errs}"


# ── No user-types-prove-ConstShare in v1 ─────────────────────────


def test_user_struct_does_not_prove_const_share_yet(tmp_path, capsys):
	"""Forward-looking pin: a user struct that holds a
	`core.ConstArc<U>` field would intuitively be a candidate for
	auto-derived ConstShare via composition.  In this milestone,
	however, NO user struct proves ConstShare — auto-derive is
	deferred until method-body synthesis lands together with the
	structural proof rule.  This test pins the current "no user
	composition yet" state; when the next milestone ships, this
	test should be deleted (or flipped to `assert rc == 0`) as
	part of that landing."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub struct Holder {
\tpub handle: core.ConstArc<Int>
}

fn main() nothrow -> Int {
\tassert_cs<type Holder>();
\treturn 0;
}
""")
	assert rc != 0, (
		"user struct must NOT prove ConstShare in v1 — auto-derive "
		"+ method synthesis is deferred to the next milestone.  "
		"If this passes, structural ConstShare proof landed without "
		"the matching method-body synthesis, leaving the trait in "
		"an incomplete state."
	)


# ── Sealed direct-impl rejection ─────────────────────────────────


def test_user_implement_const_share_is_rejected(tmp_path, capsys):
	"""User-written `implement ConstShare for X` must be rejected
	with `E_CONST_SHARE_USER_IMPL_REJECTED`.  Mirrors the
	`Frozen` user-impl gate."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub struct Wrap {
\tpub n: Int
}

implement shareable.ConstShare for Wrap {
\tpub fn const_share(self: &Wrap) nothrow -> Wrap {
\t\treturn Wrap(n = self.n);
\t}
}

fn main() nothrow -> Int {
\treturn 0;
}
""")
	assert rc != 0
	rejected = any(
		e.get("code") == "E_CONST_SHARE_USER_IMPL_REJECTED"
		for e in errs
	)
	assert rejected, (
		"user `implement ConstShare for Wrap` MUST be rejected with "
		"E_CONST_SHARE_USER_IMPL_REJECTED.  Diagnostics:\n"
		+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
	)
