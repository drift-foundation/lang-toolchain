# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""string-view-performance §10: performance-TIER guard bands over
the SHIPPED API (StringByteView / StringByteSource / with_view_bytes
— not a prototype), so real-implementation tier regressions fail.

This is NOT a benchmark — the measured numbers live in the design
record (STRING-VIEW-PERFORMANCE-CHECKPOINT.md §2).  It pins the
ORDERING and coarse ratios of the three tiers with wide, contention-
safe margins (ratios between interleaved same-process runs), so a
regression that collapses a tier (e.g. a retain sneaking into the
read path, or the bulk window losing its base-once property) fails
loudly while machine noise does not:

  * bulk windows (direct AND composed-through-std.text) must beat
    indexed byte_at scanning by >= 1.4x      (measured: ~2x after the
    string_byte_at OOB fix removed the per-byte C bounds call);
  * BOTH public Result accessors within the HARD <=2x target
    (measured: StringByteView.byte_at ~1.9x, String.byte_at ~1.7x),
    asserted on SAME-LAUNCH median/median ratios for EVERY one of the
    fresh launches — no minima selection;
  * the exported-internal _StringByteSource read path within 2x
    (measured 1.15x) and ViewBytesIter within 2.5x (measured 1.59x);
  * per-token substring materialization must cost > 1.3x the
    per-token retained-view shape            (measured: ~2.1x);
  * checksums of all variants must agree (semantic equivalence).
"""
from __future__ import annotations

import re
import statistics
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]

BENCH_SRC = r"""
module main;

import std.core as core;
import std.text as text;
import std.ffi as ffi;
import std.mem as mem;
import std.time as time;
import std.console as cons;
import std.format as fmt;

// PRODUCTION-API tier gate: w4/w5/w6 exercise the SHIPPED
// StringByteView / StringByteSource / with_view_bytes — not a
// prototype — so tier regressions in the real implementation fail
// this gate.

fn build_input(target_bytes: Int) nothrow -> String {
	val chunk = "alpha,bravo12,charlie345,dd,echo_echo_echo,f,";
	var sb = text.string_builder(target_bytes + 64);
	var total = 0;
	val n = chunk.byte_length();
	while total < target_bytes {
		text.sb_append_string(&mut sb, &chunk);
		total = total + n;
	}
	return text.sb_build(&mut sb);
}

fn w1_byte_at(s: &String) nothrow -> Int {
	val n = s.byte_length();
	var tokens = 0;
	var len_sum = 0;
	var first_sum = 0;
	var tok_start = 0;
	var i = 0;
	while i < n {
		val b = core.string_byte_at(s, i);
		if cast<Int>(b) == 44 {
			val tl = i - tok_start;
			if tl > 0 {
				tokens = tokens + 1;
				len_sum = len_sum + tl;
				first_sum = first_sum + cast<Int>(core.string_byte_at(s, tok_start));
			}
			tok_start = i + 1;
		}
		i = i + 1;
	}
	return tokens * 1000000 + len_sum + first_sum;
}

fn w2_with_bytes(s: &String) nothrow -> Int {
	val scan: core.Callback2<mem.Ptr<Byte>, Int, Int> =
		core.callback2(|p: mem.Ptr<Byte>, n: Int| => {
			var tokens = 0;
			var len_sum = 0;
			var first_sum = 0;
			var tok_start = 0;
			var i = 0;
			while i < n {
				val b = mem.ptr_read<type Byte>(mem.ptr_offset<type Byte>(p, i));
				if cast<Int>(b) == 44 {
					val tl = i - tok_start;
					if tl > 0 {
						tokens = tokens + 1;
						len_sum = len_sum + tl;
						first_sum = first_sum + cast<Int>(mem.ptr_read<type Byte>(mem.ptr_offset<type Byte>(p, tok_start)));
					}
					tok_start = i + 1;
				}
				i = i + 1;
			}
			tokens * 1000000 + len_sum + first_sum
		});
	return ffi.with_bytes<type Int, core.Callback2<mem.Ptr<Byte>, Int, Int> >(s, scan);
}

