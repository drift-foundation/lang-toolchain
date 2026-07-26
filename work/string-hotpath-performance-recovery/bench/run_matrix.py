# string-hotpath-performance-recovery: interleaved factorial matrix
# runner (rev 3 — reproducible from the tree; review residual 1).
#
# Builds EVERY side itself into a fresh temp directory (configurable
# via STRING_BENCH_WORKDIR; default mkdtemp) from checked-in sources:
#   * prim_bench.drift compiled with the certified 0.33.87 driftc and
#     with the current tree's driftc;
#   * every ablation runtime regenerated via gen_ablations.py
#     (--check enforced against the preserved sources), compiled, and
#     linked against the current-tree IR + runtime archive.
# Records: both driftc identities, C compiler version, exact build,
# link, and run commands, sha256 of every source and binary, the
# per-launch side order (deterministic shuffle, seed recorded), and
# host/loadavg provenance.  FAILS CLOSED on: missing sides, missing
# rows, row-count mismatches, non-zero exits, or hash mismatches
# between what was built and what was run.  DRIFT_STR_TRACE and
# DRIFT_STR_TRACE_FILTER are scrubbed from every timing run.
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
RESULTS = BENCH / "results"
CERT_DRIFTC = Path.home() / "opt/drift/certified/current/toolchain/bin/driftc"
CLANG = "/usr/bin/clang"
PY = str(ROOT / ".venv" / "bin" / "python")

ABLATIONS = ["ab_cached_current", "ab_cached_branchlean",
             "ab_none_current", "ab_none_branchlean", "ab_lean_ref"]
EXPECTED_ROWS = 18  # RESULT lines per launch of prim_bench


def sha256(p: Path) -> str:
	return hashlib.sha256(p.read_bytes()).hexdigest()


def run(cmd, log, **kw):
	log.append({"cmd": [str(c) for c in cmd]})
	r = subprocess.run([str(c) for c in cmd], capture_output=True,
	                   text=True, **kw)
	if r.returncode != 0:
		raise SystemExit(
			f"FAIL-CLOSED: command exited {r.returncode}:\n"
			f"{' '.join(str(c) for c in cmd)}\n{r.stdout[-800:]}\n{r.stderr[-800:]}")
	return r


def _git(*args) -> str:
	return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
	                      text=True).stdout.strip()


def build_all(work: Path, log: list) -> tuple[dict[str, Path], dict]:
	sides: dict[str, Path] = {}
	prov: dict = {"sources": {}, "binaries": {}}

	src = BENCH / "prim_bench.drift"
	prov["sources"]["prim_bench.drift"] = sha256(src)

	# certified side
	if not CERT_DRIFTC.exists():
		raise SystemExit(f"FAIL-CLOSED: certified driftc missing at {CERT_DRIFTC}")
	p87 = work / "prim_87.bin"
	run([CERT_DRIFTC, src, "--entry", "main::main", "-o", p87], log)
	sides["87_certified"] = p87

	# current-tree side
	pcur = work / "prim_cur.bin"
	run([PY, "-m", "lang.driftc.driftc", "--dev", "--stdlib-root",
	     str(ROOT / "stdlib"), src, "--entry", "main::main", "-o", pcur],
	    log, cwd=ROOT)
	sides["cur_abi22"] = pcur
	cur_ll = Path(str(pcur) + ".ll")

	# ablations: use the PRESERVED sources (frozen decision evidence);
	# --check verifies byte-for-byte reproduction while the tree still
	# matches the pinned evidence base, and reports the base change
	# once the accepted fix has landed.
	run([PY, str(BENCH / "gen_ablations.py"), "--check"], log, cwd=ROOT)
	archive = ROOT / "build/runtime_libs/default/libdrift_rt_abi22.a"
	if not archive.exists():
		raise SystemExit(f"FAIL-CLOSED: runtime archive missing at {archive}")
	for ab in ABLATIONS:
		absrc = BENCH / "ablations" / f"{ab}.c"
		prov["sources"][f"ablations/{ab}.c"] = sha256(absrc)
		obj = work / f"{ab}.o"
		run([CLANG, "-std=gnu11", "-O2", "-pthread",
		     "-I", ROOT / "lang/language_runtime", "-I", ROOT / "lang",
		     "-c", absrc, "-o", obj], log)
		out = work / f"prim_{ab}.bin"
		run([CLANG, "-fuse-ld=gold", "-O2", "-x", "ir", cur_ll,
		     "-x", "none", obj, archive, "-lz", "-Wl,--as-needed",
		     "-pthread", "-o", out], log)
		sides[ab.replace("ab_", "")] = out

	for name, binp in sides.items():
		prov["binaries"][name] = sha256(binp)
	return sides, prov


