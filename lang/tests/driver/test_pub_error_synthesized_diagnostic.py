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
  4. Collection projectability: `Optional<U>`, `Array<U>`,
     `Map<String, V>` are auto-projectable when contained values
     are projectable.  Per K-correction (2026-05-03) this case is
     split into its own probe so the primitives probe stays
     readable; if container scaffolding turns out to be too heavy
     for a driver test, the collection probe will be left for
     implementation-phase tests.

**Byte-level JSON output verification (e.g., that the projection
emits `{"message":"bad","offset":12}` exactly, in lex-utf8 order)
is NOT performed in driver tests — it requires runtime execution
and belongs in the e2e suite.  These probes verify only the static
trait-satisfaction surface; lex-utf8 ordering correctness is an
implementation concern asserted at the e2e layer.**

**Out of scope:** non-projectable-field rejection
(test_pub_error_non_projectable_field.py); manual override
(test_pub_error_manual_diagnostic.py).

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


@_SLICE_5_PENDING
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


@_SLICE_5_PENDING
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


@_SLICE_5_PENDING
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


# ── Probe 4 ─ collection projectability (split per K-correction) ──


@_SLICE_5_PENDING
def test_synthesizes_diagnostic_for_collections(tmp_path, capsys):
	"""`Optional<U>`, `Array<U>`, `Map<String, V>` field types are
	auto-projectable when contained values are projectable (spec
	§7.2).  Probe is split off from the primitives probe per
	K-correction (2026-05-03) so primitives stay readable.  If
	container construction noise grows too heavy this probe may be
	deferred to implementation-phase tests; for now we just probe
	that the synthesized trait-satisfaction surface compiles."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error CollectionError {
\ttags: core.Array<String>,
\thint: core.Optional<String>,
\tmeta: core.Map<String, Int>,
}

fn main() nothrow -> Int {
\tassert_diag<type CollectionError>();
\treturn 0;
}
""")
	_ok(rc, errs, "synthesized Diagnostic for collection fields")