fn w3_substring(s: &String) nothrow -> Int {
	val n = s.byte_length();
	var tokens = 0;
	var len_sum = 0;
	var first_sum = 0;
	var tok_start = 0;
	var i = 0;
	while i < n {
		val b = core.string_byte_at(s, i);
		if cast<Int>(b) == 44 {
			val tl = i - tok_start;
			if tl > 0 {
				match text.substring(s, tok_start, tl) {
					Ok(tok) => {
						tokens = tokens + 1;
						len_sum = len_sum + tok.byte_length();
						first_sum = first_sum + cast<Int>(core.string_byte_at(&tok, 0));
					},
					Err(e) => { }
				}
			}
			tok_start = i + 1;
		}
		i = i + 1;
	}
	return tokens * 1000000 + len_sum + first_sum;
}

// Per-token PRODUCTION view construction (byte_view + drop each token).
fn w4_view(s: &String) nothrow -> Int {
	val n = s.byte_length();
	var tokens = 0;
	var len_sum = 0;
	var first_sum = 0;
	var tok_start = 0;
	var i = 0;
	while i < n {
		val b = core.string_byte_at(s, i);
		if cast<Int>(b) == 44 {
			val tl = i - tok_start;
			if tl > 0 {
				match text.byte_view(s, tok_start, tl) {
					Ok(v) => {
						val bs = text._byte_source(&v);
						tokens = tokens + 1;
						len_sum = len_sum + v.byte_length();
						first_sum = first_sum + cast<Int>(bs.read(0));
					},
					Err(e) => { }
				}
			}
			tok_start = i + 1;
		}
		i = i + 1;
	}
	return tokens * 1000000 + len_sum + first_sum;
}

// PRIMARY safe-read tier: the SHIPPED Result-returning
// StringByteView.byte_at, match-unwrapped per byte (nothrow — bounds
// failure is data; the scan is in-bounds so Err never fires).
fn w5_byte_at_reads(s: &String) nothrow -> Int {
	val v = text.byte_view_all(s);
	val n = v.byte_length();
	var tokens = 0;
	var len_sum = 0;
	var first_sum = 0;
	var tok_start = 0;
	var i = 0;
	while i < n {
		var bi = 0;
		match v.byte_at(i) {
			Ok(b) => { bi = cast<Int>(b); },
			Err(e) => { return 0 - 1; }
		}
		if bi == 44 {
			val tl = i - tok_start;
			if tl > 0 {
				tokens = tokens + 1;
				len_sum = len_sum + tl;
				var fb = 0;
				match v.byte_at(tok_start) {
					Ok(b) => { fb = cast<Int>(b); },
					Err(e) => { return 0 - 1; }
				}
				first_sum = first_sum + fb;
			}
			tok_start = i + 1;
		}
		i = i + 1;
	}
	return tokens * 1000000 + len_sum + first_sum;
}

// String.byte_at Result tier (the OTHER public accessor).
fn w5a_string_byte_at(s: &String) nothrow -> Int {
	val n = s.byte_length();
	var tokens = 0;
	var len_sum = 0;
	var first_sum = 0;
	var tok_start = 0;
	var i = 0;
	while i < n {
		var bi = 0;
		match s.byte_at(i) {
			Ok(b) => { bi = cast<Int>(b); },
			Err(e) => { return 0 - 1; }
		}
		if bi == 44 {
			val tl = i - tok_start;
			if tl > 0 {
				tokens = tokens + 1;
				len_sum = len_sum + tl;
				var fb = 0;
				match s.byte_at(tok_start) {
					Ok(b) => { fb = cast<Int>(b); },
					Err(e) => { return 0 - 1; }
				}
				first_sum = first_sum + fb;
			}
			tok_start = i + 1;
		}
		i = i + 1;
	}
	return tokens * 1000000 + len_sum + first_sum;
}

// ViewBytesIter tier: consuming byte iterator (Optional<Byte> steps).
fn w7_iter(s: &String) nothrow -> Int {
	val v = text.byte_view_all(s);
	var it = v.bytes();
	var tokens = 0;
	var len_sum = 0;
	var run = 0;
	var going = true;
	while going {
		match it.next() {
			Some(b) => {
				if cast<Int>(b) == 44 {
					if run > 0 {
						tokens = tokens + 1;
						len_sum = len_sum + run;
					}
					run = 0;
				} else {
					run = run + 1;
				}
			},
			None() => { going = false; }
		}
	}
	return tokens * 1000000 + len_sum;
}

