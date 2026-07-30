# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Boundary pins for the std.json default nesting cap (2026-07-27).

The iterative parser holds one HEAP frame per open container, so nesting
depth no longer costs fiber-stack frames — a deeply nested client
document can no longer drive a serve fiber into its guard page.  As
defense-in-depth against unbounded parser MEMORY, the standard profiles
carry a finite default `max_depth = 128`; `None` is the documented opt-in
for unbounded depth.

These pins nail the exact boundary (128 accepted, 129 → `limit-depth`)
and prove the unbounded opt-in parses far past the default cap WITHOUT a
crash (the iterative parser's whole point — deep input returns cleanly or
succeeds, never SIGSEGV).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]

SRC = r"""
module main;

import std.json as json;
import std.console as cons;
import std.core as core;
import std.format as fmt;

// A JSON array nested `depth` levels deep: `depth` '[' then `depth` ']'.
fn nested(depth: Int) nothrow -> String {
	var s = "";
	var i = 0;
	while i < depth { s = s + "["; i = i + 1; }
	var j = 0;
	while j < depth { s = s + "]"; j = j + 1; }
	return move s;
}

// 0 = Ok, 1 = limit-depth error, 2 = any other error.
fn outcome(s: &String, cfg: &json.JsonParseConfig) nothrow -> Int {
	match json.parse_with_config(s, cfg) {
		core.Result::Ok(_n) => { return 0; },
		core.Result::Err(e) => {
			if e.tag == "limit-depth" { return 1; }
			return 2;
		}
	}
}

fn unlimited_cfg() nothrow -> json.JsonParseConfig {
	var b = json.parse_config_builder();
	val lim = json.JsonLimits(
		max_document_bytes = Optional<Int>::None(),
		max_depth = Optional<Int>::None(),
		max_string_bytes = Optional<Int>::None(),
		max_number_bytes = Optional<Int>::None(),
		max_array_items = Optional<Int>::None(),
		max_object_fields = Optional<Int>::None()
	);
	b.limits(lim);
	match b.build() {
		core.Result::Ok(c) => { return move c; },
		core.Result::Err(_e) => { return json.permissive(); }
	}
}

pub fn main() nothrow -> Int {
	val def = json.permissive();          // default max_depth = 128
	val at_cap = nested(128);
	val over_cap = nested(129);
	cons.println("at128=" + fmt.format_int(outcome(at_cap, def)));
	cons.println("over129=" + fmt.format_int(outcome(over_cap, def)));

	// Unbounded opt-in (max_depth = None): 512 deep parses cleanly on the
	// default fiber stack — heap frames, not native frames.
	val ucfg = unlimited_cfg();
	val deep = nested(512);
	cons.println("deep512=" + fmt.format_int(outcome(deep, ucfg)));
	// And the default cap still rejects that same deep input.
	cons.println("deep512_default=" + fmt.format_int(outcome(deep, def)));
	return 0;
}
"""


def test_default_depth_cap_boundary_and_unbounded_optin(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(SRC)
	out_bin = tmp_path / "depth.bin"
	stdlib = stdlib_root() or (ROOT / "stdlib")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(stdlib), str(src), "--entry", "main::main",
		 "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(240))
	assert res.returncode == 0, f"compile failed:\n{res.stdout}\n{res.stderr[-2000:]}"
	run = subprocess.run([str(out_bin)], capture_output=True, text=True,
	                     timeout=sanitizer_timeout(120))
	assert run.returncode == 0, f"rc={run.returncode}\n{run.stdout}\n{run.stderr[:800]}"
	lines = run.stdout.splitlines()
	# Exact boundary: 128 accepted, 129 rejected as limit-depth.
	assert "at128=0" in lines, run.stdout          # depth == cap → Ok
	assert "over129=1" in lines, run.stdout         # depth == cap+1 → limit-depth
	# Unbounded opt-in parses 512 deep cleanly (no crash, no limit error)…
	assert "deep512=0" in lines, run.stdout
	# …while the default cap still rejects the same deep input.
	assert "deep512_default=1" in lines, run.stdout
