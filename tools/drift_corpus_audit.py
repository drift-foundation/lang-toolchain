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

_scratch_base_cached = None


def _scratch_base():
	"""Lazily create the compile scratch root — ONLY when actually compiling.
	The compile-free preflight imports this module but must not create a
	session directory as an import side effect."""
	global _scratch_base_cached
	if _scratch_base_cached is None:
		_scratch_base_cached = _drift_session_root(base=ROOT / "build" / "tmp")
	return _scratch_base_cached


_compile_contract_cached = None


def _compile_contract():
	"""The single corpus compile contract (argv / normalized env / variant /
	tool selection).  BOTH _compile_one and the fingerprint consume it."""
	global _compile_contract_cached
	if _compile_contract_cached is None:
		import importlib.util
		spec = importlib.util.spec_from_file_location(
			"corpus_compile_contract", ROOT / "tools" / "corpus_compile_contract.py")
		mod = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(mod)
		_compile_contract_cached = mod
	return _compile_contract_cached

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
	cc = _compile_contract()
	with tempfile.TemporaryDirectory(prefix="corpus-audit-", dir=str(_scratch_base())) as td:
		# The SINGLE compile contract builds both the normalized child env (a
		# minimal constructed env with a CONTROLLED TMPDIR pointed at this
		# per-fixture scratch dir) and the exact argv — the same authority the
		# fingerprint records, so they can never diverge.
		env = cc.normalized_child_env(scratch=td)
		env[cc.AUDIT_FILE_ENV] = str(audit_file)
		argv = cc.driftc_argv(fixture / "main.drift", entry, Path(td) / "bin",
		                      extra_args, ROOT / "stdlib")
		res = subprocess.run(
			[sys.executable, "-m", "lang.driftc.driftc", *argv],
			cwd=ROOT, env=env, capture_output=True, text=True, timeout=600,
		)
	ok = res.returncode == 0 and audit_file.is_file()
	if not ok and audit_file.exists():
		# A failed compile may leave partial audit lines; exclude it
		# entirely so aggregation covers exactly the success set.
		audit_file.unlink()
	return name, ok


class CorpusProjectionError(RuntimeError):
	"""A per-fixture audit file could not be parsed into a trustworthy
	projection.  This is an INFRASTRUCTURE error (truncated/duplicated/malformed
	audit output), never a compile finding — it must abort the run, never be
	silently absorbed as an empty or under-counted projection."""


def _fixture_projection(audit_file: Path) -> dict[str, int]:
	"""The per-fixture PROJECTION: the ownership-authoring counters from a SINGLE
	fixture's compile, read from its audit jsonl.  The corpus is a
	single-compilation-unit universe, so a well-formed file carries EXACTLY ONE
	`aggregate` record; the sum of projections IS the run aggregate.

	Fails closed (CorpusProjectionError) on anything that would corrupt the
	count: unreadable file, an audit-tagged line with malformed JSON or a
	non-object record, ZERO aggregate records (truncated/empty output), MORE THAN
	ONE aggregate record (duplicate — would double-count), a non-string counter
	key, or a non-int / bool counter value."""
	try:
		lines = audit_file.read_text().splitlines()
	except OSError as e:
		raise CorpusProjectionError(f"cannot read audit file {audit_file}: {e}") from e
	aggregates: list[tuple[int, dict]] = []
	for lineno, line in enumerate(lines, 1):
		m = _AUDIT_LINE_RE.search(line)
		if not m:
			continue
		try:
			rec = json.loads(m.group(1))
		except json.JSONDecodeError as e:
			raise CorpusProjectionError(
				f"malformed audit JSON in {audit_file} line {lineno}: {e}") from e
		if not isinstance(rec, dict):
			raise CorpusProjectionError(
				f"audit record in {audit_file} line {lineno} is not an object")
		if rec.get("record") == "aggregate":
			aggregates.append((lineno, rec))
	if not aggregates:
		raise CorpusProjectionError(
			f"no aggregate record in {audit_file} — truncated or empty audit output")
	if len(aggregates) > 1:
		raise CorpusProjectionError(
			f"{len(aggregates)} aggregate records in {audit_file} "
			f"(lines {[ln for ln, _ in aggregates]}) — duplicate would double-count")
	counters: dict[str, int] = {}
	for key, val in aggregates[0][1].items():
		if key == "record":
			continue
		if not isinstance(key, str):
			raise CorpusProjectionError(f"non-string counter key {key!r} in {audit_file}")
		if not isinstance(val, int) or isinstance(val, bool):
			raise CorpusProjectionError(
				f"counter {key!r} in {audit_file} is {val!r}, not a non-bool int")
		counters[key] = val
	return dict(sorted(counters.items()))


