#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Ownership-corpus lifecycle: check, verify, promote.

  committed golden baseline -> fresh verified observation + candidate (verify)
                            -> fast validation + install, zero compiles (promote)
                            -> committed golden baseline
  CI reads the committed golden state only, via verify.  `check` is the quick
  incremental/projected REPORT-ONLY iteration lane (it never mints the
  candidate); discovery-to-install is exactly ONE full-universe compile.

  developer   `just ownership-corpus-check [<dir>]`  (default work dir
              build/tmp/ownership-corpus-work)
      Fast, full-universe.  Seeds an empty cache from the committed baseline's
      per-fixture projections (no compile for unchanged fixtures); records key on
      fixture CONTENT HASH, so only new / source-edited / --select'ed fixtures
      recompile and become CURRENT, while a compiler-fingerprint move keeps old
      observations PROJECTED (stale, visibly marked — never a full rebuild).
      Reused successes AND failures are accounted.  REPORT-ONLY: writes its
      work-dir records/report and prints projected deltas, but never mints
      the canonical promotion candidate (possibly-projected results are not
      fresh evidence; `verify` is the sole candidate producer).

  verify      `just ownership-corpus-verify`   (CI / cert gate AND candidate producer)
      IGNORES the developer cache and never CONSUMES the handoff (a pre-existing
      candidate is invalidated up front).  One fresh full-universe compile
      compared EXACTLY to the committed reviewed baseline (inclusion rule,
      source hashes, exclusions + reasons, buckets, every per-fixture
      projection, aggregate, zero hard gates).  Every complete, stable,
      zero-hard-gate observation republishes the promotion candidate — exact
      matches included; hard-gate/aborted runs publish nothing.  An ABSENT
      baseline is the bootstrap discovery run (initial candidate); a present
      but malformed baseline fails closed.  Fails loudly on any drift; NEVER
      installs or writes a baseline file (cannot reach _staged_install).  A
      golden clean clone passes with zero tracked diffs.  check/verify/promote
      serialize on one coarse advisory lock.

  promote     `just ownership-corpus-promote`   (fast-or-fail install; ZERO compiles)
      Never reads developer records; accepts no worker count.  REQUIRES the
      fresh-verify candidate — missing/malformed/corrupt/wrong-kind/
      projected/hard-gate/stale is an immediate error with the baseline
      untouched; there is no fallback and no reproduction compile.
      Validates schema + digest seal + producer kind + exhaustive
      observation, recomputes the CURRENT toolchain/universe snapshot
      passively (source/tool/library hashing + existing-artifact bytes,
      nothing built), requires exact identity with the candidate,
      then staged installation of the candidate's observation (verify's
      snapshot and measured metadata install VERBATIM; byte-preserving
      no-op when already equal — but a missing/stale baseline fingerprint
      forces a real install).  NEVER wired into CI / just test / just
      certify.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORK_DIR = ROOT / "build" / "tmp" / "ownership-corpus-work"
HANDOFF_PATH = ROOT / "build" / "tmp" / "ownership-corpus-projection.json"
ACTUAL_DIR = ROOT / "build" / "tmp" / "ownership-corpus-actual"
# Coarse advisory mutual-exclusion lock shared by check/verify/promote: the
# candidate (HANDOFF_PATH) and retained actual (ACTUAL_DIR) are fixed global
# paths, so overlapping corpus commands would race each other's publications.
LOCK_PATH = ROOT / "build" / "tmp" / "ownership-corpus.lock"


def _load(name: str):
	spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	return mod


_audit = _load("drift_corpus_audit")
_fp = _load("drift_corpus_fingerprint")

RECORD_SCHEMA_VERSION = 3
# Schema 2 (fast-or-fail promotion): the candidate is a COMPLETE, self-
# validating fresh-verify observation — full snapshot object + verbatim
# verify-run metadata + payload digest — and verify is its ONLY producer.
HANDOFF_SCHEMA_VERSION = 2
CANDIDATE_KIND = "verified_fresh_observation"

_RECORD_KEYS = {"schema_version", "name", "fixture_hash", "compiled_ok",
                "projection", "observed_toolchain_composite", "payload_sha256"}


class InfraError(RuntimeError):
	"""A fingerprint / tool-resolution / infrastructure failure — surfaced as a
	controlled exit 2 with a stderr diagnostic, never a traceback."""


# ── projections + fixture-keyed records ──────────────────────────────

def _valid_projection(proj) -> bool:
	return isinstance(proj, dict) and all(
		isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
		for k, v in proj.items())


def _valid_projection_map(projections) -> bool:
	return isinstance(projections, dict) and all(
		isinstance(n, str) and _valid_projection(p) for n, p in projections.items())


def _record_payload(name, fx_hash, ok, projection, observed_tc) -> dict:
	return {
		"name": name,
		"fixture_hash": fx_hash,
		"compiled_ok": ok,
		"projection": dict(sorted(projection.items())) if ok else {},
		"observed_toolchain_composite": observed_tc,
	}


def _payload_digest(payload: dict) -> str:
	return hashlib.sha256(_fp.canonical_json(payload).encode()).hexdigest()


def _make_record(name, fx_hash, ok, projection, observed_tc) -> dict:
	payload = _record_payload(name, fx_hash, ok, projection, observed_tc)
	return {"schema_version": RECORD_SCHEMA_VERSION, **payload,
	        "payload_sha256": _payload_digest(payload)}


def _valid_record(d) -> bool:
	if not isinstance(d, dict) or set(d) != _RECORD_KEYS:
		return False
	if not (d["schema_version"] == RECORD_SCHEMA_VERSION
	        and isinstance(d["name"], str)
	        and _fp._is_hex64(d["fixture_hash"])
	        and isinstance(d["compiled_ok"], bool)
	        and _valid_projection(d["projection"])
	        and _fp._is_hex64(d["observed_toolchain_composite"])
	        and _fp._is_hex64(d["payload_sha256"])
	        and (d["compiled_ok"] or d["projection"] == {})):
		return False
	payload = _record_payload(d["name"], d["fixture_hash"], d["compiled_ok"],
	                          d["projection"], d["observed_toolchain_composite"])
	return _payload_digest(payload) == d["payload_sha256"]


def _load_record(path: Path):
	try:
		data = json.loads(path.read_text())
	except (OSError, ValueError):
		return None
	return data if _valid_record(data) else None


# ── compile helpers ──────────────────────────────────────────────────

def _compile_and_project(fixture: Path, compile_dir: Path, extra) -> tuple[str, bool, dict]:
	name, ok = _audit._compile_one(fixture, compile_dir, extra)
	if not ok:
		return name, False, {}
	return name, True, _audit._fixture_projection(compile_dir / "audit" / f"{name}.jsonl")


