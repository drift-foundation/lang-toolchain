# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""B-repr(B5) §2.6 observation contract at the COMPILER's three
layout-authority observation lowerings (StringLen / StringByteAt /
StringBytesBase): the emitted `__drift_string_observe_guard` must fail
closed on the reserved all-zero tombstone, on malformed handles, AND on
the pinned illegal-flag states (reserved bits, STATIC+IMMORTAL,
HAS_INTERIOR_NUL without NUL_SCANNED) BEFORE any length/storage use — `byte_length()` may never read a
tombstone as 0, and `with_bytes`-style code may never receive the bogus
`NULL + 16` pointer.

Valid Drift source cannot construct such handles (that is the point),
so these are LINK-DRIVEN teeth: a Drift module exports observer
functions (compiled by the real driver, so the REAL lowerings + guard
are in the IR), its `@main` is renamed, and a C driver fabricates
tombstone / malformed handles and calls the observers through their
mangled symbols.  Run against the runtime built in BOTH configurations
(normal and NDEBUG/optimized) — the guard calls `drift_contract_fail`,
which is unconditional in both.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root
from lang.language_runtime import build_runtime_archive, runtime_archive_variant

ROOT = Path(__file__).resolve().parents[3]
RUNTIME = ROOT / "lang" / "language_runtime"

BUILD_MODES = {
	"normal": [],
	"ndebug": ["-DNDEBUG", "-O2"],
}

DRIFT_SRC = r"""module main;

import std.core as core;
import std.ffi as ffi;
import std.mem as mem;

pub fn observe_len(s: String) nothrow -> Int {
	return s.byte_length();
}

pub fn observe_byte(s: String) nothrow -> Int {
	return cast<Int>(core.string_byte_at(s, 0));
}

pub fn observe_bytes_base(s: String) nothrow -> Int {
	// The callback body never runs for bad handles — the observation
	// guard aborts BEFORE the bytes-base pointer is computed.
	val cb: core.Callback2<mem.Ptr<Byte>, Int, Int> =
		core.callback2(|p: mem.Ptr<Byte>, len: Int| => { len + 1 });
	return ffi.with_bytes<type Int, core.Callback2<mem.Ptr<Byte>, Int, Int> >(s, cb);
}

pub fn main() nothrow -> Int {
	// Anchor the observers in the reachability set (this main is renamed
	// out of the way before linking; the C driver never calls it).
	val keep = observe_len("k") + observe_byte("k") + observe_bytes_base("k");
	if keep == 0 { return 1; }
	return 0;
}
"""

C_DRIVER = r"""
#include "string_runtime.h"
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

/* main-module pub fns carry their plain names in the IR */
extern long observe_len(struct DriftString);
extern long observe_byte(struct DriftString);
extern long observe_bytes_base(struct DriftString);

int main(int argc, char **argv) {
	if (argc < 3) return 90;
	const char *which = argv[1];
	const char *handle = argv[2];

	struct DriftString s;
	if (strcmp(handle, "tombstone") == 0) {
		s.len = 0; s.storage = NULL;
	} else if (strcmp(handle, "malformed") == 0) {
		s.len = 3; s.storage = NULL;
	} else if (strcmp(handle, "negative") == 0) {
		s = drift_string_from_cstr("neg");
		s.len = -1;
	} else if (strcmp(handle, "negative_badptr") == 0) {
		/* NEGATIVE length + deliberately INVALID non-NULL storage: the
		 * guard must report the negative-length contract failure
		 * BEFORE dereferencing storage (a flags load from this pointer
		 * would fault). */
		s.len = -7;
		s.storage = (struct DriftRcBytes *)0x8;
	} else if (strcmp(handle, "valid") == 0) {
		s = drift_string_from_cstr("ok!");
	} else if (strcmp(handle, "flags_reserved") == 0) {
		s = drift_string_from_cstr("ok!");
		atomic_store_explicit(&s.storage->flags, 1ULL << 33, memory_order_relaxed);
	} else if (strcmp(handle, "flags_static_immortal") == 0) {
		s = drift_string_from_cstr("ok!");
		atomic_store_explicit(&s.storage->flags,
			DRIFT_RCBYTES_STATIC | DRIFT_RCBYTES_IMMORTAL, memory_order_relaxed);
	} else if (strcmp(handle, "flags_orphan") == 0) {
		s = drift_string_from_cstr("ok!");
		atomic_store_explicit(&s.storage->flags,
			DRIFT_RCBYTES_HAS_INTERIOR_NUL, memory_order_relaxed);
	} else {
		return 91;
	}

	long r = -1;
	if (strcmp(which, "len") == 0) r = observe_len(s);
	else if (strcmp(which, "byte") == 0) r = observe_byte(s);
	else if (strcmp(which, "bytes_base") == 0) r = observe_bytes_base(s);
	else return 92;
	printf("RESULT=%ld\n", r);
	return 0;
}
"""