def _merge_counters(parts) -> dict[str, int]:
	total: dict[str, int] = {}
	for part in parts:
		for key, val in part.items():
			total[key] = total.get(key, 0) + val
	return dict(sorted(total.items()))


def _aggregate(run_dir: Path, compiled_ok: list[str]) -> dict[str, int]:
	return _merge_counters(
		_fixture_projection(run_dir / "audit" / f"{name}.jsonl")
		for name in compiled_ok)


def _stable_json(obj) -> str:
	return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def _emit_run(run_dir: Path, universe: dict,
              compiled_ok: list[str], failed: list[str],
              counters: dict[str, int], started: float, jobs: int,
              metadata: "dict | None" = None) -> None:
	"""Write the manifest/aggregate/metadata run files from an ALREADY-CAPTURED
	universe dict (the resumable check passes the exact universe it validated at
	the start of the run, so the manifest can never describe re-hashed source
	that drifted from the projections)."""
	universe = dict(universe)
	universe["compiled_ok"] = sorted(compiled_ok)
	universe["failed"] = sorted(failed)
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
	# `metadata` (when given) is installed VERBATIM: fast promotion passes
	# the verify run's measured metadata so the review gap never leaks into
	# duration_s.
	(run_dir / "metadata.json").write_text(_stable_json(metadata if metadata is not None else {
		"started_unix": started,
		"duration_s": round(time.time() - started, 1),
		"jobs": jobs,
		"repo_root": str(ROOT),
		"python": sys.version.split()[0],
	}))


def _write_run(run_dir: Path, fixtures: list[Path], excluded: list[dict],
               compiled_ok: list[str], failed: list[str],
               counters: dict[str, int], started: float, jobs: int) -> None:
	"""Discover-and-emit: recomputes the universe from the fixture dirs.  Used by
	the standalone audit path; the resumable check calls _emit_run with its
	captured universe instead."""
	_emit_run(run_dir, _universe_dict(fixtures, excluded),
	          compiled_ok, failed, counters, started, jobs)


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


def _valid_counter_map(counters) -> bool:
	"""Boolean form of _validate_counters_schema: str keys, non-bool int values."""
	try:
		_validate_counters_schema("", counters)
		return True
	except ValueError:
		return False


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
		bu, nu = base_universe, new_universe
		# Classify by the WHOLE fixture set, not per-bucket, so an added
		# fixture reads as "added" (not as noise in every bucket) and a
		# fixture that merely changed compile outcome reads as "moved".
		base_all = set(bu["compiled_ok"]) | set(bu["failed"])
		new_all = set(nu["compiled_ok"]) | set(nu["failed"])
		added = sorted(new_all - base_all)
		removed = sorted(base_all - new_all)
		bucket_of_base = {**{n: "compiled_ok" for n in bu["compiled_ok"]},
		                  **{n: "failed" for n in bu["failed"]}}
		bucket_of_new = {**{n: "compiled_ok" for n in nu["compiled_ok"]},
		                 **{n: "failed" for n in nu["failed"]}}
		moved = sorted(
			n for n in base_all & new_all
			if bucket_of_base.get(n) != bucket_of_new.get(n))
		bh = {f["name"]: f["sha256"] for f in bu["fixtures"]}
		nh = {f["name"]: f["sha256"] for f in nu["fixtures"]}
		changed = sorted(k for k in bh.keys() & nh.keys() if bh[k] != nh[k])

		def _emit(label: str, names: list[str], *, hint: str = "") -> None:
			if not names:
				return
			suffix = f"  ({hint})" if hint else ""
			print(f"  {label} ({len(names)}):{suffix}", file=sys.stderr)
			for name in names:
				print(f"      {name}", file=sys.stderr)

		print("UNIVERSE MISMATCH: baseline and new run do not cover the "
		      "identical fixture universe — deltas would be meaningless.",
		      file=sys.stderr)
		_emit("added fixtures", added, hint="present only in the new run")
		_emit("removed fixtures", removed, hint="present only in the baseline")
		_emit("changed compile outcome", moved,
		      hint="same fixture, compiled_ok<->failed flipped")
		_emit("changed source (same name)", changed)
		print("  → the fixture set changed; re-capture the baseline before "
		      "comparing (run the audit without --baseline to mint a fresh "
		      "reference over the current universe), or restrict --only to the "
		      "shared set.", file=sys.stderr)
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