def _toolchain(extra):
	try:
		return _fp.toolchain_fingerprint(ROOT, extra_args=extra)
	except Exception as e:            # clang missing, unreadable source, etc.
		raise InfraError(f"toolchain fingerprint failed ({type(e).__name__}: {e})") from e


def _toolchain_passive(extra):
	"""Fast promotion's identity probe: NEVER builds.  A missing/unreadable
	runtime artifact (or any probe failure) is a controlled failure
	instructing a fresh verify; stale CONTENT is caught by the composite
	identity comparison, not here."""
	try:
		return _fp.toolchain_fingerprint_passive(ROOT, extra_args=extra)
	except Exception as e:
		raise InfraError(f"passive toolchain fingerprint failed "
		                 f"({type(e).__name__}: {e})") from e


def _snapshot(tc, universe: dict) -> dict:
	return _fp.run_snapshot(tc, _fp.static_universe_digest(universe))


def _mkscratch(prefix: str) -> Path:
	return Path(tempfile.mkdtemp(prefix=prefix, dir=str(_audit._scratch_base())))


def _compile_set(to_compile, compile_dir, extra, jobs, out, started):
	compiled: dict[str, dict] = {}
	failed: list[str] = []
	with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
		futs = {pool.submit(_compile_and_project, f, compile_dir, extra): f for f in to_compile}
		done = 0
		last = started
		for fut in concurrent.futures.as_completed(futs):
			name, ok, proj = fut.result()
			(compiled.__setitem__(name, proj) if ok else failed.append(name))
			done += 1
			now = time.time()
			if done == len(to_compile) or now - last >= 30.0:
				print(f"progress: {done}/{len(to_compile)} compiled "
				      f"elapsed {now - started:.0f}s", file=out, flush=True)
				last = now
	return compiled, failed


def _hashes_stable(fixtures, fx_hash, out) -> bool:
	for f in fixtures:
		if _audit._fixture_hash(f) != fx_hash[f.name]:
			print(f"ABORT: fixture {f.name} changed on disk during the run; its "
			      f"projection no longer matches its source — re-run.", file=out)
			return False
	return True


def _finish_snapshot_ok(tc_start_snapshot, extra, out) -> bool:
	fixtures, excluded = _audit._discover_fixtures(None)
	snap_finish = _snapshot(_toolchain(extra), _audit._universe_dict(fixtures, excluded))
	if snap_finish["composite"] != tc_start_snapshot["composite"]:
		print(f"ABORT: toolchain/universe fingerprint changed between start "
		      f"({tc_start_snapshot['composite'][:12]}) and finish "
		      f"({snap_finish['composite'][:12]}) — the sweep did not run under a "
		      f"single stable toolchain; nothing retained.", file=out)
		return False
	return True


# ── the reviewed baseline (universe + per-fixture projections) ───────

def _read_baseline(baseline: Path):
	"""Return {base_universe, compiled_ok, failed, counters, projections|None,
	toolchain} for the checked-in baseline, or None if unreadable."""
	try:
		manifest = json.loads((baseline / "manifest.json").read_text())
		aggregate = json.loads((baseline / "aggregate.json").read_text())
		universe = manifest["universe"]
		_audit._validate_universe_schema("baseline", universe)
		_audit._validate_counters_schema("baseline", aggregate["counters"])
	except (OSError, ValueError, KeyError, TypeError):
		return None
	# A PRESENT new-format file must be valid; only an ABSENT file is treated as
	# the transitional legacy format.  A corrupt new-format baseline fails closed.
	projections = None
	pj = baseline / "projections.json"
	if pj.is_file():
		try:
			cand = json.loads(pj.read_text())
		except (OSError, ValueError):
			return None
		if not _valid_projection_map(cand):
			return None
		projections = {k: dict(sorted(v.items())) for k, v in cand.items()}
	toolchain = None
	fpj = baseline / "fingerprint.json"
	if fpj.is_file():
		try:
			toolchain = _fp.read_fingerprint(fpj)["toolchain"]["composite"]
		except (OSError, ValueError, KeyError, TypeError):
			return None
	return {
		"base_universe": {k: universe[k] for k in ("inclusion_rule", "fixtures", "excluded")},
		"compiled_ok": sorted(universe["compiled_ok"]),
		"failed": sorted(universe["failed"]),
		"counters": aggregate["counters"],
		"projections": projections,
		"toolchain": toolchain,
	}


# ══ developer mode ═══════════════════════════════════════════════════

