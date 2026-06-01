#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Scenario-agnostic parallel job executor for test/perf/stress gates.

This is the shared "runner plumbing" that package teams (drift-web,
mariadb-client, net-*) were each re-rolling as a ~500-700 line shell fork
(`tools/drift_test_parallel_runner.sh`).  Per the shared-test-runner RFC it
implements **mechanism, never scenario policy**: it executes a project-supplied
*plan* of jobs; it knows nothing about databases, servers, queues, lanes,
sanitizers, or what a gate means.  Those stay entirely in the plan content and
in the team's own harness that brackets execution.

A plan (JSON) is ordered **phases**; a phase is a list of **jobs**.  The engine
runs phases in order (a barrier between them); within a phase it runs parallel
jobs concurrently (bounded by the flocker pool) and serial jobs one-at-a-time
per named group.

Concurrency budget (load-bearing): the parallel pool size N is sourced from the
`pytest_jobs.py` protocol (`DRIFT_TEST_JOBS`, else `ceil(nproc/2)`) — never
hardcoded — and every job is wrapped in `flocker --key <pool> -j N`, so several
concurrent runs/lanes on one host stay bounded by the *single* host-global
flocker semaphore instead of multiplying past RAM.  See docs/test-run.md and
docs/flocker.md.

Usage:
  tools/drift_test_run.py --plan PATH --work-dir DIR [options]

The plan format and a worked example are documented in docs/test-run.md.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

_VALID_MODES = ("parallel", "serial")
_VALID_WRAPS = ("none", "memcheck", "massif")

# Canonical instrumentation incantations — owned HERE so a leak/sanitizer policy
# change happens in one place, not in every team's fork (RFC #3).
#   --error-exitcode=97  : distinguishes a tool-detected error from the program's
#                          own exit codes (teams already standardized on 97).
#   --fair-sched=yes     : forces fair thread scheduling so a non-cooperative
#                          busy-spin under Valgrind can't starve the rest of the
#                          program and surface as an opaque timeout (the
#                          hard-won default from the concurrency-test lane).
_WRAP_VALGRIND = {
	"memcheck": [
		"valgrind", "--tool=memcheck", "--fair-sched=yes",
		"--error-exitcode=97", "--leak-check=full", "--errors-for-leak-kinds=definite,possible",
	],
	"massif": [
		"valgrind", "--tool=massif", "--fair-sched=yes", "--error-exitcode=97",
	],
}

# Sanitizer runtime-option defaults applied to every job's env unless the job (or
# the ambient env) already sets them.  Harmless for non-sanitized binaries.
_SANITIZER_ENV_DEFAULTS = {
	"ASAN_OPTIONS": "detect_leaks=0:halt_on_error=1",
	"UBSAN_OPTIONS": "halt_on_error=1:print_stacktrace=1",
}


class PlanError(Exception):
	"""Raised for a malformed plan or invalid CLI combination (exit 2)."""


@dataclass
class Job:
	id: str
	cmd: list[str]
	mode: str = "parallel"
	group: str | None = None      # serial only: resource + ordering bucket
	order: int = 0                # serial only: sequence within the group
	needs: list[str] = field(default_factory=list)
	env: dict[str, str] = field(default_factory=dict)
	wrap: str = "none"
	out: str | None = None        # dedup key: same out ⇒ run once


@dataclass
class JobResult:
	id: str
	phase: str
	status: str          # "ok" | "fail" | "skipped"
	exit_code: int | None
	wall_s: float
	log_path: str | None


# ── Plan loading & validation ────────────────────────────────────────────

