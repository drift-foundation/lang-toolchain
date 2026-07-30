# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""regex-engine-allocation-removal: COUNT-EXACT allocation teeth for
the std.regex packed-workspace executor via a `-Wl,--wrap` counting
shim (same mechanism as test_string_byte_view_counts.py).

Steady-state contract pinned (checkpoint §3-§4):
  * ONE top-level search (`find_first` / `is_match` /
    `find_first_view` / one `_find_from` invocation) performs EXACTLY
    1 REAL allocation (the packed workspace) and 1 real free —
    INDEPENDENT of input length, candidate-start count, and byte
    count.  Proven by identical windows at 4 KiB vs 256 KiB no-match
    subjects (the legacy engine did ~40,970 vs ~2.6M real allocs).
  * ZERO allocations per consumed byte and per candidate start
    (implied by the size-independence pin: bytes and starts differ by
    64x between the two subjects while the window is equal).
  * a manual advance-past-match loop over `_find_from` performs
    exactly (matches + 1) real allocations per full scan (each
    invocation is one compat-wrapper workspace).
  * `replace_all` constructs ONE workspace for the ENTIRE operation.
  * String matching adds 0 retains and 0 REAL releases; view matching
    exactly +1 retain and +1 real release (the subject-view backing);
    from_utf8 stays 0 on all pure-match windows.
  * epoch-overflow reset: `_find_from_gen_saturated` (workspace
    generation pre-set to the reset ceiling) returns spans identical
    to `_find_from` across a multi-attempt search — checked in-Drift,
    result pinned here.

Counting semantics: --wrap sees cross-TU calls only; real allocations
are classified by elem_size>0 && max(len,cap)>0 (the zero-capacity
empty-init returns the runtime sentinel and is a no-op); real frees
are exact via a live-pointer set; releases are split real
(storage != NULL) vs null-tombstone (move machinery no-ops).
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

// no-match filler: letters+commas, no digits (heap-backed, non-static)
fn mk_fill(n: Int) nothrow -> String {
	var sb = text.string_builder(n + 32);
	var len = 0;
	while len + 28 <= n {
		text.sb_append_string(sb, "worker,tunnel,socket,buffer,");
		len = len + 28;
	}
	while len < n {
		text.sb_append_string(sb, "x");
		len = len + 1;
	}
	return text.sb_build(sb);
}

pub fn mk_small() nothrow -> String {
	return mk_fill(4096);
}

pub fn mk_large() nothrow -> String {
	return mk_fill(256 * 1024);
}

pub fn mk_carrier() nothrow -> String {
	var sb = text.string_builder(4200);
	var k = 0;
	while k < 90 {
		text.sb_append_string(sb, "alpha,bravo12,charlie345,dd,");
		k = k + 1;
	}
	return text.sb_build(sb);
}

pub fn op_compile_only(s: String) nothrow -> Int {
	match regex.compile("[a-z]+[0-9]+") {
		Ok(re) => { return 1; },
		Err(e) => { return 0 - 1; }
	}
}

pub fn op_compile_anchor_only(s: String) nothrow -> Int {
	match regex.compile("^[a-z,x]+$") {
		Ok(re) => { return 1; },
		Err(e) => { return 0 - 1; }
	}
}

// one top-level find per rep, x100
pub fn op_find_nomatch_x100(s: String) nothrow -> Int {
	match regex.compile("[a-z]+[0-9]+") {
		Ok(re) => {
			var acc = 0;
			var k = 0;
			while k < 100 {
				match regex.find_first(re, s) {
					Some(m) => { return 0 - 1; },
					None() => { acc = acc + 1; }
				}
				k = k + 1;
			}
			return acc;
		},
		Err(e) => { return 0 - 1; }
	}
}

pub fn op_find_view_x100(s: String) nothrow -> Int {
	match regex.compile("[a-z]+[0-9]+") {
		Ok(re) => {
			val v = text.byte_view_all(s);
			var acc = 0;
			var k = 0;
			while k < 100 {
				match regex.find_first_view(re, v) {
					Some(m) => { return 0 - 1; },
					None() => { acc = acc + 1; }
				}
				k = k + 1;
			}
			return acc;
		},
		Err(e) => { return 0 - 1; }
	}
}

