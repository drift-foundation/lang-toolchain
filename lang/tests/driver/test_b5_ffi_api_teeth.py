# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""B-repr(B5) §7 API teeth over the SHIPPED std.ffi surface:

  * `with_cstr{2,4}` validate LEFT-TO-RIGHT and report BOTH the 1-based
    argument ordinal and the byte index of the first interior NUL;
  * canonical empty String: len 0, non-null NUL bytes base, C-string
    conversion succeeds (zero-copy borrowed base reads as "");
  * borrowed pointers MAY syntactically escape (mem.Ptr is Copy) — the
    escape COMPILES; validity ends with the callback (documented
    unsafe, no checker claim) — decision 8;
  * OwnedCStr / OwnedCBytes: release-then-drop is safe (no double
    free); drop-only paths free via their PAIRED deallocators; both
    release() paths are exercised and the released blocks are freed
    with `cstr_free` / `cbytes_free` (real paired frees — the
    leak-sensitive dimension is pinned by the memcheck-lane sibling
    lang/tests/memcheck/test_b5_owned_types_lifecycle.py);
  * CStringScope.argv reports the failing ELEMENT ordinal; scope
    cleanup frees pins on exit (clean run under the normal lane;
    memcheck covers the leak dimension in the memcheck battery);
  * owned-copy isolation (decision 7): the released copy is ACTUALLY
    MUTATED and the source String proven unchanged.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]

