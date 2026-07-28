# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Reproducible clone-vs-take wall-clock A/B for the std.json object member
hand-off (durable evidence behind doc/perf-analysis-json-iterative-parser.md
§1.1).  NOT a pytest module (no `test_` prefix) — run it directly:

    PYTHONPATH=. .venv/bin/python lang/tests/driver/perf_json_ab_runner.py \
        --out lang/tests/driver/perf_json_ab_samples.json

What it does, fail-closed at every step:
  1. Reads the SHIPPED take source `stdlib/std/json/json.drift` and asserts its
     sha256 matches `_PINNED_TAKE_SHA` — the samples are only meaningful for
     that exact source.
  2. Builds the TAKE oracle stdlib (tree source + recursive-oracle frag).
  3. Reconstructs the CLONE alternative from the take oracle stdlib by an
     EXACT-ONE textual replacement of each of the two `mem.replace` inserts
     back to `(*pkey).clone()` — asserting exactly one hit each, so a drift in
     the source can never silently produce a bogus "clone" build.
  4. Compiles the SAME timing program against both stdlibs (identical flags).
  5. Runs UNPINNED, letting the OS schedule normally — the protocol is a
     SERIAL run on an otherwise idle host (no taskset / affinity selection);
     the ratio is a same-process paired reading, so machine-speed drift
     cancels within each launch.
  6. Interleaves clone/take per round in a recorded-seed shuffled order, 50
     rounds, and records EVERY launch (both A/B orders, no minima).
  7. Emits JSON: source/binary sha256, host provenance, seed, per-launch raw
     microseconds, and the per-form median ns / ratio / abs-delta.

Run this SERIALLY on an idle host (it is part of the native `perf-protocols`
lane, never the parallel xdist correctness lane).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import random
import re
import statistics
import subprocess
import sys
from pathlib import Path

from lang.tests.driver._json_oracle_stdlib import build_oracle_stdlib

ROOT = Path(__file__).resolve().parents[3]
JSON_SRC = ROOT / "stdlib" / "std" / "json" / "json.drift"

# The shipped TAKE hand-off.  Update ONLY when the hand-off source changes.
_PINNED_TAKE_SHA = "4c24f438d11313495cf010fa02cbcb6e96b9c638c94c9fcd13b6029dc740fa10"

# Exact-one replacements: take -> clone (both object-insert call sites).
_TAKE_TO_CLONE = [
	('fields.insert_if_absent(mem.replace(&mut *pkey, ""), move node);',
	 'fields.insert_if_absent((*pkey).clone(), move node);'),
	('fields.insert(mem.replace(&mut *pkey, ""), move node);',
	 'fields.insert((*pkey).clone(), move node);'),
]

_ROUNDS = 50
_N = 200000            # parses per timed loop (matches the perf-gate carrier)
_SEED = 20260727

_TIMING_SRC = r"""
module main;
import std.json as json;
import std.core as core;
import std.console as cons;
import std.format as fmt;
import std.time as time;
fn loop_iter(doc: &String, cfg: &json.JsonParseConfig, iters: Int) nothrow -> Int {
	var ok = 0; var i = 0;
	while i < iters { match json.parse_with_config(doc, cfg) { core.Result::Ok(_n) => { ok = ok + 1; }, core.Result::Err(_e) => { } } i = i + 1; }
	return ok;
}
fn loop_orac(doc: &String, cfg: &json.JsonParseConfig, iters: Int) nothrow -> Int {
	var ok = 0; var i = 0;
	while i < iters { match json._oracle_parse_with_config(doc, cfg) { core.Result::Ok(_n) => { ok = ok + 1; }, core.Result::Err(_e) => { } } i = i + 1; }
	return ok;
}
pub fn main() nothrow -> Int {
	val doc = "__DOC__";
	val cfg = json.permissive();
	val n = __N__;
	val _w1 = loop_iter(&doc, &cfg, 5000);
	val _w2 = loop_orac(&doc, &cfg, 5000);
	val ta0 = time.now_monotonic(); val a1 = loop_iter(&doc, &cfg, n); val ia = time.elapsed_micros(&ta0);
	val ta1 = time.now_monotonic(); val b1 = loop_orac(&doc, &cfg, n); val oa = time.elapsed_micros(&ta1);
	val tb0 = time.now_monotonic(); val b2 = loop_orac(&doc, &cfg, n); val ob = time.elapsed_micros(&tb0);
	val tb1 = time.now_monotonic(); val a2 = loop_iter(&doc, &cfg, n); val ib = time.elapsed_micros(&tb1);
	if a1 != b1 or a1 != a2 or b1 != b2 { cons.println("MISMATCH"); return 2; }
	// Emit the success counts too — both parsers must have parsed ALL n on
	// these valid shapes, else the timings are of a program that parsed
	// nothing (both zero would slip past the equality check above).
	cons.println("iok=" + fmt.format_int(a1));
	cons.println("ook=" + fmt.format_int(b1));
	cons.println("iter_a=" + fmt.format_int(ia));
	cons.println("orac_a=" + fmt.format_int(oa));
	cons.println("orac_b=" + fmt.format_int(ob));
	cons.println("iter_b=" + fmt.format_int(ib));
	return 0;
}
"""

