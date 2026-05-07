# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Slice 5: exception envelope shape over thrown `pub error`.

Pins that the existing envelope-accessor surface (Slice 1-3)
continues to work over thrown `pub error` values:

  1. `e.encode_compact()` returns the full JSON envelope when a
     `pub error` is caught.
  2. `e.params.encode_compact()` returns the params object
     (synthesized projection of the pub error fields).
  3. `e.context.encode_compact()` returns the context array of
     `^`-capture frames (Slice 2 surface, unchanged).
  4. `e.params.get(key).as_*()` cursor accessors continue to
     work over the synthesized params (Slice 4A surface,
     unchanged).

**Byte-level envelope content (e.g., that the envelope JSON is
exactly `{"event_code":...,"event_fqn":"...","params":{...},...}`)
is verified at the e2e level, not in driver tests.  These probes
verify only the static surface — that the accessors compile when
the bound exception was originally a `pub error`.**

**Out of scope:** `e.context.get(...)` typed cursor (Slice 4B —
deferred per spec §1.2); `^`-capture in `pub error` throw paths
(uses the same `_emit_captured_locals` mechanism as Slice 2 —
verified at the e2e level).

Spec: `work/exception-diagnostics-context/slice5-spec.md` §10.
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
"""


# ── Probe 1 ─ encode_compact() over pub error ──────────────────────


def test_encode_compact_over_pub_error(tmp_path, capsys):
	"""`e.encode_compact()` returns the full envelope JSON when the
	thrown value was a `pub error`.  Envelope shape is unchanged
	from Slice 3 — pins that pub error doesn't break it.  The catch
	binder `e` is the Error envelope handle for envelope-method
	access; typed-binder field access is a separate (deferred)
	probe in `test_pub_error_throw_catch.py`."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error ParseError {
\toffset: Int,
}

fn risky() throws ParseError -> Int {
\tthrow ParseError(offset = 12);
}

fn main() nothrow -> Int {
\ttry {
\t\treturn risky();
\t} catch ParseError(e) {
\t\tval _envelope: String = e.encode_compact();
\t\treturn 0;
\t}
}
""")
	_ok(rc, errs, "encode_compact over pub error")


# ── Probe 2 ─ params + context segment access ──────────────────────


def test_params_and_context_segment_access(tmp_path, capsys):
	"""`e.params.encode_compact()` and `e.context.encode_compact()`
	both compile when bound to a `pub error` catch arm.  Pins that
	the Slice 1/2/3 surface continues unchanged."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error ParseError {
\toffset: Int,
}

fn risky() throws ParseError -> Int {
\tthrow ParseError(offset = 12);
}

fn main() nothrow -> Int {
\ttry {
\t\treturn risky();
\t} catch ParseError(e) {
\t\tval _p: String = e.params.encode_compact();
\t\tval _c: String = e.context.encode_compact();
\t\treturn 0;
\t}
}
""")
	_ok(rc, errs, "params + context segment access over pub error")


# ── Probe 3 ─ params.get cursor access ─────────────────────────────


def test_params_cursor_chained_match_resolves_without_std_core_import(tmp_path, capsys):
	"""LANGUAGE_BUG (2026-05-06): `e.params.get(k).as_int()` used as
	a `match` scrutinee was typing as `Unknown` when the user's
	module did not `import std.core`, because `JsonCursor`'s
	inherent methods required the type's defining module to be
	visible.  The `Unknown` scrutinee then cascaded to "match arms
	must produce the same type (have Int, expected Unknown)" and
	"unknown name 'v'" because the `Some(v)` pattern bound nothing.

	`pub error` exposes `e.params` (ErrorParamsView) and
	`params.get(k)` (JsonCursor) without the user explicitly
	naming `std.core`, so those types' inherent methods must
	resolve without the import.  Fix: extend
	`_PRELUDE_STD_CORE_TYPE_NAMES` to include `ErrorParamsView`,
	`ErrorContextView`, and `JsonCursor`.

	Both pre-bound (`val cur = ...; match cur`) and direct-chained
	(`match e.params.get(k).as_int()`) forms should compile cleanly
	without `import std.core` post-fix."""
	# Bound form — no `std.core` import in the source.
	source_bound = """
module main;

pub error PE { offset: Int }

fn risky() throws PE -> Int { throw PE(offset = 12); }

fn main() nothrow -> Int {
	try {
		return risky();
	} catch PE(e) {
		val cur = e.params.get("offset").as_int();
		val n = match cur {
			Some(v) => { v },
			None => { -1 }
		};
		return n;
	}
}
"""
	rc, errs = _compile(tmp_path, capsys, source_bound)
	_ok(rc, errs, "bound `as_int()` match scrutinee without std.core import")

	# Direct-chained form — same shape, no `val cur` indirection.
	source_chained = """
module main;

pub error PE { offset: Int }

fn risky() throws PE -> Int { throw PE(offset = 12); }

fn main() nothrow -> Int {
	try {
		return risky();
	} catch PE(e) {
		val n = match e.params.get("offset").as_int() {
			Some(v) => { v },
			None => { -1 }
		};
		return n;
	}
}
"""
	rc, errs = _compile(tmp_path, capsys, source_chained)
	_ok(rc, errs, "chained `as_int()` match scrutinee without std.core import")


def test_params_cursor_access_over_pub_error(tmp_path, capsys):
	"""`e.params.get(key).as_*()` continues to work over the
	synthesized params of a thrown `pub error`.  Pins that Slice
	4A's cursor surface is preserved.

	Was xfailed pre-2026-05-06 with a stale "Slice 5 not yet
	implemented" reason; the actual blocker was the missing
	`Optional<T>::unwrap_or` stdlib method.  Live as of the
	`unwrap_or` addition in `stdlib/std/core/core.drift`."""
	rc, errs = _compile(tmp_path, capsys, _PRE + """
pub error ParseError {
\toffset: Int,
}

fn risky() throws ParseError -> Int {
\tthrow ParseError(offset = 12);
}

fn main() nothrow -> Int {
\ttry {
\t\treturn risky();
\t} catch ParseError(e) {
\t\tval cur = e.params.get("offset");
\t\tval n = cur.as_int().unwrap_or(-1);
\t\treturn n;
\t}
}
""")
	_ok(rc, errs, "params.get cursor over pub error")
