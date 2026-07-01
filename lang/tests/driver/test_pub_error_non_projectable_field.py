# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Slice 5: non-projectable field rejection on `pub error` types.

Pins the synthesis-fails-closed contract — when a `pub error` declares
a field whose type is not Diagnostic-projectable AND no manual
`Diagnostic` impl is provided, the compiler rejects the declaration
at the field site with `E_PUB_ERROR_FIELD_NOT_PROJECTABLE`.

Projectability rule (K, 2026-05-04):

  * Scalars are projectable: `Int`, `Uint`, `Bool`, `Float`, `String`,
    `DiagnosticValue`.
  * `pub error` fields are projectable through synthesized / manual
    `Diagnostic`.
  * Ordinary `pub struct` / `pub variant` fields are projectable ONLY
    when they have an explicit `implement core.Diagnostic for T` —
    no automatic structural dump.
  * Collections (`Optional<U>`, `Array<U>`, `Map<K, V>`) are NEVER
    projectable automatically — even when `U` / `V` is projectable.
    User wraps them behind a carrier with a manual Diagnostic impl.
  * `RawPtr<T>`, `Ptr<T>`, `TypeBox`, function/lambda/callback types
    are not projectable.

Probes:

  1. `RawPtr<T>` field rejected.
  2. `Array<U>` field rejected (no auto collection projection).
  3. `Optional<U>` field rejected.
  4. `Map<String, V>` field rejected.
  5. Plain `pub struct` field without Diagnostic impl rejected.
  6. Plain `pub struct` field WITH explicit Diagnostic impl accepted.
  7. Manual `Diagnostic for E` impl unblocks an otherwise-non-projectable
     field type — declaration compiles when the user takes
     responsibility for the projection.

Spec: `work/exception-diagnostics-context/slice5-spec.md` §7.2-§7.4.
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

use trait core.Diagnostic;
"""


# ── Probe 1 ─ RawPtr field rejected ────────────────────────────────


def test_rawptr_field_rejected(tmp_path, capsys):
	"""`pub error E { p: RawPtr<Byte> }` is rejected at the
	declaration site with `E_PUB_ERROR_FIELD_NOT_PROJECTABLE`.
	Pointer types are not auto-projectable."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error PtrError {
\tp: RawPtr<Byte>,
}

pub fn main() nothrow -> Int {
\treturn 0;
}
""")
	_fails_with_code(rc, errs, 'E_PUB_ERROR_FIELD_NOT_PROJECTABLE',
		"RawPtr field rejected")


# ── Probe 2 ─ Array<U> field rejected ─────────────────────────────


def test_array_field_rejected(tmp_path, capsys):
	"""`Array<U>` field is NOT auto-projectable, even when `U` is
	projectable.  No automatic collection projection — user must
	wrap in a carrier with a manual Diagnostic impl."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error ArrayError {
\ttags: Array<String>,
}

pub fn main() nothrow -> Int {
\treturn 0;
}
""")
	_fails_with_code(rc, errs, 'E_PUB_ERROR_FIELD_NOT_PROJECTABLE',
		"Array<U> field rejected")


# ── Probe 3 ─ Optional<U> field rejected ──────────────────────────


def test_optional_field_rejected(tmp_path, capsys):
	"""`Optional<U>` field is NOT auto-projectable.  No automatic
	collection projection."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error OptError {
\thint: Optional<String>,
}

pub fn main() nothrow -> Int {
\treturn 0;
}
""")
	_fails_with_code(rc, errs, 'E_PUB_ERROR_FIELD_NOT_PROJECTABLE',
		"Optional<U> field rejected")


# ── Probe 4 ─ Map<String, V> field rejected ───────────────────────


def test_map_field_rejected(tmp_path, capsys):
	"""`Map<String, V>` field is NOT auto-projectable, even with
	String keys.  No automatic collection projection — user must
	wrap in a carrier with a manual Diagnostic impl."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error MapError {
\tmeta: Map<String, Int>,
}

pub fn main() nothrow -> Int {
\treturn 0;
}
""")
	_fails_with_code(rc, errs, 'E_PUB_ERROR_FIELD_NOT_PROJECTABLE',
		"Map<String, V> field rejected")


# ── Probe 5 ─ plain pub struct field rejected ──────────────────────


def test_plain_struct_field_rejected(tmp_path, capsys):
	"""A `pub struct` field without an explicit `Diagnostic` impl is
	NOT auto-projectable.  Ordinary structs are never structurally
	auto-dumped — only types with an explicit Diagnostic impl
	participate."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub struct PlainData {
\tpub n: Int,
}

