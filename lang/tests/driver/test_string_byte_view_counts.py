# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""string-view-performance §10: COUNT-EXACT retain/allocation evidence
for StringByteView via a `-Wl,--wrap` counting shim (the B5
custom-link technique).  Timing and memcheck cannot prove exact
counts; this harness can.

Wrapped symbols (all exported; `drift_string_new_copy` is static and
unwrappable by design — see the design record):
  drift_string_retain / drift_string_release  — refcount traffic;
  drift_string_from_utf8_bytes                — the to_string
      materialization entry (empty goes to the singleton, never here);
  drift_alloc_array / drift_free_array        — the allocator boxed
      callback environments go through.

Counting semantics: --wrap rewrites CROSS-TU references only, so the
counters see exactly the program-level (generated-IR) calls — runtime-
internal self-calls (e.g. concat's own release, drift_cb_env_free ->
drift_free_array inside array_runtime.c) are invisible.  Consequences:
  * retain / from_utf8 / alloc_arr counts are exact program-level
    operation counts — the load-bearing assertions;
  * release counts INCLUDE counted no-op releases (STATIC literals,
    tombstones from move machinery), so they are pinned only where
    they prove an obligation (the reads window);
  * callback-env FREES route through same-TU drift_cb_env_free and do
    not show up — env leak-freedom is the memcheck fixture's job.

Obligations proven (each op loops x100 so per-op exactness shows as
exact hundreds):
  * byte_view_all construction: EXACTLY 1 retain each (100/100), zero
    allocations;
  * dup + subview: EXACTLY 1 retain each (201 = base 1 + 100 + 100);
  * reads (2,100 Result byte_at reads + 200 searches per window —
    proving Result reads neither retain nor allocate): ZERO
    retains beyond the 1 base construction, ZERO allocations, and a
    CONSTANT release count (7 — fn-scope scaffolding only: nothing
    per-read);
  * nonempty to_string: EXACTLY ONE drift_string_from_utf8_bytes per
    call (100), one io-buffer alloc/free pair each;
  * empty to_string: ZERO from_utf8, ZERO allocs (the singleton);
  * with_view_bytes: ZERO from_utf8; EXACTLY ONE env allocation for
    the whole window (the capture-less user body does not box — only
    the capturing inner composition callback does);
  * forced-throw with_view_bytes_throw x100: retains stay at the 1
    base construction (NO leaked retain across unwind); the 100
    from_utf8 are the thrown error payloads, the 100 allocs the
    per-iteration envs (freed invisibly, see above);
  * regex String matching (is_match x100): retain DELTA vs
    compile-only is ZERO — the §11 zero-retain regression;
  * regex view matching (is_match_view x100): retain delta is exactly
    +1 (the single subject-view construction), matching itself adds
    none.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root
from lang.language_runtime import build_runtime_archive, runtime_archive_variant

ROOT = Path(__file__).resolve().parents[3]
RUNTIME = ROOT / "lang" / "language_runtime"

