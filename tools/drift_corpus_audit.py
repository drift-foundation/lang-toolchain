#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""String-ownership corpus audit runner (cleanup slice 1b).

Promotes the B-arch corpus methodology into repo tooling so ownership
slices get reproducible "identical universe / exact delta" acceptance:
compile the e2e-fixture universe with `DRIFT_STRING_ARC_AUDIT=1`,
aggregate the audit counters, and compare runs exactly.

Per-run outputs (strictly separated by volatility):
  aggregate.json — the COMPARABLE acceptance artifact.  Counters only,
      sorted keys, stable formatting.  No paths, timestamps, PIDs,
      durations, or temp dirs, ever.
  manifest.json  — the universe identity: the verbatim inclusion rule,
      sorted fixture names with WHOLE-DIRECTORY content hashes (companion
      modules, C helpers, expected.json all included), the rule-excluded
      fixtures with reasons, and the compile-success/failure partition —
      plus an informational environment section (driftc/tool versions).
      Only the "universe" object participates in baseline equality.
  metadata.json  — all volatile context (timestamps, durations, host
      paths, job count).  Explicitly non-comparable.

Baseline comparison:
  tools/drift_corpus_audit.py --out RUN2 --baseline RUN1
      Prints the per-counter delta table; fails on universe mismatch
      (exit 2) or a nonzero hard gate in the new run (exit 1).
      Nonzero deltas on non-gate counters DO NOT fail in this mode.

Certification comparison (v1.7.1):
  tools/drift_corpus_audit.py --out RUN2 --baseline BASE --require-zero-delta
      Everything above PLUS fail-closed exact equality: the counter
      key sets must be identical and every delta exactly 0 (exit 1
      otherwise).  Missing or corrupt baseline/run data, or data that
      does not match the comparison schema, exits 2.  This is the
      `just ownership-corpus-check` certification mode against the
      checked-in baseline
      (lang/tests/ownership_corpus/reviewed-baseline/).

Usage:
  tools/drift_corpus_audit.py --out DIR [-j N] [--only a,b,c]
                              [--baseline DIR] [--require-zero-delta]
                              [--driftc-args "..."]
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TOOL_VERSION = "1.7.1"
ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "lang" / "tests" / "codegen" / "e2e"

# Scratch policy: NEVER default to /tmp.  All scratch (compiled binaries,
# clang intermediates via TMPDIR) roots under repo-local build/tmp using
# the established session_root pattern (same rationale as the e2e
# runner's bootstrap: janitor-sweepable namespace, tmpfs-exhaustion
# guard).  session_root also exports DRIFT_TMP_ROOT/TMPDIR for child
# processes.
sys.path.insert(0, str(ROOT))
from lang.test_support.drift_tmp import session_root as _drift_session_root  # noqa: E402

_SCRATCH_BASE = _drift_session_root(base=ROOT / "build" / "tmp")

# Hard gates: any nonzero value fails the run — BOTH for standalone
# baseline acquisition and for the new side of a --baseline comparison.
# A reference baseline with a nonzero gate would silently bless a
# regression for the whole phase.
HARD_GATES = (
	"unclassified",
	"untagged",
	"c1_must_drop_without_release",
	"post_ledger_build_failed",
	# Promoted 2026-07-13 after the E-population close-out: shapes 1-2
	# (value corruption) fixed at the source in 0.33.83; shape 3 (dead
	# paired cleanup drop of MOVED_OUT storage) reclassifies
	# c3_moveout_zero_safe.  Any remaining divergent MoveOut is an
	# unpaired re-move of consumed storage — the zero-read bug class —
	# and fails the corpus loudly.
	"c3_moveout_not_owned",
	# Promoted 2026-07-14 with slice 4b: every late-retain stake class
	# is fail-closed at emission (the shared dead-stake tripwire), so a
	# nonzero counter here means an emission BYPASSED the tripwire (or
	# a direct audit note reintroduced the class) — gate it regardless.
	"site_class:store_value_retain",
	"site_class:call_arg_retain",
	"site_class:value_position_retain",
	"site_class:return_retain_site3",
)


def _hard_gate_failures(counters: dict[str, int]) -> list[str]:
	return [g for g in HARD_GATES if counters.get(g, 0) != 0]

