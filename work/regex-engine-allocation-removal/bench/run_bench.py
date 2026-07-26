# regex-engine-allocation-removal: harness orchestrator (rev 2 —
# reviewer blockers 1+2 resolved).
#   * content-addressed build cache: binaries keyed by source bytes,
#     stdlib tree hash, compiler commit (+dirty), versions.py, and
#     flags — never silently reuses a stale binary;
#   * classified counters: wrapper calls / real allocations / real
#     frees (live-pointer-set exact) / sentinel-noop calls, each
#     reconciled against the model EXACTLY (residual zero);
#   * retain AND release pins: String matching 0/0, view matching
#     exactly +1/+1;
#   * provenance recorded: commit, driftc version/ABI, source hashes,
#     command, timestamp, host, loadavg at each phase;
#   * canonical baseline: results/baseline-quiet.json is written ONLY
#     when REGEX_BENCH_SET_BASELINE=1 AND loadavg stayed < 1.0; every
#     run also writes a load-labeled results/run-<ts>.json;
#   * interleaved compare mode (final gate): REGEX_BENCH_COMPARE=
#     "<baseline_bin>:<candidate_bin>" runs launches ABAB on the same
#     machine and reports per-workload ratios.
# Refuses DRIFT_MEMCHECK/DRIFT_ASAN.  Heartbeats during compiles.
from __future__ import annotations

import datetime
import hashlib
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[3]
SCRATCH = Path(os.environ.get("REGEX_BENCH_SCRATCH", str(BENCH / "build")))
RESULTS = BENCH / "results"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BENCH))

import model  # noqa: E402

if os.environ.get("DRIFT_MEMCHECK") or os.environ.get("DRIFT_ASAN"):
	print("refusing to run under DRIFT_MEMCHECK/DRIFT_ASAN: this is a "
	      "perf/count protocol", file=sys.stderr)
	sys.exit(2)

PY = str(ROOT / ".venv" / "bin" / "python")


# ------------------------------------------------- build provenance

def _git(*args) -> str:
	return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
	                      text=True).stdout.strip()


def _stdlib_hash() -> str:
	h = hashlib.sha256()
	for f in sorted((ROOT / "stdlib").rglob("*.drift")):
		h.update(str(f.relative_to(ROOT)).encode())
		h.update(hashlib.sha256(f.read_bytes()).digest())
	return h.hexdigest()


def _source_hashes() -> dict[str, str]:
	out = {}
	for name in ("ops.drift", "counts.drift", "driver.c", "probe.drift",
	             "probe_driver.c", "model.py", "gen_small.py",
	             "generated/ops_small.drift", "generated/counts_small.drift",
	             "generated/driver_small.c"):
		p = BENCH / name
		if p.exists():
			out[name] = hashlib.sha256(p.read_bytes()).hexdigest()
	return out


DRIFTC_FLAGS = ["--dev", "--stdlib-root", "stdlib"]


def build_key() -> str:
	h = hashlib.sha256()
	h.update(_git("rev-parse", "HEAD").encode())
	h.update(b"dirty" if _git("status", "--porcelain",
	                          "--untracked-files=no") else b"clean")
	h.update((ROOT / "lang" / "versions.py").read_bytes())
	h.update(_stdlib_hash().encode())
	h.update(" ".join(DRIFTC_FLAGS).encode())
	for name, digest in sorted(_source_hashes().items()):
		h.update(name.encode())
		h.update(digest.encode())
	return h.hexdigest()[:16]


def provenance(extra: dict | None = None) -> dict:
	versions = (ROOT / "lang" / "versions.py").read_text()
	out = {
		"commit": _git("rev-parse", "HEAD"),
		"tree_dirty": bool(_git("status", "--porcelain",
		                        "--untracked-files=no")),
		"driftc_version": re.search(r'DRIFTC_VERSION: str = "([^"]+)"',
		                            versions).group(1),
		"abi": re.search(r"DRIFT_RT_ABI_VERSION: int = (\d+)",
		                 versions).group(1),
		"stdlib_hash": _stdlib_hash()[:16],
		"source_hashes": _source_hashes(),
		"driftc_flags": DRIFTC_FLAGS,
		"command": " ".join(sys.argv),
		"timestamp_utc": datetime.datetime.now(
			datetime.timezone.utc).isoformat(),
		"host": platform.node(),
		"build_key": build_key(),
	}
	if extra:
		out.update(extra)
	return out


