# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Slice 3 of the DV→JSON diagnostics-context migration —
`Error.encode_compact()` full canonical envelope dump.

Slice 3 lands the primary log/save path: one call serializes the
whole exception event into canonical JSON.  Envelope shape:

    {
        "event_code": <uint>,
        "event_fqn": "<escaped fqn>",
        "params": <params_json object — spliced, NOT quoted>,
        "context": <context_json array — spliced, NOT quoted>,
        "stack": null
    }

`params` and `context` segments are the canonical JSON documents
produced by Slice 1 / Slice 2 throw-side wiring; they're spliced
verbatim into the envelope (no parse/re-encode).  `event_fqn` is
JSON-escaped via `core._json_quote_string`.  `stack` stays the
literal `null` for now — full backtrace serialization is a
later track.

Slice 3 is ABI-neutral: no new runtime helpers.  Implementation
reuses `M.ErrorEvent` (event_code via extractvalue),
`M.ErrorEventFqn` (event_fqn via extractvalue + retain — new MIR
op, no new runtime symbol), `M.ExcGetParamsJson`,
`M.ExcGetContextJson`, `M.StringFromUint`, and the existing
`core._json_quote_string` helper.
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


# ─────────────────────────────────────────────────────────────────
# Test 1: empty exception → params {}, context [], stack null
# ─────────────────────────────────────────────────────────────────


def test_empty_exception_full_envelope(tmp_path):
	"""`throw E()` with no fields and no `^` captures yields a
	canonical envelope where params is {} and context is []."""
	source = """
module main;

import std.core as core;
import std.console as console;

pub exception EmptyExc()

fn _run() nothrow -> String {
\ttry {
\t\tthrow EmptyExc();
\t} catch e {
\t\treturn e.encode_compact();
\t}
\treturn "NO_THROW";
}

pub fn main() nothrow -> Int {
\tconsole.println(_run());
\treturn 0;
}
"""
	rc, stdout, stderr = _build_run(tmp_path, source)
	_ok(rc, stdout, stderr, "empty envelope")
	doc = pyjson.loads(stdout.strip())
	# All five top-level keys present.
	for key in ("event_code", "event_fqn", "params", "context", "stack"):
		assert key in doc, f"envelope missing {key!r}: {doc!r}"
	assert doc["params"] == {}, f"empty params expected, got: {doc['params']!r}"
	assert doc["context"] == [], f"empty context expected, got: {doc['context']!r}"
	assert doc["stack"] is None, f"stack must be null, got: {doc['stack']!r}"
	# event_code is numeric and non-zero.
	assert isinstance(doc["event_code"], int), f"event_code: {doc['event_code']!r}"
	assert doc["event_code"] != 0, f"event_code unexpectedly zero: {doc!r}"
	# event_fqn is the canonical FQN.
	assert isinstance(doc["event_fqn"], str), f"event_fqn: {doc['event_fqn']!r}"
	assert "EmptyExc" in doc["event_fqn"], f"fqn doesn't name EmptyExc: {doc['event_fqn']!r}"


# ─────────────────────────────────────────────────────────────────
# Test 2: declared params spliced (not re-quoted) into envelope
# ─────────────────────────────────────────────────────────────────


def test_params_spliced_as_json_object(tmp_path):
	"""When the exception has declared fields, the envelope's
	`params` value is the JSON object itself (spliced) — not a
	quoted string of JSON text."""
	source = """
module main;

import std.core as core;
import std.console as console;

pub exception Faulty(reason: String, level: Int)

fn _run() nothrow -> String {
\ttry {
\t\tthrow Faulty(reason = "boom", level = 7);
\t} catch Faulty(e) {
\t\treturn e.encode_compact();
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
	_ok(rc, stdout, stderr, "params spliced envelope")
	doc = pyjson.loads(stdout.strip())
	# params must be a JSON OBJECT, not a quoted string.
	assert isinstance(doc["params"], dict), (
		f"params must be a JSON object (spliced), not a quoted string; got {type(doc['params']).__name__}: {doc['params']!r}"
	)
	assert doc["params"] == {"level": 7, "reason": "boom"}, (
		f"params content mismatch: {doc['params']!r}"
	)
	assert doc["context"] == [], f"no captures → empty context: {doc['context']!r}"
	assert doc["stack"] is None, f"stack: {doc['stack']!r}"


# ─────────────────────────────────────────────────────────────────
# Test 3: captured-frame context spliced (not re-quoted)
# ─────────────────────────────────────────────────────────────────


def test_context_spliced_as_json_array(tmp_path):
	"""When the throw path has `^` captures, the envelope's
	`context` value is the JSON array itself (spliced) — not a
	quoted string of JSON text."""
	source = """
