# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Slice 5: synthesized `Diagnostic` for `pub error` types.

Pins:

  1. `pub error E { ...primitive fields... }` automatically gets a
     synthesized `Diagnostic` impl (no manual `to_json_text`
     required).  Witness: `assert_diag<type E>()` compiles.
  2. `pub error E {}` (empty payload) still gets a synthesized
     impl; the projection returns `"{}"` (envelope `params` is
     ALWAYS a JSON object — never `null`, never omitted).
  3. Synthesis fires recursively: a `pub error` whose field is
     itself a `pub error` is projectable.

**Byte-level JSON output verification (e.g., that the projection
emits `{"message":"bad","offset":12}` exactly, in lex-utf8 order)
is NOT performed in driver tests — it requires runtime execution
and belongs in the e2e suite.  These probes verify only the static
trait-satisfaction surface; lex-utf8 ordering correctness is an
implementation concern asserted at the e2e layer.**

**Out of scope (covered elsewhere):**
  * Collection / RawPtr / plain-struct field rejection →
    `test_pub_error_non_projectable_field.py` (no automatic
    Array / Optional / Map projection — K, 2026-05-04).
  * Manual override behavior → `test_pub_error_manual_diagnostic.py`.

Spec: `work/exception-diagnostics-context/slice5-spec.md` §6-§7.
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


_PRE = """
module main;

import std.core as core;

use trait core.Diagnostic;

// Witness: requires its type-arg to satisfy `core.Diagnostic`.
fn assert_diag<T>() nothrow -> Void require T is core.Diagnostic { }
"""


# ── Probe 1 ─ primitives synthesize ────────────────────────────────


def test_synthesizes_diagnostic_for_primitive_fields(tmp_path, capsys):
	"""`pub error E` with all-primitive fields (Int, Uint, Bool,
	Float, String) gets a synthesized `Diagnostic` impl.  Witness:
	`assert_diag<type E>()` compiles."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error PrimitiveError {
\tmessage: String,
\toffset: Int,
\tflag: Bool,
}

fn main() nothrow -> Int {
\tassert_diag<type PrimitiveError>();
\treturn 0;
}
""")
	_ok(rc, errs, "synthesized Diagnostic for primitives")


# ── Probe 2 ─ empty payload synthesizes ────────────────────────────


def test_synthesizes_diagnostic_for_empty_payload(tmp_path, capsys):
	"""`pub error E {}` (no fields) gets a synthesized `Diagnostic`
	impl.  Spec §7.3: synthesis returns `"{}"`; envelope `params`
	is always an object (never null, never omitted)."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error EmptyError {}

fn main() nothrow -> Int {
\tassert_diag<type EmptyError>();
\treturn 0;
}
""")
	_ok(rc, errs, "synthesized Diagnostic for empty payload")


# ── Probe 3 ─ pub error containing pub error ───────────────────────


def test_synthesizes_diagnostic_for_nested_pub_error(tmp_path, capsys):
	"""A `pub error` whose field is itself a `pub error` projects
	recursively; both types get synthesized impls and the outer
	composition is projectable."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error InnerError {
\tdetail: String,
}

pub error OuterError {
\tcontext: String,
\tcause: InnerError,
}

fn main() nothrow -> Int {
\tassert_diag<type InnerError>();
\tassert_diag<type OuterError>();
\treturn 0;
}
""")
	_ok(rc, errs, "synthesized Diagnostic for nested pub error")


# ── Collection probe REMOVED ───────────────────────────────────────
#
# Per K (2026-05-04) — collections are NOT auto-projectable.  The
# previous probe `test_synthesizes_diagnostic_for_collections`
# expected Array/Optional/Map<String,_> fields to synthesize; that
# expectation is gone.  Coverage moved to
# `test_pub_error_non_projectable_field.py` as positive rejections
# (`test_array_field_rejected`, `test_optional_field_rejected`,
# `test_map_field_rejected`).