def load_plan(path: Path) -> tuple[str, list[tuple[str, list[Job]]]]:
	"""Parse + validate a plan file.  Returns (plan_name, [(phase_name, jobs)])."""
	try:
		raw = json.loads(path.read_text(encoding="utf-8"))
	except FileNotFoundError:
		raise PlanError(f"plan not found: {path}")
	except json.JSONDecodeError as e:
		raise PlanError(f"plan is not valid JSON: {path}: {e}")
	if not isinstance(raw, dict):
		raise PlanError("plan must be a JSON object")

	name = raw.get("name")
	if not isinstance(name, str) or not name:
		raise PlanError("plan: 'name' must be a non-empty string")
	phases_raw = raw.get("phases")
	if not isinstance(phases_raw, list) or not phases_raw:
		raise PlanError("plan: 'phases' must be a non-empty array")

	phases: list[tuple[str, list[Job]]] = []
	seen_ids: dict[str, int] = {}   # id -> phase index, for needs validation
	for pi, ph in enumerate(phases_raw):
		if not isinstance(ph, dict):
			raise PlanError(f"phase #{pi}: must be an object")
		pname = ph.get("name")
		if not isinstance(pname, str) or not pname:
			raise PlanError(f"phase #{pi}: 'name' must be a non-empty string")
		jobs_raw = ph.get("jobs")
		if not isinstance(jobs_raw, list) or not jobs_raw:
			raise PlanError(f"phase '{pname}': 'jobs' must be a non-empty array")
		jobs: list[Job] = []
		for ji, jr in enumerate(jobs_raw):
			jobs.append(_parse_job(jr, pname, ji, seen_ids, pi))
		phases.append((pname, jobs))

	# Second pass: validate needs now that all ids are known.
	for pi, (pname, jobs) in enumerate(phases):
		for job in jobs:
			for need in job.needs:
				if need not in seen_ids:
					raise PlanError(
						f"job '{job.id}': needs unknown job id '{need}'"
					)
				if seen_ids[need] >= pi:
					raise PlanError(
						f"job '{job.id}' needs '{need}', but it is not in an earlier "
						f"phase.  This executor uses phase barriers as the dependency "
						f"mechanism — put a needed job in an earlier phase."
					)
	return name, phases


def _parse_job(jr, pname: str, ji: int, seen_ids: dict[str, int], pi: int) -> Job:
	where = f"phase '{pname}' job #{ji}"
	if not isinstance(jr, dict):
		raise PlanError(f"{where}: must be an object")
	jid = jr.get("id")
	if not isinstance(jid, str) or not jid:
		raise PlanError(f"{where}: 'id' must be a non-empty string")
	if jid in seen_ids:
		raise PlanError(f"duplicate job id '{jid}'")
	cmd = jr.get("cmd")
	if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) for c in cmd):
		raise PlanError(f"job '{jid}': 'cmd' must be a non-empty array of strings")
	mode = jr.get("mode", "parallel")
	if mode not in _VALID_MODES:
		raise PlanError(f"job '{jid}': 'mode' must be one of {_VALID_MODES} (got '{mode}')")
	wrap = jr.get("wrap", "none")
	if wrap not in _VALID_WRAPS:
		raise PlanError(f"job '{jid}': 'wrap' must be one of {_VALID_WRAPS} (got '{wrap}')")
	# `key` is accepted as an alias for `group` (RFC names it `key`; the
	# drift-web spec names it `group`).  They mean the same thing: the serial
	# resource + ordering bucket.
	group = jr.get("group", jr.get("key"))
	if group is not None and not isinstance(group, str):
		raise PlanError(f"job '{jid}': 'group'/'key' must be a string")
	if mode == "serial" and group is None:
		group = jid   # a serial job with no group serializes only against itself
	order = jr.get("order", 0)
	if not isinstance(order, int):
		raise PlanError(f"job '{jid}': 'order' must be an integer")
	needs = jr.get("needs", [])
	if not isinstance(needs, list) or not all(isinstance(n, str) for n in needs):
		raise PlanError(f"job '{jid}': 'needs' must be an array of strings")
	env = jr.get("env", {})
	if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
		raise PlanError(f"job '{jid}': 'env' must be an object of string→string")
	out = jr.get("out")
	if out is not None and not isinstance(out, str):
		raise PlanError(f"job '{jid}': 'out' must be a string")
	seen_ids[jid] = pi
	return Job(id=jid, cmd=list(cmd), mode=mode, group=group, order=int(order),
	           needs=list(needs), env=dict(env), wrap=wrap, out=out)


# ── Command construction ─────────────────────────────────────────────────

def resolve_tool(name: str, explicit: str | None, repo_root: Path) -> str:
	"""Resolve a tool path: explicit flag, then repo bin/, then PATH."""
	if explicit:
		return explicit
	sibling = repo_root / "bin" / name
	if sibling.exists():
		return str(sibling)
	found = shutil.which(name)
	if found:
		return found
	# Defer the error to use-time: a plan may not reference this tool at all.
	return name


