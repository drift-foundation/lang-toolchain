# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Slice 1 of the DV→JSON diagnostics-context migration —
throw-side params JSON projection plus the minimum public dump
surface needed to observe the result: `e.params.encode_compact()`.

Slice 1 lands:

  - Throw-side canonical-params builder: declared exception fields
    are projected (DV→JsonNode internally during this transitional
    slice) into a `JsonObject`, encoded to canonical lex-ordered
    JSON via stdlib `json.encode_compact_with_config(&node, ...)`,
    and stored in the runtime via
    `drift_error_set_params_json(err, encoded)`.
  - `e.params.encode_compact() -> String` — segment dump of the
    canonical params JSON document.
  - The internal `_dv_to_json_node` helper and `_ExcParamsBuilder`
    type/methods are PRIVATE / not exported from stdlib; both are
    explicitly scheduled for deletion alongside the DV public
    surface (Slice 5).

Out of scope for Slice 1 (intentional, narrowed per K directive):

  - `e.context.encode_compact()` and `^`-capture frame projection
    (Slice 2).
  - `Error.encode_compact()` full envelope dump (Slice 3).
  - JsonCursor / `e.params.get(k)` typed lookup (Slice 4).
  - DV public removal + `Diagnostic.to_json` migration (Slice 5).

Slice 1 is ADDITIVE: the existing DV path
(`drift_error_add_attr_dv`, `e.attrs[...]`) remains fully
functional.  No ABI bump (`DRIFT_RT_ABI_VERSION` stays 11).

The branch-completion gate (direct `String:ConstShare`) is
tracked separately in `test_string_const_share.py` and must flip
to passing before final merge.

Note on test form: tests use the statement-form try/catch
(`try { ... } catch <Type>(e) { return ... } catch e { return ... }`).
The inline expression-form `try expr catch e { ... }` has a
pre-existing LANGUAGE_BUG (pinned by xfail in
`test_inline_try_catch_attrs_lang_bug.py`) — orthogonal to this
migration.
"""
from __future__ import annotations

import json as pyjson
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]


def _build_run(tmp_path: Path, source: str) -> tuple[int, str, str]:
	"""Compile + run a Drift program; return (exit_code, stdout, stderr)."""
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out_bin = tmp_path / "test_bin"
	env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
	env["PYTHONPATH"] = str(ROOT)
	build = subprocess.run(
		[
			sys.executable,
			"-m",
			"lang.driftc.driftc",
			"--stdlib-root",
			str(ROOT / "stdlib"),
			str(src),
			"--entry",
			"main::main",
			"-o",
			str(out_bin),
		],
		cwd=ROOT,
		capture_output=True,
		text=True,
		timeout=120,
		env=env,
	)
	if build.returncode != 0:
		return (build.returncode, build.stdout, build.stderr)
	run = subprocess.run(
		[str(out_bin)],
		capture_output=True,
		text=True,
		timeout=30,
	)
	return (run.returncode, run.stdout, run.stderr)


def _ok(rc: int, stdout: str, stderr: str, label: str) -> None:
	assert rc == 0, (
		f"{label}: rc={rc}\n"
		f"stdout:\n{stdout[:2000]}\n"
		f"stderr:\n{stderr[:2000]}"
	)


# All five throw-side / dump-surface tests below are LIVE Slice 1
# regression targets.  They lock the public-surface contract:
# `<error>.params` field access returns an `ErrorParamsView`, whose
# `encode_compact()` returns the canonical lex-ordered params JSON
# document built at the throw site.
#
# The legacy-additivity baseline (`test_old_attrs_path_still_works_
# additive`) locks the existing DV path's behavior, which Slice 1
# preserves additively.


# ─────────────────────────────────────────────────────────────────
# Test 1: empty exception → params == "{}"
# ─────────────────────────────────────────────────────────────────


def test_throw_empty_exception_params_is_empty_object(tmp_path):
	"""`throw E()` with zero declared fields must produce
	`e.params.encode_compact() == "{}"`."""
	source = """
