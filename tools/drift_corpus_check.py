#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Ownership-corpus check + promote — the one public corpus process.

Two deliberately separate lanes: FAST PROJECTIONS during development, EXHAUSTIVE
FRESH EVIDENCE at promotion / certification.

  developer   `just ownership-corpus-check [<dir>]`  (default work dir
              build/tmp/ownership-corpus-work)
      A resumable, full-universe expectation.  Each fixture's PROJECTION (its
      ownership-authoring counters) is cached in a record keyed on the fixture
      CONTENT HASH.  Only new / source-edited / --select'ed fixtures recompile
      and become CURRENT observations; a compiler-fingerprint move does NOT
      force a full rebuild — old observations carry forward as PROJECTED (stale)
      values, visibly marked and never described as freshly verified.  Reused
      successes AND failures are accounted truthfully.  When the cache is empty,
      per-fixture projections are seeded from the checked-in reviewed baseline
      (fast clean clone).  The completed expectation is exported atomically to
      the cache-independent handoff build/tmp/ownership-corpus-projection.json.

  promote     `just ownership-corpus-promote`   (no directory)
      Never reads developer cache records.  The EXPECTATION is the handoff if it
      exists (validated to describe the current tree/universe first — a
      malformed or stale handoff is an error, never a silent baseline fallback),
      otherwise the checked-in reviewed baseline (clean clone / CI).  Performs
      ONE fresh full-universe compile in isolated scratch and requires a stable
      start==end fingerprint, EXACT agreement with the expectation (universe +
      source hashes, compiled/failed buckets, per-fixture projections, aggregate
      counters, exclusions + reasons), and zero hard gates.  On agreement it
      installs the reviewed baseline (a byte-preserving no-op when already
      equal); on disagreement it does NOT promote — it retains the fresh ACTUAL
      report separately and reports the unexpected differences.  Invoking
      promote is approval of the projected expectation, verified exhaustively.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib.util
import json
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


def _load(name: str):
	spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	return mod


_audit = _load("drift_corpus_audit")
_fp = _load("drift_corpus_fingerprint")

RECORD_SCHEMA_VERSION = 3
HANDOFF_SCHEMA_VERSION = 1

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
	_export_handoff(work, snap_start, universe, compiled_ok, failed, projections,
	                counters, observed, projected)
	print(f"checked {len(fixtures)} fixtures: {len(compiled_ok)} compiled_ok, "
	      f"{len(failed)} failed; {len(observed)} current / {len(projected)} projected; "
	      f"{len(counters)} counters. handoff -> {HANDOFF_PATH}", file=out)

	if baseline is not None:
		return _audit._compare(baseline, work, require_zero_delta=False)
	gate_failures = _audit._hard_gate_failures(counters)
	if gate_failures:
		for g in gate_failures:
			print(f"HARD GATE: {g}", file=out)
		return 1
	return 0