// SECONDARY coverage: the exported-internal _StringByteSource read
// path (the engine/parser plumbing tier).
fn w5b_source_reads(s: &String) nothrow -> Int {
	val v = text.byte_view_all(s);
	val src = text._byte_source(&v);
	val n = src.size();
	var tokens = 0;
	var len_sum = 0;
	var first_sum = 0;
	var tok_start = 0;
	var i = 0;
	while i < n {
		val b = src.read(i);
		if cast<Int>(b) == 44 {
			val tl = i - tok_start;
			if tl > 0 {
				tokens = tokens + 1;
				len_sum = len_sum + tl;
				first_sum = first_sum + cast<Int>(src.read(tok_start));
			}
			tok_start = i + 1;
		}
		i = i + 1;
	}
	return tokens * 1000000 + len_sum + first_sum;
}

// PRODUCTION composed bulk window.
fn w6_view_bulk(s: &String) nothrow -> Int {
	val v = text.byte_view_all(s);
	val scan: core.Callback2<mem.Ptr<Byte>, Int, Int> =
		core.callback2(|p: mem.Ptr<Byte>, m: Int| => {
			var tokens = 0;
			var len_sum = 0;
			var first_sum = 0;
			var tok_start = 0;
			var i = 0;
			while i < m {
				val b = mem.ptr_read<type Byte>(mem.ptr_offset<type Byte>(p, i));
				if cast<Int>(b) == 44 {
					val tl = i - tok_start;
					if tl > 0 {
						tokens = tokens + 1;
						len_sum = len_sum + tl;
						first_sum = first_sum + cast<Int>(mem.ptr_read<type Byte>(mem.ptr_offset<type Byte>(p, tok_start)));
					}
					tok_start = i + 1;
				}
				i = i + 1;
			}
			tokens * 1000000 + len_sum + first_sum
		});
	return text.with_view_bytes<type Int, core.Callback2<mem.Ptr<Byte>, Int, Int> >(&v, move scan);
}

