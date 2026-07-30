# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Comparative perf gate: iterative vs the preserved recursive parser
(2026-07-27).

The iterative parser holds container state on the HEAP, so the load-
bearing concern is the SHALLOW request-shaped hot path.  This gate builds
BOTH parsers under the SAME toolchain (the recursive oracle is appended to
a throwaway stdlib copy) and, PER LAUNCH, times both in BOTH A/B orders
(iterative-first and recursive-first) so launch order cannot bias the
ratio.  Across interleaved launches it reports the median AND the spread,
gating both.  It also (a) counts per-parse heap ALLOCATIONS for each parser
under valgrind with a TIGHT bound (heap-frame traffic is the rewrite's
central cost), and (b) proves LINEAR scaling by measuring several input
sizes and checking the growth ratio — not one size against a wall-clock
ceiling.

The shallow ratio is a material, honestly-reported number: the iterative
parser trades some shallow throughput for closing the deep-nesting DoS.
"""
from __future__ import annotations

import os
import re
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout, valgrind_cmd
from lang.driftc.parser import stdlib_root
from lang.tests.driver._json_oracle_stdlib import build_oracle_stdlib

ROOT = Path(__file__).resolve().parents[3]

_SHALLOW_DOC = (r'{\"id\":1234567,\"name\":\"widget-42\",\"active\":true,'
                r'\"ratio\":314,\"tags\":[\"a\",\"b\",\"c\"],\"meta\":{\"x\":1,\"y\":2}}')

# Multiple representative shapes for the primary ratio gate (not one shape):
# a bare scalar (no container frame), a tiny array, a tiny object, a
# malformed request (error path), and a full request-shaped object.
_SHAPES = {
	"scalar": r"42",
	"tiny_arr": r"[1,2,3]",
	"tiny_obj": r'{\"a\":1}',
	"malformed": r'{\"a\":1,',
	"request": _SHALLOW_DOC,
}

# MEASURED shallow tradeoff.  The iterative parser holds container state on
# the heap to close the deep-nesting DoS; that costs some shallow throughput.
# Two optimizations LANDED (root-scalar dispatch outside the engine; in-place
# `&mut stack[top]` hand-off) and the object member hand-off uses an inline
# `mem.replace` key TAKE (faster than the clone form on the binding interleaved
# idle A/B — see doc/perf-analysis-json-iterative-parser.md §1.1; an earlier
# non-interleaved best-of-12 that favoured the clone was a measurement-method
# artifact and is preserved there as such).  A node-only non-located
# specialization was NOT pursued (span residual bounded ≤0.5%, §2).
#
# PER-SHAPE BANDS, calibrated 2026-07-27 with the EXACT gate protocol:
# UNPINNED, SERIAL, on an otherwise idle host (no taskset/affinity — the OS
# schedules normally), per-launch PAIRED (iter and recursive timed in the same
# process, both A/B orders averaged), medians AND worst-launch, NO minima.
# Both a RATIO band and an ABSOLUTE ns-delta band are gated per shape: the
# ratio cancels machine speed (less host-sensitive) and is the tight gate; the
# absolute delta is a coarser host-relative backstop (it scales with machine
# speed, hence generous headroom) that catches an overhead blow-up a ratio
# alone would miss on a tiny-baseline shape (e.g. tiny_obj: a ~1.47 ratio is
# only ~+72 ns).  Values are worst-observed-across-runs + margin sized to the
# MEASURED run-to-run variance on this lane (the small-baseline shapes swing
# ~7–8% between idle runs, so ratio bands carry ~8–12% headroom; absolute bands
# ~35–40%, since absolute ns also scales with machine speed).  The gate catches
# a real regression (≳15% ratio, or an absolute-overhead blow-up) without
# false-failing on that variance.  Raw calibration samples and a reproducible
# clone-vs-take runner: lang/tests/driver/perf_json_ab_samples.json /
# perf_json_ab_runner.py.
# Fields: (ratio_median_max, ratio_worst_max, absdelta_median_max_ns,
# absdelta_worst_max_ns).
_BANDS = {
	"scalar":    (1.18, 1.24,   9.0,  16.0),
	"tiny_arr":  (1.42, 1.52,  75.0,  95.0),
	"tiny_obj":  (1.62, 1.74, 105.0, 135.0),
	"malformed": (1.50, 1.58,  85.0, 105.0),
	"request":   (1.34, 1.42, 360.0, 430.0),
}
_TIMING_N = 200000   # must match `val n = ...` in _TIMING_SRC below

# ── same-launch A/B comparative timing ──
_TIMING_SRC = r"""
module main;
import std.json as json;
import std.core as core;
import std.console as cons;
import std.format as fmt;
import std.time as time;

