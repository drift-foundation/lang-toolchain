# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""string-view-performance §10 lifetime pins, valgrind-clean end to
end:

  * views built into an Array OUTLIVE their source binding's scope
    (backing retained; reads after the original String is gone);
  * dup/subview retain exactly once (imbalance would surface as a
    leak or an underflow abort here);
  * forced-throw with_view_bytes_throw leaks neither retains nor
    boxed callback environments across 50 unwinds (the count harness
    cannot see env frees — same-TU drift_cb_env_free — so THIS is the
    env-balance proof);
  * split_views results outlive the split subject;
  * regex match_view/match_subview results outlive their subjects.

All backings are HEAP-BACKED (runtime concat) — a literal backing is
STATIC/immortal, where retain/release are no-ops and every lifetime
pin here would pass vacuously."""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout, valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]

SOURCE = r"""module main;

import std.console as console;
import std.core as core;
import std.text as text;
import std.regex as regex;
import std.mem as mem;

error Boom { at: Int }

fn heap(prefix: &String, tail: &String) nothrow -> String {
	var s = prefix.clone();
	s = s + *tail;
	return move s;
}

pub fn main() nothrow -> Int {
	// 1. Views outlive their source binding's scope.
	var held: Array<text.StringByteView> = [];
	{
		val tmp = heap("dyn-", "backing-alpha,beta,gamma");
		held.push(text.byte_view_all(tmp));
		match text.byte_view(tmp, 4, 13) {
			Ok(v) => { held.push(move v); },
			Err(e) => { return 1; }
		}
		val d = held[0].dup();
		match d.subview(4, 7) {
			Ok(sv) => { held.push(move sv); },
			Err(e) => { return 2; }
		}
	}
	if not held[1].eq_string("backing-alpha") { return 3; }
	if not held[2].eq_string("backing") { return 4; }

	// 2. Forced-throw window balance (env + retain), 50 unwinds.
	val subj = heap("throw-", "subject");
	val v = text.byte_view_all(subj);
	var caught = 0;
	var k = 0;
	while k < 50 {
		val body: core.CallbackThrow2<mem.Ptr<Byte>, Int, Int> =
			core.callback_throw2(|p: mem.Ptr<Byte>, n: Int| => {
				if n > 0 { throw Boom(at = n); }
				0
			});
		try {
			val x = text.with_view_bytes_throw<type Int, core.CallbackThrow2<mem.Ptr<Byte>, Int, Int> >(v, move body);
			return 5;
		} catch Boom(e) {
			caught = caught + 1;
		} catch {
			return 6;
		}
		k = k + 1;
	}
	if caught != 50 { return 7; }

	// 3. split_views results outlive the split subject.
	var fields: Array<text.StringByteView> = [];
	{
		val csv = heap("a1,", "b22,c333,");
		fields = text.split_views(csv, ",");
	}
	if fields.len != 4 { return 8; }
	if not fields[2].eq_string("c333") { return 9; }
	if not fields[3].is_empty() { return 10; }

	// 4. regex match views outlive their subjects.
	var mv = text.byte_view_all("seed");
	{
		val hay = heap("xx-", "alpha77-yy");
		match regex.compile("[a-z]+[0-9]+") {
			Ok(re) => {
				match regex.find_first(re, hay) {
					Some(m) => {
						match regex.match_view(m, hay) {
							Ok(x) => { mv = move x; },
							Err(e) => { return 11; }
						}
					},
					None() => { return 12; }
				}
			},
			Err(e) => { return 13; }
		}
	}
	if not mv.eq_string("alpha77") { return 14; }

	console.println("VIEW-LIFETIME-OK");
	return 0;
}
"""


def test_string_byte_view_lifetime_valgrind_clean(tmp_path: Path) -> None:
	assert shutil.which("valgrind") is not None, "valgrind required"

	src = tmp_path / "main.drift"
	src.write_text(SOURCE)
	out_bin = tmp_path / "view_lifetime_bin"

	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev", "--allow-unsafe",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	assert res.returncode == 0, f"compile failed: {res.stdout}\n{res.stderr[:2000]}"

	vg_log = tmp_path / "valgrind.log"
	vg = subprocess.run(
		valgrind_cmd("--leak-check=full",
			"--show-leak-kinds=definite,indirect",
			"--errors-for-leak-kinds=definite,indirect",
			"--error-exitcode=97",
			f"--log-file={vg_log}",
			str(out_bin)),
		capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	vg_output = vg_log.read_text() if vg_log.exists() else ""
	assert "VIEW-LIFETIME-OK" in vg.stdout, f"program failed under valgrind: {vg.stdout!r} {vg.stderr[:400]}"
	assert vg.returncode == 0, f"valgrind found errors:\n{vg_output[-2500:]}"
	assert len(re.findall(r"Invalid (read|write|free)", vg_output)) == 0, vg_output[-2500:]
	assert re.search(r"definitely lost: 0 bytes", vg_output) or "no leaks are possible" in vg_output, (
		vg_output[-2500:]
	)