OPS_SRC = r"""
module main;

import std.core as core;
import std.text as text;
import std.regex as regex;
import std.mem as mem;

error Boom { at: Int }

pub fn mk_subject() nothrow -> String {
	var s = "count-";
	s = s + "subject-payload";   // heap-backed, non-static
	return move s;
}

pub fn op_construct(s: String) nothrow -> Int {
	var acc = 0;
	var k = 0;
	while k < 100 {
		val v = text.byte_view_all(s);
		acc = acc + v.byte_length();
		k = k + 1;
	}
	return acc;
}

pub fn op_dup_subview(s: String) nothrow -> Int {
	val base = text.byte_view_all(s);
	var acc = 0;
	var k = 0;
	while k < 100 {
		val d = base.dup();
		match base.subview(1, 4) {
			Ok(sv) => { acc = acc + sv.byte_length() + d.byte_length(); },
			Err(e) => { acc = acc - 1; }
		}
		k = k + 1;
	}
	return acc;
}

pub fn op_reads(s: String) nothrow -> Int {
	val v = text.byte_view_all(s);
	// needles hoisted so the loop body contains ONLY view reads (a
	// literal temp per iteration would add counted no-op releases).
	val nope = "nope";
	val pay = "payload";
	var acc = 0;
	var k = 0;
	while k < 100 {
		var i = 0;
		val n = v.byte_length();
		while i < n {
			match v.byte_at(i) {
				Ok(b) => { acc = acc + cast<Int>(b); },
				Err(e) => { return 0 - 1; }
			}
			i = i + 1;
		}
		if v.eq_string(nope) { acc = acc - 1; }
		acc = acc + v.index_of(pay);
		k = k + 1;
	}
	return acc;
}

pub fn op_to_string_nonempty(s: String) nothrow -> Int {
	val v = text.byte_view_all(s);
	var acc = 0;
	var k = 0;
	while k < 100 {
		val o = v.to_string();
		acc = acc + o.byte_length();
		k = k + 1;
	}
	return acc;
}

pub fn op_to_string_empty(s: String) nothrow -> Int {
	var v = text.byte_view_all(s);
	match v.subview(2, 0) {
		Ok(e) => { v = move e; },
		Err(x) => { return 0 - 1; }
	}
	var acc = 0;
	var k = 0;
	while k < 100 {
		val o = v.to_string();
		acc = acc + o.byte_length();
		k = k + 1;
	}
	return acc;
}

pub fn op_bulk(s: String) nothrow -> Int {
	val v = text.byte_view_all(s);
	val body: core.Callback2<mem.Ptr<Byte>, Int, Int> =
		core.callback2(|p: mem.Ptr<Byte>, n: Int| => {
			var acc = 0;
			var i = 0;
			while i < n {
				acc = acc + cast<Int>(mem.ptr_read<type Byte>(mem.ptr_offset<type Byte>(p, i)));
				i = i + 1;
			}
			acc
		});
	return text.with_view_bytes<type Int, core.Callback2<mem.Ptr<Byte>, Int, Int> >(v, move body);
}

pub fn op_throw_balance(s: String) nothrow -> Int {
	val v = text.byte_view_all(s);
	var caught = 0;
	var k = 0;
	while k < 100 {
		val body: core.CallbackThrow2<mem.Ptr<Byte>, Int, Int> =
			core.callback_throw2(|p: mem.Ptr<Byte>, n: Int| => {
				if n > 0 { throw Boom(at = n); }
				0
			});
		try {
			val x = text.with_view_bytes_throw<type Int, core.CallbackThrow2<mem.Ptr<Byte>, Int, Int> >(v, move body);
			caught = caught - 1000;
		} catch Boom(e) {
			caught = caught + 1;
		} catch {
			caught = caught - 1000;
		}
		k = k + 1;
	}
	return caught;
}

pub fn op_regex_compile_only(s: String) nothrow -> Int {
	match regex.compile("[a-z]+-[a-z]+") {
		Ok(re) => { return 1; },
		Err(e) => { return 0 - 1; }
	}
}

pub fn op_regex_match_string(s: String) nothrow -> Int {
	match regex.compile("[a-z]+-[a-z]+") {
		Ok(re) => {
			var acc = 0;
			var k = 0;
			while k < 100 {
				if regex.is_match(re, s) { acc = acc + 1; }
				k = k + 1;
			}
			return acc;
		},
		Err(e) => { return 0 - 1; }
	}
}

pub fn op_regex_match_view(s: String) nothrow -> Int {
	match regex.compile("[a-z]+-[a-z]+") {
		Ok(re) => {
			val v = text.byte_view_all(s);
			var acc = 0;
			var k = 0;
			while k < 100 {
				if regex.is_match_view(re, v) { acc = acc + 1; }
				k = k + 1;
			}
			return acc;
		},
		Err(e) => { return 0 - 1; }
	}
}

// Baseline for the conversion delta: compile + find, NO conversions.
pub fn op_match_find_only(s: String) nothrow -> Int {
	match regex.compile("[a-z]+-[a-z]+") {
		Ok(re) => {
			match regex.find_first(re, s) {
				Some(m) => { return m.end - m.start; },
				None() => { return 0 - 1; }
			}
		},
		Err(e) => { return 0 - 1; }
	}
}

// compile + find + 100 match_view + 100 match_subview: the retain
// DELTA vs op_match_find_only must be exactly 201 (100 + 100 + the
// one whole-view construction the subviews derive from).
pub fn op_match_conversions(s: String) nothrow -> Int {
	match regex.compile("[a-z]+-[a-z]+") {
		Ok(re) => {
			match regex.find_first(re, s) {
				Some(m) => {
					val whole = text.byte_view_all(s);
					var acc = 0;
					var k = 0;
					while k < 100 {
						match regex.match_view(m, s) {
							Ok(v) => { acc = acc + v.byte_length(); },
							Err(e) => { return 0 - 1; }
						}
						match regex.match_subview(m, whole) {
							Ok(v) => { acc = acc + v.byte_length(); },
							Err(e) => { return 0 - 1; }
						}
						k = k + 1;
					}
					return acc;
				},
				None() => { return 0 - 1; }
			}
		},
		Err(e) => { return 0 - 1; }
	}
}

// split_views: one retain PER ELEMENT ("count-subject-payload" on
// "-" -> exactly 3 elements -> exactly 3 retains).
pub fn op_split_views(s: String) nothrow -> Int {
	val parts = text.split_views(s, "-");
	return parts.len;
}

pub fn main() nothrow -> Int {
	val s = mk_subject();
	val keep = op_construct(s) + op_dup_subview(s) + op_reads(s)
		+ op_to_string_nonempty(s) + op_to_string_empty(s) + op_bulk(s)
		+ op_throw_balance(s) + op_regex_compile_only(s)
		+ op_regex_match_string(s) + op_regex_match_view(s)
		+ op_match_find_only(s) + op_match_conversions(s)
		+ op_split_views(s);
	if keep == 0 { return 1; }
	return 0;
}
"""

