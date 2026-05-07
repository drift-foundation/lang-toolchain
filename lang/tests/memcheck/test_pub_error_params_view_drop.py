# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: catch-arm `e.params.encode_compact()` on a `pub error`
must not leak the params JSON string.

LANGUAGE_BUG (2026-05-06): every `pub error` catch arm that projected
through `e.params` (and analogously `e.context`) leaked exactly one
String allocation per throw — the params JSON string built by the
synthesized `to_json_text` body and stored on the runtime
`DriftError` via `drift_error_set_params_json`.  When the catch arm
read `e.params`, the compiler built an `ErrorParamsView` from
`drift_error_get_params_json` (which retains the stored String) and
stored it in a stack local.  No scope-drop was emitted for the
view's `String` field on catch-arm exit, leaking that retain.

Symptoms (pre-fix):
  - `(&a).to_json_text()` direct call: leak-clean.
  - throw + catch, never read `e.params`: leak-clean.
  - throw + catch + `e.params.encode_compact()`: leaks 1 String.
  - all 7 e2e fixtures that read `e.params` in catch arms leak
    exactly one allocation (size = JSON length).

This test pins the params view drop with two probes:
  1. `e.params.encode_compact()` — must be leak-clean.
  2. `e.context.encode_compact()` — companion check on the parallel
     `ErrorContextView` shape (same struct shape: one `String`
     field built from a runtime retain).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


_PARAMS_SOURCE = """\
module main;

import std.core as core;
import std.console as console;

pub error Boom { msg: String, code: Int }

fn risky() throws Boom -> Int {
\tthrow Boom(msg = "fire", code = 42);
}

pub fn main() nothrow -> Int {
\ttry {
\t\treturn risky();
\t} catch Boom(e) {
\t\tval s = e.params.encode_compact();
\t\tconsole.println(s);
\t\treturn 0;
\t}
}
"""

_CONTEXT_SOURCE = """\
module main;

import std.core as core;
import std.console as console;

pub error Boom { msg: String }

fn inner() throws Boom -> Int {
\tval ^who = "stage1";
\tthrow Boom(msg = "fire");
}

fn risky() throws Boom -> Int {
\treturn inner();
}

pub fn main() nothrow -> Int {
\ttry {
\t\treturn risky();
\t} catch Boom(e) {
\t\tval ctx = e.context.encode_compact();
\t\tconsole.println(ctx);
\t\treturn 0;
\t}
}
"""


def _build_and_check(tmp_path: Path, source: str, label: str) -> None:
	assert shutil.which("valgrind") is not None, "valgrind required"
	src = tmp_path / "main.drift"
	src.write_text(source)
	out_bin = tmp_path / "test_bin"
	build = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	assert build.returncode == 0, f"compile failed ({label}): {build.stderr[:400]}"
	vg_log = tmp_path / "valgrind.log"
	vg = subprocess.run(
		["valgrind", "--tool=memcheck", "--leak-check=full",
		 "--show-leak-kinds=definite,indirect",
		 "--errors-for-leak-kinds=definite,indirect",
		 "--error-exitcode=97",
		 f"--log-file={vg_log}",
		 str(out_bin)],
		capture_output=True, text=True, timeout=120,
	)
	vg_output = vg_log.read_text() if vg_log.exists() else ""
	lost = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	indirect = re.search(r"indirectly lost: (\d[\d,]*) bytes", vg_output)
	definitely_lost = int(lost.group(1).replace(",", "")) if lost else 0
	indirect_lost = int(indirect.group(1).replace(",", "")) if indirect else 0
	assert vg.returncode != 97, (
		f"valgrind detected leaks ({label}):\n"
		f"  definitely lost: {definitely_lost} bytes\n"
		f"  indirectly lost: {indirect_lost} bytes\n"
		f"valgrind log:\n{vg_output[-800:]}"
	)
	assert definitely_lost == 0 and indirect_lost == 0, (
		f"{label}: definitely lost={definitely_lost}, "
		f"indirectly lost={indirect_lost}"
	)


def test_pub_error_params_encode_compact_no_leak(tmp_path: Path) -> None:
	"""`e.params.encode_compact()` must not leak the View's
	json_text retain."""
	_build_and_check(tmp_path, _PARAMS_SOURCE, "params view")


def test_pub_error_context_encode_compact_no_leak(tmp_path: Path) -> None:
	"""`e.context.encode_compact()` must not leak the View's
	json_text retain — companion to the params view test."""
	_build_and_check(tmp_path, _CONTEXT_SOURCE, "context view")