def run_check(work: Path, *, select, jobs: int, extra, baseline: "Path | None",
              out=sys.stderr) -> int:
	fixtures, excluded = _audit._discover_fixtures(None)   # ALWAYS the full universe
	if not fixtures:
		print("no fixtures matched", file=out)
		return 2
	names = {f.name for f in fixtures}
	bad_select = sorted(n for n in select if n not in names)
	if bad_select or "" in select:
		print(f"--select names must resolve to existing fixtures; unknown/empty: "
		      f"{bad_select or ['']}", file=out)
		return 2
	try:
		tc = _toolchain(extra)
	except InfraError as e:
		print(str(e), file=out)
		return 2
	tc_comp = tc["composite"]
	universe = _audit._universe_dict(fixtures, excluded)
	snap_start = _snapshot(tc, universe)

	records_dir = work / "records"
	records_dir.mkdir(parents=True, exist_ok=True)
	fx_hash = {f.name: _audit._fixture_hash(f) for f in fixtures}
	seed = _baseline_seed(baseline) if baseline is not None else {}

	# obs[name] = {"ok": bool, "proj": dict, "current": bool}
	obs: dict[str, dict] = {}
	to_compile: list[Path] = []
	for f in fixtures:
		# --select forces the named fixtures to recompile; a full fresh run is
		# `ownership-corpus-verify`'s job (the single fresh authority).
		force = f.name in select
		rec = _load_record(records_dir / f"{f.name}.json")
		if rec is not None and rec["fixture_hash"] == fx_hash[f.name] and not force:
			obs[f.name] = {"ok": rec["compiled_ok"], "proj": rec["projection"],
			               "current": rec["observed_toolchain_composite"] == tc_comp}
			continue
		s = seed.get(f.name)
		if s is not None and s["hash"] == fx_hash[f.name] and not force:
			_fp.write_atomic(records_dir / f"{f.name}.json",
			                 _make_record(f.name, fx_hash[f.name], s["ok"], s["proj"], s["toolchain"]))
			obs[f.name] = {"ok": s["ok"], "proj": s["proj"], "current": s["toolchain"] == tc_comp}
			continue
		to_compile.append(f)

	compile_dir = _mkscratch("corpus-check-")
	(compile_dir / "audit").mkdir(parents=True, exist_ok=True)
	started = time.time()
	print(f"check: {len(obs)} reused ({sum(1 for v in obs.values() if not v['current'])} "
	      f"projected), {len(to_compile)} to compile (developer lane, {jobs} jobs)",
	      file=out, flush=True)
	try:
		compiled, fresh_failed = _compile_set(to_compile, compile_dir, extra, jobs, out, started)
		if not _hashes_stable(fixtures, fx_hash, out):
			return 2
		if not _finish_snapshot_ok(snap_start, extra, out):
			return 2
		for name, proj in compiled.items():
			obs[name] = {"ok": True, "proj": proj, "current": True}
			_fp.write_atomic(records_dir / f"{name}.json",
			                 _make_record(name, fx_hash[name], True, proj, tc_comp))
		for name in fresh_failed:
			obs[name] = {"ok": False, "proj": {}, "current": True}
			_fp.write_atomic(records_dir / f"{name}.json",
			                 _make_record(name, fx_hash[name], False, {}, tc_comp))
	except _audit.CorpusProjectionError as e:
		print(f"ABORT (infrastructure): {e}", file=out)
		return 2
	except InfraError as e:
		print(str(e), file=out)
		return 2
	finally:
		shutil.rmtree(compile_dir, ignore_errors=True)

	compiled_ok = sorted(n for n, v in obs.items() if v["ok"])
	failed = sorted(n for n, v in obs.items() if not v["ok"])
	projections = {n: obs[n]["proj"] for n in compiled_ok}
	counters = _audit._merge_counters(projections.values())
	observed = sorted(n for n, v in obs.items() if v["current"])
	projected = sorted(n for n, v in obs.items() if not v["current"])

	_audit._emit_run(work, universe, compiled_ok, failed, counters, started, jobs)
	_fp.write_atomic(work / "fingerprint.json", snap_start)
	_write_report(work, tc_comp, snap_start["composite"], fixtures, excluded,
	              compiled_ok, failed, projections, counters, observed, projected)
	# Report-only lane: the incremental check NEVER writes the canonical
	# promotion candidate — possibly-projected results are not fresh
	# evidence, and only `ownership-corpus-verify` may mint the candidate
	# the fast promote installs.
	print(f"checked {len(fixtures)} fixtures: {len(compiled_ok)} compiled_ok, "
	      f"{len(failed)} failed; {len(observed)} current / {len(projected)} projected; "
	      f"{len(counters)} counters. report -> {work / 'report.json'}  "
	      f"(report-only; run `ownership-corpus-verify` for fresh promotable "
	      f"evidence)", file=out)

	# The developer lane is provisional and INFORMATIONAL: it prints the deltas
	# vs the reviewed baseline (including an expected universe drift — you are
	# developing) but never fails on them.  Fresh promotable evidence comes
	# from `ownership-corpus-verify` (the single fresh authority whose
	# candidate the fast-or-fail promote installs).
	if baseline is not None:
		_audit._compare(baseline, work, require_zero_delta=False)
	gate_failures = _audit._hard_gate_failures(counters)
	if gate_failures:
		print("NOTE (informational): hard-gate counters are nonzero in this "
		      "developer run — a fresh `ownership-corpus-verify` would emit no "
		      "promotable candidate for this state: "
		      + ", ".join(gate_failures), file=out)
	print("developer check complete (report-only). Review the deltas above; "
	      "when fresh promotable evidence is wanted, run "
	      "`just ownership-corpus-verify`.", file=out)
	return 0


def _baseline_seed(baseline: Path) -> dict:
	"""Per-fixture seed entries from the reviewed baseline, so a clean clone's
	empty cache reuses baseline projections (as PROJECTED values) instead of
	recompiling.  {name: {hash, ok, proj, toolchain}}.

	Seeds from any baseline that carries per-fixture PROJECTIONS (manifest +
	projections.json), including a freshly MIGRATED baseline that has no
	fingerprint.json yet — those seeds are marked projected (unknown/old
	toolchain).  A legacy baseline without projections seeds NOTHING, so the
	initial transition there is a clean full run rather than carrying forward
	stale pass/fail outcomes."""
	b = _read_baseline(baseline)
	if b is None or b["projections"] is None:
		return {}
	# a baseline without a fingerprint (just migrated) has an unknown toolchain;
	# its seeds are classified projected via a sentinel that never matches.
	tc = b["toolchain"] if _fp._is_hex64(b["toolchain"] or "") else "0" * 64
	base_hash = {e["name"]: e["sha256"] for e in b["base_universe"]["fixtures"]}
	seed: dict[str, dict] = {}
	for name in b["failed"]:
		if name in base_hash:
			seed[name] = {"hash": base_hash[name], "ok": False, "proj": {}, "toolchain": tc}
	for name in b["compiled_ok"]:
		if name in base_hash and name in b["projections"]:
			seed[name] = {"hash": base_hash[name], "ok": True,
			              "proj": b["projections"][name], "toolchain": tc}
	return seed


def _write_report(work, tc_comp, snap_comp, fixtures, excluded, compiled_ok,
                  failed, projections, counters, observed, projected) -> None:
	report = {
		"schema_version": RECORD_SCHEMA_VERSION,
		"mode": "developer",
		"full_universe": True,
		"toolchain_composite": tc_comp,
		"run_snapshot_composite": snap_comp,
		"fixtures_total": len(fixtures),
		"excluded": len(excluded),
		"compiled_ok": compiled_ok,
		"compile_failed": failed,
		# truthful observed/projected partitions (both cover successes AND failures)
		"observed": observed,
		"projected": projected,
		"counters": counters,
		"projections": dict(sorted(projections.items())),
	}
	_atomic_json(work / "report.json", report)


def _export_handoff(work, snapshot, universe, compiled_ok, failed, projections,
                    counters, observed, projected, run_meta) -> None:
	"""Serialize the schema-2 promotion candidate: a complete, self-validating
	fresh observation.  Carries the FULL run-snapshot object (fingerprint.json
	is installed from it verbatim) and the verify run's MEASURED metadata
	(installed verbatim — promotion never recomputes duration from its own
	wall clock), sealed with a canonical payload digest."""
	payload = {
		"schema_version": HANDOFF_SCHEMA_VERSION,
		"kind": CANDIDATE_KIND,
		"origin": {
			"work_dir": str(work),
			"toolchain_composite": snapshot["toolchain"]["composite"],
			"run_snapshot_composite": snapshot["composite"],
		},
		"snapshot": snapshot,
		"run_meta": run_meta,
		"universe": {
			"inclusion_rule": universe["inclusion_rule"],
			"fixtures": universe["fixtures"],
			"excluded": universe["excluded"],
			"compiled_ok": compiled_ok,
			"failed": failed,
		},
		"projections": dict(sorted(projections.items())),
		"counters": counters,
		"observed": observed,
		"projected": projected,
	}
	handoff = {**payload,
	           "payload_sha256": hashlib.sha256(_fp.canonical_json(payload).encode()).hexdigest()}
	HANDOFF_PATH.parent.mkdir(parents=True, exist_ok=True)
	_atomic_json(HANDOFF_PATH, handoff)