def substitute(cmd: list[str], subs: dict[str, str]) -> list[str]:
	out = []
	for tok in cmd:
		for k, v in subs.items():
			tok = tok.replace("{" + k + "}", v)
		out.append(tok)
	return out


def build_argv(job: Job, *, flocker: str, pool_key: str, jobs_n: int, subs: dict[str, str]) -> list[str]:
	"""Construct the full flocker-wrapped argv for a job."""
	inner = substitute(job.cmd, subs)
	if job.wrap != "none":
		inner = _WRAP_VALGRIND[job.wrap] + inner
	if job.mode == "serial":
		# One-at-a-time on the named resource: -j 1 on a per-group key.
		return [flocker, "--key", f"serial-{job.group}", "-j", "1", "--", *inner]
	# Parallel: share the host-global pool at the budgeted size.
	return [flocker, "--key", pool_key, "-j", str(jobs_n), "--", *inner]


# ── Execution ────────────────────────────────────────────────────────────

class Heartbeat:
	"""Executor-owned watchdog feed.

	The executor — not flocker — emits the heartbeat, because it has the best
	view of the whole run (phase, running/done/failed counts, elapsed).  It
	prints to stdout, which is the point: it keeps a stdout-inactivity watchdog
	alive across long silent stretches (a big compile, a memcheck grind).
	"""

	def __init__(self, interval: int | None, total: int):
		self.interval = interval
		self.total = total
		self._lock = threading.Lock()
		self.phase = ""
		self.running = 0
		self.done = 0
		self.failed = 0
		self._stop = threading.Event()
		self._thread: threading.Thread | None = None
		self._t0 = time.monotonic()

	def start(self):
		if not self.interval:
			return
		self._thread = threading.Thread(target=self._loop, daemon=True)
		self._thread.start()

	def stop(self):
		self._stop.set()
		if self._thread:
			self._thread.join(timeout=1)

	def set_phase(self, name: str):
		with self._lock:
			self.phase = name

	def inc_running(self, d: int):
		with self._lock:
			self.running += d

	def inc_done(self, failed: bool):
		with self._lock:
			self.done += 1
			self.running -= 1
			if failed:
				self.failed += 1

	def _loop(self):
		while not self._stop.wait(self.interval):
			with self._lock:
				elapsed = int(time.monotonic() - self._t0)
				line = (f"[drift-test-run] phase={self.phase} running={self.running} "
				        f"done={self.done}/{self.total} failed={self.failed} elapsed={elapsed}s")
			print(line, flush=True)


def run_job(job: Job, argv: list[str], logs_dir: Path, base_env: dict[str, str]) -> JobResult:
	env = dict(base_env)
	for k, v in _SANITIZER_ENV_DEFAULTS.items():
		env.setdefault(k, v)
	env.update(job.env)   # explicit per-job overlay wins
	log_path = logs_dir / f"{_safe(job.id)}.log"
	t0 = time.monotonic()
	with open(log_path, "wb") as logf:
		try:
			proc = subprocess.run(argv, stdout=logf, stderr=subprocess.STDOUT, env=env)
			code = proc.returncode
		except FileNotFoundError as e:
			logf.write(f"executor: command not found: {e}\n".encode())
			code = 127
	wall = time.monotonic() - t0
	status = "ok" if code == 0 else "fail"
	return JobResult(id=job.id, phase="", status=status, exit_code=code,
	                 wall_s=wall, log_path=str(log_path))


def _safe(s: str) -> str:
	return "".join(c if c.isalnum() or c in "._-" else "_" for c in s)