C_DRIVER = r"""
#include "string_runtime.h"
#include <stdio.h>
#include <string.h>
#include <stddef.h>

/* ── wrap counters (calls made from generated IR / cross-TU only) ── */
static long n_retain, n_release, n_from_utf8, n_alloc_arr, n_free_arr;

DriftString __real_drift_string_retain(DriftString s);
void __real_drift_string_release(DriftString s);
DriftString __real_drift_string_from_utf8_bytes(const char *p, drift_isize len);
void *__real_drift_alloc_array(size_t es, size_t ea, long len, long cap);
void __real_drift_free_array(void *p);

DriftString __wrap_drift_string_retain(DriftString s) { n_retain++; return __real_drift_string_retain(s); }
void __wrap_drift_string_release(DriftString s) { n_release++; __real_drift_string_release(s); }
DriftString __wrap_drift_string_from_utf8_bytes(const char *p, drift_isize len) { n_from_utf8++; return __real_drift_string_from_utf8_bytes(p, len); }
void *__wrap_drift_alloc_array(size_t es, size_t ea, long len, long cap) { n_alloc_arr++; return __real_drift_alloc_array(es, ea, len, cap); }
void __wrap_drift_free_array(void *p) { n_free_arr++; __real_drift_free_array(p); }

static void reset(void) { n_retain = n_release = n_from_utf8 = n_alloc_arr = n_free_arr = 0; }
static void report(const char *op) {
	printf("OP=%s retain=%ld release=%ld from_utf8=%ld alloc_arr=%ld free_arr=%ld\n",
		op, n_retain, n_release, n_from_utf8, n_alloc_arr, n_free_arr);
}

extern DriftString mk_subject(void);
extern long op_construct(DriftString);
extern long op_dup_subview(DriftString);
extern long op_reads(DriftString);
extern long op_to_string_nonempty(DriftString);
extern long op_to_string_empty(DriftString);
extern long op_bulk(DriftString);
extern long op_throw_balance(DriftString);
extern long op_regex_compile_only(DriftString);
extern long op_regex_match_string(DriftString);
extern long op_regex_match_view(DriftString);
extern long op_match_find_only(DriftString);
extern long op_match_conversions(DriftString);
extern long op_split_views(DriftString);

#define RUN(name) do { \
	DriftString a = drift_string_retain(subj); \
	reset(); \
	long r = name(a); \
	report(#name); \
	if (r == -1 || r <= -1000) { printf("OPFAIL=%s r=%ld\n", #name, r); return 70; } \
} while (0)

int main(void) {
	DriftString subj = mk_subject();
	RUN(op_construct);
	RUN(op_dup_subview);
	RUN(op_reads);
	RUN(op_to_string_nonempty);
	RUN(op_to_string_empty);
	RUN(op_bulk);
	RUN(op_throw_balance);
	RUN(op_regex_compile_only);
	RUN(op_regex_match_string);
	RUN(op_regex_match_view);
	RUN(op_match_find_only);
	RUN(op_match_conversions);
	RUN(op_split_views);
	drift_string_release(subj);
	printf("DONE\n");
	return 0;
}
"""

WRAPPED = ("drift_string_retain", "drift_string_release",
           "drift_string_from_utf8_bytes", "drift_alloc_array", "drift_free_array")

# (retain, from_utf8, alloc_arr) exact pins; None = not pinned.
EXPECTED = {
	"op_construct":          {"retain": 100, "from_utf8": 0,   "alloc_arr": 0},
	"op_dup_subview":        {"retain": 201, "from_utf8": 0,   "alloc_arr": 0},
	"op_reads":              {"retain": 1,   "from_utf8": 0,   "alloc_arr": 0, "release": 7},
	"op_to_string_nonempty": {"retain": 1,   "from_utf8": 100, "alloc_arr": 100, "free_arr": 100},
	"op_to_string_empty":    {"retain": 2,   "from_utf8": 0,   "alloc_arr": 0},
	"op_bulk":               {"retain": 1,   "from_utf8": 0,   "alloc_arr": 1},
	"op_throw_balance":      {"retain": 1,   "from_utf8": 100, "alloc_arr": 100},
}


