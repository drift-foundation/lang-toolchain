# regex-engine-allocation-removal RESUME-STATUS §5.1: the two
# hot-loop ablations, measured in ISOLATED SCRATCH stdlib copies —
# production is mutated only if a variant shows a material,
# repeatable improvement without semantic movement.
#
#   A: workspace/program-size assert moved OUT of
#      _try_match_at_scratch (currently once per candidate start) to
#      once per top-level search (entry points + compat wrappers).
#   B: matching successors collected into one shared worklist and
#      drained ONCE per byte, instead of calling _closure_into
#      separately for every matching state (epoch-guarded sets, so
#      contents are identical).
#
# Measure: tools/perf/regex_bench.drift (representative small-subject
# + wide-alternation suites), interleaved stock/A/B, 5 launches,
# same-launch medians.  Differential harness rerun against each
# variant proves semantic identity before timing counts.
from __future__ import annotations

import datetime
import hashlib
import json
import os
import platform
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[3]
PY = str(ROOT / ".venv" / "bin" / "python")
LAUNCHES = 5
SEED = int(os.environ.get("REGEX_HOTLOOP_SEED", "20260726"))
EXPECTED_ROW_NAMES = {"rx_late_256_x8000", "rx_late_4k_x500",
                      "rx_nomatch_256_x8000", "rx_nomatch_4k_x500",
                      "rx_alt_1k_x2000"}


def timing_env() -> dict:
	env = dict(os.environ)
	env.pop("DRIFT_STR_TRACE", None)
	env.pop("DRIFT_STR_TRACE_FILTER", None)
	return env


def replace_once(s: str, old: str, new: str) -> str:
	assert s.count(old) == 1, f"expected 1 occurrence:\n{old[:100]}"
	return s.replace(old, new, 1)


def variant_a(src: str) -> str:
	"""per-search (not per-start) workspace assert."""
	src = replace_once(src,
		'''	val accept_pc = prog_len - 1;
	assert(sc.prog_len == prog_len, "regex workspace/program size mismatch");''',
		'''	val accept_pc = prog_len - 1;''')
	# assert once per top-level search: in the scratch-threaded finder
	# and in is_match's loop head (before first use)
	src = replace_once(src,
		'''fn _find_from_scratch(nfa: &_NfaProg, src: &text._StringByteSource, from: Int, sc: &mut _NfaScratch) nothrow -> Optional<RegexMatch> {
	val sublen = src.size();''',
		'''fn _find_from_scratch(nfa: &_NfaProg, src: &text._StringByteSource, from: Int, sc: &mut _NfaScratch) nothrow -> Optional<RegexMatch> {
	assert(sc.prog_len == nfa.ops.len, "regex workspace/program size mismatch");
	val sublen = src.size();''')
	src = replace_once(src,
		'''	val src = text._byte_source_all(input);
	var sc = _scratch_new(re.nfa.ops.len);
	val input_len = src.size();
	var start = 0;''',
		'''	val src = text._byte_source_all(input);
	var sc = _scratch_new(re.nfa.ops.len);
	assert(sc.prog_len == re.nfa.ops.len, "regex workspace/program size mismatch");
	val input_len = src.size();
	var start = 0;''')
	src = replace_once(src,
		'''pub fn _try_match_at(nfa: &_NfaProg, input: &String, start: Int) nothrow -> Int {
	val src = text._byte_source_all(input);
	var sc = _scratch_new(nfa.ops.len);''',
		'''pub fn _try_match_at(nfa: &_NfaProg, input: &String, start: Int) nothrow -> Int {
	val src = text._byte_source_all(input);
	var sc = _scratch_new(nfa.ops.len);
	assert(sc.prog_len == nfa.ops.len, "regex workspace/program size mismatch");''')
	return src