_MODULE_RE = re.compile(r"^\s*module\s+([A-Za-z_][A-Za-z0-9_.]*)\s*;", re.M)
_AUDIT_LINE_RE = re.compile(r"\]\s*(\{.*\})\s*$")

# The corpus is a COMPILE-ONLY, SINGLE-COMPILATION-UNIT audit universe.
# This inclusion rule is recorded verbatim in every manifest so the
# universe claim cannot silently drift from the real e2e universe
# (which additionally runs binaries, honors diagnostics cases, module
# paths, C sources, lanes, args, ...).
INCLUSION_RULE = (
	"fixture dirs under lang/tests/codegen/e2e with a main.drift, excluding "
	"names starting with '__' and excluding fixtures whose expected.json "
	"declares module_paths or c_sources (multi-unit/C-helper shapes are not "
	"compiled by this audit tool); compile failures are further excluded "
	"from aggregation and recorded in the failed partition"
)
_EXCLUDING_KEYS = ("module_paths", "c_sources")


def _discover_fixtures(only: list[str] | None) -> tuple[list[Path], list[dict]]:
	"""Returns (included fixture dirs, excluded records {name, reason})."""
	dirs: list[Path] = []
	excluded: list[dict] = []
	for d in sorted(FIXTURE_ROOT.iterdir()):
		if not d.is_dir() or d.name.startswith("__"):
			continue
		if not (d / "main.drift").is_file():
			continue
		if only is not None and d.name not in only:
			continue
		exp = d / "expected.json"
		if exp.is_file():
			try:
				meta = json.loads(exp.read_text())
			except json.JSONDecodeError:
				excluded.append({"name": d.name, "reason": "unparseable expected.json"})
				continue
			hit = [k for k in _EXCLUDING_KEYS if k in meta]
			if hit:
				excluded.append({"name": d.name, "reason": f"declares {'+'.join(hit)}"})
				continue
		dirs.append(d)
	return dirs, excluded


def _fixture_hash(fixture: Path) -> str:
	"""Content hash over EVERY file in the fixture dir (sorted rel-path +
	sha256 pairs) — companion modules, C helpers, expected.json included,
	so no source input can change without changing the universe."""
	h = hashlib.sha256()
	for f in sorted(fixture.rglob("*")):
		if not f.is_file():
			continue
		h.update(str(f.relative_to(fixture)).encode())
		h.update(b"\0")
		h.update(hashlib.sha256(f.read_bytes()).hexdigest().encode())
		h.update(b"\0")
	return h.hexdigest()


def _entry_for(source: str) -> str | None:
	m = _MODULE_RE.search(source)
	return f"{m.group(1)}::main" if m else None


def _compile_one(fixture: Path, run_dir: Path, extra_args: list[str]) -> tuple[str, bool]:
	name = fixture.name
	source = (fixture / "main.drift").read_text()
	entry = _entry_for(source)
	if entry is None:
		return name, False
	audit_file = run_dir / "audit" / f"{name}.jsonl"
	with tempfile.TemporaryDirectory(prefix="corpus-audit-", dir=str(_SCRATCH_BASE)) as td:
		env = os.environ.copy()
		env["DRIFT_STRING_ARC_AUDIT"] = "1"
		env["DRIFT_STRING_ARC_AUDIT_FILE"] = str(audit_file)
		res = subprocess.run(
			[sys.executable, "-m", "lang.driftc.driftc", "--dev",
			 "--stdlib-root", str(ROOT / "stdlib"),
			 *extra_args,
			 str(fixture / "main.drift"),
			 "--entry", entry,
			 "-o", str(Path(td) / "bin")],
			cwd=ROOT, env=env, capture_output=True, text=True, timeout=600,
		)
	ok = res.returncode == 0 and audit_file.is_file()
	if not ok and audit_file.exists():
		# A failed compile may leave partial audit lines; exclude it
		# entirely so aggregation covers exactly the success set.
		audit_file.unlink()
	return name, ok