@pytest.fixture(scope="module")
def observers_ll(tmp_path_factory) -> Path:
	tmp = tmp_path_factory.mktemp("obs_guards")
	src = tmp / "main.drift"
	src.write_text(DRIFT_SRC)
	out_bin = tmp / "obs.bin"
	stdlib = stdlib_root() or (ROOT / "stdlib")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev", "--allow-unsafe",
		 "--stdlib-root", str(stdlib), str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	assert res.returncode == 0, f"{res.stdout}\n---\n{res.stderr[:2000]}"
	ll = Path(str(out_bin) + ".ll")
	ir = ll.read_text()
	# The guard must be present and wired at all three lowerings.
	assert "__drift_string_observe_guard" in ir
	# Hand `main` to the C driver.
	ir = re.sub(r"define ([^@\n]*)@main\(", r"define \1@__drift_unused_main(", ir, count=1)
	patched = tmp / "observers.ll"
	patched.write_text(ir)
	return patched


@pytest.mark.parametrize("mode", sorted(BUILD_MODES))
def test_observation_guards_fail_closed_both_builds(observers_ll: Path, tmp_path: Path, mode: str) -> None:
	driver_c = tmp_path / "driver.c"
	driver_c.write_text(C_DRIVER)
	out = tmp_path / f"obs_{mode}.bin"
	# String runtime compiled with THIS mode's flags as loose objects —
	# the linker prefers them over the archive members, so the guarded
	# path runs against the normal AND the NDEBUG/optimized runtime; the
	# full archive supplies everything else (ABI stamp, atomics,
	# callbacks, thread runtime).
	sr_o = tmp_path / f"string_runtime_{mode}.o"
	ryu_o = tmp_path / f"ryu_{mode}.o"
	for c_src, obj in ((RUNTIME / "string_runtime.c", sr_o), (RUNTIME / "ryu_d2s.c", ryu_o)):
		cres = subprocess.run(
			["/usr/bin/clang", "-std=gnu11", *BUILD_MODES[mode], "-I", str(RUNTIME),
			 "-c", str(c_src), "-o", str(obj)],
			capture_output=True, text=True, timeout=sanitizer_timeout(120))
		assert cres.returncode == 0, f"runtime compile failed ({mode}):\n{cres.stderr[:1000]}"
	import shutil as _shutil
	archive = build_runtime_archive(ROOT, clang=_shutil.which("clang"),
		variant=runtime_archive_variant(debug_style=False, asan_enabled=False, alloc_track_enabled=False))
	cmd = ["/usr/bin/clang", "-std=gnu11", "-pthread", *BUILD_MODES[mode],
		"-x", "ir", str(observers_ll), "-x", "c", str(driver_c),
		"-x", "none", str(sr_o), str(ryu_o), str(archive),
		"-lz", "-Wl,--as-needed",
		"-I", str(RUNTIME), "-o", str(out)]
	res = subprocess.run(cmd, capture_output=True, text=True, timeout=sanitizer_timeout(240))
	assert res.returncode == 0, f"link failed ({mode}):\n{res.stderr[:2000]}"

	def run(which: str, handle: str):
		return subprocess.run([str(out), which, handle], capture_output=True,
			text=True, timeout=sanitizer_timeout(60))

	# Valid handles: observers behave normally through the guard.
	ok = run("len", "valid")
	assert ok.returncode == 0 and "RESULT=3" in ok.stdout, f"[{mode}] len/valid: {ok.stdout!r} {ok.stderr[:300]}"
	ok = run("byte", "valid")
	assert ok.returncode == 0 and "RESULT=111" in ok.stdout, f"[{mode}] byte/valid: {ok.stdout!r}"  # 'o'
	ok = run("bytes_base", "valid")
	assert ok.returncode == 0 and "RESULT=4" in ok.stdout, f"[{mode}] bytes_base/valid: {ok.stdout!r}"

	# Tombstone + malformed + negative: EVERY observer aborts with the
	# contract message BEFORE producing a value.
	for which in ("len", "byte", "bytes_base"):
		for handle, needle in (
			("tombstone", "String tombstone observed"),
			("malformed", "nonzero len, NULL storage"),
			("negative", "negative len"),
			("negative_badptr", "negative len"),
			("flags_reserved", "reserved bit set"),
			("flags_static_immortal", "STATIC+IMMORTAL"),
			("flags_orphan", "HAS_INTERIOR_NUL without NUL_SCANNED"),
		):
			r = run(which, handle)
			assert r.returncode != 0, f"[{mode}] {which}/{handle}: observed a bad handle (rc=0, {r.stdout!r})"
			assert "RESULT=" not in r.stdout, f"[{mode}] {which}/{handle}: produced a value {r.stdout!r}"
			assert needle in r.stderr, f"[{mode}] {which}/{handle}: missing {needle!r}:\n{r.stderr[:400]}"