def variant_b(src: str) -> str:
	"""shared worklist: seed all matching successors, drain once/byte."""
	src = replace_once(src,
		'''		val cur_base = sc.cur_base;
		val cur_len = sc.cur_len;
		_scratch_next_gen(sc);
		var nxt_len = 0;
		var ci = 0;
		while ci < cur_len {
			val pc = sc.ws[cur_base + ci];
			if _byte_matches(nfa, pc, b) {
				nxt_len = _closure_into(nfa, sc, nxt_base, nxt_len, pc + 1, pos + 1, input_len);
			}
			ci = ci + 1;
		}
		sc.cur_base = nxt_base;
		sc.cur_len = nxt_len;''',
		'''		val cur_base = sc.cur_base;
		val cur_len = sc.cur_len;
		_scratch_next_gen(sc);
		// seed ALL matching successors into one shared worklist,
		// then drain it once (epoch-guarded set: same contents)
		val prog_len_b = sc.prog_len;
		val stack_base = 3 * prog_len_b;
		var slen = 0;
		var ci = 0;
		while ci < cur_len {
			val pc = sc.ws[cur_base + ci];
			if _byte_matches(nfa, pc, b) {
				val nxt = pc + 1;
				if nxt >= 0 and nxt < prog_len_b and sc.ws[nxt] != sc.gen {
					sc.ws[nxt] = sc.gen;
					sc.ws[stack_base + slen] = nxt;
					slen = slen + 1;
				}
			}
			ci = ci + 1;
		}
		val nxt_len = _drain_worklist(nfa, sc, nxt_base, 0, slen, pos + 1, input_len);
		sc.cur_base = nxt_base;
		sc.cur_len = nxt_len;''')
	# _drain_worklist = _closure_into without the seed push
	src = replace_once(src,
		'''fn _try_match_at_scratch(''',
		'''// Variant B: drain an ALREADY-SEEDED worklist (length `slen0`)
// into the target list region under the current epoch.
fn _drain_worklist(nfa: &_NfaProg, sc: &mut _NfaScratch, tgt_base: Int, tgt_len: Int, slen0: Int, pos: Int, input_len: Int) nothrow -> Int {
	val prog_len = sc.prog_len;
	val stack_base = 3 * prog_len;
	var slen = slen0;
	var tl = tgt_len;
	while slen > 0 {
		slen = slen - 1;
		val pc = sc.ws[stack_base + slen];
		val op = nfa.ops[pc];
		match op {
			_NfaOp::Split(a, b) => {
				if b >= 0 and b < prog_len and sc.ws[b] != sc.gen {
					sc.ws[b] = sc.gen;
					sc.ws[stack_base + slen] = b;
					slen = slen + 1;
				}
				if a >= 0 and a < prog_len and sc.ws[a] != sc.gen {
					sc.ws[a] = sc.gen;
					sc.ws[stack_base + slen] = a;
					slen = slen + 1;
				}
			},
			_NfaOp::Jump(target) => {
				if target >= 0 and target < prog_len and sc.ws[target] != sc.gen {
					sc.ws[target] = sc.gen;
					sc.ws[stack_base + slen] = target;
					slen = slen + 1;
				}
			},
			_NfaOp::AssertStart => {
				val nxt = pc + 1;
				if pos == 0 and nxt < prog_len and sc.ws[nxt] != sc.gen {
					sc.ws[nxt] = sc.gen;
					sc.ws[stack_base + slen] = nxt;
					slen = slen + 1;
				}
			},
			_NfaOp::AssertEnd => {
				val nxt = pc + 1;
				if pos == input_len and nxt < prog_len and sc.ws[nxt] != sc.gen {
					sc.ws[nxt] = sc.gen;
					sc.ws[stack_base + slen] = nxt;
					slen = slen + 1;
				}
			},
			_NfaOp::ByteMatch(_) => {
				sc.ws[tgt_base + tl] = pc;
				tl = tl + 1;
			},
			_NfaOp::AnyByte => {
				sc.ws[tgt_base + tl] = pc;
				tl = tl + 1;
			},
			_NfaOp::Ranges(_, _, _) => {
				sc.ws[tgt_base + tl] = pc;
				tl = tl + 1;
			},
			_NfaOp::Accept => {
				sc.ws[tgt_base + tl] = pc;
				tl = tl + 1;
			},
			default => { }
		}
	}
	return tl;
}

fn _try_match_at_scratch(''')
	return src