SRC = r"""module main;

import std.console as console;
import std.core as core;
import std.ffi as ffi;
import std.mem as mem;

fn code_of(r: core.Result<Int, ffi.CStringError>) nothrow -> Int {
	// Encodes Ok(v) as v, Err(InteriorNul(arg, index)) as 1000 + arg*100 + index.
	return match r {
		core.Result::Ok(v) => { v },
		core.Result::Err(e) => {
			match e { ffi.CStringError::InteriorNul(arg, index) => { 1000 + arg * 100 + index }, }
		},
	};
}

pub fn main() nothrow -> Int {
	val ok = "ok";
	val bad1 = "x\x00y";
	val bad2 = "clean-then\x00";

	// Callbacks are move-only: one binding per call site.
	val cb2a: core.Callback2<mem.Ptr<Byte>, mem.Ptr<Byte>, Int> =
		core.callback2(|p: mem.Ptr<Byte>, q: mem.Ptr<Byte>| => { 7 });
	val cb2b: core.Callback2<mem.Ptr<Byte>, mem.Ptr<Byte>, Int> =
		core.callback2(|p: mem.Ptr<Byte>, q: mem.Ptr<Byte>| => { 7 });
	val cb2c: core.Callback2<mem.Ptr<Byte>, mem.Ptr<Byte>, Int> =
		core.callback2(|p: mem.Ptr<Byte>, q: mem.Ptr<Byte>| => { 7 });

	// LEFT-TO-RIGHT: first failing argument wins, ordinal + byte index.
	val a = code_of(ffi.with_cstr2<type Int, core.Callback2<mem.Ptr<Byte>, mem.Ptr<Byte>, Int> >(&bad1, &bad2, cb2a));
	if a != 1101 { console.println("FAIL lr-first"); return 1; }        // arg 1, index 1
	val b = code_of(ffi.with_cstr2<type Int, core.Callback2<mem.Ptr<Byte>, mem.Ptr<Byte>, Int> >(&ok, &bad2, cb2b));
	if b != 1210 { console.println("FAIL lr-second"); return 2; }       // arg 2, index 10
	val c = code_of(ffi.with_cstr2<type Int, core.Callback2<mem.Ptr<Byte>, mem.Ptr<Byte>, Int> >(&ok, &ok, cb2c));
	if c != 7 { console.println("FAIL lr-ok"); return 3; }

	val cb4: core.Callback4<mem.Ptr<Byte>, mem.Ptr<Byte>, mem.Ptr<Byte>, mem.Ptr<Byte>, Int> =
		core.callback4(|p: mem.Ptr<Byte>, q: mem.Ptr<Byte>, r: mem.Ptr<Byte>, s: mem.Ptr<Byte>| => { 9 });
	val d = code_of(ffi.with_cstr4<type Int, core.Callback4<mem.Ptr<Byte>, mem.Ptr<Byte>, mem.Ptr<Byte>, mem.Ptr<Byte>, Int> >(&ok, &ok, &ok, &bad1, cb4));
	if d != 1401 { console.println("FAIL lr-fourth"); return 4; }       // arg 4, index 1

	// Canonical empty: zero-copy C string reads as "" (base -> NUL).
	val empty = "";
	val cbe: core.Callback1<mem.Ptr<Byte>, Int> =
		core.callback1(|p: mem.Ptr<Byte>| => {
			val b0 = mem.ptr_read<type Byte>(p);
			cast<Int>(b0)
		});
	val e = code_of(ffi.with_cstr<type Int, core.Callback1<mem.Ptr<Byte>, Int> >(&empty, cbe));
	if e != 0 { console.println("FAIL empty-cstr"); return 5; }
	if empty.byte_length() != 0 { console.println("FAIL empty-len"); return 6; }

	// Escape is SYNTACTICALLY allowed (Ptr is Copy): storing the borrowed
	// pointer compiles; it is documented-invalid after return (not used here).
	val cbp: core.Callback1<mem.Ptr<Byte>, mem.Ptr<Byte> > =
		core.callback1(|p: mem.Ptr<Byte>| => { p });
	var escaped = ffi.with_cstr_unsafe<type mem.Ptr<Byte>, core.Callback1<mem.Ptr<Byte>, mem.Ptr<Byte> > >(&ok, cbp);
	val esc_null = mem.ptr_is_null<type Byte>(escaped);
	if esc_null { console.println("FAIL escape-null"); return 7; }

	// OwnedCStr: release-then-drop is safe; the released block is freed
	// with the PAIRED deallocator (real paired-free coverage).
	match ffi.to_owned_cstr(&ok) {
		core.Result::Ok(o) => {
			var owned = move o;
			val raw = owned.release();
			val b0 = mem.ptr_read<type Byte>(raw);
			if cast<Int>(b0) != 111 { console.println("FAIL owned-byte"); return 8; }  // 'o'
			ffi.cstr_free(raw);
		},
		core.Result::Err(e2) => { console.println("FAIL owned-err"); return 9; },
	}

	// to_owned_cstr yields an OWNED COPY: mutating the copy must not
	// affect the source String (decision 7 copy-isolation, actually
	// mutated here).
	val iso_src = "copy-me";
	match ffi.to_owned_cstr(&iso_src) {
		core.Result::Ok(o2) => {
			var owned2 = move o2;
			val raw2 = owned2.release();
			mem.ptr_write<type Byte>(raw2, core.string_byte_at("X", 0));
			val mutated = mem.ptr_read<type Byte>(raw2);
			val src_b0 = core.string_byte_at(iso_src, 0);
			if cast<Int>(mutated) != 88 { console.println("FAIL iso-mutate"); return 14; }   // 'X'
			if cast<Int>(src_b0) != 99 { console.println("FAIL iso-source"); return 15; }    // 'c' unchanged
			ffi.cstr_free(raw2);
		},
		core.Result::Err(e6) => { console.println("FAIL iso-err"); return 16; },
	}

	// OwnedCBytes: drop-only path (no release) — destructor frees via the
	// PAIRED cbytes deallocator.
	var ob = ffi.to_owned_cbytes(&bad1);
	val view = ob.get();
	if view.size() != 3 { console.println("FAIL cbytes-size"); return 10; }
	val mid = mem.ptr_read<type Byte>(mem.ptr_offset<type Byte>(view.data(), 1));
	if cast<Int>(mid) != 0 { console.println("FAIL cbytes-interior"); return 11; }

	// OwnedCBytes: RELEASE path — receiver frees with the paired
	// cbytes_free (real paired-free coverage).
	var ob2 = ffi.to_owned_cbytes(&ok);
	val rel = ob2.release();
	if rel.size() != 2 { console.println("FAIL cbytes-rel-size"); return 17; }
	val rb0 = mem.ptr_read<type Byte>(rel.data());
	if cast<Int>(rb0) != 111 { console.println("FAIL cbytes-rel-byte"); return 18; }  // 'o'
	ffi.cbytes_free(rel);

	// Scope: argv element ordinal on failure; success path counts.
	val cbs: core.Callback1<&mut ffi.CStringScope, Int> =
		core.callback1(|sc: &mut ffi.CStringScope| => {
			val bad_av = match sc.argv(&["fine", "al\x00so", "x"]) {
				core.Result::Ok(v) => { 0 },
				core.Result::Err(e3) => {
					match e3 { ffi.CStringError::InteriorNul(arg, index) => { 1000 + arg * 100 + index }, }
				},
			};
			val good_av = match sc.argv(&["a", "bc"]) {
				core.Result::Ok(v) => { v.count() },
				core.Result::Err(e4) => { -1 },
			};
			val pin = match sc.cstr(&"pinned") { core.Result::Ok(p) => { 1 }, core.Result::Err(e5) => { 0 }, };
			bad_av * 10 + good_av + pin
		});
	val s = ffi.with_cstring_scope<type Int, core.Callback1<&mut ffi.CStringScope, Int> >(cbs);
	if s != 12023 { console.println("FAIL scope"); return 12; }         // 1202*10 + 2 + 1

	console.println("API-TEETH-OK");
	return 0;
}
"""


def test_b5_ffi_api_teeth(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(SRC)
	out_bin = tmp_path / "bin"
	stdlib = stdlib_root() or (ROOT / "stdlib")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev", "--allow-unsafe",
		 "--stdlib-root", str(stdlib), str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	assert res.returncode == 0, f"{res.stdout}\n---\n{res.stderr[:3000]}"
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=sanitizer_timeout(60))
	assert run.returncode == 0 and "API-TEETH-OK" in run.stdout, (
		f"exit={run.returncode}\n{run.stdout}\n{run.stderr[:800]}"
	)