def _atomic_json(path: Path, obj) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	tmp = path.parent / f".{path.name}.tmp"
	tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")
	os.replace(tmp, path)


# ══ verify mode (CI/cert gate + candidate producer; tracked baseline read-only) ══

@contextlib.contextmanager
def _corpus_lock(out=sys.stderr):
	"""Coarse advisory mutual exclusion for check/verify/promote.  Blocks
	(with a note) rather than failing so serialized invocations queue up;
	the fixed candidate/actual paths are only ever touched under it."""
	LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
	f = open(LOCK_PATH, "w")
	try:
		try:
			fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
		except OSError:
			print(f"another ownership-corpus command holds {LOCK_PATH}; waiting...",
			      file=out, flush=True)
			fcntl.flock(f, fcntl.LOCK_EX)
		yield
	finally:
		try:
			fcntl.flock(f, fcntl.LOCK_UN)
		finally:
			f.close()


# Every artifact a reviewed-baseline bundle can contain (see
# lang/tests/ownership_corpus/reviewed-baseline and _retain_actual's audit
# dir).  Bootstrap requires NONE of them to exist: a directory holding ANY
# recognized artifact is partial/corrupt baseline STATE, not absence, and
# routes through the fail-closed reader (exit 2) instead.
_BASELINE_ARTIFACTS = ("manifest.json", "aggregate.json", "projections.json",
                       "fingerprint.json", "metadata.json", "BASELINE.md",
                       "audit")


def _baseline_absent(baseline: Path) -> bool:
	"""Bootstrap detection: NO recognized baseline artifact exists at all.
	Absence, not damage, is the only bootstrap signal — any present
	artifact (core or auxiliary) means the ordinary reader decides, and a
	then-unreadable core fails closed (exit 2).  An EXISTING path that is
	not a directory is damage too (it can never hold artifacts, so the
	no-children probe would otherwise misread it as absence)."""
	# Lexical existence (lexists) at BOTH levels: Path.exists follows
	# symlinks, so a DANGLING baseline symlink (or dangling artifact
	# symlink) would otherwise read as absence — but something IS there,
	# which is damage, not bootstrap.
	if os.path.lexists(baseline) and not baseline.is_dir():
		return False
	return not any(os.path.lexists(baseline / a) for a in _BASELINE_ARTIFACTS)


def _publish_fresh_candidate(universe, fresh_proj, fresh_failed, counters,
                             snap_start, out, *, origin_label: str,
                             started: float, jobs: int) -> None:
	"""Every COMPLETE, stable, zero-hard-gate fresh verify publishes the
	promotion candidate as a side effect — exact matches included (the
	observation is reviewable; promotion is simply unnecessary).  Invalid
	runs and hard-gate runs never reach this.  The candidate embeds this
	verify run's MEASURED metadata (same fields `_emit_run` would write),
	so a later fast promote installs it verbatim."""
	compiled_ok = sorted(fresh_proj)
	failed = sorted(fresh_failed)
	run_meta = {
		"started_unix": started,
		"duration_s": round(time.time() - started, 1),
		"jobs": jobs,
		"repo_root": str(ROOT),
		"python": sys.version.split()[0],
	}
	_export_handoff(origin_label, snap_start, universe, compiled_ok, failed,
	                fresh_proj, counters, sorted(compiled_ok + failed), [],
	                run_meta)
	print(f"promotion candidate exported -> {HANDOFF_PATH}", file=out)


def run_verify(baseline: Path, *, jobs: int, extra, out=sys.stderr) -> int:
	"""CI/cert gate AND the single fresh candidate producer.  Ignores the
	developer cache and never CONSUMES the projection handoff (the old one is
	invalidated up front; a fresh candidate is republished only from this
	run's own complete, stable, zero-hard-gate observation).  One fresh
	full-universe compile compared EXACTLY to the committed reviewed
	baseline; fail loud on any drift; NEVER installs or writes any baseline
	file (it cannot reach _staged_install).  An ABSENT baseline is the
	bootstrap case: the fresh observation is itself the (maximal) drift and
	yields the initial candidate.  Diagnostic output under build/tmp is
	fine; committed data stays byte-identical."""
	# Begin-invalidate FIRST, before any operation that can return or
	# fail: no candidate from an earlier run may survive into this
	# verification's identity (a failed/aborted run — including discovery
	# and toolchain-probe failures — must not leave a stale candidate that
	# appears to describe it).  An unlink OSError falls through to the
	# top-level infrastructure boundary (controlled exit 2).
	try:
		HANDOFF_PATH.unlink()
		print(f"invalidated previous candidate {HANDOFF_PATH}", file=out)
	except FileNotFoundError:
		pass
	fixtures, excluded = _audit._discover_fixtures(None)
	if not fixtures:
		print("no fixtures matched", file=out)
		return 2
	try:
		tc = _toolchain(extra)
	except InfraError as e:
		print(str(e), file=out)
		return 2
	universe = _audit._universe_dict(fixtures, excluded)
	snap_start = _snapshot(tc, universe)
	bootstrap = _baseline_absent(baseline)
	exp = None
	if bootstrap:
		print(f"no reviewed baseline at {baseline}: BOOTSTRAP discovery run — "
		      f"the fresh observation is the drift and yields the initial "
		      f"candidate.", file=out)
	else:
		exp = _expectation_from_baseline(baseline, universe, out)
		if exp is None:
			return 2
		if exp["projections"] is None:
			print(f"reviewed baseline {baseline} lacks per-fixture projections "
			      f"(projections.json); migrate/promote it before verify can check "
			      f"per-fixture exactness.", file=out)
			return 2

	print(f"verify: fresh full compile of {len(fixtures)} fixtures vs the reviewed "
	      f"baseline ({jobs} jobs)", file=out, flush=True)
	compile_dir = _mkscratch("corpus-verify-")
	(compile_dir / "audit").mkdir(parents=True, exist_ok=True)
	fx_hash = {f.name: _audit._fixture_hash(f) for f in fixtures}
	started = time.time()
	try:
		fresh_proj, fresh_failed = _compile_set(fixtures, compile_dir, extra, jobs, out, started)
		if not _hashes_stable(fixtures, fx_hash, out):
			return 2
		if not _finish_snapshot_ok(snap_start, extra, out):
			return 2
	except _audit.CorpusProjectionError as e:
		print(f"ABORT (infrastructure): {e}", file=out)
		return 2
	except InfraError as e:
		print(str(e), file=out)
		return 2
	finally:
		shutil.rmtree(compile_dir, ignore_errors=True)

	counters = _audit._merge_counters(fresh_proj.values())
	if bootstrap:
		problems = ["no reviewed baseline exists (bootstrap): the fresh "
		            "observation is the initial candidate"]
	else:
		problems = _fresh_vs_expectation(universe, fresh_proj, fresh_failed, exp)
	gate_failures = _audit._hard_gate_failures(counters)
	if problems or gate_failures:
		for p in problems:
			print(f"VERIFY DRIFT: {p}", file=out)
		for g in gate_failures:
			print(f"HARD GATE: {g}", file=out)
		_retain_actual(universe, fresh_proj, fresh_failed, counters, snap_start, started, jobs)
		if gate_failures:
			print(f"hard gates are nonzero: NO promotion candidate emitted.  "
			      f"Fresh actual retained at {ACTUAL_DIR} for diagnosis.", file=out)
			return 1
		_publish_fresh_candidate(universe, fresh_proj, fresh_failed, counters,
		                         snap_start, out, origin_label=str(ACTUAL_DIR),
		                         started=started, jobs=jobs)
		print(f"the fresh full run drifted from the committed reviewed baseline; "
		      f"FAILING with the baseline untouched.  Fresh actual retained at "
		      f"{ACTUAL_DIR} for diagnosis.  After review, re-baseline "
		      f"deliberately with `ownership-corpus-promote` (fast-or-fail: it "
		      f"validates and installs THIS candidate with zero compiles).", file=out)
		return 1
	# Exact match: verification with candidate-publication side effects —
	# the reviewable observation is exported even though promotion is
	# unnecessary.
	_publish_fresh_candidate(universe, fresh_proj, fresh_failed, counters,
	                         snap_start, out, origin_label="verify:exact-match",
	                         started=started, jobs=jobs)
	print("verify: fresh full compile EXACTLY matches the committed reviewed "
	      "baseline; zero hard gates.  Baseline untouched.", file=out)
	return 0