def timing_env() -> dict:
	env = dict(os.environ)
	env.pop("DRIFT_STR_TRACE", None)
	env.pop("DRIFT_STR_TRACE_FILTER", None)
	return env


def main():
	launches = int(os.environ.get("STRING_BENCH_LAUNCHES", "5"))
	seed = int(os.environ.get("STRING_BENCH_SEED", "20260726"))
	workdir = os.environ.get("STRING_BENCH_WORKDIR")
	if workdir:
		work = Path(workdir)
	else:
		from lang.test_support.drift_tmp import session_root
		work = Path(tempfile.mkdtemp(prefix="string-hotpath-matrix-",
		                             dir=session_root()))
	work.mkdir(parents=True, exist_ok=True)

	build_log: list = []
	sides, prov = build_all(work, build_log)

	cc_id = subprocess.run([CLANG, "--version"], capture_output=True,
	                       text=True).stdout.splitlines()[0]
	cert_id = subprocess.run([str(CERT_DRIFTC), "--version"],
	                         capture_output=True, text=True).stdout.strip()

	rng = random.Random(seed)
	env = timing_env()
	rows: dict[str, dict[str, list[float]]] = {}
	loads = [os.getloadavg()[0]]
	orders = []
	for launch in range(launches):
		order = list(sides)
		rng.shuffle(order)
		orders.append(order)
		for side in order:
			binp = sides[side]
			if sha256(binp) != prov["binaries"][side]:
				raise SystemExit(f"FAIL-CLOSED: binary hash changed mid-run: {side}")
			r = subprocess.run([str(binp)], capture_output=True, text=True,
			                   timeout=600, env=env)
			if r.returncode != 0:
				raise SystemExit(
					f"FAIL-CLOSED: {side} exited {r.returncode}\n{r.stdout[-400:]}")
			found = 0
			for line in r.stdout.splitlines():
				m = re.match(r"RESULT (\w+) us=([\d,]+),?$", line)
				if m:
					found += 1
					med = statistics.median(
						int(x) for x in m.group(2).split(",") if x)
					rows.setdefault(m.group(1), {}).setdefault(
						side, []).append(med)
			if found != EXPECTED_ROWS:
				raise SystemExit(
					f"FAIL-CLOSED: {side} produced {found} RESULT rows, "
					f"expected {EXPECTED_ROWS}")
		loads.append(os.getloadavg()[0])
		print(f"launch {launch + 1}/{launches} done (load {loads[-1]:.2f}, "
		      f"order {'>'.join(order)})", flush=True)

	# fail closed on missing side data in any row
	for name, per_side in rows.items():
		missing = [s for s in sides if s not in per_side
		           or len(per_side[s]) != launches]
		if missing:
			raise SystemExit(f"FAIL-CLOSED: row {name} missing sides {missing}")

	out = {
		"provenance": {
			"commit": _git("rev-parse", "HEAD"),
			"tree_dirty": bool(_git("status", "--porcelain",
			                        "--untracked-files=no")),
			"certified_driftc": cert_id,
			"current_driftc": f"tree {_git('rev-parse', 'HEAD')[:12]} via {PY} -m lang.driftc.driftc --dev",
			"c_compiler": cc_id,
			"host": platform.node(),
			"loadavg_samples": [round(x, 2) for x in loads],
			"timestamp_utc": datetime.datetime.now(
				datetime.timezone.utc).isoformat(),
			"command": " ".join(sys.argv),
			"launches": launches,
			"shuffle_seed": seed,
			"side_orders": orders,
			"workdir": str(work),
			"env_scrubbed": ["DRIFT_STR_TRACE", "DRIFT_STR_TRACE_FILTER"],
			**prov,
			"build_commands": build_log,
		},
		"rows": {},
	}
	names = list(sides)
	print("\n| row | " + " | ".join(names) + " |")
	print("|" + "---|" * (len(names) + 1))
	for name, per_side in rows.items():
		meds = {s: statistics.median(v) for s, v in per_side.items()}
		out["rows"][name] = {s: {"median_us": statistics.median(v),
		                         "launch_medians_us": v}
		                     for s, v in per_side.items()}
		print(f"| {name} | " + " | ".join(
			str(int(meds[s])) for s in names) + " |")
	RESULTS.mkdir(exist_ok=True)
	ts = datetime.datetime.now(
		datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
	(RESULTS / f"matrix-{ts}.json").write_text(json.dumps(out, indent=1))
	print(f"\nresults written: results/matrix-{ts}.json")


if __name__ == "__main__":
	main()
