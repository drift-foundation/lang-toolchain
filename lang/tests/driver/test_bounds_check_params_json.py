# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression — runtime-emitted IndexError carries valid canonical
params JSON.

Slice 7a (0.31.62, 2026-05-05; K finding 3, 2026-05-05): the
`drift_bounds_check_fail` runtime helper builds the
`{"container_id":"...","index":N}` document by hand and hands it to
`drift_error_set_params_json`.  The first-pass implementation spliced
`container_id` directly into the JSON without escaping; in-tree
callers all pass stdlib container-id constants today, but it is a
runtime boundary producing canonical params JSON — the contract must
hold for any input.

These probes pin:

  1. **Sanity round-trip** — for the in-tree
     `std.containers:Array` container_id, `e.params.encode_compact()`
     returns valid JSON containing the expected key/value pair, and
     `e.container_id` typed projection matches.
  2. **Defense in depth** — a separate probe parses
     `e.encode_compact()` with `std.json.parse` to confirm the
     produced envelope is syntactically valid JSON.  This is the
     contract that would silently break under future maintenance if
     a caller passed a container_id with `"`, `\\`, or a control
     character.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _build_run(tmp_path: Path, source: str) -> tuple[int, str, str]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out_bin = tmp_path / "test_bin"
	env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
	env["PYTHONPATH"] = str(ROOT)
	build = subprocess.run(
		[
			sys.executable, "-m", "lang.driftc.driftc",
			"--stdlib-root", str(ROOT / "stdlib"),
			str(src),
			"--entry", "main::main",
			"-o", str(out_bin),
		],
		cwd=ROOT,
		capture_output=True,
		text=True,
		timeout=120,
		env=env,
	)
	if build.returncode != 0:
		return (build.returncode, build.stdout, build.stderr)
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=30)
	return (run.returncode, run.stdout, run.stderr)


def test_bounds_check_params_json_round_trip(tmp_path):
	"""For the array OOB path, `e.params.encode_compact()` returns
	the canonical `{"container_id":"std.containers:Array","index":3}`
	document; `e.container_id` and `e.index` typed projections
	round-trip the values."""
	source = """
module main;

import std.core as core;
import std.err;
import std.console as console;
import std.format as format;

fn main() nothrow -> Int {
\tval xs = [1, 2, 3];
\ttry {
\t\tval _v = xs[3];
\t\treturn 90;
\t} catch std.err:IndexError(e) {
\t\tval params: core.ErrorParamsView = e.params;
\t\tconsole.println(params.encode_compact());
\t\tconsole.println(e.container_id);
\t\tconsole.println(format.format_int(e.index));
\t\treturn 0;
\t}
}
"""
	rc, stdout, stderr = _build_run(tmp_path, source)
	assert rc == 0, f"rc={rc}\nstdout:\n{stdout}\nstderr:\n{stderr[:2000]}"
	lines = stdout.strip().splitlines()
	assert len(lines) == 3, f"expected 3 lines, got: {lines!r}"
	assert lines[0] == '{"container_id":"std.containers:Array","index":3}', (
		f"params JSON shape regressed: got {lines[0]!r}"
	)
	assert lines[1] == "std.containers:Array", (
		f"container_id typed projection regressed: got {lines[1]!r}"
	)
	assert lines[2] == "3", (
		f"index typed projection regressed: got {lines[2]!r}"
	)


def test_bounds_check_envelope_parses_as_valid_json(tmp_path):
	"""`e.encode_compact()` for the runtime-emitted IndexError must
	parse as valid JSON via `std.json.parse`.  Pins the JSON-shape
	invariant that future maintainers of the runtime helper would
	otherwise silently break by reintroducing an unescaped splice."""
	source = """
module main;

import std.err;
import std.json as json;

fn main() nothrow -> Int {
\tval xs = [1, 2, 3];
\ttry {
\t\tval _v = xs[3];
\t\treturn 90;
\t} catch std.err:IndexError(e) {
\t\tval envelope: String = e.encode_compact();
\t\tmatch json.parse(&envelope) {
\t\t\tOk(_) => { return 0; },
\t\t\tErr(_) => { return 1; }
\t\t}
\t}
}
"""
	rc, stdout, stderr = _build_run(tmp_path, source)
	assert rc == 0, (
		f"runtime-emitted IndexError envelope failed JSON parse: rc={rc}\n"
		f"stdout:\n{stdout}\nstderr:\n{stderr[:2000]}"
	)