# ── Static preflight ─────────────────────────────────────────────────
# A fast, COMPILE-FREE universe check against the reviewed baseline.  It
# CANNOT detect compiler-result flips (compiled_ok/failed) — that needs a
# full corpus run — but it immediately reveals KNOWN universe drift
# (fixture add/remove/content-change, exclusion changes, inclusion-rule
# changes, included<->excluded transitions) so a full run is not spent
# merely to rediscover it.  This is also the single static-universe
# authority a later --resume layer will reuse.

def _universe_dict(fixtures, excluded) -> dict:
	"""The SINGLE builder of the static-universe shape — inclusion rule +
	included {name, content-sha256} + exclusions {name, reason}.  Used by BOTH
	the preflight (`_static_universe`) and the full run (`_write_run`) so the
	manifest universe authority is never duplicated."""
	return {
		"inclusion_rule": INCLUSION_RULE,
		"fixtures": [{"name": f.name, "sha256": _fixture_hash(f)} for f in fixtures],
		"excluded": sorted(excluded, key=lambda e: e["name"]),
	}


def _static_universe(only: list[str] | None = None) -> dict:
	"""The current STATIC corpus universe — inclusion rule, included fixtures
	(name + content sha256), and exclusions (name + reason) — computed WITHOUT
	compiling.  Deliberately omits compiled_ok/failed: those are compiler
	RESULTS a static preflight cannot know and must not guess."""
	return _universe_dict(*_discover_fixtures(only))


def _baseline_partition_errors(u: dict) -> list[str]:
	"""Integrity of the BASELINE partition itself (independent of the current
	tree): included / excluded / compiled_ok / failed must be a well-formed
	partition — no duplicates, no name in two sets, and compiled_ok∪failed must
	equal the included set exactly.  A broken baseline is a HARD error: it
	cannot be a trustworthy comparison point."""
	errs: list[str] = []
	inc = [f["name"] for f in u.get("fixtures", [])]
	exc = [e["name"] for e in u.get("excluded", [])]
	ok = list(u.get("compiled_ok", []))
	failed = list(u.get("failed", []))

	def _dups(names: list[str], label: str) -> None:
		seen: set[str] = set()
		dup: set[str] = set()
		for n in names:
			(dup if n in seen else seen).add(n)
		if dup:
			errs.append(f"baseline {label} has duplicate names: {sorted(dup)}")

	_dups(inc, "included")
	_dups(exc, "excluded")
	_dups(ok, "compiled_ok")
	_dups(failed, "failed")
	inc_s, exc_s, ok_s, failed_s = set(inc), set(exc), set(ok), set(failed)
	if inc_s & exc_s:
		errs.append(f"baseline names in BOTH included and excluded: {sorted(inc_s & exc_s)}")
	if ok_s & failed_s:
		errs.append(f"baseline names in BOTH compiled_ok and failed: {sorted(ok_s & failed_s)}")
	if (ok_s | failed_s) != inc_s:
		missing = inc_s - (ok_s | failed_s)
		extra = (ok_s | failed_s) - inc_s
		if missing:
			errs.append(f"baseline included fixtures absent from compiled_ok∪failed: {sorted(missing)}")
		if extra:
			errs.append(f"baseline compiled_ok∪failed names absent from included set: {sorted(extra)}")
	return errs


_PREFLIGHT_DRIFT_KEYS = (
	"inclusion_rule_changed", "included_added", "included_removed",
	"content_changed", "excluded_added", "excluded_removed",
	"exclusion_reason_changed", "included_to_excluded", "excluded_to_included",
)


