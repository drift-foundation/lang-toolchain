# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Slice 5: non-projectable field rejection on `pub error` types.

Pins the synthesis-fails-closed contract (spec §7.4) — when a
`pub error` declares a field whose type is not
Diagnostic/projectable AND no manual `Diagnostic` impl is
provided, the compiler rejects the declaration at the field site
with a targeted diagnostic.

Probes:

  1. `RawPtr<T>` field is not auto-projectable (spec §7.2 lists
     pointer types as not-projectable) →
     `E_PUB_ERROR_FIELD_NOT_PROJECTABLE`.
  2. `Map<Int, V>` (non-`String` key) is not auto-projectable
     (JSON object keys must be strings) →
     `E_PUB_ERROR_FIELD_NOT_PROJECTABLE`.
  3. Manual `Diagnostic` impl unblocks an otherwise-non-projectable
     field type — declaration compiles when the user takes
     responsibility for the projection.

**Out of scope:** function/lambda field types (compile harness
unclear; deferred to implementation-phase tests if needed);
recursive non-projectability through nested struct fields
(implementation-phase concern).

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


@_SLICE_5_PENDING
def test_rawptr_field_rejected(tmp_path, capsys):
	"""`pub error E { p: RawPtr<Byte> }` is rejected at the
	declaration site with `E_PUB_ERROR_FIELD_NOT_PROJECTABLE`.
	Pointer types are not auto-projectable per spec §7.2."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error PtrError {
\tp: core.RawPtr<Byte>,
}

fn main() nothrow -> Int {
\treturn 0;
}
""")
	_fails_with_code(rc, errs, 'E_PUB_ERROR_FIELD_NOT_PROJECTABLE',
		"RawPtr field rejected")


# ── Probe 2 ─ Map<Int, V> rejected ─────────────────────────────────


@_SLICE_5_PENDING
def test_map_with_non_string_key_rejected(tmp_path, capsys):
	"""`Map<Int, V>` is not auto-projectable — JSON object keys must
	be strings.  Diagnostic
	`E_PUB_ERROR_FIELD_NOT_PROJECTABLE`."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error MapError {
\tindexed: core.Map<Int, String>,
}

fn main() nothrow -> Int {
\treturn 0;
}
""")
	_fails_with_code(rc, errs, 'E_PUB_ERROR_FIELD_NOT_PROJECTABLE',
		"Map<Int, V> field rejected")


# ── Probe 3 ─ manual impl unblocks non-projectable field ──────────


@_SLICE_5_PENDING
def test_manual_impl_unblocks_non_projectable_field(tmp_path, capsys):
	"""When the user supplies a manual `Diagnostic for E` impl, the
	non-projectable-field rule does not fire — the user has taken
	responsibility for the projection."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error PtrError {
\tp: core.RawPtr<Byte>,
}

implement Diagnostic for PtrError {
\tpub fn to_json_text(self: &PtrError) nothrow -> String {
\t\treturn "{}";
\t}
}

fn main() nothrow -> Int {
\treturn 0;
}
""")
	_ok(rc, errs, "manual impl unblocks non-projectable field")
