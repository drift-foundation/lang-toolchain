# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Slice 5: `Debuggable` trait migrates to JSON-text return.

`Debuggable` stays a separate trait from `Diagnostic` (different
audience — debug/log vs error projection — may diverge on
redaction).  Method renamed `to_debug` → `to_debug_json_text`;
return type changed `DiagnosticValue` → `String`.

Pins:

  1. New trait shape:
     `pub trait Debuggable { fn to_debug_json_text(&Self) nothrow -> String }`
     compiles and is callable.
  2. Stdlib primitive impls (Int, String, …) satisfy the new
     contract.
  3. **Old shape rejected:** user impl returning `DiagnosticValue`
     via `to_debug` is rejected with `E_TO_DEBUG_DEPRECATED`
     (spec §13.2).

**Out of scope:** `std.log` public API surface migration (e.g.,
`HashMap<String, DiagnosticValue>` → `HashMap<String, String>`)
— that's stdlib-internal surgery covered by the implementation
phase, not driver tests.

Spec: `work/exception-diagnostics-context/slice5-spec.md` §8.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main


_SLICE_5_PENDING = pytest.mark.xfail(
	strict=True,
	reason=(
		"Slice 5 (pub error language migration) not yet implemented; "
		"spec locked at work/exception-diagnostics-context/slice5-spec.md"
	),
)


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


def _fails_with_code(rc: int, errs: list[dict], code: str, label: str) -> None:
	codes = [e.get('code') for e in errs]
	assert rc != 0, f"{label}: expected compile failure but rc=0"
	assert code in codes, (
		f"{label}: expected diagnostic {code} not in {codes}\n"
		+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
	)


_PRE = """
module main;

import std.core as core;
import std.log as log;

use trait log.Debuggable;
"""


# ── Probe 1 ─ to_debug_json_text exists + dispatchable ─────────────
#
# Slice 7a (0.31.62, 2026-05-05): flipped live alongside the std.log
# Debuggable migration to `to_debug_json_text -> String`.


def test_debuggable_method_callable_on_primitive(tmp_path, capsys):
	"""`Debuggable.to_debug_json_text` is callable on stdlib
	primitives (Int).  Pins that the migrated trait shape exists
	in std.log."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub fn main() nothrow -> Int {
\tval n: Int = 42;
\tval _s: String = n.to_debug_json_text();
\treturn 0;
}
""")
	_ok(rc, errs, "to_debug_json_text callable on primitive")


# ── Probe 2 ─ user Debuggable impl compiles ───────────────────────
#
# Slice 7a (0.31.62, 2026-05-05): flipped live with the std.log
# Debuggable migration to JSON-text return.  Note: the impl block
# uses `implement log.Debuggable for UserId` (qualified) — bare
# `implement Debuggable for UserId` after `use trait log.Debuggable`
# is not currently supported by the impl-target resolver.  Method
# dispatch (`u.to_debug_json_text()`) still uses the `use trait`.


def test_user_debuggable_impl_compiles(tmp_path, capsys):
	"""User `implement Debuggable for T` with the new method shape
	compiles.  Body returns canonical JSON text via std.core
	helpers."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
struct UserId {
\tvalue: Int,
}

implement log.Debuggable for UserId {
\tpub fn to_debug_json_text(self: &UserId) nothrow -> String {
\t\treturn core.diagnostic_json_int(self.value);
\t}
}

pub fn main() nothrow -> Int {
\tval u: UserId = UserId(value = 7);
\tval _s: String = u.to_debug_json_text();
\treturn 0;
}
""")
	_ok(rc, errs, "user Debuggable impl compiles")


# ── Probe 3 ─ old to_debug shape rejected ──────────────────────────
#
# Slice 7a (0.31.62, 2026-05-05): flipped live with the
# `_reject_deprecated_trait_method_shapes` workspace pre-scan rejection.
# Note: the impl block uses `implement log.Debuggable` (qualified) so
# the trait identity resolves to std.log.Debuggable; bare `implement
# Debuggable` after `use trait log.Debuggable` does not currently
# resolve through the impl-target lookup.  The `to_debug` method body
# returns `Int` rather than `core.DiagnosticValue` to avoid the
# overlapping `E_DV_PUBLIC_REMOVED` rejection — the diagnostic under
# test is keyed on (trait, method name), not return type.


def test_old_to_debug_shape_rejected(tmp_path, capsys):
	"""User impl using the OLD trait method name `to_debug`
	returning `DiagnosticValue` is rejected with
	`E_TO_DEBUG_DEPRECATED` (spec §13.2).  This protects the
	migration completeness — old shape cannot silently coexist."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
struct UserId {
\tvalue: Int,
}

implement log.Debuggable for UserId {
\tpub fn to_debug(self: &UserId) nothrow -> Int {
\t\treturn self.value;
\t}
}

pub fn main() nothrow -> Int {
\treturn 0;
}
""")
	_fails_with_code(rc, errs, 'E_TO_DEBUG_DEPRECATED',
		"old to_debug shape rejected")
