# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Slice 5: `pub exception` transitional alias for `pub error`.

Per K-correction (2026-05-03), this file is split:

  - **Alias behavior probes** (this file): `pub exception` parses,
    behaves like `pub error` for declaration / construction /
    field access / throw / catch.  All strict-xfail.
  - **Warning emission probe** (also here, gated on stable
    warning-capture plumbing): `W_PUB_EXCEPTION_DEPRECATED`
    diagnostic at the declaration site.  If warning capture turns
    out to be awkward, this probe is the one to defer.

Pins:

  1. `pub exception E { ... }` declaration parses (transitional
     alias for one release; spec §2.1).
  2. Throw + catch over a `pub exception`-declared type works
     identically to `pub error` (alias semantics).
  3. The compiler emits a `W_PUB_EXCEPTION_DEPRECATED` warning at
     the `pub exception` declaration site, recommending migration
     to `pub error`.

**Out of scope:** the 0.33.0 promotion to hard error
(implementation-phase concern).

Spec: `work/exception-diagnostics-context/slice5-spec.md` §2.1.
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


def _compile_all(tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str) -> tuple[int, list[dict]]:
	"""Variant of _compile that returns ALL diagnostics (errors AND
	warnings).  Needed for the deprecation-warning probe."""
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	rc = driftc_main(["--stdlib-root", "stdlib", "--test-build-only", str(src), "--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload.get("diagnostics", [])


def _ok_no_errors(rc: int, all_diags: list[dict], label: str) -> None:
	"""Compile must succeed; warnings are tolerated (the alias is
	expected to emit a deprecation warning)."""
	errs = [d for d in all_diags if d.get('severity') == 'error']
	assert rc == 0, (
		f"{label}: rc={rc}, errs:\n"
		+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
	)


def _warns_with_code(rc: int, all_diags: list[dict], code: str, label: str) -> None:
	"""Compile must succeed AND a warning with `code` must be in
	the diagnostics."""
	errs = [d for d in all_diags if d.get('severity') == 'error']
	assert rc == 0, (
		f"{label}: expected compile success but rc={rc}; errs:\n"
		+ "\n".join(f"  {e.get('code')}: {e.get('message','')[:200]}" for e in errs)
	)
	warns = [d for d in all_diags if d.get('severity') == 'warning']
	codes = [w.get('code') for w in warns]
	assert code in codes, (
		f"{label}: expected warning {code} not in {codes}"
	)


_PRE = """
module main;

import std.core as core;
"""


# ── Probe 1 ─ pub exception parses (alias) ─────────────────────────


@_SLICE_5_PENDING
def test_pub_exception_alias_parses(tmp_path, capsys):
	"""`pub exception E { ... }` declaration parses as a transitional
	alias for `pub error`.  No errors at compile (warnings are
	tolerated; the deprecation-warning probe asserts that
	separately)."""
	rc, all_diags = _compile_all(tmp_path, capsys, _PRE + """
pub exception ParseError {
\tmessage: String,
\toffset: Int,
}

fn main() nothrow -> Int {
\treturn 0;
}
""")
	_ok_no_errors(rc, all_diags, "pub exception alias parses")


# ── Probe 2 ─ throw + catch over pub exception ─────────────────────


@_SLICE_5_PENDING
def test_pub_exception_throw_catch_works(tmp_path, capsys):
	"""Throw + typed catch over a `pub exception`-declared type
	works identically to `pub error` (alias semantics)."""
	rc, all_diags = _compile_all(tmp_path, capsys, _PRE + """
pub exception ParseError {
\toffset: Int,
}

fn risky() throws ParseError -> Int {
\tthrow ParseError(offset = 12);
}

fn main() nothrow -> Int {
\ttry {
\t\treturn risky();
\t} catch ParseError(e) {
\t\treturn e.offset;
\t}
}
""")
	_ok_no_errors(rc, all_diags, "pub exception throw + catch")


# ── Probe 3 ─ deprecation warning emitted ──────────────────────────


@_SLICE_5_PENDING
def test_pub_exception_emits_deprecation_warning(tmp_path, capsys):
	"""`pub exception E { ... }` declaration emits the warning
	`W_PUB_EXCEPTION_DEPRECATED` at the declaration site, pointing
	users at `pub error`.

	NOTE: this probe depends on the driver harness exposing
	severity=warning diagnostics with stable codes.  Per
	K-correction (2026-05-03), if warning-capture plumbing turns
	out to be awkward at implementation time, THIS probe is the
	one to defer to a follow-up; the alias-behavior probes (1 and
	2) stand alone."""
	rc, all_diags = _compile_all(tmp_path, capsys, _PRE + """
pub exception ParseError {
\tmessage: String,
\toffset: Int,
}

fn main() nothrow -> Int {
\treturn 0;
}
""")
	_warns_with_code(rc, all_diags, 'W_PUB_EXCEPTION_DEPRECATED',
		"pub exception emits deprecation warning")