# ══ promote mode ═════════════════════════════════════════════════════

_HANDOFF_KEYS = {"schema_version", "kind", "origin", "snapshot", "run_meta",
                 "universe", "projections", "counters", "observed", "projected",
                 "payload_sha256"}
_RUN_META_KEYS = {"started_unix", "duration_s", "jobs", "repo_root", "python"}


_UNIVERSE_KEYS = {"inclusion_rule", "fixtures", "excluded", "compiled_ok", "failed"}
_ORIGIN_KEYS = {"work_dir", "toolchain_composite", "run_snapshot_composite"}


def _validate_handoff(h) -> "str | None":
	"""ONE exact handoff-schema validator: exact top-level + universe + origin key
	sets, typed fields, hex origin composites, unique/disjoint/exhaustive buckets
	AND observed/projected partitions, projections that merge to the aggregate.
	Returns None if valid, else a diagnostic string.  Nothing here can raise on a
	malformed nested value."""
	if not isinstance(h, dict):
		return "handoff is not an object"
	if set(h) != _HANDOFF_KEYS:
		return f"key set {sorted(set(h))} != {sorted(_HANDOFF_KEYS)}"
	if h["schema_version"] != HANDOFF_SCHEMA_VERSION:
		return f"schema {h['schema_version']} != {HANDOFF_SCHEMA_VERSION}"
	if h["kind"] != CANDIDATE_KIND:
		return f"kind {h['kind']!r} is not the fresh-verify candidate kind"
	# Digest seal FIRST: it covers the canonical PARSED payload (key order/
	# whitespace re-serialization is intentionally digest-neutral), so any
	# semantic payload mutation fails before deeper interpretation unless
	# deliberately resealed.
	if not _fp._is_hex64(h.get("payload_sha256")):
		return "payload_sha256 must be a hex digest"
	payload = {k: v for k, v in h.items() if k != "payload_sha256"}
	if hashlib.sha256(_fp.canonical_json(payload).encode()).hexdigest() != h["payload_sha256"]:
		return "payload digest mismatch (candidate corrupted)"
	snap = h["snapshot"]
	try:
		_fp.validate_fingerprint(snap)
	except ValueError as e:
		return f"snapshot is not a valid run snapshot ({e})"
	if snap.get("kind") != "run_snapshot":
		return "snapshot is not a run-snapshot object"
	m = h["run_meta"]
	def _finite_num(v):
		return (isinstance(v, (int, float)) and not isinstance(v, bool)
		        and math.isfinite(v))
	# repo_root/python are deliberately HISTORICAL verify provenance —
	# validated for shape only, never cross-checked against the current
	# identity (a moved checkout must not reject a valid delayed
	# promotion).
	if not (isinstance(m, dict) and set(m) == _RUN_META_KEYS
	        and _finite_num(m["started_unix"]) and m["started_unix"] > 0
	        and _finite_num(m["duration_s"]) and m["duration_s"] >= 0
	        and isinstance(m["jobs"], int) and not isinstance(m["jobs"], bool)
	        and m["jobs"] > 0
	        and isinstance(m["repo_root"], str) and m["repo_root"]
	        and isinstance(m["python"], str) and m["python"]):
		return "run_meta is not the verify-run metadata object"
	o = h["origin"]
	if not isinstance(o, dict) or set(o) != _ORIGIN_KEYS:
		return "origin has the wrong key set"
	if not isinstance(o["work_dir"], str):
		return "origin.work_dir must be a string"
	if not (_fp._is_hex64(o["toolchain_composite"]) and _fp._is_hex64(o["run_snapshot_composite"])):
		return "origin composites must be hex digests"
	u = h["universe"]
	if not isinstance(u, dict) or set(u) != _UNIVERSE_KEYS:
		return "universe has the wrong key set"
	try:
		_audit._validate_universe_schema("handoff", u)   # types of every nested value
	except ValueError as e:
		return str(e)
	ok, failed = u["compiled_ok"], u["failed"]
	included = [fx["name"] for fx in u["fixtures"]]
	if len(set(included)) != len(included):
		return "duplicate fixture names"
	if len(set(ok)) != len(ok) or len(set(failed)) != len(failed):
		return "duplicate entries in a bucket"
	if set(ok) & set(failed):
		return f"compiled_ok and failed overlap: {sorted(set(ok) & set(failed))}"
	if set(ok) | set(failed) != set(included):
		return "buckets are not exhaustive/disjoint over the included fixtures"
	if not _valid_projection_map(h["projections"]):
		return "projections are malformed"
	if set(h["projections"]) != set(ok):
		return "projection keys do not equal the compiled_ok bucket"
	if not _audit._valid_counter_map(h["counters"]):
		return "counters violate the strict schema"
	if _audit._merge_counters(h["projections"].values()) != h["counters"]:
		return "per-fixture projections do not merge to the aggregate counters"
	for key in ("observed", "projected"):
		v = h[key]
		if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
			return f"{key} must be a list of strings"
		if len(set(v)) != len(v):
			return f"{key} has duplicate entries"
	obs, proj = set(h["observed"]), set(h["projected"])
	if obs & proj:
		return f"observed and projected overlap: {sorted(obs & proj)}"
	if obs | proj != set(included):
		return "observed + projected are not exhaustive/disjoint over the universe"
	if (o["toolchain_composite"] != snap["toolchain"]["composite"]
			or o["run_snapshot_composite"] != snap["composite"]):
		return "origin composites disagree with the embedded snapshot"
	return None