# Shapes measured in the durable A/B (the two most decision-relevant; the full
# five-shape band calibration lives in the perf gate).
_SHAPES = {
	"tiny_obj": r'{\"a\":1}',
	"request": (r'{\"id\":1234567,\"name\":\"widget-42\",\"active\":true,'
	            r'\"ratio\":314,\"tags\":[\"a\",\"b\",\"c\"],\"meta\":{\"x\":1,\"y\":2}}'),
}


def _sha(p: Path) -> str:
	return hashlib.sha256(p.read_bytes()).hexdigest()


def _build_variant(work: Path, name: str, clone: bool) -> Path:
	"""Return a compiled timing binary dir/binary for the given hand-off form."""
	base = build_oracle_stdlib(work / f"oracle_{name}")   # tree take + frag
	if clone:
		jp = base / "std" / "json" / "json.drift"
		s = jp.read_text()
		for take, clon in _TAKE_TO_CLONE:
			if s.count(take) != 1:
				raise RuntimeError(f"expected exactly 1 '{take[:40]}...' in oracle "
				                   f"json.drift, found {s.count(take)} — source drift")
			s = s.replace(take, clon)
		jp.write_text(s)
	return base


def _compile(work: Path, name: str, stdlib: Path, doc: str) -> Path:
	src = work / f"timing_{name}.drift"
	src.write_text(_TIMING_SRC.replace("__DOC__", doc).replace("__N__", str(_N)))
	out = work / f"timing_{name}"
	c = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--stdlib-root", str(stdlib),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True)
	if c.returncode != 0:
		raise RuntimeError(f"compile {name} failed:\n{c.stderr[-1500:]}")
	return out


