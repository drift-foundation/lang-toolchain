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
asserts universe equality (exit 2 on mismatch — the deltas would be
meaningless), prints the sorted per-counter exact-delta table, and
FAILS (exit 1) if any hard gate is nonzero in the new run.

Usage:
  tools/drift_corpus_audit.py --out DIR [-j N] [--only a,b,c]
                              [--baseline DIR] [--driftc-args "..."]
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

TOOL_VERSION = "1.4.0"
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


def _compare(baseline_dir: Path, run_dir: Path) -> int:
	base_manifest = json.loads((baseline_dir / "manifest.json").read_text())
	new_manifest = json.loads((run_dir / "manifest.json").read_text())
	if base_manifest["universe"] != new_manifest["universe"]:
		print("UNIVERSE MISMATCH: baseline and new run do not cover the "
		      "identical fixture universe — deltas would be meaningless.",
		      file=sys.stderr)
		bu, nu = base_manifest["universe"], new_manifest["universe"]
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

	base = json.loads((baseline_dir / "aggregate.json").read_text())["counters"]
	new = json.loads((run_dir / "aggregate.json").read_text())["counters"]
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
	return 0


def main(argv: list[str] | None = None) -> int:
	ap = argparse.ArgumentParser(description=__doc__,
	                             formatter_class=argparse.RawDescriptionHelpFormatter)
	ap.add_argument("--out", required=True, help="run output directory")
	ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4)
	ap.add_argument("--only", help="comma-separated fixture-name subset")
	ap.add_argument("--baseline", help="prior run dir to compare against")
	ap.add_argument("--driftc-args", default="",
	                help="extra args passed to every driftc invocation")
	args = ap.parse_args(argv)

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
	with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
		futures = [pool.submit(_compile_one, f, run_dir, extra) for f in fixtures]
		for fut in concurrent.futures.as_completed(futures):
			name, ok = fut.result()
			(compiled_ok if ok else failed).append(name)

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
		return _compare(Path(args.baseline), run_dir)
	gate_failures = _hard_gate_failures(counters)
	if gate_failures:
		print(f"HARD GATE FAILURE: nonzero in this run: {gate_failures}",
		      file=sys.stderr)
		return 1
	return 0


if __name__ == "__main__":
	sys.exit(main())
