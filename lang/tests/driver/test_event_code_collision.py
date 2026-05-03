# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Slice 5: per-package `event_code` duplicate detection.

Per K-correction (2026-05-03), this test exercises EXPLICIT
duplicate event-code assignment in the same package, NOT a forced
auto-hash collision.  Auto-hash collision detection (currently
xxHash64 of the `module:Name` FQN, per
`lang/driftc/core/event_codes.py`) is covered by unit-level
compiler tests if/when a deterministic injection hook exists.

Probes:

  1. Two `pub error` types in the same package with the SAME
     explicit event_code → diagnostic
     `E_EVENT_CODE_DUPLICATE` (or whatever the spec §13.2 final
     code allocation chooses; placeholder name pinned in the
     spec).  The diagnostic should recommend explicit code
     assignment for one of them.
  2. Distinct explicit event_codes in the same package compile
     cleanly (positive control).

**Out of scope:** auto-hash collision (no hook for forced
collision through the xxHash64 scheme); cross-package duplicate
detection (a different problem — packages have their own
event_code namespaces via event_fqn).

Spec: `work/exception-diagnostics-context/slice5-spec.md` §0
(event_code algorithm), §16 (collision-detection mitigation).
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
"""


# ── Probe 1 ─ explicit duplicate event_code rejected ──────────────


@_SLICE_5_PENDING
def test_explicit_duplicate_event_code_rejected(tmp_path, capsys):
	"""Two `pub error` types in the same module/package both
	pinning event_code 0x1234 fail compile with
	`E_EVENT_CODE_DUPLICATE`.  Per-package collision detection
	per spec §0 / §16."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error A(0x1234) {}
pub error B(0x1234) {}

fn main() nothrow -> Int {
\treturn 0;
}
""")
	_fails_with_code(rc, errs, 'E_EVENT_CODE_DUPLICATE',
		"explicit duplicate event_code rejected")


# ── Probe 2 ─ distinct explicit event_codes compile (control) ─────


def test_distinct_explicit_event_codes_compile(tmp_path, capsys):
	"""Positive control: two `pub error` types with DISTINCT
	explicit event_codes compile cleanly.  This rules out a
	false-positive in the duplicate-detection pass."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error A(0x1234) {}
pub error B(0x5678) {}

fn main() nothrow -> Int {
\treturn 0;
}
""")
	_ok(rc, errs, "distinct explicit event_codes compile")