@pytest.fixture(scope="module")
def ops_ll(tmp_path_factory) -> Path:
	tmp = tmp_path_factory.mktemp("view_counts")
	src = tmp / "main.drift"
	src.write_text(OPS_SRC)
	out_bin = tmp / "ops.bin"
	stdlib = stdlib_root() or (ROOT / "stdlib")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev", "--allow-unsafe",
		 "--stdlib-root", str(stdlib), str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	assert res.returncode == 0, f"{res.stdout}\n---\n{res.stderr[:2000]}"
	ir = Path(str(out_bin) + ".ll").read_text()
	ir = re.sub(r"define ([^@\n]*)@main\(", r"define \1@__drift_unused_main(", ir, count=1)
	patched = tmp / "ops_patched.ll"
	patched.write_text(ir)
	return patched


def test_view_operation_counts_exact(ops_ll: Path, tmp_path: Path) -> None:
	driver_c = tmp_path / "driver.c"
	driver_c.write_text(C_DRIVER)
	out = tmp_path / "counts.bin"
	archive = build_runtime_archive(ROOT, clang=shutil.which("clang"),
		variant=runtime_archive_variant(debug_style=False, asan_enabled=False, alloc_track_enabled=False))
	wraps = [f"-Wl,--wrap={s}" for s in WRAPPED]
	cmd = ["/usr/bin/clang", "-std=gnu11", "-pthread",
		"-x", "ir", str(ops_ll), "-x", "c", str(driver_c),
		"-x", "none", str(archive), *wraps, "-lz", "-Wl,--as-needed",
		"-I", str(RUNTIME), "-o", str(out)]
	res = subprocess.run(cmd, capture_output=True, text=True, timeout=sanitizer_timeout(240))
	assert res.returncode == 0, f"link failed:\n{res.stderr[:2000]}"

	run = subprocess.run([str(out)], capture_output=True, text=True,
		timeout=sanitizer_timeout(120))
	assert run.returncode == 0, f"driver rc={run.returncode}\n{run.stdout}\n{run.stderr[:500]}"
	assert "DONE" in run.stdout and "OPFAIL" not in run.stdout, run.stdout

	counts: dict[str, dict[str, int]] = {}
	for line in run.stdout.splitlines():
		m = re.match(r"OP=(\w+) retain=(\d+) release=(\d+) from_utf8=(\d+) alloc_arr=(\d+) free_arr=(\d+)", line)
		if m:
			counts[m.group(1)] = {
				"retain": int(m.group(2)), "release": int(m.group(3)),
				"from_utf8": int(m.group(4)), "alloc_arr": int(m.group(5)),
				"free_arr": int(m.group(6)),
			}

	for op, expected in EXPECTED.items():
		assert op in counts, f"missing {op} in driver output:\n{run.stdout}"
		for key, val in expected.items():
			assert counts[op][key] == val, (
				f"{op}.{key}: expected {val}, got {counts[op][key]} — "
				f"full row {counts[op]}"
			)

	# split_views: EXACTLY one retain per element (3 for this subject).
	assert counts["op_split_views"]["retain"] == 3, counts["op_split_views"]
	assert counts["op_split_views"]["from_utf8"] == 0, counts["op_split_views"]

	# match_view/match_subview: EXACTLY one retain each, by SUBTRACTION
	# against the compile+find baseline (100 + 100 + 1 whole-view).
	conv_delta = (counts["op_match_conversions"]["retain"]
	              - counts["op_match_find_only"]["retain"])
	assert conv_delta == 201, (
		f"conversion retain delta {conv_delta}: "
		f"{counts['op_match_conversions']} vs {counts['op_match_find_only']}"
	)
	assert (counts["op_match_conversions"]["from_utf8"]
	        == counts["op_match_find_only"]["from_utf8"]), "conversions must not materialize"

	# §11 zero-retain regressions by SUBTRACTION (compile noise cancels):
	base = counts["op_regex_compile_only"]["retain"]
	assert counts["op_regex_match_string"]["retain"] - base == 0, (
		"String matching (x100) must add ZERO retains: "
		f"{counts['op_regex_match_string']} vs {counts['op_regex_compile_only']}"
	)
	assert counts["op_regex_match_view"]["retain"] - base == 1, (
		"view matching (x100) must add exactly the ONE subject-view retain: "
		f"{counts['op_regex_match_view']} vs {counts['op_regex_compile_only']}"
	)