pub fn main() nothrow -> Int {
	val input = build_input(512 * 1024);
	cons.println("input bytes: " + fmt.format_int(input.byte_length()));

	val iters = 5;

	val c1 = w1_byte_at(&input);
	val c2 = w2_with_bytes(&input);
	val c3 = w3_substring(&input);
	val c4 = w4_view(&input);
	val c5 = w5_byte_at_reads(&input);
	val c5a = w5a_string_byte_at(&input);
	if c5a != c1 { return 24; }
	val c5b = w5b_source_reads(&input);
	if c5b != c1 { return 21; }
	val c7chk = w7_iter(&input);
	if c7chk == 0 { return 25; }
	val c6 = w6_view_bulk(&input);
	if c1 != c2 { return 1; }
	if c1 != c3 { return 2; }
	if c1 != c4 { return 3; }
	if c1 != c5 { return 4; }
	if c1 != c6 { return 5; }

	var line1 = "RESULT w1_byte_at us=";
	var k = 0;
	while k < iters {
		val t0 = time.now_monotonic();
		val r = w1_byte_at(&input);
		line1 = line1 + fmt.format_int(time.elapsed_micros(&t0)) + ",";
		if r != c1 { return 10; }
		k = k + 1;
	}
	cons.println(line1);

	var line2 = "RESULT w2_with_bytes us=";
	k = 0;
	while k < iters {
		val t0 = time.now_monotonic();
		val r = w2_with_bytes(&input);
		line2 = line2 + fmt.format_int(time.elapsed_micros(&t0)) + ",";
		if r != c1 { return 11; }
		k = k + 1;
	}
	cons.println(line2);

	var line3 = "RESULT w3_substring us=";
	k = 0;
	while k < iters {
		val t0 = time.now_monotonic();
		val r = w3_substring(&input);
		line3 = line3 + fmt.format_int(time.elapsed_micros(&t0)) + ",";
		if r != c1 { return 12; }
		k = k + 1;
	}
	cons.println(line3);

	var line4 = "RESULT w4_view us=";
	k = 0;
	while k < iters {
		val t0 = time.now_monotonic();
		val r = w4_view(&input);
		line4 = line4 + fmt.format_int(time.elapsed_micros(&t0)) + ",";
		if r != c1 { return 13; }
		k = k + 1;
	}
	cons.println(line4);

	var line5 = "RESULT w5_byte_at_reads us=";
	k = 0;
	while k < iters {
		val t0 = time.now_monotonic();
		val r = w5_byte_at_reads(&input);
		line5 = line5 + fmt.format_int(time.elapsed_micros(&t0)) + ",";
		if r != c1 { return 14; }
		k = k + 1;
	}
	cons.println(line5);

	var line5a = "RESULT w5a_string_byte_at us=";
	k = 0;
	while k < iters {
		val t0 = time.now_monotonic();
		val r = w5a_string_byte_at(&input);
		line5a = line5a + fmt.format_int(time.elapsed_micros(&t0)) + ",";
		if r != c1 { return 26; }
		k = k + 1;
	}
	cons.println(line5a);

	var line7 = "RESULT w7_iter us=";
	k = 0;
	while k < iters {
		val t0 = time.now_monotonic();
		val r = w7_iter(&input);
		line7 = line7 + fmt.format_int(time.elapsed_micros(&t0)) + ",";
		if r != c7chk { return 27; }
		k = k + 1;
	}
	cons.println(line7);

	var line5b = "RESULT w5b_source_reads us=";
	k = 0;
	while k < iters {
		val t0 = time.now_monotonic();
		val r = w5b_source_reads(&input);
		line5b = line5b + fmt.format_int(time.elapsed_micros(&t0)) + ",";
		if r != c1 { return 23; }
		k = k + 1;
	}
	cons.println(line5b);

	var line6 = "RESULT w6_view_bulk us=";
	k = 0;
	while k < iters {
		val t0 = time.now_monotonic();
		val r = w6_view_bulk(&input);
		line6 = line6 + fmt.format_int(time.elapsed_micros(&t0)) + ",";
		if r != c1 { return 15; }
		k = k + 1;
	}
	cons.println(line6);

	return 0;
}
"""


def _medians(stdout: str) -> dict[str, float]:
	out: dict[str, float] = {}
	for line in stdout.splitlines():
		m = re.match(r"RESULT (\w+) ", line)
		if m:
			nums = [int(x) for x in re.search(r"us=([\d,]+),?$", line).group(1).split(",") if x]
			out[m.group(1)] = statistics.median(nums)
	return out


def test_view_performance_tiers(tmp_path: Path) -> None:
	src = tmp_path / "bench.drift"
	src.write_text(BENCH_SRC)
	out_bin = tmp_path / "bench.bin"
	stdlib = stdlib_root() or (ROOT / "stdlib")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev", "--allow-unsafe",
		 "--stdlib-root", str(stdlib), str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[:2000]}"

	# HONEST multi-launch protocol (review-mandated): compile ONCE,
	# launch several FRESH processes, compute SAME-LAUNCH median/median
	# ratios, and require EVERY launch to satisfy the hard bands.  No
	# minima selection — a slow per-launch mode must fail the gate, not
	# be filtered out.  (The earlier String.byte_at bimodality
	# disappeared with the shape-narrowed inlinehint; 8/8 probe
	# launches sat at 1.66-1.70x.)
	LAUNCHES = 5
	keys = ("w1_byte_at", "w2_with_bytes", "w3_substring", "w4_view",
	        "w5_byte_at_reads", "w5a_string_byte_at", "w7_iter",
	        "w5b_source_reads", "w6_view_bulk")
	for launch in range(LAUNCHES):
		run = subprocess.run([str(out_bin)], capture_output=True, text=True,
			timeout=sanitizer_timeout(240))
		assert run.returncode == 0, (
			f"launch {launch}: bench exited {run.returncode} (checksum mismatch?)\n{run.stderr[:400]}"
		)
		med = _medians(run.stdout)
		for k in keys:
			assert k in med, f"launch {launch}: missing {k}: {run.stdout}"
		raw = med["w1_byte_at"]
		ctx = f"launch {launch}: {med}"
		# bulk >= 1.4x faster than indexed (measured ~2.1x)
		assert med["w2_with_bytes"] * 1.4 < raw, ctx
		assert med["w6_view_bulk"] * 1.4 < raw, ctx
		# BOTH public Result accessors: the binding <=2x target, HARD,
		# per launch (measured: view ~1.9x, String ~1.7x).
		assert med["w5_byte_at_reads"] < raw * 2, ctx
		assert med["w5a_string_byte_at"] < raw * 2, ctx
		# internal source path and the byte iterator.
		assert med["w5b_source_reads"] < raw * 2, ctx
		assert med["w7_iter"] < raw * 2.5, ctx
		# substring materialization > 1.3x the view shape (measured ~1.8x)
		assert med["w3_substring"] > med["w4_view"] * 1.3, ctx
