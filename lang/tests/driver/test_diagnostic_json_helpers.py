# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Slice 5: `std.core` JSON-text helper public surface.

Pins the six new public helpers in std.core (spec §9):

  - `diagnostic_json_string(s: &String) -> String`
  - `diagnostic_json_null() -> String`
  - `diagnostic_json_bool(v: Bool) -> String`
  - `diagnostic_json_int(n: Int) -> String`
  - `diagnostic_json_uint(n: Uint) -> String`
  - `diagnostic_json_float(f: Float) -> String`

Probes verify the helpers are callable from user code with the
documented signatures (compile-only — byte-level escape
correctness is verified at the e2e level).

Per K-correction (2026-05-03) the minimum edge-case set the
string helper must accept as INPUT (not byte-level output —
that's e2e):
  - empty string
  - quote (\\")
  - backslash (\\\\)
  - newline / carriage-return / tab / backspace / formfeed
  - ordinary UTF-8 passthrough (non-ASCII characters)

**NaN/Inf for `diagnostic_json_float` are explicitly OUT OF SCOPE
in this slice** — JSON numbers cannot represent NaN/Inf and Float's
stable runtime form for them is not yet pinned.  When/if
NaN/Inf semantics are decided, a separate probe is added.

Spec: `work/exception-diagnostics-context/slice5-spec.md` §9.
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
"""


# ── Probe 1 ─ all six helpers callable ─────────────────────────────


@_SLICE_5_PENDING
def test_all_six_helpers_callable(tmp_path, capsys):
	"""All six `diagnostic_json_*` helpers exist in std.core with
	the documented signatures and return `String`."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn main() nothrow -> Int {
\tval s1: String = "x";
\tval _a: String = core.diagnostic_json_string(&s1);
\tval _b: String = core.diagnostic_json_null();
\tval _c: String = core.diagnostic_json_bool(true);
\tval _d: String = core.diagnostic_json_int(42);
\tval _e: String = core.diagnostic_json_uint(42);
\tval _f: String = core.diagnostic_json_float(1.5);
\treturn 0;
}
""")
	_ok(rc, errs, "all six helpers callable")


# ── Probe 2 ─ string helper edge-case inputs ───────────────────────


@_SLICE_5_PENDING
def test_string_helper_accepts_edge_inputs(tmp_path, capsys):
	"""`diagnostic_json_string` accepts the K-listed minimum
	edge-case inputs (compile-only — byte output is e2e-verified):
	empty / quote / backslash / control whitespace (newline, CR,
	tab, backspace, formfeed) / ordinary UTF-8."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn main() nothrow -> Int {
\tval empty: String = "";
\tval quote: String = "\\"";
\tval bs: String = "\\\\";
\tval nl: String = "\\n";
\tval cr: String = "\\r";
\tval tab: String = "\\t";
\tval bksp: String = "\\b";
\tval ff: String = "\\f";
\tval utf8: String = "héllo";

\tval _a: String = core.diagnostic_json_string(&empty);
\tval _b: String = core.diagnostic_json_string(&quote);
\tval _c: String = core.diagnostic_json_string(&bs);
\tval _d: String = core.diagnostic_json_string(&nl);
\tval _e: String = core.diagnostic_json_string(&cr);
\tval _f: String = core.diagnostic_json_string(&tab);
\tval _g: String = core.diagnostic_json_string(&bksp);
\tval _h: String = core.diagnostic_json_string(&ff);
\tval _i: String = core.diagnostic_json_string(&utf8);
\treturn 0;
}
""")
	_ok(rc, errs, "string helper accepts edge inputs")


# ── Probe 3 ─ number helpers accept representative finite values ──


@_SLICE_5_PENDING
def test_number_helpers_accept_finite_values(tmp_path, capsys):
	"""Number helpers accept zero, positive, negative (where
	applicable), and a normal finite Float (1.5).  NaN/Inf are
	explicitly OUT OF SCOPE — see file docstring."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
fn main() nothrow -> Int {
\tval _a: String = core.diagnostic_json_int(0);
\tval _b: String = core.diagnostic_json_int(-1);
\tval _c: String = core.diagnostic_json_int(2147483647);
\tval _d: String = core.diagnostic_json_uint(0);
\tval _e: String = core.diagnostic_json_uint(4294967295);
\tval _f: String = core.diagnostic_json_float(0.0);
\tval _g: String = core.diagnostic_json_float(1.5);
\tval _h: String = core.diagnostic_json_float(-1.5);
\treturn 0;
}
""")
	_ok(rc, errs, "number helpers accept finite values")