def _launch(binp: Path) -> dict:
	out = subprocess.run([str(binp)], capture_output=True, text=True)   # unpinned
	if out.returncode != 0:
		raise RuntimeError(f"{binp.name} rc={out.returncode}: {out.stdout}{out.stderr[:300]}")
	d = {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", out.stdout)}
	for k in ("iok", "ook", "iter_a", "orac_a", "orac_b", "iter_b"):
		if k not in d:
			raise RuntimeError(f"{binp.name}: missing {k} in output: {out.stdout!r}")
	# Reject a launch whose parsers did not both parse ALL _N — otherwise the
	# timings are of a program that parsed nothing (both zero passes the
	# same-count equality check).  These are all valid shapes → expect _N.
	if not (d["iok"] == d["ook"] == _N):
		raise RuntimeError(f"{binp.name}: parse-success mismatch iok={d['iok']} "
		                   f"ook={d['ook']} expected {_N} — timing is meaningless")
	return d


def _summarize(launches: dict) -> dict:
	"""Derive the per-(shape, form) medians PURELY from the raw launches, so a
	stored summary can be recomputed and checked against its raw data."""
	def per_parse(rec, key_a, key_b):
		return (rec[key_a] + rec[key_b]) / 2 / _N * 1000.0
	shapes = sorted({k.split("|", 1)[1] for k in launches})
	summary = {}
	for shape in shapes:
		summary[shape] = {}
		for form in ("take", "clone"):
			recs = launches[f"{form}|{shape}"]
			iters = [per_parse(x, "iter_a", "iter_b") for x in recs]
			oracs = [per_parse(x, "orac_a", "orac_b") for x in recs]
			ratios = [i / o for i, o in zip(iters, oracs)]
			deltas = [i - o for i, o in zip(iters, oracs)]
			summary[shape][form] = {
				"n_samples": len(recs),
				"iter_ns_median": round(statistics.median(iters), 2),
				"orac_ns_median": round(statistics.median(oracs), 2),
				"ratio_median": round(statistics.median(ratios), 4),
				"ratio_min": round(min(ratios), 4),
				"ratio_max": round(max(ratios), 4),
				"absdelta_ns_median": round(statistics.median(deltas), 2),
			}
	return summary


def _verify_file(path: str) -> int:
	"""Cheap integrity check: recompute the summary from the stored raw launches
	and require it to equal the stored summary; also re-check every launch's
	iok==ook==_N.  Catches a tampered/corrupt artifact without re-benchmarking."""
	rec = json.loads(Path(path).read_text())
	launches = rec["launches"]
	for key, recs in launches.items():
		for r in recs:
			if not (r.get("iok") == r.get("ook") == _N):
				print(f"INTEGRITY FAIL: {key} launch iok={r.get('iok')} "
				      f"ook={r.get('ook')} != _N={_N}", file=sys.stderr)
				return 4
	recomputed = _summarize(launches)
	if recomputed != rec["summary"]:
		print("INTEGRITY FAIL: stored summary != recomputed-from-launches",
		      file=sys.stderr)
		return 4
	print(f"integrity OK: {path} — summary matches raw launches; every launch "
	      f"parsed all {_N}")
	return 0


def main() -> int:
	ap = argparse.ArgumentParser()
	ap.add_argument("--verify", metavar="SAMPLES_JSON",
	                help="integrity-check an existing samples file and exit")
	ap.add_argument("--out", help="samples JSON path (required unless --verify)")
	ap.add_argument("--work", default=None, help="scratch dir (default: temp)")
	ap.add_argument("--rounds", type=int, default=_ROUNDS)
	args = ap.parse_args()

	if args.verify:
		return _verify_file(args.verify)
	if not args.out:
		ap.error("--out is required unless --verify is given")

	actual = _sha(JSON_SRC)
	if actual != _PINNED_TAKE_SHA:
		print(f"REFUSING: {JSON_SRC} sha256 {actual} != pinned take "
		      f"{_PINNED_TAKE_SHA}. Update _PINNED_TAKE_SHA only if the hand-off "
		      f"source legitimately changed, then re-run.", file=sys.stderr)
		return 3

	import tempfile
	work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="json_ab_"))
	work.mkdir(parents=True, exist_ok=True)

	bins = {}
	shas = {}
	for form, clone in (("take", False), ("clone", True)):
		stdlib = _build_variant(work, form, clone)
		for shape, doc in _SHAPES.items():
			b = _compile(work, f"{form}_{shape}", stdlib, doc)
			bins[(form, shape)] = b
			shas[f"{form}_{shape}"] = _sha(b)

	rng = random.Random(_SEED)
	launches = {f"{form}|{shape}": [] for form in ("take", "clone") for shape in _SHAPES}
	for r in range(args.rounds):
		for shape in _SHAPES:
			order = ["take", "clone"]
			rng.shuffle(order)
			for form in order:
				launches[f"{form}|{shape}"].append(_launch(bins[(form, shape)]))

	summary = _summarize(launches)

	record = {
		"record": "json-handoff-ab",
		"provenance": {
			"json_drift_sha256": actual,
			"pinned_take_sha256": _PINNED_TAKE_SHA,
			"binary_sha256": shas,
			"host": platform.node(),
			"platform": platform.platform(),
			"pinning": "none (unpinned; serial run on idle host, OS-scheduled)",
			"seed": _SEED,
			"rounds": args.rounds,
			"parses_per_loop": _N,
			"order_averaged_per_launch": True,
			"samples_per_form_per_shape": args.rounds,
			"minima_selection": False,
		},
		"summary": summary,
		"launches": launches,
	}
	# Self-check: the stored summary must be exactly recomputable from the raw
	# launches (round-trips through JSON so the on-disk artifact is validated).
	Path(args.out).write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
	if _verify_file(args.out) != 0:
		return 4
	print(f"wrote {args.out}  (unpinned, seed={_SEED}, rounds={args.rounds})")
	for shape in _SHAPES:
		t, c = summary[shape]["take"], summary[shape]["clone"]
		print(f"  {shape:9s} take {t['iter_ns_median']}ns (r{t['ratio_median']}) "
		      f"vs clone {c['iter_ns_median']}ns (r{c['ratio_median']}) "
		      f"=> take {'faster' if t['iter_ns_median'] < c['iter_ns_median'] else 'slower'} "
		      f"by {abs(t['iter_ns_median'] - c['iter_ns_median']):.1f}ns")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