def _preflight_compare(base_u: dict, cur_u: dict) -> dict:
	"""Static universe diff (pure): current tree vs reviewed baseline.  Covers
	the inclusion RULE, included fixture add/remove/content-change, exclusion
	add/remove/reason-change, and included<->excluded TRANSITIONS."""
	base_inc = {f["name"]: f["sha256"] for f in base_u.get("fixtures", [])}
	cur_inc = {f["name"]: f["sha256"] for f in cur_u.get("fixtures", [])}
	base_exc = {e["name"]: e.get("reason") for e in base_u.get("excluded", [])}
	cur_exc = {e["name"]: e.get("reason") for e in cur_u.get("excluded", [])}
	rep: dict = {}
	if base_u.get("inclusion_rule") != cur_u.get("inclusion_rule"):
		rep["inclusion_rule_changed"] = {
			"baseline": base_u.get("inclusion_rule"),
			"current": cur_u.get("inclusion_rule"),
		}
	rep["included_added"] = sorted(set(cur_inc) - set(base_inc))
	rep["included_removed"] = sorted(set(base_inc) - set(cur_inc))
	rep["content_changed"] = sorted(
		n for n in (set(base_inc) & set(cur_inc)) if base_inc[n] != cur_inc[n])
	rep["excluded_added"] = sorted(set(cur_exc) - set(base_exc))
	rep["excluded_removed"] = sorted(set(base_exc) - set(cur_exc))
	rep["exclusion_reason_changed"] = sorted(
		f"{n}: {base_exc[n]!r} -> {cur_exc[n]!r}"
		for n in (set(base_exc) & set(cur_exc)) if base_exc[n] != cur_exc[n])
	# Transitions — called out explicitly (derivable from the sets above, but
	# an included fixture becoming excluded, or vice-versa, is the highest-signal
	# drift: it silently changes what the corpus even attempts to compile).
	rep["included_to_excluded"] = sorted(set(base_inc) & set(cur_exc))
	rep["excluded_to_included"] = sorted(set(base_exc) & set(cur_inc))
	return rep


def _preflight_has_drift(rep: dict) -> bool:
	return any(rep.get(k) for k in _PREFLIGHT_DRIFT_KEYS)


def _run_preflight(baseline_dir: Path) -> int:
	"""Exit 0 = current universe matches the reviewed baseline exactly AND the
	baseline partition is intact; 1 = static drift (categorized report);
	2 = malformed baseline or broken baseline partition."""
	try:
		bm = json.loads((baseline_dir / "manifest.json").read_text())
		base_u = bm["universe"]
		_validate_universe_schema("baseline", base_u)
	except (OSError, ValueError, KeyError, TypeError) as e:
		print(f"PREFLIGHT: unusable baseline ({type(e).__name__}: {e})", file=sys.stderr)
		return 2
	part_errs = _baseline_partition_errors(base_u)
	if part_errs:
		print("PREFLIGHT: baseline partition integrity FAILED:", file=sys.stderr)
		for e in part_errs:
			print(f"  - {e}", file=sys.stderr)
		return 2
	try:
		cur_u = _static_universe(None)
	except (OSError, ValueError) as e:
		# The complete current-tree read/parse failure family: OSError (missing /
		# unreadable file), and ValueError incl. UnicodeDecodeError (a non-UTF-8
		# expected.json / source) — either exits 2 cleanly, never tracebacks.
		print(f"PREFLIGHT: failed reading the current fixture tree ({type(e).__name__}: {e})", file=sys.stderr)
		return 2
	rep = _preflight_compare(base_u, cur_u)
	print(f"PREFLIGHT: baseline {baseline_dir}")
	print(f"  inclusion rule: {'CHANGED' if 'inclusion_rule_changed' in rep else 'unchanged'}")
	print(f"  included: {len(cur_u['fixtures'])} (baseline {len(base_u.get('fixtures', []))})"
	      f"  +{len(rep['included_added'])} -{len(rep['included_removed'])}"
	      f"  ~{len(rep['content_changed'])} content-changed")
	print(f"  excluded: {len(cur_u['excluded'])} (baseline {len(base_u.get('excluded', []))})"
	      f"  +{len(rep['excluded_added'])} -{len(rep['excluded_removed'])}"
	      f"  reason~{len(rep['exclusion_reason_changed'])}")
	print(f"  transitions: included→excluded {len(rep['included_to_excluded'])},"
	      f" excluded→included {len(rep['excluded_to_included'])}")
	if _preflight_has_drift(rep):
		for k in _PREFLIGHT_DRIFT_KEYS:
			v = rep.get(k)
			if not v:
				continue
			if isinstance(v, list) and len(v) > 20:
				shown = v[:20] + [f"...(+{len(v) - 20} more)"]
			else:
				shown = v
			print(f"  [{k}] {shown}")
		print("PREFLIGHT: static universe DRIFT vs reviewed baseline (expected "
		      "during development — review, then re-baseline via promotion). "
		      "NOTE: cannot detect compiler-result flips — run the full corpus for that.")
		return 1
	print("PREFLIGHT: static universe matches the reviewed baseline exactly. "
	      "(Cannot detect compiler-result flips — run the full corpus for that.)")
	return 0