def run_phase(
	pname: str,
	jobs: list[Job],
	*,
	flocker: str,
	pool_key: str,
	jobs_n: int,
	subs: dict[str, str],
	logs_dir: Path,
	base_env: dict[str, str],
	seen_out: set[str],
	hb: Heartbeat,
	dry_run: bool,
) -> list[JobResult]:
	hb.set_phase(pname)
	results: list[JobResult] = []
	results_lock = threading.Lock()

	# Dedup: a job whose `out` was already produced this run is skipped.
	runnable: list[Job] = []
	for job in jobs:
		if job.out is not None:
			out_resolved = substitute([job.out], subs)[0]
			if out_resolved in seen_out:
				results.append(JobResult(job.id, pname, "skipped", None, 0.0, None))
				print(f"  skip  {job.id} (dedup: {out_resolved} already built)", flush=True)
				continue
			seen_out.add(out_resolved)
		runnable.append(job)

	parallel = [j for j in runnable if j.mode == "parallel"]
	serial_groups: dict[str, list[Job]] = {}
	for j in runnable:
		if j.mode == "serial":
			serial_groups.setdefault(j.group, []).append(j)
	for g in serial_groups.values():
		g.sort(key=lambda j: (j.order, j.id))

	def execute(job: Job) -> JobResult:
		argv = build_argv(job, flocker=flocker, pool_key=pool_key, jobs_n=jobs_n, subs=subs)
		if dry_run:
			print(f"  plan  {job.id}: {' '.join(argv)}", flush=True)
			return JobResult(job.id, pname, "ok", 0, 0.0, None)
		hb.inc_running(1)
		res = run_job(job, argv, logs_dir, base_env)
		res.phase = pname
		hb.inc_done(res.status == "fail")
		mark = "ok  " if res.status == "ok" else "FAIL"
		print(f"  {mark}  {job.id} ({res.wall_s:.1f}s, exit={res.exit_code})", flush=True)
		return res

	def run_serial_group(group_jobs: list[Job]) -> list[JobResult]:
		out = []
		for job in group_jobs:
			out.append(execute(job))
		return out

	# Parallel jobs: a local pool of size N (so we don't spawn a flocker waiter
	# per job); flocker provides the host-global cap across concurrent runs.
	# Serial groups: one worker thread each, running their jobs in order; they
	# proceed alongside the parallel pool and each other (distinct flocker keys).
	with concurrent.futures.ThreadPoolExecutor(
		max_workers=max(1, jobs_n) + len(serial_groups),
		thread_name_prefix="dtr",
	) as pool:
		futs = []
		# Submit parallel jobs (the pool's max_workers bounds local parallelism;
		# extra capacity beyond jobs_n is reserved for the serial-group runners).
		for job in parallel:
			futs.append(pool.submit(execute, job))
		serial_futs = [pool.submit(run_serial_group, g) for g in serial_groups.values()]
		for f in concurrent.futures.as_completed(futs):
			with results_lock:
				results.append(f.result())
		for f in concurrent.futures.as_completed(serial_futs):
			with results_lock:
				results.extend(f.result())
	return results