def _expectation_from_baseline(baseline: Path, universe: dict, out):
	b = _read_baseline(baseline)
	if b is None:
		print(f"reviewed baseline {baseline} is unreadable/malformed.", file=out)
		return None
	exp = {
		"base_universe": b["base_universe"],
		"compiled_ok": b["compiled_ok"],
		"failed": b["failed"],
		"projections": b["projections"],   # may be None (older baseline)
		"counters": b["counters"],
		"source": "reviewed baseline",
	}
	problem = _expectation_consistency(exp, require_projections=False)
	if problem:
		print(f"reviewed baseline {baseline} is internally inconsistent: {problem}", file=out)
		return None
	return exp


def _expectation_consistency(exp, *, require_projections: bool) -> "str | None":
	"""Basic internal consistency: buckets exhaustive+disjoint over the included
	fixtures, per-fixture projections (when present) merge to the aggregate, and
	all counters obey the strict schema."""
	included = {e["name"] for e in exp["base_universe"]["fixtures"]}
	ok, failed = set(exp["compiled_ok"]), set(exp["failed"])
	if ok & failed:
		return f"compiled_ok and failed overlap: {sorted(ok & failed)}"
	if ok | failed != included:
		missing = sorted(included - (ok | failed))
		extra = sorted((ok | failed) - included)
		return f"buckets not exhaustive over included fixtures (missing={missing}, extra={extra})"
	if not _audit._valid_counter_map(exp["counters"]):
		return "counter values violate the strict schema"
	proj = exp["projections"]
	if proj is None:
		if require_projections:
			return "per-fixture projections missing"
		return None
	if set(proj) != ok:
		return "projection keys do not equal the compiled_ok bucket"
	if _audit._merge_counters(proj.values()) != exp["counters"]:
		return "per-fixture projections do not merge to the aggregate counters"
	return None



def run_promote(baseline: Path, *, extra, out=sys.stderr) -> int:
	"""FAST-OR-FAIL re-baseline: ZERO fixture compilation, no fallback.
	Validates the canonical fresh-verify candidate (schema, digest seal,
	producer kind, exhaustive observation, zero hard gates), recomputes the
	CURRENT toolchain/universe snapshot (hashing every fixture source — the
	staleness protection), requires exact identity with the candidate, and
	installs the candidate's observation via the existing staged-install
	proofs.  The candidate's verify-run snapshot and metadata are installed
	VERBATIM — promotion never recomputes duration from its own wall clock.
	Any mismatch fails before baseline mutation and instructs a new
	`ownership-corpus-verify` (the sole candidate producer)."""
	if not HANDOFF_PATH.is_file():
		print(f"promote requires the verified candidate {HANDOFF_PATH}; run "
		      f"`ownership-corpus-verify` to produce one (promotion never "
		      f"compiles).", file=out)
		return 2
	try:
		h = json.loads(HANDOFF_PATH.read_text())
	except (OSError, ValueError) as e:
		print(f"candidate {HANDOFF_PATH} is unreadable/malformed "
		      f"({type(e).__name__}: {e}); re-run `ownership-corpus-verify`.",
		      file=out)
		return 2
	problem = _validate_handoff(h)
	if problem is not None:
		print(f"candidate {HANDOFF_PATH} is invalid: {problem}; re-run "
		      f"`ownership-corpus-verify`.", file=out)
		return 2
	u = h["universe"]
	included = [fx["name"] for fx in u["fixtures"]]
	if h["projected"] != [] or sorted(h["observed"]) != sorted(included):
		print(f"candidate {HANDOFF_PATH} is not a complete fresh observation "
		      f"(projected results are never promotable); re-run "
		      f"`ownership-corpus-verify`.", file=out)
		return 2
	gate_failures = _audit._hard_gate_failures(h["counters"])
	if gate_failures:
		for g in gate_failures:
			print(f"HARD GATE: {g}", file=out)
		print(f"candidate {HANDOFF_PATH} carries nonzero hard gates and is not "
		      f"promotable; re-run `ownership-corpus-verify`.", file=out)
		return 2

	# CURRENT-TREE identity (zero builds of ANY kind: discovery + source/
	# tool/library hashing + existing-artifact bytes — the runtime archive
	# is never rebuilt here; a MISSING artifact fails toward a fresh
	# verify, and stale content shows up as the composite mismatch below).
	fixtures, excluded = _audit._discover_fixtures(None)
	if not fixtures:
		print("no fixtures matched", file=out)
		return 2
	try:
		tc = _toolchain_passive(extra)
	except InfraError as e:
		print(str(e), file=out)
		return 2
	universe = _audit._universe_dict(fixtures, excluded)
	snap_now = _snapshot(tc, universe)
	if (h["snapshot"]["composite"] != snap_now["composite"]
			or h["snapshot"]["toolchain"]["composite"] != snap_now["toolchain"]["composite"]):
		print(f"candidate {HANDOFF_PATH} was produced under a different "
		      f"toolchain/universe snapshot than the current tree; it is stale "
		      f"— re-run `ownership-corpus-verify`.", file=out)
		return 2
	cand_base = {k: u[k] for k in ("inclusion_rule", "fixtures", "excluded")}
	cur_base = {k: universe[k] for k in ("inclusion_rule", "fixtures", "excluded")}
	if cand_base != cur_base:
		print(f"candidate {HANDOFF_PATH} describes a different fixture universe "
		      f"than the current tree; it is stale — re-run "
		      f"`ownership-corpus-verify`.", file=out)
		return 2

	proj = {k: dict(sorted(v.items())) for k, v in h["projections"].items()}
	failed = sorted(u["failed"])
	counters = h["counters"]
	if _equals_current_baseline(baseline, universe, proj, failed, counters, snap_now):
		print(f"promote: the verified candidate already equals the reviewed "
		      f"baseline at {baseline}; no-op (baseline unchanged).", file=out)
		return 0
	try:
		_staged_install(baseline, universe, proj, failed, counters,
		                h["snapshot"], h["run_meta"]["started_unix"],
		                h["run_meta"]["jobs"], out, run_meta=h["run_meta"])
	except InfraError as e:
		print(str(e), file=out)
		return 2
	print(f"promote: reviewed baseline at {baseline} installed from the "
	      f"verified candidate (zero compiles; verify remains the sole fresh "
	      f"authority).  Review the diff and commit.", file=out)
	return 0