def main(argv: list[str] | None = None) -> int:
	ap = argparse.ArgumentParser(description=__doc__,
	                             formatter_class=argparse.RawDescriptionHelpFormatter)
	ap.add_argument("--out", help="run output directory (required unless --preflight)")
	ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4)
	ap.add_argument("--only", help="comma-separated fixture-name subset")
	ap.add_argument("--baseline", help="prior run dir to compare against")
	ap.add_argument("--preflight", action="store_true",
	                help="STATIC universe check vs --baseline: compare inclusion "
	                     "rule, included-fixture names + content hashes, "
	                     "exclusions + reasons, and included<->excluded "
	                     "transitions WITHOUT compiling, and verify baseline "
	                     "partition integrity.  Fast; cannot detect "
	                     "compiler-result flips.  Exit 0=match, 1=drift, 2=error")
	ap.add_argument("--require-zero-delta", action="store_true",
	                help="certification mode: with --baseline, require the "
	                     "IDENTICAL counter key set and every delta exactly 0 "
	                     "(plus the usual identical-universe and hard-gate "
	                     "checks); any divergence fails")
	ap.add_argument("--driftc-args", default="",
	                help="extra args passed to every driftc invocation")
	args = ap.parse_args(argv)
	if args.preflight:
		# Static, compile-free path — never touches --out / the run directory.
		if not args.baseline:
			print("--preflight needs --baseline", file=sys.stderr)
			return 2
		if args.require_zero_delta:
			print("--require-zero-delta is a full-run flag; not used with --preflight",
			      file=sys.stderr)
			return 2
		if args.only:
			# A subset would report every other baseline fixture as "removed" —
			# meaningless.  The preflight compares the FULL current universe to the
			# full baseline; refuse --only rather than emit a misleading diff.
			print("--only is not supported with --preflight (it compares the FULL "
			      "universe against the baseline)", file=sys.stderr)
			return 2
		return _run_preflight(Path(args.baseline))
	if not args.out:
		print("--out is required (unless --preflight)", file=sys.stderr)
		return 2
	if args.require_zero_delta and not args.baseline:
		print("--require-zero-delta needs --baseline", file=sys.stderr)
		return 2

	# FAIL FAST on a missing/corrupt --baseline BEFORE the (~minutes-long)
	# compile sweep: `_compare` loads exactly these two files at the very end,
	# so validate them now (<1s) rather than after wasting the whole run.  This
	# is a pure precheck — a valid baseline passes it and the run proceeds
	# normally; `_compare` re-validates both sides (incl. the new run) at the end.
	if args.baseline:
		bl = Path(args.baseline)
		try:
			bm = json.loads((bl / "manifest.json").read_text())
			bc = json.loads((bl / "aggregate.json").read_text())["counters"]
			_validate_universe_schema("baseline", bm["universe"])
			_validate_counters_schema("baseline", bc)
		except (OSError, ValueError, KeyError, TypeError) as e:
			print(f"BASELINE PRECHECK FAILED ({type(e).__name__}: {e}) — fix "
			      f"--baseline {bl} and re-run; refusing to spend the compile "
			      f"sweep on an unusable baseline.", file=sys.stderr)
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