def _aggregate(run_dir: Path, compiled_ok: list[str]) -> dict[str, int]:
	counters: dict[str, int] = {}
	for name in compiled_ok:
		audit_file = run_dir / "audit" / f"{name}.jsonl"
		for line in audit_file.read_text().splitlines():
			m = _AUDIT_LINE_RE.search(line)
			if not m:
				continue
			try:
				rec = json.loads(m.group(1))
			except json.JSONDecodeError:
				continue
			if rec.get("record") != "aggregate":
				continue
			for key, val in rec.items():
				if key == "record" or not isinstance(val, int):
					continue
				counters[key] = counters.get(key, 0) + val
	return dict(sorted(counters.items()))


def _stable_json(obj) -> str:
	return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def _write_run(run_dir: Path, fixtures: list[Path], excluded: list[dict],
               compiled_ok: list[str], failed: list[str],
               counters: dict[str, int], started: float, jobs: int) -> None:
	universe = {
		"inclusion_rule": INCLUSION_RULE,
		"fixtures": [
			{"name": f.name, "sha256": _fixture_hash(f)} for f in fixtures
		],
		"excluded": sorted(excluded, key=lambda e: e["name"]),
		"compiled_ok": sorted(compiled_ok),
		"failed": sorted(failed),
	}
	try:
		versions = (ROOT / "lang" / "versions.py").read_text()
		driftc_version = re.search(r'DRIFTC_VERSION: str = "([^"]+)"', versions).group(1)
		abi = re.search(r"DRIFT_RT_ABI_VERSION: int = (\d+)", versions).group(1)
	except Exception:
		driftc_version, abi = "unknown", "unknown"
	(run_dir / "aggregate.json").write_text(_stable_json({
		"counters": counters,
		"fixtures_compiled": len(compiled_ok),
	}))
	(run_dir / "manifest.json").write_text(_stable_json({
		"universe": universe,
		"environment": {
			"driftc_version": driftc_version,
			"abi": abi,
			"tool_version": TOOL_VERSION,
		},
	}))
	(run_dir / "metadata.json").write_text(_stable_json({
		"started_unix": started,
		"duration_s": round(time.time() - started, 1),
		"jobs": jobs,
		"repo_root": str(ROOT),
		"python": sys.version.split()[0],
	}))


def _validate_universe_schema(side: str, universe: object) -> None:
	"""The complete universe shape the comparison relies on — validated
	BEFORE use so malformed manifests fail closed (exit 2), never
	traceback."""
	if not isinstance(universe, dict):
		raise ValueError(f"{side} universe must be an object")
	for key in ("compiled_ok", "excluded", "failed", "fixtures", "inclusion_rule"):
		if key not in universe:
			raise ValueError(f"{side} universe missing key {key!r}")
	for key in ("compiled_ok", "failed"):
		val = universe[key]
		if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
			raise ValueError(f"{side} universe[{key!r}] must be a list of strings")
	if not isinstance(universe["inclusion_rule"], str):
		raise ValueError(f"{side} universe['inclusion_rule'] must be a string")
	excluded = universe["excluded"]
	if not isinstance(excluded, list):
		raise ValueError(f"{side} universe['excluded'] must be a list")
	for entry in excluded:
		if (not isinstance(entry, dict) or not isinstance(entry.get("name"), str)
				or not isinstance(entry.get("reason"), str)):
			raise ValueError(f"{side} universe excluded entries must carry "
			                 f"string 'name' and 'reason'")
	fixtures = universe["fixtures"]
	if not isinstance(fixtures, list):
		raise ValueError(f"{side} universe['fixtures'] must be a list")
	for fx in fixtures:
		if (not isinstance(fx, dict) or not isinstance(fx.get("name"), str)
				or not isinstance(fx.get("sha256"), str)):
			raise ValueError(f"{side} universe fixture entries must carry "
			                 f"string 'name' and 'sha256'")


def _validate_counters_schema(side: str, counters: object) -> None:
	if not isinstance(counters, dict):
		raise ValueError(f"{side} counters must be an object")
	for key, val in counters.items():
		if not isinstance(key, str):
			raise ValueError(f"{side} counters key {key!r} must be a string")
		if not isinstance(val, int) or isinstance(val, bool):
			raise ValueError(f"{side} counter {key!r} must be an integer, "
			                 f"got {type(val).__name__}")