def _fresh_vs_expectation(universe, fresh_proj, fresh_failed, exp) -> list[str]:
	problems: list[str] = []
	cur_base = {k: universe[k] for k in ("inclusion_rule", "fixtures", "excluded")}
	if cur_base != exp["base_universe"]:
		problems.append("fixture universe / source hashes / exclusions differ")
	if sorted(fresh_proj) != exp["compiled_ok"]:
		problems.append(
			f"compiled_ok bucket differs (unexpectedly compiling "
			f"{sorted(set(fresh_proj) - set(exp['compiled_ok']))}, unexpectedly failing "
			f"{sorted(set(exp['compiled_ok']) - set(fresh_proj))})")
	if sorted(fresh_failed) != exp["failed"]:
		problems.append(
			f"failed bucket differs (unexpectedly failing "
			f"{sorted(set(fresh_failed) - set(exp['failed']))}, unexpectedly compiling "
			f"{sorted(set(exp['failed']) - set(fresh_failed))})")
	if exp["projections"] is not None:
		for name in sorted(set(fresh_proj) & set(exp["projections"])):
			if dict(sorted(fresh_proj[name].items())) != exp["projections"][name]:
				problems.append(f"per-fixture projection differs for {name}")
	if _audit._merge_counters(fresh_proj.values()) != exp["counters"]:
		problems.append("aggregate counters differ from the expectation")
	return problems


def _retain_actual(universe, fresh_proj, fresh_failed, counters, snapshot, started, jobs) -> None:
	shutil.rmtree(ACTUAL_DIR, ignore_errors=True)
	(ACTUAL_DIR / "audit").mkdir(parents=True, exist_ok=True)
	_audit._emit_run(ACTUAL_DIR, universe, sorted(fresh_proj), sorted(fresh_failed),
	                 counters, started, jobs)
	_atomic_json(ACTUAL_DIR / "projections.json", dict(sorted(fresh_proj.items())))
	_fp.write_atomic(ACTUAL_DIR / "fingerprint.json", snapshot)


def _equals_current_baseline(baseline, universe, fresh_proj, fresh_failed,
                             counters, snap_start) -> bool:
	"""True only if the installed baseline is ALREADY byte-equivalent to the fresh
	result — universe, buckets, per-fixture projections, aggregate, AND a stored
	fingerprint whose composite equals this run's snapshot.  A missing / malformed
	/ stale fingerprint is NOT equal, so promotion installs (and creates/refreshes
	fingerprint.json) rather than silently no-op'ing past it."""
	b = _read_baseline(baseline)
	if b is None:
		return False
	cur_base = {k: universe[k] for k in ("inclusion_rule", "fixtures", "excluded")}
	if (cur_base != b["base_universe"] or sorted(fresh_proj) != b["compiled_ok"]
			or sorted(fresh_failed) != b["failed"] or counters != b["counters"]):
		return False
	# a LEGACY baseline (no per-fixture projections) is never byte-equal to a
	# fresh result that carries them — force the install so it gains projections.
	if b["projections"] is None:
		return False
	fresh_sorted = {k: dict(sorted(v.items())) for k, v in fresh_proj.items()}
	if dict(sorted(fresh_sorted.items())) != b["projections"]:
		return False
	# the stored fingerprint must exist AND match this run's snapshot exactly;
	# missing/malformed/stale forces installation (creates a real fingerprint).
	try:
		stored = _fp.read_fingerprint(baseline / "fingerprint.json")
	except (OSError, ValueError, KeyError, TypeError):
		return False
	return stored["composite"] == snap_start["composite"]


def _staged_install(baseline, universe, fresh_proj, fresh_failed, counters,
                    snapshot, started, jobs, out, run_meta=None) -> None:
	"""Build+validate the complete replacement in a sibling STAGING dir, then
	install each file with os.replace, then reload+validate the installed bundle.
	No crash-recovery subsystem."""
	baseline.mkdir(parents=True, exist_ok=True)
	staging = baseline.parent / f".{baseline.name}.staging"
	shutil.rmtree(staging, ignore_errors=True)
	(staging / "audit").mkdir(parents=True, exist_ok=True)
	try:
		_audit._emit_run(staging, universe, sorted(fresh_proj), sorted(fresh_failed),
		                 counters, started, jobs, metadata=run_meta)
		projections = dict(sorted({k: dict(sorted(v.items())) for k, v in fresh_proj.items()}.items()))
		_atomic_json(staging / "projections.json", projections)
		_fp.write_atomic(staging / "fingerprint.json", snapshot)
		(staging / "BASELINE.md").write_text(_baseline_md(snapshot, counters, universe))
		# validate the staged bundle before touching the live baseline.
		if _read_baseline(staging) is None:
			raise InfraError("staged baseline failed validation; live baseline untouched")
		files = ("aggregate.json", "manifest.json", "metadata.json",
		         "fingerprint.json", "projections.json", "BASELINE.md")
		try:
			for name in files:
				os.replace(staging / name, baseline / name)
		except OSError as e:
			raise InfraError(f"baseline install failed ({type(e).__name__}: {e})") from e
	finally:
		shutil.rmtree(staging, ignore_errors=True)
	# post-install: the installed bundle must reload and match the fresh result
	# EXACTLY — universe, buckets, per-fixture projections, counters, fingerprint.
	installed = _read_baseline(baseline)
	if installed is None:
		raise InfraError("post-install validation failed: installed baseline does not load")
	cur_base = {k: universe[k] for k in ("inclusion_rule", "fixtures", "excluded")}
	fresh_sorted = {k: dict(sorted(v.items())) for k, v in fresh_proj.items()}
	if (installed["base_universe"] != cur_base
			or installed["compiled_ok"] != sorted(fresh_proj)
			or installed["failed"] != sorted(fresh_failed)
			or installed["counters"] != counters
			or installed["projections"] != dict(sorted(fresh_sorted.items()))):
		raise InfraError("post-install validation failed: installed baseline != fresh result")
	try:
		if _fp.read_fingerprint(baseline / "fingerprint.json")["composite"] != snapshot["composite"]:
			raise InfraError("post-install validation failed: installed fingerprint mismatch")
	except (OSError, ValueError, KeyError, TypeError) as e:
		raise InfraError(f"post-install fingerprint unreadable ({type(e).__name__}: {e})") from e