module main;

import std.core as core;
import std.console as console;

pub exception WrappedErr(tag: String)

fn _inner() throws -> Int {
\tval ^record_id: String as "record_id" = "rec-99";
\tthrow WrappedErr(tag = "boom");
}

fn _run() nothrow -> String {
\ttry {
\t\tval _ = _inner();
\t} catch WrappedErr(e) {
\t\treturn e.encode_compact();
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
	_ok(rc, stdout, stderr, "context spliced envelope")
	doc = pyjson.loads(stdout.strip())
	assert isinstance(doc["context"], list), (
		f"context must be a JSON array (spliced), not a quoted string; got {type(doc['context']).__name__}: {doc['context']!r}"
	)
	assert len(doc["context"]) == 1, (
		f"expected 1 frame from _inner; got {doc['context']!r}"
	)
	frame = doc["context"][0]
	assert "_inner" in frame["fn"], f"frame fn: {frame['fn']!r}"
	assert frame["locals"] == {"record_id": "rec-99"}, (
		f"frame locals: {frame['locals']!r}"
	)
	assert doc["params"] == {"tag": "boom"}, f"params: {doc['params']!r}"
	assert doc["stack"] is None, f"stack: {doc['stack']!r}"


# ─────────────────────────────────────────────────────────────────
# Test 4: event_code is numeric (not a string)
# ─────────────────────────────────────────────────────────────────


def test_event_code_is_numeric(tmp_path):
	"""`event_code` is the deterministic 64-bit hash of the FQN,
	emitted as a JSON number (not a quoted string)."""
	source = """
module main;

import std.core as core;
import std.console as console;

pub exception NumericEv()

fn _run() nothrow -> String {
\ttry {
\t\tthrow NumericEv();
\t} catch e {
\t\treturn e.encode_compact();
\t}
\treturn "NO_THROW";
}

pub fn main() nothrow -> Int {
\tconsole.println(_run());
\treturn 0;
}
"""
	rc, stdout, stderr = _build_run(tmp_path, source)
	_ok(rc, stdout, stderr, "numeric event_code")
	doc = pyjson.loads(stdout.strip())
	# Python's json maps JSON numbers to int (or float for non-integer);
	# a quoted code would parse as str.  Pin int/non-zero.
	assert isinstance(doc["event_code"], int), (
		f"event_code must be a JSON number (Python int after parse); "
		f"got {type(doc['event_code']).__name__}: {doc['event_code']!r}"
	)
	assert doc["event_code"] > 0, f"event_code zero/negative: {doc!r}"
	# Also pin the canonical-FQN format: "<module>:<event>".
	assert ":" in doc["event_fqn"], f"event_fqn missing ':' separator: {doc['event_fqn']!r}"


# ─────────────────────────────────────────────────────────────────
# Test 5: event_fqn JSON-escaping survives raw concatenation
# ─────────────────────────────────────────────────────────────────


def test_event_fqn_escaping(tmp_path):
	"""The envelope must embed `event_fqn` as a properly JSON-
	escaped string.  Drift event FQNs use only printable ASCII
	(module path dots + colon-separated event name), so the more
	exotic escape cases (\\b, \\u00xx, control chars) don't fire
	in practice — but the path must produce parseable JSON for
	any well-formed FQN."""
	source = """
module main;

import std.core as core;
import std.console as console;

pub exception EscapeProbe()

fn _run() nothrow -> String {
\ttry {
\t\tthrow EscapeProbe();
\t} catch e {
\t\treturn e.encode_compact();
\t}
\treturn "NO_THROW";
}

pub fn main() nothrow -> Int {
\tconsole.println(_run());
\treturn 0;
}
"""
	rc, stdout, stderr = _build_run(tmp_path, source)
	_ok(rc, stdout, stderr, "event_fqn escaping")
	# The dump must parse as JSON — the most direct guarantee that
	# event_fqn is properly escaped (an unescaped quote or
	# backslash inside the FQN would break parsing).
	doc = pyjson.loads(stdout.strip())
	# The raw byte form must double-quote the fqn (first
	# occurrence after the `"event_fqn":` literal).
	raw = stdout.strip()
	marker = '"event_fqn":"'
	assert marker in raw, f"envelope missing event_fqn marker: {raw!r}"
	# And the whole thing must round-trip.
	assert "EscapeProbe" in doc["event_fqn"], f"fqn: {doc['event_fqn']!r}"