def _compare(baseline_dir: Path, run_dir: Path,
             *, require_zero_delta: bool = False) -> int:
	# Fail closed on missing/corrupt baseline or run data: a comparison
	# that cannot read both sides completely must never pass.
	try:
		base_manifest = json.loads((baseline_dir / "manifest.json").read_text())
		new_manifest = json.loads((run_dir / "manifest.json").read_text())
		base_universe = base_manifest["universe"]
		new_universe = new_manifest["universe"]
		base_counters = json.loads((baseline_dir / "aggregate.json").read_text())["counters"]
		new_counters = json.loads((run_dir / "aggregate.json").read_text())["counters"]
		for side, universe in (("baseline", base_universe), ("new", new_universe)):
			_validate_universe_schema(side, universe)
		for side, counters in (("baseline", base_counters), ("new", new_counters)):
			_validate_counters_schema(side, counters)
	except (OSError, ValueError, KeyError, TypeError) as e:
		print(f"BASELINE/RUN DATA ERROR: cannot load comparison inputs "
		      f"({type(e).__name__}: {e}) — failing closed.", file=sys.stderr)
		return 2
	if base_universe != new_universe:
		print("UNIVERSE MISMATCH: baseline and new run do not cover the "
		      "identical fixture universe — deltas would be meaningless.",
		      file=sys.stderr)
		bu, nu = base_universe, new_universe
		for key in ("compiled_ok", "failed"):
			b, n = set(bu[key]), set(nu[key])
			if b != n:
				print(f"  {key}: only-baseline={sorted(b - n)} only-new={sorted(n - b)}",
				      file=sys.stderr)
		bh = {f["name"]: f["sha256"] for f in bu["fixtures"]}
		nh = {f["name"]: f["sha256"] for f in nu["fixtures"]}
		changed = [k for k in bh.keys() & nh.keys() if bh[k] != nh[k]]
		if changed:
			print(f"  fixtures with changed sources: {sorted(changed)}", file=sys.stderr)
		return 2

	base = base_counters
	new = new_counters
	keys = sorted(set(base) | set(new))
	width = max(len(k) for k in keys) if keys else 10
	print(f"{'counter':<{width}}  {'baseline':>12}  {'new':>12}  {'delta':>12}")
	for k in keys:
		b, n = base.get(k, 0), new.get(k, 0)
		delta = n - b
		print(f"{k:<{width}}  {b:>12}  {n:>12}  {delta:>+12}")
	gate_failures = _hard_gate_failures(new)
	if gate_failures:
		print(f"HARD GATE FAILURE: nonzero in new run: {gate_failures}",
		      file=sys.stderr)
		return 1
	if require_zero_delta:
		# Certification policy: EXACT equality.  The plain --baseline
		# comparison prints deltas but does not fail on them; the
		# zero-delta mode fails closed on ANY divergence — missing
		# counter keys, unexpected new keys, or a nonzero delta.
		problems: list[str] = []
		missing = sorted(set(base) - set(new))
		unexpected = sorted(set(new) - set(base))
		nonzero = {k: new[k] - base[k]
		           for k in sorted(set(base) & set(new)) if new[k] != base[k]}
		if missing:
			problems.append(f"counter keys missing from the new run: {missing}")
		if unexpected:
			problems.append(f"unexpected new counter keys: {unexpected}")
		if nonzero:
			problems.append(f"nonzero counter deltas: {nonzero}")
		if problems:
			print("EXACT-DELTA FAILURE (--require-zero-delta):", file=sys.stderr)
			for prob in problems:
				print(f"  {prob}", file=sys.stderr)
			return 1
	return 0