def build_side(name: str, mutate, work: Path) -> Path:
	stdlib = work / f"stdlib-{name}"
	shutil.copytree(ROOT / "stdlib", stdlib)
	rx = stdlib / "std/regex/regex.drift"
	src = rx.read_text()
	if mutate:
		src = mutate(src)
	rx.write_text(src)
	out = work / f"regex_bench_{name}.bin"
	r = subprocess.run(
		[PY, "-m", "lang.driftc.driftc", "--dev", "--allow-unsafe",
		 "--stdlib-root", str(stdlib),
		 str(ROOT / "tools/perf/regex_bench.drift"),
		 "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=600)
	assert r.returncode == 0, f"{name} compile failed:\n{r.stderr[-1500:]}"
	# semantic identity: the 1000-case differential vs the legacy
	# snapshot must stay at 0 mismatches for every variant
	diff = work / f"diff_{name}.bin"
	r = subprocess.run(
		[PY, "-m", "lang.driftc.driftc", "--dev", "--allow-unsafe",
		 "--stdlib-root", str(stdlib),
		 str(BENCH / "generated/diff_main.drift"),
		 "--entry", "main::main", "-o", str(diff)],
		cwd=ROOT, capture_output=True, text=True, timeout=600)
	assert r.returncode == 0, f"{name} diff compile failed:\n{r.stderr[-1500:]}"
	dr = subprocess.run([str(diff)], capture_output=True, text=True, timeout=300)
	assert dr.returncode == 0 and "DIFF-TOTAL 0 mismatches" in dr.stdout, \
		f"{name}: differential FAILED\n{dr.stdout[-400:]}"
	print(f"{name}: built + differential 0 mismatches", flush=True)
	return out


def main():
	work = Path(tempfile.mkdtemp(prefix="regex-hotloop-"))
	sides = {
		"stock": build_side("stock", None, work),
		"assertA": build_side("assertA", variant_a, work),
		"worklistB": build_side("worklistB", variant_b, work),
	}
	env = timing_env()
	rng = random.Random(SEED)
	orders = []
	loads = [os.getloadavg()[0]]
	rows: dict[str, dict[str, list[float]]] = {}
	for launch in range(LAUNCHES):
		order = list(sides)
		rng.shuffle(order)
		orders.append(order)
		for name in order:
			binp = sides[name]
			r = subprocess.run([str(binp)], capture_output=True, text=True,
			                   timeout=600, env=env)
			if r.returncode != 0:
				raise SystemExit(f"FAIL-CLOSED: {name} exited {r.returncode}")
			seen = set()
			for line in r.stdout.splitlines():
				m = re.match(r"RESULT (\w+) us=([\d,]+),?$", line)
				if m:
					seen.add(m.group(1))
					med = statistics.median(
						int(x) for x in m.group(2).split(",") if x)
					rows.setdefault(m.group(1), {}).setdefault(
						name, []).append(med)
			if seen != EXPECTED_ROW_NAMES:
				raise SystemExit(
					f"FAIL-CLOSED: {name} rows {sorted(seen)} != expected "
					f"{sorted(EXPECTED_ROW_NAMES)}")
		loads.append(os.getloadavg()[0])
		print(f"launch {launch + 1}/{LAUNCHES} done "
		      f"(load {loads[-1]:.2f}, order {'>'.join(order)})", flush=True)
	for name, per in rows.items():
		missing = [sname for sname in sides
		           if sname not in per or len(per[sname]) != LAUNCHES]
		if missing:
			raise SystemExit(f"FAIL-CLOSED: row {name} missing sides {missing}")

	def _git(*args):
		return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
		                      text=True).stdout.strip()

	out = {
		"provenance": {
			"commit": _git("rev-parse", "HEAD"),
			"tree_dirty": bool(_git("status", "--porcelain",
			                        "--untracked-files=no")),
			"host": platform.node(),
			"timestamp_utc": datetime.datetime.now(
				datetime.timezone.utc).isoformat(),
			"launches": LAUNCHES,
			"shuffle_seed": SEED,
			"side_orders": orders,
			"loadavg_samples": [round(x, 2) for x in loads],
			"env_scrubbed": ["DRIFT_STR_TRACE", "DRIFT_STR_TRACE_FILTER"],
			"binaries": {n: hashlib.sha256(Path(b).read_bytes()).hexdigest()
			             for n, b in sides.items()},
			"workdir": str(work),
			"note": ("differential (1000 cases) re-proved 0 mismatches per "
			         "variant during build_side, BEFORE timing"),
		},
		"rows": {},
	}
	print("\n| row | stock | assertA | worklistB | A/stock | B/stock |")
	print("|---|---|---|---|---|---|")
	for name, per in rows.items():
		st = statistics.median(per["stock"])
		a = statistics.median(per["assertA"])
		b = statistics.median(per["worklistB"])
		out["rows"][name] = {
			"launch_medians_us": {sname: v for sname, v in per.items()},
			"median_us": {sname: statistics.median(v)
			              for sname, v in per.items()},
			"ratio_vs_stock": {"assertA": a / st, "worklistB": b / st},
		}
		print(f"| {name} | {int(st)} | {int(a)} | {int(b)} "
		      f"| {a / st:.3f} | {b / st:.3f} |")
	results = BENCH / "results"
	results.mkdir(exist_ok=True)
	ts = datetime.datetime.now(
		datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
	(results / f"hotloop-{ts}.json").write_text(json.dumps(out, indent=1))
	print(f"\nresults written: results/hotloop-{ts}.json")
	print(f"workdir: {work}")


if __name__ == "__main__":
	main()
