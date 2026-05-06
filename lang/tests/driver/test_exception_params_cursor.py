# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Slice 4A of the DV→JSON diagnostics-context migration —
JsonCursor lookup on `<error>.params`.

Slice 4A scope: scalar accessors on the canonical params JSON
text.  `<error>.params.get("k")` returns a `core.JsonCursor`
that distinguishes "absent key" from "present-and-explicit-JSON-
null".  Typed accessors (`as_int`, `as_bool`, `as_float`,
`as_string`) return `Optional<T>` — `None` for absent, wrong-
typed, malformed, or null-shaped lookups.

Out of scope:
  - `e.context.get(...)` cursor (later slice).
  - Nested object/array drilling (later slice).
  - `as_float` on `IntVal` auto-promotion (Slice 4A returns None
    because the v1 numeric scalar cast surface lacks Int→Float).

JSON Number exponent notation IS supported in Slice 4A —
`std.format.format_float` always emits `[eE]<sign?><digits>`
(e.g. `1.5` round-trips as `"1.5E0"`), so the parser handles
the full `[-]<digits>[.<digits>][[eE][+-]?<digits>]` form.

Slice 4A is ABI-neutral: no new runtime helpers.  Parsing
happens in std.core inline (text-only — no JsonNode dependency).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]


def _build_run(tmp_path: Path, source: str) -> tuple[int, str, str]:
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
# Test 1: scalar lookup — Int / String / Bool present
# ─────────────────────────────────────────────────────────────────


def test_scalar_lookups_int_string_bool(tmp_path):
	"""`e.params.get(k).as_int()` / `.as_string()` / `.as_bool()`
	return `Some(value)` for fields present in params."""
	source = """
module main;

import std.core as core;
import std.console as console;

pub error ScalarErr { order_id: Int, code: String, active: Bool }
fn _run() nothrow -> String {
\ttry {
\t\tthrow ScalarErr(order_id = 42, code = "x", active = true);
\t} catch ScalarErr(e) {
\t\tval id_opt = e.params.get("order_id").as_int();
\t\tval code_opt = e.params.get("code").as_string();
\t\tval active_opt = e.params.get("active").as_bool();
\t\tval id_str = match id_opt { Some(v) => { f"{v}" }, None => { "MISSING_INT" } };
\t\tval code_str = match code_opt { Some(s) => { s }, None => { "MISSING_STR" } };
\t\tval active_str = match active_opt { Some(b) => { f"{b}" }, None => { "MISSING_BOOL" } };
\t\treturn id_str + "|" + code_str + "|" + active_str;
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
	_ok(rc, stdout, stderr, "scalar lookups int/string/bool")
	assert stdout.strip() == "42|x|true", f"got {stdout!r}"


# ─────────────────────────────────────────────────────────────────
# Test 2: Float lookup
# ─────────────────────────────────────────────────────────────────


def test_scalar_lookup_float(tmp_path):
	"""Float fields parse as `Some(Float)` from canonical params
	JSON.  Slice 4A accepts `[-]<digits>[.<digits>]` form (no
	exponent)."""
	source = """
module main;

import std.core as core;
import std.console as console;