fn loop_iter(doc: &String, cfg: &json.JsonParseConfig, iters: Int) nothrow -> Int {
	var ok = 0; var i = 0;
	while i < iters {
		match json.parse_with_config(doc, cfg) { core.Result::Ok(_n) => { ok = ok + 1; }, core.Result::Err(_e) => { } }
		i = i + 1;
	}
	return ok;
}
fn loop_orac(doc: &String, cfg: &json.JsonParseConfig, iters: Int) nothrow -> Int {
	var ok = 0; var i = 0;
	while i < iters {
		match json._oracle_parse_with_config(doc, cfg) { core.Result::Ok(_n) => { ok = ok + 1; }, core.Result::Err(_e) => { } }
		i = i + 1;
	}
	return ok;
}
pub fn main() nothrow -> Int {
	val doc = "__DOC__";
	val cfg = json.permissive();
	val n = 200000;
	val _w1 = loop_iter(doc, cfg, 5000);
	val _w2 = loop_orac(doc, cfg, 5000);
	// Order A: iterative first, recursive second.
	val ta0 = time.now_monotonic(); val a1 = loop_iter(doc, cfg, n); val ia = time.elapsed_micros(ta0);
	val ta1 = time.now_monotonic(); val b1 = loop_orac(doc, cfg, n); val oa = time.elapsed_micros(ta1);
	// Order B: recursive first, iterative second.
	val tb0 = time.now_monotonic(); val b2 = loop_orac(doc, cfg, n); val ob = time.elapsed_micros(tb0);
	val tb1 = time.now_monotonic(); val a2 = loop_iter(doc, cfg, n); val ib = time.elapsed_micros(tb1);
	if a1 != b1 or a1 != a2 or b1 != b2 { cons.println("MISMATCH_OK"); return 2; }
	// Report the ACTUAL success count so the test can assert valid shapes
	// produced n successes and malformed shapes produced zero (a program
	// that parses nothing must not pass the timing gate).
	cons.println("iok=" + fmt.format_int(a1));
	cons.println("ook=" + fmt.format_int(b1));
	cons.println("iter_a=" + fmt.format_int(ia));
	cons.println("orac_a=" + fmt.format_int(oa));
	cons.println("orac_b=" + fmt.format_int(ob));
	cons.println("iter_b=" + fmt.format_int(ib));
	return 0;
}
"""

# ── alloc-count program: one parser, loop size from substitution ──
# Exits NONZERO unless EVERY parse succeeded (ok == iters) — a program that
# silently parsed nothing must not be measured as a valid alloc carrier.
_ALLOC_SRC = r"""
module main;
import std.json as json;
import std.core as core;
pub fn main() nothrow -> Int {
	val doc = "__DOC__";
	val cfg = json.permissive();
	var ok = 0; var i = 0;
	while i < __ITERS__ {
		match json.__PARSE__(doc, cfg) { core.Result::Ok(_n) => { ok = ok + 1; }, core.Result::Err(_e) => { } }
		i = i + 1;
	}
	if ok != __ITERS__ { return 1; }
	return 0;
}
"""

# ── scaling program: flat array of __ELEMS__ ints, __ITERS__ times ──
_LARGE_SRC = r"""
module main;
import std.json as json;
import std.core as core;
import std.console as cons;
import std.format as fmt;
import std.time as time;
pub fn main() nothrow -> Int {
	var doc = "[";
	var k = 0;
	while k < __ELEMS__ { if k > 0 { doc = doc + ","; } doc = doc + fmt.format_int(k); k = k + 1; }
	doc = doc + "]";
	val cfg = json.permissive();
	var ok = 0; var i = 0;
	val t0 = time.now_monotonic();
	while i < __ITERS__ {
		match json.parse_with_config(doc, cfg) { core.Result::Ok(_n) => { ok = ok + 1; }, core.Result::Err(_e) => { } }
		i = i + 1;
	}
	val us = time.elapsed_micros(t0);
	cons.println("large_us=" + fmt.format_int(us));
	cons.println("large_ok=" + fmt.format_int(ok));
	return 0;
}
"""


def _compile(tmp_path: Path, name: str, src: str, doc: str = _SHALLOW_DOC) -> Path:
	src_path = tmp_path / f"{name}.drift"
	src_path.write_text(src.replace("__DOC__", doc))
	out_bin = tmp_path / f"{name}.bin"
	stdlib = build_oracle_stdlib(tmp_path)
	comp = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc",   # release lane (-O2)
		 "--stdlib-root", str(stdlib), str(src_path), "--entry", "main::main",
		 "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(300))
	assert comp.returncode == 0, f"[{name}] compile failed:\n{comp.stderr[-2000:]}"
	return out_bin


import pytest

# The whole module is the SERIAL native perf lane — NEVER run under the
# parallel xdist correctness lane (`lang-driver-test` excludes `-m "not perf"`;
# `just perf-protocols` runs it serially on an idle host).  Concurrent
# xdist rows would contend for the CPU and inflate the ratios (an early
# calibration mistake: the malformed shape read ~1.48 under xdist contention
# vs ~1.27 serial), which is exactly why these measurements must not share the
# parallel lane.  No CPU pinning — the OS schedules normally on the idle host;
# the ratio is a same-process paired reading so machine-speed drift cancels.
pytestmark = pytest.mark.perf


@pytest.fixture(autouse=True)
def _forbid_xdist_worker():
	"""Fail CLOSED if a perf test is ever run inside an xdist worker — a future
	recipe that forgets `-m "not perf"` would otherwise benchmark under CPU
	contention and silently produce inflated, meaningless timings.  This is a
	hard failure (not a skip): the measurement is invalid, so surface it loudly.
	Run these serially via `just perf-protocols`."""
	if os.environ.get("PYTEST_XDIST_WORKER"):
		pytest.fail(
			"perf gate ran inside an xdist worker "
			f"(PYTEST_XDIST_WORKER={os.environ['PYTEST_XDIST_WORKER']}); it must "
			"run SERIALLY (no -n / -p no:xdist) — CPU contention invalidates the "
			"timings. Run `just perf-protocols`.", pytrace=False)


def _measure_shape(tmp_path: Path, shape: str) -> tuple:
	"""Build the shape's timing carrier and take 11 UNPINNED launches, each a
	same-process PAIRED (iterative, recursive) reading in both A/B orders,
	averaged.  Returns (ratios, deltas_ns) — no minima."""
	out_bin = _compile(tmp_path, f"timing_{shape}", _TIMING_SRC, doc=_SHAPES[shape])
	expect_ok = 0 if shape == "malformed" else _TIMING_N
	ratios, deltas_ns = [], []
	for _ in range(11):
		run = subprocess.run([str(out_bin)], capture_output=True, text=True,
		                     timeout=sanitizer_timeout(120))
		assert run.returncode == 0, f"rc={run.returncode}\n{run.stdout}\n{run.stderr[:400]}"
		iok = int(re.search(r"iok=(\d+)", run.stdout).group(1))
		ook = int(re.search(r"ook=(\d+)", run.stdout).group(1))
		assert iok == expect_ok and ook == expect_ok, (
			f"[{shape}] parse-success count wrong: iter={iok} orac={ook} "
			f"expected {expect_ok} — the timing carrier parsed the wrong thing")
		ia = int(re.search(r"iter_a=(\d+)", run.stdout).group(1))
		oa = int(re.search(r"orac_a=(\d+)", run.stdout).group(1))
		ob = int(re.search(r"orac_b=(\d+)", run.stdout).group(1))
		ib = int(re.search(r"iter_b=(\d+)", run.stdout).group(1))
		iter_ns = (ia + ib) / 2 / _TIMING_N * 1000.0    # micros over _TIMING_N → ns/parse
		orac_ns = (oa + ob) / 2 / _TIMING_N * 1000.0
		ratios.append(iter_ns / orac_ns)
		deltas_ns.append(iter_ns - orac_ns)
	return ratios, deltas_ns


def test_iterative_vs_recursive_perf_bands() -> None:
	"""SERIAL perf gate over EVERY shape in ONE test (never split into
	xdist-parallel rows).  Per shape it gates the RATIO (median + worst launch)
	and the ABSOLUTE ns overhead (median + worst launch) against `_BANDS`,
	calibrated unpinned/serial/idle.  Reports spread; takes no minima."""
	from lang.test_support.drift_tmp import drift_tempdir
	failures, lines = [], []
	for shape in _SHAPES:
		with drift_tempdir(prefix=f"json_perf_{shape}_") as td:
			ratios, deltas_ns = _measure_shape(Path(td), shape)
		r_med, r_max, r_min = statistics.median(ratios), max(ratios), min(ratios)
		d_med, d_max = statistics.median(deltas_ns), max(deltas_ns)
		r_med_max, r_worst_max, d_med_max, d_worst_max = _BANDS[shape]
		lines.append(
			f"[perf] {shape:9s} ratio median={r_med:.3f} worst={r_max:.3f} spread={r_max/r_min:.3f} "
			f"(band {r_med_max}/{r_worst_max}) | absΔ median={d_med:.1f}ns worst={d_max:.1f}ns "
			f"(band {d_med_max}/{d_worst_max}) n={len(ratios)}")
		rs, ds = [round(r, 3) for r in ratios], [round(d, 1) for d in deltas_ns]
		if r_med > r_med_max:
			failures.append(f"[{shape}] median ratio {r_med:.3f} > band {r_med_max} (ratios {rs})")
		if r_max > r_worst_max:
			failures.append(f"[{shape}] worst-launch ratio {r_max:.3f} > band {r_worst_max} (ratios {rs})")
		if d_med > d_med_max:
			failures.append(f"[{shape}] median absΔ {d_med:.1f}ns > band {d_med_max}ns (deltas {ds})")
		if d_max > d_worst_max:
			failures.append(f"[{shape}] worst-launch absΔ {d_max:.1f}ns > band {d_worst_max}ns (deltas {ds})")
	print("\n".join(lines))
	assert not failures, "perf band(s) exceeded:\n  " + "\n  ".join(failures)


def _valgrind_allocs(out_bin: Path) -> int:
	res = subprocess.run(
		valgrind_cmd("--tool=memcheck", "--error-exitcode=98", str(out_bin)),
		capture_output=True, text=True, timeout=sanitizer_timeout(300))
	# The carrier returns 0 ONLY if every parse succeeded; valgrind returns
	# 98 on any memory error.  Either non-zero code invalidates the count.
	assert res.returncode == 0, (
		f"alloc carrier under valgrind exited {res.returncode} (parse failure "
		f"or memory error) — alloc count invalid:\n{res.stderr[-1500:]}")
	assert "ERROR SUMMARY: 0 errors" in res.stderr, (
		f"valgrind reported memory errors:\n{res.stderr[-1500:]}")
	m = re.search(r"total heap usage: ([\d,]+) allocs", res.stderr)
	assert m, f"could not parse valgrind alloc count:\n{res.stderr[-1500:]}"
	return int(m.group(1).replace(",", ""))


def test_per_parse_allocation_counts(tmp_path: Path) -> None:
	if shutil.which("valgrind") is None:
		import pytest
		pytest.skip("valgrind required for allocation counting")
	n1, n2 = 1000, 3000

	def allocs_for(parse_fn: str) -> float:
		b1 = _compile(tmp_path, f"alloc_{parse_fn}_{n1}",
		              _ALLOC_SRC.replace("__PARSE__", parse_fn).replace("__ITERS__", str(n1)))
		b2 = _compile(tmp_path, f"alloc_{parse_fn}_{n2}",
		              _ALLOC_SRC.replace("__PARSE__", parse_fn).replace("__ITERS__", str(n2)))
		a1, a2 = _valgrind_allocs(b1), _valgrind_allocs(b2)
		return (a2 - a1) / (n2 - n1)

	iter_pp = allocs_for("parse_with_config")
	orac_pp = allocs_for("_oracle_parse_with_config")
	print(f"[perf] per-parse heap allocs: iterative={iter_pp:.2f} "
	      f"recursive={orac_pp:.2f} delta={iter_pp - orac_pp:+.2f}")
	# TIGHT bound pinned to the MEASURED delta (+2: the container frame stack
	# for a depth-2 request; `_Completed` adds none).  +1 noise margin catches
	# any real allocation blowup (per-element / per-byte alloc).
	assert iter_pp <= orac_pp + 3, (
		f"iterative per-parse allocations ({iter_pp:.2f}) exceed recursive "
		f"({orac_pp:.2f}) by more than 3 (measured +2) — heap-frame traffic regressed")


def test_large_flat_array_linear_scaling(tmp_path: Path) -> None:
	# Fixed total element-work across sizes; per-element time must stay ~flat
	# (linear).  A super-linear parser (O(n^2)) blows the growth ratio.
	sizes = [(5000, 400), (10000, 200), (20000, 100), (40000, 50)]
	per_elem_us = {}
	for elems, iters in sizes:
		out_bin = _compile(tmp_path, f"large_{elems}",
		                   _LARGE_SRC.replace("__ELEMS__", str(elems)).replace("__ITERS__", str(iters)))
		# median of 3 launches for stability
		samples = []
		for _ in range(3):
			run = subprocess.run([str(out_bin)], capture_output=True, text=True,
			                     timeout=sanitizer_timeout(120))
			assert run.returncode == 0, f"rc={run.returncode}\n{run.stderr[:400]}"
			ok = int(re.search(r"large_ok=(\d+)", run.stdout).group(1))
			assert ok == iters, (
				f"scaling carrier parsed {ok}/{iters} — the large array did not "
				f"parse; timing is meaningless")
			us = int(re.search(r"large_us=(\d+)", run.stdout).group(1))
			samples.append(us / (elems * iters))
		per_elem_us[elems] = statistics.median(samples)
	# Gate EVERY size, not just the endpoints: the max normalized per-element
	# time across all sizes must be within 1.6x of the min, so an intermediate
	# cliff (a size where scaling breaks) cannot slip through.  Also check each
	# ADJACENT step's growth.
	vals = [per_elem_us[e] for e, _ in sizes]
	lo, hi = min(vals), max(vals)
	spread = hi / lo if lo > 0 else float("inf")
	print(f"[perf] per-element parse time (ns): "
	      + " ".join(f"{e}={t*1e3:.3f}" for e, t in sorted(per_elem_us.items()))
	      + f"  spread(max/min)={spread:.2f}")
	assert spread <= 1.6, (
		f"per-element parse time spread {spread:.2f}x across sizes — scaling is "
		f"not linear (an intermediate cliff) ({per_elem_us})")
	ordered = [per_elem_us[e] for e, _ in sizes]
	for a, b in zip(ordered, ordered[1:]):
		step = b / a if a > 0 else float("inf")
		assert step <= 1.5, (
			f"per-element time jumped {step:.2f}x between adjacent sizes — "
			f"non-linear step ({per_elem_us})")