pub error WithStructField {
\tdata: PlainData,
}

pub fn main() nothrow -> Int {
\treturn 0;
}
""")
	_fails_with_code(rc, errs, 'E_PUB_ERROR_FIELD_NOT_PROJECTABLE',
		"plain pub struct field rejected")


# ── Probe 6 ─ plain pub struct field with manual Diagnostic accepted ──


def test_plain_struct_field_with_manual_impl_accepted(tmp_path, capsys):
	"""A `pub struct` field with an explicit `implement core.Diagnostic`
	IS projectable — the struct opted in, the carrier owns its JSON
	shape."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub struct PlainData {
\tpub n: Int,
}

implement core.Diagnostic for PlainData {
\tpub fn to_json_text(self: &PlainData) nothrow -> String {
\t\treturn core.diagnostic_json_int(self.n);
\t}
}

pub error WithStructField {
\tdata: PlainData,
}

pub fn main() nothrow -> Int {
\treturn 0;
}
""")
	_ok(rc, errs, "plain struct field with manual Diagnostic accepted")


# ── Probe 8 ─ cross-module Diagnostic-impl recognized ─────────────


def test_cross_module_diagnostic_impl_field_accepted(tmp_path, capsys):
	"""Slice 5 (Diagnostic / projectability — K, 2026-05-04): a
	`pub error` field whose type is an EXTERNAL nominal with an
	explicit `implement core.Diagnostic for T` impl in its
	defining module is recognized as projectable by the synthesis
	gate.  Pins the workspace pre-scan
	(`workspace_diagnostic_targets`) + external-trait-world scan
	(`_scan_external_diagnostic_targets`) so the rule "struct
	fields participate when that struct has an explicit
	Diagnostic impl" doesn't silently reject every cross-module
	case.

	Uses `std.err.IteratorOpId` (a `pub variant` in std.err with a
	manual `implement core.Diagnostic for IteratorOpId`) — the
	same shape stdlib's own `pub error IteratorInvalidated` relies
	on.  If the gate were local-only, the user-side
	`pub error WrapsOpId { op_id: err.IteratorOpId }` would fail
	with `E_PUB_ERROR_FIELD_NOT_PROJECTABLE`."""
	rc, errs = _compile(tmp_path, capsys, """
module main;

import std.core as core;
import std.err as err;

use trait core.Diagnostic;

pub error WrapsOpId {
\top_id: err.IteratorOpId,
}

pub fn main() nothrow -> Int {
\treturn 0;
}
""")
	_ok(rc, errs, "cross-module Diagnostic impl recognized")


# ── Probe 9 ─ private error field rejected (no synthesized impl) ──


def test_private_error_field_rejected(tmp_path, capsys):
	"""Slice 5 (Diagnostic / projectability — K, 2026-05-04): a
	private (non-pub) `error E` decl does NOT get a synthesized
	Diagnostic impl, so naming a private error as a field type in
	a sibling `pub error Outer` is rejected with
	`E_PUB_ERROR_FIELD_NOT_PROJECTABLE`.  Closes the
	exception_pub-blind hole where the projectability check
	previously accepted any `kind=="error"` entry without
	consulting `is_pub`."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
error PrivateInner {
\tcode: Int,
}

pub error PubOuter {
\tinner: PrivateInner,
}

pub fn main() nothrow -> Int {
\treturn 0;
}
""")
	_fails_with_code(rc, errs, 'E_PUB_ERROR_FIELD_NOT_PROJECTABLE',
		"private error field rejected")


# ── Probe 7 ─ manual impl on the pub error itself unblocks ────────


def test_manual_impl_unblocks_non_projectable_field(tmp_path, capsys):
	"""When the user supplies a manual `Diagnostic for E` impl, the
	non-projectable-field rule does not fire — the user owns the
	whole JSON shape (no blending of manual + synthesized).

	Slice 6 (0.31.61): the auto-`Throw for E` body (`throw E(p=self.p)`)
	now lowers through the manual-Diagnostic Site C path
	(`_construct_error_via_manual_diagnostic`), which builds the
	Path-A struct and calls `to_json_text(&E)` instead of walking
	fields one-by-one through the DV-attachment validator.  No
	per-field `Diagnostic` requirement — `RawPtr<Byte>` is fine."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error PtrError {
\tp: RawPtr<Byte>,
}

implement core.Diagnostic for PtrError {
\tpub fn to_json_text(self: &PtrError) nothrow -> String {
\t\treturn "{}";
\t}
}

pub fn main() nothrow -> Int {
\treturn 0;
}
""")
	_ok(rc, errs, "manual impl on pub error unblocks non-projectable field")