pub fn op_is_match_x100(s: String) nothrow -> Int {
	match regex.compile("^[a-z,x]+$") {
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

// manual advance-past-match scan over the compat wrapper: exactly
// (matches + 1) workspace allocations per full scan
pub fn op_scan_all(s: String) nothrow -> Int {
	match regex.compile("[a-z]+[0-9]+") {
		Ok(re) => {
			var count = 0;
			var cursor = 0;
			val n = s.byte_length();
			while cursor <= n {
				match regex._find_from(re, s, cursor) {
					Some(m) => {
						count = count + 1;
						if m.end == m.start {
							cursor = m.start + 1;
						} else {
							cursor = m.end;
						}
					},
					None() => { break; }
				}
			}
			return count;
		},
		Err(e) => { return 0 - 1; }
	}
}

// one workspace for the WHOLE replace_all (plus its string building)
pub fn op_replace_all(s: String) nothrow -> Int {
	match regex.compile("[a-z]+[0-9]+") {
		Ok(re) => {
			val out = regex.replace_all(re, s, "#");
			return out.byte_length();
		},
		Err(e) => { return 0 - 1; }
	}
}

// epoch-overflow reset equivalence, checked in-Drift over a
// multi-attempt search (reset fires on the FIRST epoch advance and
// the search keeps advancing epochs across attempts/bytes)
pub fn op_gen_saturated_equal(s: String) nothrow -> Int {
	match regex.compile("[a-z]+[0-9]+") {
		Ok(re) => {
			var normal_start = 0 - 1;
			var normal_end = 0 - 1;
			match regex._find_from(re, s, 0) {
				Some(m) => { normal_start = m.start; normal_end = m.end; },
				None() => { }
			}
			var sat_start = 0 - 1;
			var sat_end = 0 - 1;
			match regex._find_from_gen_saturated(re, s, 0) {
				Some(m) => { sat_start = m.start; sat_end = m.end; },
				None() => { }
			}
			if normal_start != sat_start or normal_end != sat_end {
				return 0 - 1;
			}
			return 1;
		},
		Err(e) => { return 0 - 1; }
	}
}

pub fn main() nothrow -> Int {
	val s = mk_small();
	if op_compile_only(s) != 1 { return 1; }
	return 0;
}
"""

C_DRIVER = r"""
#include "string_runtime.h"
#include <stdio.h>
#include <string.h>

static long n_retain, n_release_real, n_release_null, n_from_utf8;
static long n_alloc_calls, n_alloc_real, n_alloc_sentinel;
static long n_free_calls, n_free_real, n_free_noop;
static const void *sentinel_addr;

#define LIVE_CAP (1u << 16)
static const void *live[LIVE_CAP];
static long live_count;

static unsigned live_slot(const void *p) {
	unsigned long long h = (unsigned long long)(size_t)p;
	h ^= h >> 33; h *= 0xff51afd7ed558ccdULL; h ^= h >> 33;
	return (unsigned)(h & (LIVE_CAP - 1));
}

static void live_add(const void *p) {
	unsigned i = live_slot(p);
	while (live[i]) i = (i + 1) & (LIVE_CAP - 1);
	live[i] = p;
	live_count++;
}

static int live_remove(const void *p) {
	unsigned i = live_slot(p);
	while (live[i]) {
		if (live[i] == p) {
			unsigned j = i;
			live[i] = NULL;
			for (unsigned k = (i + 1) & (LIVE_CAP - 1); live[k];
			     k = (k + 1) & (LIVE_CAP - 1)) {
				unsigned home = live_slot(live[k]);
				if ((k > j) ? (home <= j || home > k)
				            : (home <= j && home > k)) {
					live[j] = live[k];
					live[k] = NULL;
					j = k;
				}
			}
			live_count--;
			return 1;
		}
		i = (i + 1) & (LIVE_CAP - 1);
	}
	return 0;
}

DriftString __real_drift_string_retain(DriftString s);
void __real_drift_string_release(DriftString s);
DriftString __real_drift_string_from_utf8_bytes(const char *p, drift_isize len);
void *__real_drift_alloc_array(size_t es, size_t ea, long len, long cap);
void __real_drift_free_array(void *p);

DriftString __wrap_drift_string_retain(DriftString s) { n_retain++; return __real_drift_string_retain(s); }
void __wrap_drift_string_release(DriftString s) {
	if (s.storage) n_release_real++; else n_release_null++;
	__real_drift_string_release(s);
}
DriftString __wrap_drift_string_from_utf8_bytes(const char *p, drift_isize len) { n_from_utf8++; return __real_drift_string_from_utf8_bytes(p, len); }

void *__wrap_drift_alloc_array(size_t es, size_t ea, long len, long cap) {
	n_alloc_calls++;
	long eff = cap < len ? len : cap;
	void *p = __real_drift_alloc_array(es, ea, len, cap);
	if (es == 0 || eff == 0) {
		n_alloc_sentinel++;
		if (!sentinel_addr) sentinel_addr = p;
	} else {
		n_alloc_real++;
		live_add(p);
	}
	return p;
}

void __wrap_drift_free_array(void *p) {
	n_free_calls++;
	if (p && p != sentinel_addr && live_remove(p)) n_free_real++;
	else n_free_noop++;
	__real_drift_free_array(p);
}

static void reset(void) {
	n_retain = n_release_real = n_release_null = n_from_utf8 = 0;
	n_alloc_calls = n_alloc_real = n_alloc_sentinel = 0;
	n_free_calls = n_free_real = n_free_noop = 0;
	memset((void *)live, 0, sizeof live);
	live_count = 0;
}

static void report(const char *label, long r) {
	printf("OP=%s r=%ld retain=%ld release_real=%ld release_null=%ld "
	       "from_utf8=%ld alloc_real=%ld free_real=%ld live_end=%ld\n",
	       label, r, n_retain, n_release_real, n_release_null,
	       n_from_utf8, n_alloc_real, n_free_real, live_count);
}

extern DriftString mk_small(void);
extern DriftString mk_large(void);
extern DriftString mk_carrier(void);
extern long op_compile_only(DriftString);
extern long op_compile_anchor_only(DriftString);
extern long op_find_nomatch_x100(DriftString);
extern long op_find_view_x100(DriftString);
extern long op_is_match_x100(DriftString);
extern long op_scan_all(DriftString);
extern long op_replace_all(DriftString);
extern long op_gen_saturated_equal(DriftString);

#define RUN(fn, subj, label) do { \
	DriftString a = drift_string_retain(subj); \
	reset(); \
	long r = fn(a); \
	report(label, r); \
	if (r < 0) { printf("OPFAIL=%s r=%ld\n", label, r); return 70; } \
} while (0)

int main(void) {
	DriftString small = mk_small();
	DriftString large = mk_large();
	DriftString carrier = mk_carrier();

	RUN(op_compile_only, small, "compile_only");
	RUN(op_compile_anchor_only, small, "compile_anchor_only");
	RUN(op_find_nomatch_x100, small, "find_nomatch_small");
	RUN(op_find_nomatch_x100, large, "find_nomatch_large");
	RUN(op_find_view_x100, small, "find_view_small");
	RUN(op_is_match_x100, small, "is_match_small");
	RUN(op_scan_all, carrier, "scan_all_carrier");
	RUN(op_replace_all, carrier, "replace_all_carrier");
	RUN(op_gen_saturated_equal, carrier, "gen_saturated");

	drift_string_release(small);
	drift_string_release(large);
	drift_string_release(carrier);
	printf("DONE\n");
	return 0;
}
"""

WRAPPED = ("drift_string_retain", "drift_string_release",
           "drift_string_from_utf8_bytes", "drift_alloc_array",
           "drift_free_array")


@pytest.fixture(scope="module")
def ops_ll(tmp_path_factory) -> Path:
	tmp = tmp_path_factory.mktemp("regex_scratch_counts")
	src = tmp / "main.drift"
	src.write_text(OPS_SRC)
	out_bin = tmp / "ops.bin"
	stdlib = stdlib_root() or (ROOT / "stdlib")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev", "--allow-unsafe",
		 "--stdlib-root", str(stdlib), str(src), "--entry", "main::main",
		 "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	assert res.returncode == 0, f"{res.stdout}\n---\n{res.stderr[:2000]}"
	ir = Path(str(out_bin) + ".ll").read_text()
	ir = re.sub(r"define ([^@\n]*)@main\(", r"define \1@__drift_unused_main(",
	            ir, count=1)
	patched = tmp / "ops_patched.ll"
	patched.write_text(ir)
	return patched


def test_regex_scratch_allocation_contract(ops_ll: Path, tmp_path: Path) -> None:
	driver_c = tmp_path / "driver.c"
	driver_c.write_text(C_DRIVER)
	out = tmp_path / "counts.bin"
	archive = build_runtime_archive(ROOT, clang=shutil.which("clang"),
		variant=runtime_archive_variant(debug_style=False, asan_enabled=False,
		                                alloc_track_enabled=False))
	wraps = [f"-Wl,--wrap={s}" for s in WRAPPED]
	cmd = ["/usr/bin/clang", "-std=gnu11", "-pthread",
		"-x", "ir", str(ops_ll), "-x", "c", str(driver_c),
		"-x", "none", str(archive), *wraps, "-lz", "-Wl,--as-needed",
		"-I", str(RUNTIME), "-o", str(out)]
	res = subprocess.run(cmd, capture_output=True, text=True,
	                     timeout=sanitizer_timeout(240))
	assert res.returncode == 0, f"link failed:\n{res.stderr[:2000]}"

	run = subprocess.run([str(out)], capture_output=True, text=True,
	                     timeout=sanitizer_timeout(240))
	assert run.returncode == 0, \
		f"driver rc={run.returncode}\n{run.stdout}\n{run.stderr[:500]}"
	assert "DONE" in run.stdout and "OPFAIL" not in run.stdout, run.stdout

	counts: dict[str, dict[str, int]] = {}
	pat = (r"OP=(\w+) r=(-?\d+) retain=(\d+) release_real=(\d+) "
	       r"release_null=(\d+) from_utf8=(\d+) alloc_real=(\d+) "
	       r"free_real=(\d+) live_end=(\d+)")
	for line in run.stdout.splitlines():
		m = re.match(pat, line)
		if m:
			counts[m.group(1)] = {
				"r": int(m.group(2)), "retain": int(m.group(3)),
				"release_real": int(m.group(4)),
				"release_null": int(m.group(5)),
				"from_utf8": int(m.group(6)), "alloc_real": int(m.group(7)),
				"free_real": int(m.group(8)), "live_end": int(m.group(9)),
			}

	base = counts["compile_only"]

	def window(op: str, key: str) -> int:
		return counts[op][key] - base[key]

	# every window leaks nothing
	for op, row in counts.items():
		assert row["live_end"] == 0, f"{op}: leaked real allocations {row}"

	# 100 top-level finds = EXACTLY 100 real allocations (1 workspace
	# each), 100 real frees — at BOTH sizes (size independence: the
	# large subject has 64x the bytes and candidate starts)
	for op in ("find_nomatch_small", "find_nomatch_large"):
		assert window(op, "alloc_real") == 100, (op, counts[op], base)
		assert window(op, "free_real") == 100, (op, counts[op], base)
		assert window(op, "retain") == 0, (op, counts[op], base)
		assert window(op, "release_real") == 0, (op, counts[op], base)
		assert window(op, "from_utf8") == 0, (op, counts[op], base)
	assert counts["find_nomatch_small"]["alloc_real"] == \
		counts["find_nomatch_large"]["alloc_real"], "size independence broken"

	# view form: same 100 workspaces + EXACTLY one subject-view
	# retain/real-release pair
	assert window("find_view_small", "alloc_real") == 100
	assert window("find_view_small", "retain") == 1
	assert window("find_view_small", "release_real") == 1

	# is_match: one workspace per top-level call (its own compile
	# twin — the anchored pattern's compile-time allocations differ
	# from p1's)
	anchor_base = counts["compile_anchor_only"]

	def anchor_window(key: str) -> int:
		return counts["is_match_small"][key] - anchor_base[key]

	assert anchor_window("alloc_real") == 100, 		(counts["is_match_small"], anchor_base)
	assert anchor_window("retain") == 0
	assert anchor_window("release_real") == 0

	# manual scan over _find_from: exactly (matches + 1) workspaces
	matches = counts["scan_all_carrier"]["r"]
	assert matches == 180, counts["scan_all_carrier"]  # 90 chunks x 2 tokens
	assert window("scan_all_carrier", "alloc_real") == matches + 1, \
		(counts["scan_all_carrier"], base)

	# replace_all: ONE workspace for the whole operation; everything
	# else in the window is result-string building (buffers +
	# from_utf8 materializations), which must scale with SEGMENTS not
	# searches: window = 1 workspace + one io.buffer per _substr call.
	# 180 matches -> <= 1 + (segments) real allocs; pin the exact
	# workspace share by subtracting the from_utf8-paired buffers.
	ra = window("replace_all_carrier", "alloc_real")
	substr_allocs = window("replace_all_carrier", "from_utf8")
	assert ra - substr_allocs == 1, (
		f"replace_all must use exactly ONE workspace beyond its "
		f"string building: {counts['replace_all_carrier']} vs {base}")

	# epoch-overflow reset: saturated-generation search equals normal
	assert counts["gen_saturated"]["r"] == 1, counts["gen_saturated"]
