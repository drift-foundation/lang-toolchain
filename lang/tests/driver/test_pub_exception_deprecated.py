# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Slice 5: `pub exception` is rejected — negative migration tests.

Per K (2026-05-03 hard-break direction): `pub exception` user-facing
syntax is REMOVED in 0.32.0.  `pub error` is the only declaration
syntax accepted at the user-source boundary.  Stdlib migrated its
declarations in implementation slice 2 prep.

This file replaces the original alias-behavior tests with negative
probes:

  1. `pub exception E { ... }` brace form — DOES NOT PARSE (the
     grammar accepts only paren-form `pub exception E(...)`, and
     even that paren form is targeted for rejection).  Test asserts
     a compile failure.  Flippable today.
  2. `pub exception E(...)` paren form — once the user-source
     rejection diagnostic (`E_PUB_EXCEPTION_REMOVED`) is enabled,
     this fails compile with a migration hint pointing at
     `pub error`.  Currently the diagnostic is GATED on the
     test-corpus mass-migration sub-slice (101 driver/codegen test
     files use paren-form `pub exception` in heredocs and need to
     migrate first).  Strict-xfail until the gate lifts.

**Out of scope:** there is no transitional alias path — `pub error`
is the only user-facing form.  Throw/catch behavior tests for
`pub error` live in `test_pub_error_throw_catch.py`.

Spec: `work/exception-diagnostics-context/slice5-spec.md` §2.1
(superseded — alias preservation removed by K, 2026-05-03 follow-up).
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


def _fails_to_compile(rc: int, errs: list[dict], label: str) -> None:
	"""Compile must fail with at least one error — any rejection
	is acceptable for the brace-form probe (grammar-level rejection
	is itself the migration boundary)."""
	assert rc != 0, f"{label}: expected compile failure but rc=0"
	assert errs, f"{label}: expected at least one error diagnostic; got none"


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
"""


# ── Probe 1 ─ brace form rejected (grammar boundary) ─────────────


def test_pub_exception_brace_form_rejected(tmp_path, capsys):
	"""`pub exception E { ... }` does NOT parse — the grammar
	accepts brace bodies only for `pub error`.  Compile fails with
	a parse-level rejection.  This is the user-facing migration
	boundary: anyone reaching for brace form should land on
	`pub error E { ... }` instead."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub exception ParseError {
\tmessage: String,
\toffset: Int,
}

pub fn main() nothrow -> Int {
\treturn 0;
}
""")
	_fails_to_compile(rc, errs, "pub exception brace form rejected")


# ── Probe 2 ─ paren form rejection diagnostic ────────────────────


def test_pub_exception_paren_form_rejected_with_migration_diag(tmp_path, capsys):
	"""Paren-form `pub exception E(...)` is rejected with diagnostic
	`E_PUB_EXCEPTION_REMOVED` and a migration hint pointing at
	`pub error E { ... }`.  Diagnostic emitted from
	`lang/driftc/parser/__init__.py:_build_exception_catalog` after the
	test-corpus migration sub-slice landed (2026-05-03)."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub exception ParseError(message: String, offset: Int);

pub fn main() nothrow -> Int {
\treturn 0;
}
""")
	_fails_with_code(rc, errs, 'E_PUB_EXCEPTION_REMOVED',
		"pub exception paren form rejected with migration diagnostic")