pub error FloatErr { ratio: Float }
fn _run() nothrow -> String {
\ttry {
\t\tthrow FloatErr(ratio = 1.5);
\t} catch FloatErr(e) {
\t\tval r_opt = e.params.get("ratio").as_float();
\t\tmatch r_opt {
\t\t\tSome(v) => { return f"GOT={v}"; },
\t\t\tNone => { return "MISSING_FLOAT"; }
\t\t}
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
	_ok(rc, stdout, stderr, "scalar lookup float")
	assert stdout.strip().startswith("GOT="), f"expected GOT=<float>, got: {stdout!r}"
	val_str = stdout.strip()[4:]
	# Float canonical form may render as 1.5 or 1.5e0 or similar — accept any 1.5 round-trip.
	assert float(val_str) == 1.5, f"float value mismatch: {val_str!r}"


# ─────────────────────────────────────────────────────────────────
# Test 3: absent key → is_missing == true; typed accessors → None
# ─────────────────────────────────────────────────────────────────


def test_absent_key_is_missing(tmp_path):
	"""A key absent from params makes `is_missing()` true and all
	typed accessors return None.  `is_null()` is false (the key
	is missing, NOT explicitly null)."""
	source = """
module main;

import std.core as core;
import std.console as console;

pub error AbsentErr { present: Int }
fn _run() nothrow -> String {
\ttry {
\t\tthrow AbsentErr(present = 1);
\t} catch AbsentErr(e) {
\t\tval c = e.params.get("absent");
\t\tval missing = c.is_missing();
\t\tval is_null = c.is_null();
\t\tval int_opt = c.as_int();
\t\tval string_opt = c.as_string();
\t\tval int_str = match int_opt { Some(v) => { f"{v}" }, None => { "NONE" } };
\t\tval str_str = match string_opt { Some(s) => { s }, None => { "NONE" } };
\t\treturn f"missing={missing}|null={is_null}|int={int_str}|str={str_str}";
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
	_ok(rc, stdout, stderr, "absent key is_missing")
	assert stdout.strip() == "missing=true|null=false|int=NONE|str=NONE", (
		f"unexpected output: {stdout!r}"
	)


# ─────────────────────────────────────────────────────────────────
# Test 4: explicit JSON null → is_null == true; is_missing == false
# ─────────────────────────────────────────────────────────────────


def test_explicit_null_distinguished_from_missing(tmp_path):
	"""When a params field is the JSON literal `null`, the cursor's
	`is_null()` is true and `is_missing()` is false.  Typed
	accessors still return None — JSON null is not coercible to
	Int/Bool/Float/String."""
	# Slice 7a (0.31.62, 2026-05-05): the original probe used
	# `pub error DvErr { payload: DiagnosticValue }` + DV::Null to
	# inject a JSON null into the params document.  DV is no longer
	# user-nameable.  A manual `core.Diagnostic` impl on the pub
	# error builds the same `{"payload":null}` shape via
	# `core.diagnostic_json_null()` / lex-utf8 sort + key
	# concatenation, exercising the same JsonCursor surface.
	source = """
module main;

import std.core as core;
import std.console as console;

pub error DvErr { tag: Int }

implement core.Diagnostic for DvErr {
\tpub fn to_json_text(self: &DvErr) nothrow -> String {
\t\tval _tag = self.tag;
\t\treturn "{\\"payload\\":" + core.diagnostic_json_null() + "}";
\t}
}

fn _run() nothrow -> String {
\ttry {
\t\tthrow DvErr(tag = 0);
\t} catch e {
\t\tval c = e.params.get("payload");
\t\tval missing = c.is_missing();
\t\tval is_null = c.is_null();
\t\treturn f"missing={missing}|null={is_null}";
\t}
\treturn "NO_THROW";
}

pub fn main() nothrow -> Int {
\tconsole.println(_run());
\treturn 0;
}
"""
	rc, stdout, stderr = _build_run(tmp_path, source)
	_ok(rc, stdout, stderr, "explicit null distinct from missing")
	assert stdout.strip() == "missing=false|null=true", (
		f"unexpected output: {stdout!r}"
	)


# ─────────────────────────────────────────────────────────────────
# Test 5: wrong-typed accessor returns None (not malformed)
# ─────────────────────────────────────────────────────────────────


def test_wrong_typed_accessor_returns_none(tmp_path):
	"""When a present key has a value of a different scalar type,
	the typed accessor returns None — not malformed/abort."""
	source = """
module main;

import std.core as core;
import std.console as console;

pub error MixedErr { label: String, count: Int }
fn _run() nothrow -> String {
\ttry {
\t\tthrow MixedErr(label = "hello", count = 7);
\t} catch MixedErr(e) {
\t\tval label_as_int = e.params.get("label").as_int();
\t\tval count_as_string = e.params.get("count").as_string();
\t\tval a = match label_as_int { Some(_v) => { "GOT_INT" }, None => { "NONE" } };
\t\tval b = match count_as_string { Some(_s) => { "GOT_STR" }, None => { "NONE" } };
\t\treturn a + "|" + b;
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
	_ok(rc, stdout, stderr, "wrong-typed accessor")
	assert stdout.strip() == "NONE|NONE", f"unexpected: {stdout!r}"


# ─────────────────────────────────────────────────────────────────
# Test 6: empty params object → all lookups missing
# ─────────────────────────────────────────────────────────────────


def test_empty_params_all_lookups_missing(tmp_path):
	"""When params is `{}` (empty exception), all lookups are
	missing.  Defensive fallback."""
	source = """
module main;

import std.core as core;
import std.console as console;

pub error EmptyParams {}
fn _run() nothrow -> String {
\ttry {
\t\tthrow EmptyParams();
\t} catch e {
\t\tval c = e.params.get("anything");
\t\treturn f"missing={c.is_missing()}|null={c.is_null()}";
\t}
\treturn "NO_THROW";
}

pub fn main() nothrow -> Int {
\tconsole.println(_run());
\treturn 0;
}
"""
	rc, stdout, stderr = _build_run(tmp_path, source)
	_ok(rc, stdout, stderr, "empty params lookups missing")
	assert stdout.strip() == "missing=true|null=false", f"unexpected: {stdout!r}"