def _baseline_md(snapshot, counters, universe) -> str:
	try:
		import re
		versions = (ROOT / "lang" / "versions.py").read_text()
		drv = re.search(r'DRIFTC_VERSION: str = "([^"]+)"', versions).group(1)
		abi = re.search(r"DRIFT_RT_ABI_VERSION: int = (\d+)", versions).group(1)
	except Exception:
		drv, abi = "unknown", "unknown"
	return (
		"# Reviewed ownership-corpus baseline\n\n"
		"The authoritative golden state `just ownership-corpus-verify` (CI/cert)\n"
		"checks against, and the seed for a clean clone's `just ownership-corpus-check`.\n"
		"The exact universe and counters live in the machine files beside this note;\n"
		"`projections.json` holds the per-fixture ownership projections used for fast\n"
		"clean-clone seeding and exact per-fixture verification.\n\n"
		"## Provenance\n\n"
		"Produced ONLY by `just ownership-corpus-promote` (fast-or-fail, ZERO\n"
		"compiles): the complete fresh observation came from a single\n"
		"`ownership-corpus-verify` run — stable start==end toolchain fingerprint,\n"
		"every hard gate at zero — published as the digest-sealed candidate,\n"
		"reviewed, validated against the then-current tree identity, and installed\n"
		"via staged writes with the verify run's snapshot and metadata verbatim.\n"
		"CI/cert (`ownership-corpus-verify`) NEVER writes this baseline.  The Git\n"
		"commit that lands these files IS the approval; reviewer identity and date\n"
		"come from Git history.\n\n"
		f"| field | value |\n|---|---|\n"
		f"| driftc / ABI | **{drv}** / **ABI {abi}** |\n"
		f"| run snapshot composite | `{snapshot['composite']}` |\n"
		f"| toolchain composite | `{snapshot['toolchain']['composite']}` |\n"
		f"| discovered fixtures | {len(universe.get('fixtures', []))} included / "
		f"{len(universe.get('excluded', []))} rule-excluded |\n"
		f"| counter keys | {len(counters)} |\n\n"
		"## Distinct from the ownership matrix\n\n"
		"The 51-fixture ownership **matrix** (`just ownership-matrix-check`, inside\n"
		"`just test`) and this full-corpus **audit** are separate gates.  Earlier\n"
		"version-by-version provenance is in `doc/history.md`; the process is documented\n"
		"in `doc/ownership-corpus-gate.md`.\n")


def main(argv: "list[str] | None" = None) -> int:
	ap = argparse.ArgumentParser(description=__doc__,
	                             formatter_class=argparse.RawDescriptionHelpFormatter)
	ap.add_argument("work", nargs="?", help="developer work dir "
	                f"(default {DEFAULT_WORK_DIR}); ignored by --verify/--promote")
	mode = ap.add_mutually_exclusive_group()
	mode.add_argument("--verify", action="store_true",
	                  help="CI/cert gate and candidate producer: fresh full compile vs "
	                       "the committed reviewed baseline; fail on any drift; every "
	                       "complete zero-hard-gate run republishes the promotion "
	                       "candidate; tracked baseline files are never written")
	mode.add_argument("--promote", action="store_true",
	                  help="deliberate re-baseline, fast-or-fail: validates the REQUIRED "
	                       "fresh-verify candidate against the current tree identity and "
	                       "installs it with ZERO compilation; any mismatch fails and "
	                       "requires a new verify")
	ap.add_argument("-j", "--jobs", type=int, default=None,
	                help="compile worker count for check/verify (default: cpu count); "
	                     "not accepted with --promote, which never compiles")
	ap.add_argument("--select", help="comma-separated fixtures to force-recompile "
	                                 "(developer lane; the run stays full-universe)")
	ap.add_argument("--baseline", default=str(ROOT / "lang" / "tests" / "ownership_corpus" / "reviewed-baseline"),
	                help="reviewed baseline dir")
	ap.add_argument("--driftc-args", default="", help="extra args for every driftc invocation")
	args = ap.parse_args(argv)

	if args.promote and args.jobs is not None:
		print("--promote performs no compilation; -j/--jobs is not accepted "
		      "(worker counts apply to check/verify only)", file=sys.stderr)
		return 2
	jobs = args.jobs if args.jobs is not None else (os.cpu_count() or 4)
	if jobs <= 0:
		print(f"--jobs must be positive (got {jobs})", file=sys.stderr)
		return 2
	try:
		extra = shlex.split(args.driftc_args) if args.driftc_args else []
	except ValueError as e:
		print(f"--driftc-args is not a valid shell string: {e}", file=sys.stderr)
		return 2
	baseline = Path(args.baseline)

	# Top-level guard: convert every known infrastructure boundary (discovery,
	# hashing, scratch creation, worker subprocess timeouts/OSError, actual-result
	# retention, staged writes) into a controlled exit 2 with a stderr
	# diagnostic — never a traceback.
	try:
		if args.verify or args.promote:
			if args.select:
				print("--select is a developer-lane option; not used with "
				      "--verify/--promote", file=sys.stderr)
				return 2
			if args.work:
				print("--verify/--promote take no work directory (they never read the "
				      "developer cache)", file=sys.stderr)
				return 2
			with _corpus_lock():
				if args.verify:
					return run_verify(baseline, jobs=jobs, extra=extra)
				return run_promote(baseline, extra=extra)

		work = Path(args.work) if args.work else DEFAULT_WORK_DIR
		select = set(args.select.split(",")) if args.select else set()
		with _corpus_lock():
			return run_check(work, select=select, jobs=jobs, extra=extra,
			                 baseline=baseline)
	except InfraError as e:
		print(str(e), file=sys.stderr)
		return 2
	except (OSError, subprocess.SubprocessError, ValueError) as e:
		# ValueError covers UnicodeDecodeError (e.g. non-UTF-8 expected.json escaping
		# _discover_fixtures) — every known current-tree read boundary fails closed.
		print(f"infrastructure error ({type(e).__name__}: {e})", file=sys.stderr)
		return 2


if __name__ == "__main__":
	raise SystemExit(main())
