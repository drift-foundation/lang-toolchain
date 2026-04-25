#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 3A Task #5 driver — compile a representative slice of e2e cases
with the ownership ledger in observe mode, capturing all records to
disk for triage aggregation.

Strategy:
- Walk lang/tests/codegen/e2e/, collect cases whose main.drift has a
  `module main;` (or other `module X;`) declaration so they compile on
  their own with --emit-ir running the full pipeline.
- Compile each in parallel (worker pool, 8-way) with
  `DRIFT_COMPILER_DEBUG='{"ownership_ledger":true}'` set.  --emit-ir
  forces the full pipeline so string_arc (sites 3/4) runs and HIR→MIR
  recording (sites 1/2) drains.
- Capture stderr per case to build/ownership-ledger/triage/triage-raw/<case>.log.
- Print a one-line progress event per case (for Monitor consumption).

Aggregation is a separate script (`aggregate_triage.py`) that reads
the per-case logs and writes the bucketed report.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STDLIB = REPO / "stdlib"
E2E_ROOT = REPO / "lang/tests/codegen/e2e"
# Policy: transient artifacts (IR, logs) live under `build/`, never in
# `lang/` or `work/`.  The repo convention is: source tree = source;
# `build/` = generated.
BUILD_ROOT = REPO / "build/ownership-ledger/triage"
RAW_DIR = BUILD_ROOT / "triage-raw"


def collect_cases() -> list[Path]:
	"""Return a list of e2e case directories whose main.drift declares
	a module — those are reliably compilable as a single unit."""
	out: list[Path] = []
	for case_dir in sorted(E2E_ROOT.iterdir()):
		if not case_dir.is_dir():
			continue
		main = case_dir / "main.drift"
		if not main.exists():
			continue
		try:
			head = main.read_text(errors="replace").splitlines()[:5]
		except Exception:
			continue
		if any(line.strip().startswith("module ") for line in head):
			out.append(case_dir)
	return out


def compile_case(case_dir: Path) -> tuple[str, int, int, int]:
	"""Compile one case with the ledger flag on.  Returns
	`(case_name, exit_code, stderr_bytes, ledger_record_count)`."""
	name = case_dir.name
	log_path = RAW_DIR / f"{name}.log"
	# Per-case build mirror under build/ — case path relative to REPO
	# so cases from lang/tests/codegen/e2e/<case> land at
	# build/ownership-ledger/triage/lang/tests/codegen/e2e/<case>/.
	build_dir = BUILD_ROOT / case_dir.relative_to(REPO)
	build_dir.mkdir(parents=True, exist_ok=True)
	ir_path = build_dir / "out.ll"
	# Find drift sources in the case dir (one or more .drift files).
	drift_files = sorted(str(p) for p in case_dir.rglob("*.drift"))
	# Prefer workspace mode with -M when the case has a module decl —
	# matches what the e2e runner does for declared cases.
	main_text = (case_dir / "main.drift").read_text(errors="replace")
	has_module_decl = any(
		ln.strip().startswith("module ") and ln.strip().endswith(";")
		for ln in main_text.splitlines()[:5]
	)
	cmd = [str(REPO / ".venv/bin/python3"), "-m", "lang.driftc"]
	if has_module_decl:
		cmd += ["-M", str(case_dir)]
	cmd += ["--stdlib-root", str(STDLIB), "--dev", "--emit-ir", str(ir_path)]
	cmd += drift_files
	env = dict(os.environ)
	env["PYTHONPATH"] = str(REPO)
	env["DRIFT_COMPILER_DEBUG"] = '{"ownership_ledger":true}'
	try:
		res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
		stderr = res.stderr
		exit_code = res.returncode
	except subprocess.TimeoutExpired:
		stderr = "[runner] TIMEOUT\n"
		exit_code = 124
	log_path.write_text(stderr)
	# Cheap record count: lines starting with the prefix.
	count = sum(1 for ln in stderr.splitlines() if ln.startswith("[drift:ownership_ledger] "))
	return name, exit_code, len(stderr), count


def main() -> int:
	ap = argparse.ArgumentParser()
	ap.add_argument("--limit", type=int, default=0, help="Cap cases (0 = all)")
	ap.add_argument("--workers", type=int, default=16)
	args = ap.parse_args()
	BUILD_ROOT.mkdir(parents=True, exist_ok=True)
	RAW_DIR.mkdir(parents=True, exist_ok=True)
	cases = collect_cases()
	if args.limit:
		cases = cases[: args.limit]
	print(f"[runner] {len(cases)} cases, {args.workers} workers", flush=True)
	total_records = 0
	failures = 0
	timeouts = 0
	with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
		futs = {pool.submit(compile_case, case): case for case in cases}
		done = 0
		for fut in concurrent.futures.as_completed(futs):
			done += 1
			name, code, _err_bytes, count = fut.result()
			total_records += count
			if code == 124:
				timeouts += 1
				status = "TIMEOUT"
			elif code != 0:
				failures += 1
				status = f"exit={code}"
			else:
				status = "ok"
			# One line per case → one Monitor event.  Brief.
			if done % 25 == 0 or status != "ok" or count > 0:
				print(f"[runner] {done:4d}/{len(cases)} {name}: {status} records={count}", flush=True)
	print(f"[runner] DONE total_records={total_records} failures={failures} timeouts={timeouts}", flush=True)
	return 0


if __name__ == "__main__":
	sys.exit(main())