module main;

import std.core as core;
import std.console as console;

pub error EmptyExc {}
fn _run() nothrow -> String {
\ttry {
\t\tthrow EmptyExc();
\t} catch e {
\t\treturn e.params.encode_compact();
\t}
\treturn "NO_THROW";
}

pub fn main() nothrow -> Int {
\tconsole.println(_run());
\treturn 0;
}
"""
	rc, stdout, stderr = _build_run(tmp_path, source)
	_ok(rc, stdout, stderr, "throw EmptyExc() params dump")
	assert stdout.strip() == "{}", (
		f"expected params == '{{}}', got: {stdout!r}\n"
		f"stderr:\n{stderr[:1000]}"
	)


# ─────────────────────────────────────────────────────────────────
# Test 2: Int + String fields → exact key/value JSON
# ─────────────────────────────────────────────────────────────────


def test_throw_int_string_fields(tmp_path):
	"""`throw E(order_id=42, code="X")` produces a JSON object
	containing both fields with their projected values.  String
	field is HIGH-RISK per K directive — pinned explicitly here."""
	source = """
module main;

import std.core as core;
import std.console as console;

pub error InvalidOrder { order_id: Int, code: String }
fn _run() nothrow -> String {
\ttry {
\t\tthrow InvalidOrder(order_id = 42, code = "X");
\t} catch InvalidOrder(e) {
\t\treturn e.params.encode_compact();
\t} catch e {
\t\treturn "WRONG_CATCH";
\t}
\treturn "NO_THROW";
}

pub fn main() nothrow -> Int {
\tconsole.println(_run());
\treturn 0;
}
"""
	rc, stdout, stderr = _build_run(tmp_path, source)
	_ok(rc, stdout, stderr, "throw InvalidOrder params dump")
	doc = pyjson.loads(stdout.strip())
	assert doc == {"order_id": 42, "code": "X"}, (
		f"expected params == {{order_id: 42, code: 'X'}}, got: {doc!r}\n"
		f"raw stdout: {stdout!r}"
	)


# ─────────────────────────────────────────────────────────────────
# Test 3: Bool + Float fields
# ─────────────────────────────────────────────────────────────────


def test_throw_bool_float_fields(tmp_path):
	"""`throw E(active=true, ratio=1.5)` projects Bool and Float
	primitives correctly."""
	source = """
module main;

import std.core as core;
import std.console as console;

pub error StatusFault { active: Bool, ratio: Float }
fn _run() nothrow -> String {
\ttry {
\t\tthrow StatusFault(active = true, ratio = 1.5);
\t} catch StatusFault(e) {
\t\treturn e.params.encode_compact();
\t} catch e {
\t\treturn "WRONG_CATCH";
\t}
\treturn "NO_THROW";
}

pub fn main() nothrow -> Int {
\tconsole.println(_run());
\treturn 0;
}
"""
	rc, stdout, stderr = _build_run(tmp_path, source)
	_ok(rc, stdout, stderr, "throw StatusFault params dump")
	doc = pyjson.loads(stdout.strip())
	assert doc.get("active") is True, f"active: {doc.get('active')!r}; full: {doc!r}"
	assert isinstance(doc.get("ratio"), (int, float)), f"ratio: {doc.get('ratio')!r}"
	assert float(doc.get("ratio")) == 1.5, f"ratio mismatch: {doc!r}"


# ─────────────────────────────────────────────────────────────────
# Test 4: cross-module event_code routing unchanged.
# (Slice 2 will add a context dump test; Slice 3 will add a full
# envelope dump test.)
# ─────────────────────────────────────────────────────────────────


def test_qualified_catch_event_code_routing_unchanged(tmp_path):
	"""Qualified-catch syntax (`catch <mod>:<Event>(e)`) routes by
	the deterministic event_code derived from the canonical FQN.
	Phase 2 throw-side params changes must not perturb this."""
	source = """
