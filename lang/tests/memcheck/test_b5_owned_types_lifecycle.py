# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""B-repr(B5) §7 leak-sensitive lifecycle coverage for the std.ffi owned
types, valgrind-clean end to end:

  * OwnedCStr:  drop-only (destructor frees via drift_cstr_free) AND
    release-then-paired-free (`cstr_free`) AND release-then-drop (the
    drop must NOT double-free the released block);
  * OwnedCBytes: drop-only (destructor frees via the PAIRED
    drift_cbytes_free) AND release-then-paired-free (`cbytes_free`);
  * CStringScope: checked + unchecked pins and an argv vector, all
    freed at scope exit (cstr-family pins via drift_cstr_free, argv
    vectors via mem.dealloc).

Zero definite/indirect leaks and zero invalid accesses prove the
allocator PAIRING is real (to_owned_cbytes <-> cbytes_free;
to_owned_cstr/_unchecked <-> cstr_free), not merely coincidentally
compatible."""
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
import std.ffi as ffi;
import std.mem as mem;

pub fn main() nothrow -> Int {
	val s = "lifecycle";
	val nul = "with\x00nul";

	// OwnedCStr drop-only: destructor frees.
	match ffi.to_owned_cstr(s) {
		core.Result::Ok(o) => { var owned = move o; },
		core.Result::Err(e) => { return 1; },
	}

	// OwnedCStr release -> paired free; the drop after release must
	// NOT double-free.
	match ffi.to_owned_cstr(s) {
		core.Result::Ok(o) => {
			var owned = move o;
			val raw = owned.release();
			ffi.cstr_free(raw);
		},
		core.Result::Err(e) => { return 2; },
	}

	// OwnedCBytes drop-only: destructor frees via the PAIRED cbytes free.
	var ob = ffi.to_owned_cbytes(nul);
	val v = ob.get();
	if v.size() != 8 { return 3; }

	// OwnedCBytes release -> paired free.
	var ob2 = ffi.to_owned_cbytes(s);
	val rel = ob2.release();
	ffi.cbytes_free(rel);

	// Scope: checked pin + unchecked pin (interior NUL preserved) +
	// argv vector — everything freed at scope exit.
	val cbs: core.Callback1<&mut ffi.CStringScope, Int> =
		core.callback1(|sc: &mut ffi.CStringScope| => {
			val p1 = match sc.cstr("pin-a") { core.Result::Ok(p) => { 1 }, core.Result::Err(e) => { 0 }, };
			val p2u = sc.cstr_unsafe("un\x00safe");
			val p2 = mem.ptr_is_null<type Byte>(p2u);
			val av = match sc.argv(["x", "yz", "argv-elem"]) {
				core.Result::Ok(a) => { a.count() },
				core.Result::Err(e) => { -1 },
			};
			val p2i = 0;
			p1 + av
		});
	val scoped = ffi.with_cstring_scope<type Int, core.Callback1<&mut ffi.CStringScope, Int> >(cbs);
	if scoped != 4 { return 4; }

	console.println("LIFECYCLE-OK");
	return 0;
}
"""


def test_b5_owned_types_lifecycle_valgrind_clean(tmp_path: Path) -> None:
	assert shutil.which("valgrind") is not None, "valgrind required"

	src = tmp_path / "main.drift"
	src.write_text(SOURCE)
	out_bin = tmp_path / "lifecycle_bin"

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
	assert "LIFECYCLE-OK" in vg.stdout, f"program failed under valgrind: {vg.stdout!r} {vg.stderr[:400]}"
	assert vg.returncode == 0, f"valgrind found errors:\n{vg_output[-2500:]}"
	assert len(re.findall(r"Invalid (read|write|free)", vg_output)) == 0, vg_output[-2500:]
	assert re.search(r"definitely lost: 0 bytes", vg_output) or "no leaks are possible" in vg_output, (
		vg_output[-2500:]
	)
