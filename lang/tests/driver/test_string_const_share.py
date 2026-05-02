# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Load-bearing String + ConstShare regressions, pinned ahead of the
Phase 2 throw-side JSON projection (DV→JSON migration).

The throw-time canonical-params builder will project declared
exception fields via `Diagnostic.to_json(self: &Self) -> JsonNode`
into a JSON object, then call `drift_error_set_params_json`.
String-typed fields show up at every throw site; if `String` cannot
flow through the value-flow surface in a ConstShare-equivalent way
(or via Copy, where the substrate is currently anchored), the
projection diverges from the implicit-duplication contract that
covers other carriers.

These tests pin three claims:

  1. `assert_cs<type String>()` — does the String type prove
     `shareable.ConstShare` (directly or via the Copy bridge)?
  2. `val b = a` over a `String` binding — does implicit
     duplication keep both bindings live and value-equal?
  3. A synthesized struct with a String field — does it prove
     ConstShare and duplicate cleanly?

If a probe fails, that surfaces the prerequisite explicitly: either
String is currently Copy-only (no ConstShare proof) — meaning the
Phase 2 throw lowering must work over Copy semantics — or the
String-normalization track must run before Phase 2 can rely on
String going through the ConstShare path.
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


@pytest.mark.xfail(
	strict=True,
	reason=(
		"TRANSITIONAL — direct String:ConstShare is owned by the later "
		"'String normalization' track, NOT by the diagnostics-context "
		"migration.  String is currently treated as Copy+Frozen via "
		"string_arc's refcount-aware lowering, which is sufficient for "
		"every Phase 2/3 throw-projection carrier shape (bare String "
		"flows through Copy; struct/variant carriers prove ConstShare "
		"via Phase 1/4 synthesis).\n\n"
		"BRANCH-COMPLETION GATE (K directive 2026-05-01): this xfail "
		"MUST flip to passing before final merge/release.  No final "
		"merge while String:ConstShare is still transitional.  When the "
		"String normalization track lands direct ConstShare for String, "
		"this xfail flips to passing automatically — at that point "
		"remove the xfail decorator and treat String as a first-class "
		"ConstShare adopter throughout the JSON throw-projection path."
	),
)
def test_string_proves_const_share(tmp_path, capsys):
	"""`assert_cs<type String>()` must compile if String adopts
	`shareable.ConstShare` directly.  Currently expected to FAIL —
	see xfail reason."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn main() nothrow -> Int {
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
fn main() nothrow -> Int {
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
fn main() nothrow -> Int {
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

fn main() nothrow -> Int {
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

fn main() nothrow -> Int {
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

fn main() nothrow -> Int {
\tval a: Carrier = Carrier(name = "Alice");
\tval b = a;
\tval la: Int = a.name.byte_length();
\tval lb: Int = b.name.byte_length();
\treturn la + lb;
}
""")
	_ok(rc, errs, "struct-with-String implicit duplication")