# ── CLI ──────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
	script_dir = Path(__file__).resolve().parent
	repo_root = script_dir.parent

	p = argparse.ArgumentParser(
		prog="drift_test_run.py",
		description="Scenario-agnostic parallel job executor (see docs/test-run.md).",
	)
	p.add_argument("--plan", type=Path, required=True, help="Path to the plan JSON file")
	p.add_argument("--work-dir", type=Path, required=True,
	               help="Work dir for {work} substitution + job logs")
	p.add_argument("--jobs", type=int, default=None,
	               help="Parallel pool size N. Default: the pytest_jobs.py protocol "
	                    "(DRIFT_TEST_JOBS, else ceil(nproc/2)). Do not hardcode in plans.")
	p.add_argument("--driftc", type=str, default=None, help="Path to driftc ({driftc})")
	p.add_argument("--drift", type=str, default=None, help="Path to drift ({drift})")
	p.add_argument("--flocker", type=str, default=None, help="Path to flocker")
	p.add_argument("--pool-key", type=str, default="drift-jobs",
	               help="flocker key for the parallel pool (default: drift-jobs)")
	p.add_argument("--heartbeat", type=int, default=None, metavar="SECS",
	               help="Emit an executor heartbeat line every SECS (default: off)")
	p.add_argument("--report", type=Path, default=None,
	               help="Write a JSON per-job result report to this path")
	p.add_argument("--keep-going", action="store_true",
	               help="Run all phases even after a phase has failures (default: stop)")
	p.add_argument("--dry-run", action="store_true",
	               help="Print the resolved flocker argv per job; do not execute")
	args = p.parse_args(argv)

	if args.heartbeat is not None and args.heartbeat <= 0:
		print("drift_test_run: --heartbeat must be a positive integer", file=sys.stderr)
		return 2
	if args.jobs is not None and args.jobs <= 0:
		print("drift_test_run: --jobs must be a positive integer", file=sys.stderr)
		return 2

	try:
		plan_name, phases = load_plan(args.plan)
	except PlanError as e:
		print(f"drift_test_run: {e}", file=sys.stderr)
		return 2

	# Budget: the load-bearing requirement — source N from the pytest_jobs.py
	# protocol unless the operator explicitly overrides with --jobs.
	if args.jobs is not None:
		jobs_n = args.jobs
	else:
		try:
			sys.path.insert(0, str(script_dir))
			from pytest_jobs import recommended_workers
			jobs_n = recommended_workers()
		except Exception:
			jobs_n = max(1, (os.cpu_count() or 2) // 2)

	flocker = resolve_tool("flocker", args.flocker, repo_root)
	driftc = resolve_tool("driftc", args.driftc, repo_root)
	drift = resolve_tool("drift", args.drift, repo_root)
	work_dir = args.work_dir.resolve()
	work_dir.mkdir(parents=True, exist_ok=True)
	logs_dir = work_dir / "logs"
	logs_dir.mkdir(parents=True, exist_ok=True)

	subs = {
		"work": str(work_dir),
		"driftc": driftc,
		"drift": drift,
		"jobs": str(jobs_n),
	}

	total_jobs = sum(len(j) for _, j in phases)
	print(f"drift-test-run: plan='{plan_name}' phases={len(phases)} jobs={total_jobs} "
	      f"-j {jobs_n} pool='{args.pool_key}'"
	      + (" [dry-run]" if args.dry_run else ""), flush=True)

	hb = Heartbeat(args.heartbeat, total_jobs)
	hb.start()
	all_results: list[JobResult] = []
	seen_out: set[str] = set()
	failed_phase = False
	t0 = time.monotonic()
	try:
		for pname, jobs in phases:
			print(f"phase '{pname}' ({len(jobs)} jobs)", flush=True)
			res = run_phase(
				pname, jobs,
				flocker=flocker, pool_key=args.pool_key, jobs_n=jobs_n,
				subs=subs, logs_dir=logs_dir, base_env=dict(os.environ),
				seen_out=seen_out, hb=hb, dry_run=args.dry_run,
			)
			all_results.extend(res)
			if any(r.status == "fail" for r in res):
				failed_phase = True
				if not args.keep_going:
					print(f"phase '{pname}' had failures — stopping (use --keep-going to continue)",
					      flush=True)
					break
	finally:
		hb.stop()

	elapsed = time.monotonic() - t0
	n_ok = sum(1 for r in all_results if r.status == "ok")
	n_fail = sum(1 for r in all_results if r.status == "fail")
	n_skip = sum(1 for r in all_results if r.status == "skipped")
	print(f"drift-test-run: {n_ok} ok, {n_fail} failed, {n_skip} skipped "
	      f"in {elapsed:.1f}s", flush=True)

	if n_fail and not args.dry_run:
		print("failed jobs:", flush=True)
		for r in all_results:
			if r.status == "fail":
				tail = _log_tail(r.log_path)
				print(f"  ✗ {r.id} (exit={r.exit_code})"
				      + (f"\n      {tail}" if tail else ""), flush=True)

	if args.report:
		report = {
			"plan": plan_name,
			"jobs_n": jobs_n,
			"elapsed_s": round(elapsed, 3),
			"results": [
				{"id": r.id, "phase": r.phase, "status": r.status,
				 "exit_code": r.exit_code, "wall_s": round(r.wall_s, 3),
				 "log": r.log_path}
				for r in all_results
			],
		}
		args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")

	return 1 if (failed_phase or n_fail) else 0


def _log_tail(path: str | None, lines: int = 8) -> str:
	if not path:
		return ""
	try:
		text = Path(path).read_text(encoding="utf-8", errors="replace")
	except OSError:
		return ""
	tail = text.strip().splitlines()[-lines:]
	return "\n      ".join(tail)


if __name__ == "__main__":
	sys.exit(main())