module main;

import std.core as core;
import std.console as console;

pub error OuterFault { reason: String }
fn _inner() throws -> Int {
\tthrow main:OuterFault(reason = "from-inner");
}

fn _run() nothrow -> String {
\ttry {
\t\tval _ = _inner();
\t} catch main:OuterFault(e) {
\t\treturn e.params.encode_compact();
\t} catch e {
\t\treturn "WRONG_CATCH";
\t}
\treturn "NO_THROW";
}

pub fn main() nothrow -> Int {
\tconsole.println(_run());
\treturn 0;
}
"""
	rc, stdout, stderr = _build_run(tmp_path, source)
	_ok(rc, stdout, stderr, "qualified-catch event_code routing")
	assert "WRONG_CATCH" not in stdout, (
		f"qualified catch failed to route — fell to wildcard.\n"
		f"stdout: {stdout!r}"
	)
	assert "NO_THROW" not in stdout, f"throw didn't fire: {stdout!r}"
	doc = pyjson.loads(stdout.strip())
	assert doc == {"reason": "from-inner"}, f"params: {doc!r}"


# ─────────────────────────────────────────────────────────────────
# Test 7: canonical / deterministic params JSON ordering.
#
# Phase 2 must produce a canonical, deterministic params JSON
# document — same throw must yield byte-identical output across
# repeated runs and builds.  HashMap iteration order is NOT
# deterministic, so the encoder must impose a fixed ordering
# (lex-utf8 over keys, or another stable order).  This test pins
# the ordering by asserting an exact byte-level string match;
# nondeterministic output would intermittently fail.
#
# The test uses keys chosen so declaration order != lex order —
# a regression that emits in declaration order would fail here.
# ─────────────────────────────────────────────────────────────────


def test_throw_params_json_is_canonical_ordered(tmp_path):
	"""Two-field exception with `z_last` declared FIRST and
	`a_first` declared SECOND.  Canonical lex ordering forces
	`a_first` ahead of `z_last` in the output regardless of
	declaration order — pinned by exact-string match."""
	source = """
module main;

import std.core as core;
import std.console as console;

pub error OrderProbe { z_last: Int, a_first: Int }
fn _run() nothrow -> String {
\ttry {
\t\tthrow OrderProbe(z_last = 2, a_first = 1);
\t} catch OrderProbe(e) {
\t\treturn e.params.encode_compact();
\t} catch e {
\t\treturn "WRONG_CATCH";
\t}
\treturn "NO_THROW";
}

pub fn main() nothrow -> Int {
\tconsole.println(_run());
\treturn 0;
}
"""
	rc, stdout, stderr = _build_run(tmp_path, source)
	_ok(rc, stdout, stderr, "canonical params ordering")
	# Lex-utf8: a_first < z_last → "{"a_first":1,"z_last":2}".
	expected = '{"a_first":1,"z_last":2}'
	assert stdout.strip() == expected, (
		f"params JSON not canonical/deterministic.\n"
		f"  expected: {expected!r}\n"
		f"  got:      {stdout.strip()!r}\n"
		f"If this test flakes across runs, encoding order is "
		f"nondeterministic — fix the encoder, not the test."
	)


# ─────────────────────────────────────────────────────────────────
# Test 8: ADDITIVE legacy `e.attrs[...]` path retired in Slice 7a
# (0.31.62, 2026-05-05).  The DV-typed attrs view is no longer
# user-accessible — `Error.attrs[...]` from user source is rejected
# with `E_EXC_ATTRS_REMOVED` (see test_dv_public_removed.py).  The
# JSON-text path on `e.params.encode_compact()` / `e.params.get(k)`
# is the sole supported user surface; covered by Tests 1–7 above.
# ─────────────────────────────────────────────────────────────────