def main(argv: list[str] | None = None) -> int:
	ap = argparse.ArgumentParser(description=__doc__,
	                             formatter_class=argparse.RawDescriptionHelpFormatter)
	ap.add_argument("--out", required=True, help="run output directory")
	ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4)
	ap.add_argument("--only", help="comma-separated fixture-name subset")
	ap.add_argument("--baseline", help="prior run dir to compare against")
	ap.add_argument("--require-zero-delta", action="store_true",
	                help="certification mode: with --baseline, require the "
	                     "IDENTICAL counter key set and every delta exactly 0 "
	                     "(plus the usual identical-universe and hard-gate "
	                     "checks); any divergence fails")
	ap.add_argument("--driftc-args", default="",
	                help="extra args passed to every driftc invocation")
	args = ap.parse_args(argv)
	if args.require_zero_delta and not args.baseline:
		print("--require-zero-delta needs --baseline", file=sys.stderr)
		return 2

	run_dir = Path(args.out)
	if run_dir.exists() and any(run_dir.iterdir()):
		print(f"refusing to reuse non-empty --out {run_dir}: stale audit files "
		      f"from a prior run could be aggregated as current results — this "
		      f"is an acceptance tool; use a fresh directory", file=sys.stderr)
		return 2
	(run_dir / "audit").mkdir(parents=True, exist_ok=True)
	only = args.only.split(",") if args.only else None
	fixtures, excluded = _discover_fixtures(only)
	if not fixtures:
		print("no fixtures matched", file=sys.stderr)
		return 2
	extra = shlex.split(args.driftc_args) if args.driftc_args else []

	started = time.time()
	compiled_ok: list[str] = []
	failed: list[str] = []
	total = len(fixtures)
	# Progress output doubles as a LIVENESS HEARTBEAT: the certification
	# watchdog treats prolonged silence as a stuck job.  Emit a line on
	# whichever comes first — every ~5% of completions (min 10) or every
	# HEARTBEAT_S seconds even when nothing completed in the window (a
	# single in-flight fixture may legitimately hold its 600s subprocess
	# timeout) — plus a guaranteed final line.  Deliberately NO
	# compile-failed count here: the expected-fail partition is part of
	# the universe (checked exactly at the end), so a mid-run "N failed"
	# reads as an error report when nothing is wrong.
	step = max(10, total // 20)
	heartbeat_s = 30.0
	def _emit_progress() -> None:
		done = len(compiled_ok) + len(failed)
		elapsed = time.time() - started
		rate = done / elapsed if elapsed > 0 else 0.0
		eta = (total - done) / rate if rate > 0 else 0.0
		print(f"progress: {done}/{total} fixtures "
		      f"elapsed {elapsed:.0f}s eta {eta:.0f}s",
		      file=sys.stderr, flush=True)
	with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
		pending = {pool.submit(_compile_one, f, run_dir, extra) for f in fixtures}
		emitted_at = started
		emitted_done = 0
		while pending:
			# Sleep only until the NEXT heartbeat deadline (not a full
			# window) so the max silent gap is ~heartbeat_s, never ~2x it.
			done_set, pending = concurrent.futures.wait(
				pending,
				timeout=max(1.0, heartbeat_s - (time.time() - emitted_at)),
				return_when=concurrent.futures.FIRST_COMPLETED)
			for fut in done_set:
				name, ok = fut.result()
				(compiled_ok if ok else failed).append(name)
			done = len(compiled_ok) + len(failed)
			now = time.time()
			if (done == total or done - emitted_done >= step
					or now - emitted_at >= heartbeat_s):
				_emit_progress()
				emitted_at = now
				emitted_done = done

	counters = _aggregate(run_dir, sorted(compiled_ok))
	_write_run(run_dir, fixtures, excluded, compiled_ok, failed, counters,
	           started, args.jobs)
	print(f"compiled {len(compiled_ok)}/{len(fixtures)} fixtures "
	      f"({len(failed)} compile-failed, {len(excluded)} excluded by rule); "
	      f"counters: {len(counters)}")

	if args.baseline:
		# _compare owns the whole baseline-mode contract: universe
		# mismatch DOMINATES (exit 2 — deltas over different universes
		# are meaningless, and a coincident gate failure must not mask
		# that), then the delta table, then the new-side hard gates
		# (exit 1).
		return _compare(Path(args.baseline), run_dir,
		                require_zero_delta=args.require_zero_delta)
	gate_failures = _hard_gate_failures(counters)
	if gate_failures:
		print(f"HARD GATE FAILURE: nonzero in this run: {gate_failures}",
		      file=sys.stderr)
		return 1
	return 0


if __name__ == "__main__":
	sys.exit(main())