def run_hb(cmd, label, **kw):
	stop = threading.Event()

	def beat():
		t0 = time.time()
		while not stop.wait(20):
			print(f"hb: {label} running {time.time() - t0:.0f}s", flush=True)

	th = threading.Thread(target=beat, daemon=True)
	th.start()
	try:
		return subprocess.run(cmd, **kw)
	finally:
		stop.set()


def compile_drift(src: Path, out_bin: Path) -> None:
	res = run_hb(
		[PY, "-m", "lang.driftc.driftc", *DRIFTC_FLAGS,
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		f"driftc {src.name}",
		cwd=ROOT, capture_output=True, text=True, timeout=600)
	assert res.returncode == 0, \
		f"compile failed:\n{res.stdout}\n{res.stderr[-3000:]}"


def cached_bin(src_name: str) -> Path:
	"""Content-addressed build: (re)compiles iff the key-stamped binary
	is absent.  A tree/source/flag change changes the key, so a stale
	binary can never be silently reused."""
	key = build_key()
	cache = SCRATCH / f"cache-{key}"
	cache.mkdir(parents=True, exist_ok=True)
	out_bin = cache / f"{Path(src_name).stem}.bin"
	if not out_bin.exists():
		print(f"compiling {src_name} into cache-{key} ...", flush=True)
		compile_drift(BENCH / src_name, out_bin)
	return out_bin


# ------------------------------------------------------- timing

def parse_launch(stdout: str):
	rows, checks = {}, {}
	for line in stdout.splitlines():
		m = re.match(r"RESULT (\w+) us=([\d,]+),?$", line)
		if m:
			rows[m.group(1)] = [int(x) for x in m.group(2).split(",") if x]
		m = re.match(r"CHECK (\w+)=(\d+)", line)
		if m:
			checks[m.group(1)] = int(m.group(2))
	return rows, checks


def timing_phase(src_name: str, launches: int):
	out_bin = cached_bin(src_name)
	load_samples = [os.getloadavg()[0]]
	rows: dict[str, list[list[int]]] = {}
	checks: dict[str, int] = {}
	for launch in range(launches):
		res = subprocess.run([str(out_bin)], capture_output=True,
		                     text=True, timeout=600)
		assert res.returncode == 0, f"timing rc={res.returncode}\n{res.stdout}"
		r, c = parse_launch(res.stdout)
		for k, v in r.items():
			rows.setdefault(k, []).append(v)
		checks.update(c)
		load_samples.append(os.getloadavg()[0])
		print(f"timing launch {launch + 1}/{launches} done "
		      f"(load {load_samples[-1]:.1f})", flush=True)
	summary = {}
	for name, launch_vals in rows.items():
		medians = [statistics.median(v) for v in launch_vals]
		summary[name] = {
			"launch_medians_us": medians,
			"median_of_medians_us": statistics.median(medians),
			"all_us": launch_vals,
		}
	return summary, checks, load_samples


def compare_phase(baseline_bin: str, candidate_bin: str, launches: int):
	"""Final-gate mode: interleave baseline/candidate launches ABAB on
	the same machine; report per-workload median-of-medians ratios."""
	load_samples = [os.getloadavg()[0]]
	sides = {"baseline": Path(baseline_bin), "candidate": Path(candidate_bin)}
	rows = {side: {} for side in sides}
	for launch in range(launches):
		for side, binp in sides.items():
			res = subprocess.run([str(binp)], capture_output=True,
			                     text=True, timeout=600)
			assert res.returncode == 0, f"{side} rc={res.returncode}"
			r, _ = parse_launch(res.stdout)
			for k, v in r.items():
				rows[side].setdefault(k, []).append(statistics.median(v))
			load_samples.append(os.getloadavg()[0])
		print(f"compare launch {launch + 1}/{launches} done", flush=True)
	out = {}
	for name in rows["baseline"]:
		b = statistics.median(rows["baseline"][name])
		c = statistics.median(rows["candidate"].get(name, [float('nan')]))
		out[name] = {"baseline_us": b, "candidate_us": c,
		             "ratio": c / b if b else None,
		             "baseline_launches": rows["baseline"][name],
		             "candidate_launches": rows["candidate"].get(name)}
	return out, load_samples


# ------------------------------------------------------- counts

COUNT_FIELDS = ("retain", "release", "release_real", "release_null",
                "from_utf8", "alloc_calls",
                "alloc_real", "alloc_sentinel", "free_calls", "free_real",
                "free_noop")


def counts_phase(src_name: str = "counts.drift",
                 driver_name: str = "driver.c"):
	out_bin = cached_bin(src_name)
	out_ll = Path(str(out_bin) + ".ll")
	ir = out_ll.read_text()
	ir = re.sub(r"define ([^@\n]*)@main\(", r"define \1@__drift_unused_main(",
	            ir, count=1)
	patched = out_bin.parent / f"{Path(src_name).stem}_patched.ll"
	patched.write_text(ir)

	from lang.language_runtime import build_runtime_archive, runtime_archive_variant
	archive = build_runtime_archive(
		ROOT, clang=shutil.which("clang"),
		variant=runtime_archive_variant(debug_style=False, asan_enabled=False,
		                                alloc_track_enabled=False))
	wraps = [f"-Wl,--wrap={s}" for s in (
		"drift_string_retain", "drift_string_release",
		"drift_string_from_utf8_bytes", "drift_alloc_array",
		"drift_free_array")]
	driver_hash = hashlib.sha256((BENCH / driver_name).read_bytes()).hexdigest()[:12]
	linked = out_bin.parent / f"{Path(src_name).stem}_wrapped-{driver_hash}.bin"
	if not linked.exists():
		res = run_hb(
			["/usr/bin/clang", "-std=gnu11", "-pthread", "-O2",
			 "-x", "ir", str(patched), "-x", "c", str(BENCH / driver_name),
			 "-x", "none", str(archive), *wraps, "-lz", "-Wl,--as-needed",
			 "-I", str(ROOT / "lang" / "language_runtime"), "-o", str(linked)],
			"clang link counts", capture_output=True, text=True, timeout=300)
		assert res.returncode == 0, f"link failed:\n{res.stderr[-3000:]}"

	run = subprocess.run([str(linked)], capture_output=True, text=True,
	                     timeout=600)
	assert run.returncode == 0, \
		f"counts rc={run.returncode}\n{run.stdout}\n{run.stderr[:500]}"
	assert "DONE" in run.stdout and "OPFAIL" not in run.stdout, run.stdout
	obs = {}
	pat = (r"OP=(\S+) r=(-?\d+) retain=(\d+) release=(\d+) "
	       r"release_real=(\d+) release_null=(\d+) from_utf8=(\d+) "
	       r"alloc_calls=(\d+) alloc_real=(\d+) alloc_sentinel=(\d+) "
	       r"free_calls=(\d+) free_real=(\d+) free_noop=(\d+) live_end=(\d+)")
	for line in run.stdout.splitlines():
		m = re.match(pat, line)
		if m:
			vals = [int(x) for x in m.groups()[1:]]
			obs[m.group(1)] = dict(zip(("r",) + COUNT_FIELDS + ("live_end",),
			                           vals))
	return obs


TWIN = {
	"scan_all_64k": "compile_p1", "scan_all_2m": "compile_p1",
	"find_nomatch_64k": "compile_p1", "find_nomatch_2m": "compile_p1",
	"find_nomatch_view_64k": "compile_p1", "alt_64k": "compile_alt",
	"zw_x100": "compile_zw", "short_hit_x100": "compile_p1",
	"short_miss_x100": "compile_p1", "anchor_x100": "compile_anchor",
}
RETAIN_PIN = {label: (1 if label == "find_nomatch_view_64k" else 0)
              for label in TWIN}
RELEASE_PIN = dict(RETAIN_PIN)  # view: the +1 retained backing is released


def reconcile(obs, predictions, twin_map, retain_pins, checks=None):
	report, failures = [], []
	for label, twin in twin_map.items():
		o, t = obs[label], obs[twin]
		window = {k: o[k] - t[k] for k in COUNT_FIELDS}
		pred = predictions[label]["calls"]
		row = {"op": label, "window": window, "predicted": pred,
		       "counters": predictions[label]["counters"],
		       "result_expected": predictions[label]["result"],
		       "result_observed": o["r"], "live_end": o["live_end"]}
		ok = True

		def fail(msg):
			nonlocal ok
			ok = False
			failures.append(f"{label}: {msg}")

		if o["r"] != predictions[label]["result"]:
			fail(f"result {o['r']} != model {predictions[label]['result']}")
		for k in ("alloc_calls", "alloc_real", "alloc_sentinel",
		          "free_calls", "free_real", "free_noop"):
			if window[k] != pred[k]:
				fail(f"{k} window {window[k]} != predicted {pred[k]}")
		pin = retain_pins[label]
		if window["retain"] != pin:
			fail(f"retain window {window['retain']} != pin {pin}")
		# REAL releases must equal the pin exactly (the view's retained
		# backing released once); null-tombstone release CALLS are
		# move-machinery no-ops, reported but not pinned to the retain
		# count — they must still be zero for String-form windows.
		if window["release_real"] != pin:
			fail(f"release_real window {window['release_real']} != pin {pin}")
		if pin == 0 and window["release_null"] != 0:
			fail(f"release_null window {window['release_null']} != 0 on a String-form op")
		if window["from_utf8"] != 0:
			fail(f"from_utf8 window {window['from_utf8']} != 0")
		if o["live_end"] != 0:
			fail(f"live_end {o['live_end']} != 0 (leaked real allocations)")
		row["ok"] = ok
		report.append(row)

	if checks is None:
		return report, failures
	if checks.get("scan_all") != predictions["_check_scan_all_2m"]:
		failures.append(f"timing CHECK scan_all {checks.get('scan_all')} != "
		                f"model {predictions['_check_scan_all_2m']}")
	if checks.get("short_hit") != predictions["_check_short_hit"]:
		failures.append(f"timing CHECK short_hit {checks.get('short_hit')} != "
		                f"model {predictions['_check_short_hit']}")
	return report, failures


def render_md(out) -> str:
	lines = ["# regex bench results", "",
	         f"provenance: commit {out['provenance']['commit'][:12]}"
	         f"{' (dirty)' if out['provenance']['tree_dirty'] else ''}, "
	         f"driftc {out['provenance']['driftc_version']} / "
	         f"ABI {out['provenance']['abi']}, host {out['provenance']['host']}, "
	         f"{out['provenance']['timestamp_utc']}",
	         f"load samples: {out['load_samples']}", ""]
	if out.get("timing"):
		lines += ["## timing (same-launch medians, us)", ""]
		for name, row in out["timing"].items():
			meds = ", ".join(str(int(v)) for v in row["launch_medians_us"])
			lines.append(f"- {name}: median-of-medians "
			             f"{int(row['median_of_medians_us'])} us "
			             f"(launch medians: {meds})")
	if out.get("timing_small"):
		lines += ["", "## small-subject suite (PRIMARY gate): ns/search", ""]
		lines.append("| row | size | scenario | form | ns/search | searches/s |")
		lines.append("|---|---|---|---|---|---|")
		meta = out.get("small_meta", {})
		for name, row in out["timing_small"].items():
			m = meta.get(name, {})
			reps = m.get("reps", 1)
			ns = row["median_of_medians_us"] * 1000.0 / reps
			lines.append(
				f"| {name} | {m.get('size')} | {m.get('scenario')} "
				f"| {m.get('form')} | {ns:,.0f} | {1e9 / ns:,.0f} |")
	if out.get("reconciliation"):
		lines += ["", "## count windows (matching-only = op - compile twin; "
		          "obs=pred, residual must be zero)", ""]
		lines.append("| op | arrays | alloc calls | real allocs | real frees "
		             "| noop frees | retain/release | ok |")
		lines.append("|---|---|---|---|---|---|---|---|")
		for row in out["reconciliation"]:
			w, p = row["window"], row["predicted"]
			lines.append(
				f"| {row['op']} | {p['arrays']} "
				f"| {w['alloc_calls']}={p['alloc_calls']} "
				f"| {w['alloc_real']}={p['alloc_real']} "
				f"| {w['free_real']}={p['free_real']} "
				f"| {w['free_noop']}={p['free_noop']} "
				f"| {w['retain']}/{w['release_real']}+{w['release_null']}n "
				f"| {'PASS' if row['ok'] else 'FAIL'} |")
	if out.get("compare"):
		lines += ["", "## interleaved baseline vs candidate", ""]
		lines.append("| workload | baseline us | candidate us | ratio |")
		lines.append("|---|---|---|---|")
		for name, row in out["compare"].items():
			lines.append(f"| {name} | {row['baseline_us']:.0f} "
			             f"| {row['candidate_us']:.0f} | {row['ratio']:.3f} |")
	lines += ["", f"failures: {out['failures'] if out['failures'] else 'NONE'}", ""]
	return "\n".join(lines)


def main():
	SCRATCH.mkdir(parents=True, exist_ok=True)
	RESULTS.mkdir(parents=True, exist_ok=True)
	launches = int(os.environ.get("REGEX_BENCH_LAUNCHES", "5"))
	ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

	compare_spec = os.environ.get("REGEX_BENCH_COMPARE")
	if compare_spec:
		base, cand = compare_spec.split(":")
		compare, load_samples = compare_phase(base, cand, launches)
		out = {"provenance": provenance(), "compare": compare,
		       "load_samples": [round(x, 2) for x in load_samples],
		       "failures": []}
		(RESULTS / f"compare-{ts}.json").write_text(json.dumps(out, indent=2))
		print(render_md(out))
		return 0

	print("== model predictions ==", flush=True)
	predictions = model.model_windows()
	predictions_small = model.small_windows()
	small_meta = json.loads(
		(BENCH / "generated" / "small_meta.json").read_text())
	print("== count phase (big) ==", flush=True)
	obs = counts_phase()
	print("== count phase (small) ==", flush=True)
	obs_small = counts_phase("generated/counts_small.drift",
	                         "generated/driver_small.c")
	print("== timing phase (big) ==", flush=True)
	timing, checks, load_samples = timing_phase("ops.drift", launches)
	print("== timing phase (small) ==", flush=True)
	timing_small, _sc, load2 = timing_phase("generated/ops_small.drift",
	                                        launches)
	load_samples += load2
	report, failures = reconcile(obs, predictions, TWIN, RETAIN_PIN, checks)
	small_pins = {label: (1 if "_view_" in label else 0)
	              for label in small_meta["twins"]}
	report_small, fail_small = reconcile(obs_small, predictions_small,
	                                     small_meta["twins"], small_pins)
	report += report_small
	failures += fail_small

	max_load = max(load_samples)
	out = {
		"provenance": provenance(),
		"load_samples": [round(x, 2) for x in load_samples],
		"quiet": max_load < 1.0,
		"timing": timing,
		"timing_small": timing_small,
		"small_meta": small_meta["rows"],
		"checks": checks,
		"counts_observed": obs,
		"reconciliation": report,
		"failures": failures,
	}
	run_file = RESULTS / f"run-{ts}{'' if max_load < 1.0 else '-loaded'}.json"
	run_file.write_text(json.dumps(out, indent=2))
	(RESULTS / "RESULTS.md").write_text(render_md(out))

	baseline = RESULTS / "baseline-quiet.json"
	if os.environ.get("REGEX_BENCH_SET_BASELINE") == "1":
		if max_load < 1.0 and not failures:
			baseline.write_text(json.dumps(out, indent=2))
			print(f"canonical quiet baseline written: {baseline}")
		else:
			print(f"NOT writing baseline: load {max_load:.2f} or failures",
			      file=sys.stderr)

	print(render_md(out))
	if failures:
		print("RECONCILIATION FAILED", file=sys.stderr)
		return 1
	print("RECONCILIATION: residual zero on every window")
	return 0


if __name__ == "__main__":
	sys.exit(main())