def _baseline_seed(baseline: Path) -> dict:
	"""Per-fixture seed entries from the reviewed baseline, so a clean clone's
	empty cache reuses baseline projections (as PROJECTED values) instead of
	recompiling.  {name: {hash, ok, proj, toolchain}}.  Only fixtures whose
	baseline projection is available seed a success; failures always seed."""
	b = _read_baseline(baseline)
	if b is None:
		return {}
	base_hash = {e["name"]: e["sha256"] for e in b["base_universe"]["fixtures"]}
	tc = b["toolchain"] if _fp._is_hex64(b["toolchain"] or "") else "0" * 64
	seed: dict[str, dict] = {}
	for name in b["failed"]:
		if name in base_hash:
			seed[name] = {"hash": base_hash[name], "ok": False, "proj": {}, "toolchain": tc}
	if b["projections"] is not None:
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
                    counters, observed, projected) -> None:
	handoff = {
		"schema_version": HANDOFF_SCHEMA_VERSION,
		"origin": {
			"work_dir": str(work),
			"toolchain_composite": snapshot["toolchain"]["composite"],
			"run_snapshot_composite": snapshot["composite"],
		},
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
	HANDOFF_PATH.parent.mkdir(parents=True, exist_ok=True)
	_atomic_json(HANDOFF_PATH, handoff)


def _atomic_json(path: Path, obj) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	tmp = path.parent / f".{path.name}.tmp"
	tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")
	os.replace(tmp, path)


# ══ promote mode ═════════════════════════════════════════════════════

_HANDOFF_KEYS = {"schema_version", "origin", "universe", "projections",
                 "counters", "observed", "projected"}


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
	return None


def _expectation_from_handoff(universe: dict, snap_start: dict, out):
	"""Load + exactly validate the canonical handoff and confirm it describes the
	CURRENT tree/universe AND was produced under the CURRENT toolchain.  Returns
	an expectation, or None on a malformed/stale handoff (an ERROR — never a
	silent baseline fallback)."""
	try:
		h = json.loads(HANDOFF_PATH.read_text())
	except (OSError, ValueError) as e:
		print(f"handoff {HANDOFF_PATH} is unreadable/malformed ({type(e).__name__}: "
		      f"{e}); regenerate it with `ownership-corpus-check`.", file=out)
		return None
	problem = _validate_handoff(h)
	if problem is not None:
		print(f"handoff {HANDOFF_PATH} is invalid: {problem}", file=out)
		return None
	# it must have been produced under the CURRENT toolchain — a handoff from an
	# older compiler is stale even when the fixture sources are unchanged (its
	# projections no longer describe current behaviour).
	if (h["origin"]["toolchain_composite"] != snap_start["toolchain"]["composite"]
			or h["origin"]["run_snapshot_composite"] != snap_start["composite"]):
		print(f"handoff {HANDOFF_PATH} was produced under a different toolchain "
		      f"than the current tree; it is stale — regenerate with "
		      f"`ownership-corpus-check`.", file=out)
		return None
	u = h["universe"]
	base_universe = {k: u[k] for k in ("inclusion_rule", "fixtures", "excluded")}
	cur_base = {k: universe[k] for k in ("inclusion_rule", "fixtures", "excluded")}
	if base_universe != cur_base:
		print(f"handoff {HANDOFF_PATH} describes a different fixture universe than "
		      f"the current tree; it is stale — regenerate with "
		      f"`ownership-corpus-check`.", file=out)
		return None
	return {
		"base_universe": base_universe,
		"compiled_ok": sorted(u["compiled_ok"]),
		"failed": sorted(u["failed"]),
		"projections": {k: dict(sorted(v.items())) for k, v in h["projections"].items()},
		"counters": h["counters"],
		"source": f"handoff {HANDOFF_PATH.name}",
	}


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


def run_promote(baseline: Path, *, jobs: int, extra, out=sys.stderr) -> int:
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

	# EXPECTATION: handoff if present (error on malformed/stale), else baseline.
	if HANDOFF_PATH.is_file():
		exp = _expectation_from_handoff(universe, snap_start, out)
	else:
		exp = _expectation_from_baseline(baseline, universe, out)
	if exp is None:
		return 2
	print(f"promote: expectation = {exp['source']}; fresh full compile of "
	      f"{len(fixtures)} fixtures ({jobs} jobs)", file=out, flush=True)

	compile_dir = _mkscratch("corpus-promote-")
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
	problems = _fresh_vs_expectation(universe, fresh_proj, fresh_failed, exp)
	gate_failures = _audit._hard_gate_failures(counters)
	if problems or gate_failures:
		for p in problems:
			print(f"PROMOTE MISMATCH: {p}", file=out)
		for g in gate_failures:
			print(f"HARD GATE: {g}", file=out)
		_retain_actual(universe, fresh_proj, fresh_failed, counters, snap_start, started, jobs)
		print(f"the fresh full run does not match the {exp['source']} expectation; "
		      f"NOT promoting.  Fresh actual retained at {ACTUAL_DIR} for diagnosis.",
		      file=out)
		return 1

	# Agreement + gates zero -> install (byte-preserving no-op if already equal).
	if _equals_current_baseline(baseline, universe, fresh_proj, fresh_failed, counters):
		print(f"promote: fresh full run already equals the reviewed baseline at "
		      f"{baseline}; no-op (baseline unchanged).", file=out)
		return 0
	try:
		_staged_install(baseline, universe, fresh_proj, fresh_failed, counters, snap_start, started, jobs, out)
	except InfraError as e:
		print(str(e), file=out)
		return 2
	print(f"promote: reviewed baseline at {baseline} replaced from a fresh full "
	      f"compile that matched the {exp['source']} expectation exactly.  Review "
	      f"the diff and commit.", file=out)
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


def _equals_current_baseline(baseline, universe, fresh_proj, fresh_failed, counters) -> bool:
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
	return dict(sorted(fresh_sorted.items())) == b["projections"]


def _staged_install(baseline, universe, fresh_proj, fresh_failed, counters,
                    snapshot, started, jobs, out) -> None:
	"""Build+validate the complete replacement in a sibling STAGING dir, then
	install each file with os.replace, then reload+validate the installed bundle.
	No crash-recovery subsystem."""
	baseline.mkdir(parents=True, exist_ok=True)
	staging = baseline.parent / f".{baseline.name}.staging"
	shutil.rmtree(staging, ignore_errors=True)
	(staging / "audit").mkdir(parents=True, exist_ok=True)
	try:
		_audit._emit_run(staging, universe, sorted(fresh_proj), sorted(fresh_failed),
		                 counters, started, jobs)
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
		"The checked-in expectation for `just ownership-corpus-promote` (and the seed\n"
		"for a clean clone's `just ownership-corpus-check`).  The exact universe and\n"
		"counters live in the machine files beside this note; `projections.json` holds\n"
		"the per-fixture ownership projections used for fast clean-clone seeding.\n\n"
		"## Provenance\n\n"
		"Produced ONLY by `drift_corpus_check.py` promotion: a fresh full compile that\n"
		"exactly matched the reviewed expectation (the developer projection handoff, or\n"
		"this baseline itself on a clean tree), under a stable start==end toolchain\n"
		"fingerprint with every hard gate at zero, then installed via staged writes.\n"
		"The Git commit that lands these files IS the approval; reviewer identity and\n"
		"date come from Git history.\n\n"
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
	                f"(default {DEFAULT_WORK_DIR}); ignored by --promote")
	ap.add_argument("--promote", action="store_true",
	                help="fresh full-universe compile vs the handoff (or baseline) "
	                     "expectation; installs the reviewed baseline on exact agreement")
	ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4)
	ap.add_argument("--select", help="comma-separated fixtures to force-recompile "
	                                 "(developer lane; the run stays full-universe)")
	ap.add_argument("--baseline", default=str(ROOT / "lang" / "tests" / "ownership_corpus" / "reviewed-baseline"),
	                help="reviewed baseline dir")
	ap.add_argument("--driftc-args", default="", help="extra args for every driftc invocation")
	args = ap.parse_args(argv)

	if args.jobs <= 0:
		print(f"--jobs must be positive (got {args.jobs})", file=sys.stderr)
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
		if args.promote:
			if args.select:
				print("--select is a developer-lane option; not used with --promote", file=sys.stderr)
				return 2
			if args.work:
				print("--promote takes no work directory (it reads only the canonical "
				      "handoff or the reviewed baseline)", file=sys.stderr)
				return 2
			return run_promote(baseline, jobs=args.jobs, extra=extra)

		work = Path(args.work) if args.work else DEFAULT_WORK_DIR
		select = set(args.select.split(",")) if args.select else set()
		return run_check(work, select=select, jobs=args.jobs, extra=extra, baseline=baseline)
	except InfraError as e:
		print(str(e), file=sys.stderr)
		return 2
	except (OSError, subprocess.SubprocessError) as e:
		print(f"infrastructure error ({type(e).__name__}: {e})", file=sys.stderr)
		return 2


if __name__ == "__main__":
	raise SystemExit(main())
