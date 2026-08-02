# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Pins for the ownership-corpus check + promote (tools/drift_corpus_check.py).

Two lanes over synthetic fixtures and a mocked compile (never the live reviewed
baseline, never a real compilation):

  developer  full-universe expectation; records keyed on fixture CONTENT HASH so
             a compiler-fingerprint move keeps old observations as PROJECTED
             (never a full rebuild); only new/edited/--select'd fixtures
             recompile; reused successes AND failures are accounted; the
             expectation is exported to a cache-independent handoff.

  promote    no work dir; never reads records; expectation = handoff if present
             (error on malformed/stale), else the reviewed baseline; ONE fresh
             full compile; exact agreement installs (byte-preserving no-op if
             already equal), disagreement retains a fresh actual + does not
             mutate the baseline.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _hx(s: str) -> str:
	return hashlib.sha256(s.encode()).hexdigest()


def _load(name: str):
	spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	return mod


_check = _load("drift_corpus_check")
_audit = _check._audit
_fp = _check._fp


def _tc_for(seed: str) -> dict:
	comp = {"contract_schema": 2, "compile_source": _hx(seed + "cs"),
	        "stdlib": _hx(seed + "std"), "audit_tool": _hx(seed + "a"),
	        "runtime": {"variant": "default"}, "native_libs": {}, "tools": {},
	        "driftc_argv_template": ["--dev"], "env": {}}
	return {"schema_version": _fp.FINGERPRINT_SCHEMA_VERSION, "kind": "toolchain",
	        "components": comp, "composite": _fp.composite_hash(comp),
	        "diagnostic": {"git_rev": None}}


def _composite(seed: str) -> str:
	return _tc_for(seed)["composite"]


def _mk_fixtures(tmp_path: Path, names) -> list[Path]:
	dirs = []
	for n in names:
		d = tmp_path / "fx" / n
		d.mkdir(parents=True)
		(d / "main.drift").write_text(f"module {n}\n")
		dirs.append(d)
	return dirs


class Harness:
	def __init__(self, monkeypatch, tmp_path, dirs, projections, seed="tc0"):
		self.dirs = dirs
		self.projections = projections          # name -> (ok, projection)
		self.seed = seed
		self.finish_seed = None
		self.compiles: list[str] = []
		self._tc_calls = 0
		self._hashes = {d.name: _hx(d.name) for d in dirs}
		monkeypatch.setattr(_audit, "_discover_fixtures",
		                    lambda only: ([d for d in dirs if only is None or d.name in only], []))
		monkeypatch.setattr(_audit, "_fixture_hash", lambda f: self._hashes[f.name])
		monkeypatch.setattr(_check, "_toolchain", self._tc)
		monkeypatch.setattr(_check, "_compile_and_project", self._compile)
		# redirect the fixed handoff/actual paths into the tmp tree
		monkeypatch.setattr(_check, "HANDOFF_PATH", tmp_path / "handoff.json")
		monkeypatch.setattr(_check, "ACTUAL_DIR", tmp_path / "actual")

	def _tc(self, extra):
		self._tc_calls += 1
		seed = self.seed
		if self.finish_seed is not None and self._tc_calls % 2 == 0:
			seed = self.finish_seed
		return _tc_for(seed)

	def _compile(self, fixture, compile_dir, extra):
		self.compiles.append(fixture.name)
		ok, proj = self.projections[fixture.name]
		return fixture.name, ok, proj


def _projs(names):
	return {n: (True, {"cnt": i + 1}) for i, n in enumerate(names)}


# ── record unit tests ────────────────────────────────────────────────

def test_record_keyed_on_fixture_hash_not_toolchain():
	rec = _check._make_record("f", _hx("f"), True, {"c": 3}, _composite("tc0"))
	assert _check._valid_record(rec) and set(rec) == _check._RECORD_KEYS
	assert "observed_toolchain_composite" in rec


def test_record_payload_digest_binds():
	rec = _check._make_record("f", _hx("f"), True, {"c": 3}, _composite("tc0"))
	assert not _check._valid_record(dict(rec, projection={"c": 9}))
	assert not _check._valid_record(dict(rec, payload_sha256="d" * 64))


# ── developer: reuse / projection / recompile ─────────────────────────

def test_default_work_dir_used_when_no_arg(monkeypatch):
	captured = {}
	monkeypatch.setattr(_check, "run_check",
	                    lambda work, **kw: captured.setdefault("work", work) or 0)
	_check.main(["-j", "2"])
	assert captured["work"] == _check.DEFAULT_WORK_DIR


def test_cold_then_warm_reuses_all(tmp_path, monkeypatch):
	names = ["a", "b", "c"]
	h = Harness(monkeypatch, tmp_path, _mk_fixtures(tmp_path, names), _projs(names))
	work = tmp_path / "work"
	assert _check.run_check(work, select=set(), jobs=2, extra=[], baseline=None) == 0
	assert sorted(h.compiles) == names
	rep1 = json.loads((work / "report.json").read_text())
	assert rep1["observed"] == names and rep1["projected"] == []
	# warm: nothing recompiles
	assert _check.run_check(work, select=set(), jobs=2, extra=[], baseline=None) == 0
	assert sorted(h.compiles) == names


def test_compiler_move_keeps_projected_no_full_rebuild(tmp_path, monkeypatch):
	"""The core goal: a toolchain-fingerprint move must NOT recompile everything;
	old observations carry forward as PROJECTED."""
	names = ["a", "b", "c"]
	h = Harness(monkeypatch, tmp_path, _mk_fixtures(tmp_path, names), _projs(names))
	work = tmp_path / "work"
	_check.run_check(work, select=set(), jobs=2, extra=[], baseline=None)
	assert len(h.compiles) == 3
	h.seed = "tc-COMPILER-CHANGED"
	h.compiles.clear()
	assert _check.run_check(work, select=set(), jobs=2, extra=[], baseline=None) == 0
	assert h.compiles == []                              # NO recompiles
	rep = json.loads((work / "report.json").read_text())
	assert rep["projected"] == names and rep["observed"] == []   # all now projected


def test_edited_fixture_recompiles_and_becomes_current(tmp_path, monkeypatch):
	names = ["a", "b", "c"]
	h = Harness(monkeypatch, tmp_path, _mk_fixtures(tmp_path, names), _projs(names))
	work = tmp_path / "work"
	_check.run_check(work, select=set(), jobs=2, extra=[], baseline=None)
	h.seed = "tc-CHANGED"                                # everything would be projected...
	h._hashes["b"] = _hx("EDITED")                       # ...but b's source changed
	h.compiles.clear()
	_check.run_check(work, select=set(), jobs=2, extra=[], baseline=None)
	assert h.compiles == ["b"]                           # only b recompiled
	rep = json.loads((work / "report.json").read_text())
	assert rep["observed"] == ["b"] and set(rep["projected"]) == {"a", "c"}


def test_select_forces_recompile(tmp_path, monkeypatch):
	names = ["a", "b", "c"]
	h = Harness(monkeypatch, tmp_path, _mk_fixtures(tmp_path, names), _projs(names))
	work = tmp_path / "work"
	_check.run_check(work, select=set(), jobs=2, extra=[], baseline=None)
	h.compiles.clear()
	_check.run_check(work, select={"a"}, jobs=2, extra=[], baseline=None)
	assert h.compiles == ["a"]                           # forced despite unchanged


def test_reused_failures_are_accounted(tmp_path, monkeypatch):
	names = ["a", "b"]
	h = Harness(monkeypatch, tmp_path, _mk_fixtures(tmp_path, names),
	            {"a": (True, {"cnt": 1}), "b": (False, {})})
	work = tmp_path / "work"
	_check.run_check(work, select=set(), jobs=2, extra=[], baseline=None)
	h.seed = "tc-CHANGED"
	h.compiles.clear()
	_check.run_check(work, select=set(), jobs=2, extra=[], baseline=None)
	assert h.compiles == []                              # failed record reused, not recompiled
	rep = json.loads((work / "report.json").read_text())
	assert rep["compile_failed"] == ["b"] and "b" in rep["projected"]   # failure accounted as projected


def test_handoff_exported_and_cache_independent(tmp_path, monkeypatch):
	names = ["a", "b"]
	h = Harness(monkeypatch, tmp_path, _mk_fixtures(tmp_path, names), _projs(names))
	work = tmp_path / "work"
	_check.run_check(work, select=set(), jobs=2, extra=[], baseline=None)
	handoff = json.loads(_check.HANDOFF_PATH.read_text())
	assert handoff["schema_version"] == _check.HANDOFF_SCHEMA_VERSION
	assert sorted(handoff["projections"]) == names
	assert handoff["universe"]["compiled_ok"] == names
	assert handoff["origin"]["work_dir"] == str(work)   # originating dir recorded diagnostically


def test_clean_clone_seeds_from_baseline_no_compile(tmp_path, monkeypatch):
	names = ["a", "b"]
	dirs = _mk_fixtures(tmp_path, names)
	h = Harness(monkeypatch, tmp_path, dirs, _projs(names))
	# baseline was promoted under an OLDER toolchain than the current tree
	base = _make_baseline(tmp_path, dirs, {"a": {"cnt": 1}, "b": {"cnt": 2}}, [], seed="tc-OLD")
	work = tmp_path / "work"        # empty cache
	assert _check.run_check(work, select=set(), jobs=2, extra=[], baseline=base) == 0
	assert h.compiles == []         # seeded from baseline, nothing compiled
	rep = json.loads((work / "report.json").read_text())
	assert rep["projected"] == names and rep["observed"] == []   # inherited from old-toolchain baseline


# ── promote ───────────────────────────────────────────────────────────

def _make_baseline(tmp_path, dirs, projections, failed, seed="tc0", name="baseline") -> Path:
	base = tmp_path / name
	(base / "audit").mkdir(parents=True)
	universe = _audit._universe_dict(dirs, [])
	counters = _audit._merge_counters(projections.values())
	_audit._emit_run(base, universe, sorted(projections), sorted(failed), counters, 0.0, 1)
	_check._atomic_json(base / "projections.json", {k: dict(sorted(v.items())) for k, v in projections.items()})
	_fp.write_atomic(base / "fingerprint.json", _check._snapshot(_tc_for(seed), universe))
	return base


def _write_handoff(path, dirs, projections, failed, seed="tc0"):
	universe = _audit._universe_dict(dirs, [])
	tc = _tc_for(seed)
	snap = _check._snapshot(tc, universe)
	handoff = {
		"schema_version": _check.HANDOFF_SCHEMA_VERSION,
		"origin": {"work_dir": "x", "toolchain_composite": tc["composite"],
		           "run_snapshot_composite": snap["composite"]},
		"universe": {"inclusion_rule": universe["inclusion_rule"], "fixtures": universe["fixtures"],
		             "excluded": universe["excluded"], "compiled_ok": sorted(projections), "failed": sorted(failed)},
		"projections": {k: dict(sorted(v.items())) for k, v in projections.items()},
		"counters": _audit._merge_counters(projections.values()),
		"observed": sorted(projections) + sorted(failed), "projected": [],
	}
	path.write_text(json.dumps(handoff))


def test_promote_rejects_handoff_from_different_toolchain(tmp_path, monkeypatch):
	"""Same universe, but a handoff produced under an OLDER compiler is stale —
	its origin toolchain composite no longer matches the current tree."""
	names = ["a", "b"]
	dirs = _mk_fixtures(tmp_path, names)
	h = Harness(monkeypatch, tmp_path, dirs, {"a": (True, {"cnt": 1}), "b": (True, {"cnt": 2})})
	base = _make_baseline(tmp_path, dirs, {"a": {"cnt": 1}, "b": {"cnt": 2}}, [])
	_write_handoff(_check.HANDOFF_PATH, dirs, {"a": {"cnt": 1}, "b": {"cnt": 2}}, [], seed="tc-OLD-COMPILER")
	assert _check.run_promote(base, jobs=2, extra=[]) == 2
	assert h.compiles == []                                       # rejected before compiling


def test_promote_missing_handoff_uses_baseline_noop(tmp_path, monkeypatch):
	names = ["a", "b"]
	dirs = _mk_fixtures(tmp_path, names)
	Harness(monkeypatch, tmp_path, dirs, {"a": (True, {"cnt": 1}), "b": (True, {"cnt": 2})})
	base = _make_baseline(tmp_path, dirs, {"a": {"cnt": 1}, "b": {"cnt": 2}}, [])
	before = (base / "aggregate.json").read_bytes()
	assert not _check.HANDOFF_PATH.exists()
	assert _check.run_promote(base, jobs=2, extra=[]) == 0        # fresh == baseline -> no-op
	assert (base / "aggregate.json").read_bytes() == before       # byte-preserving


def test_promote_handoff_expectation_installs(tmp_path, monkeypatch):
	names = ["a", "b"]
	dirs = _mk_fixtures(tmp_path, names)
	h = Harness(monkeypatch, tmp_path, dirs, {"a": (True, {"cnt": 5}), "b": (True, {"cnt": 2})})
	base = _make_baseline(tmp_path, dirs, {"a": {"cnt": 1}, "b": {"cnt": 2}}, [])   # OLD: a=1
	_write_handoff(_check.HANDOFF_PATH, dirs, {"a": {"cnt": 5}, "b": {"cnt": 2}}, [])  # reviewed: a=5
	assert _check.run_promote(base, jobs=2, extra=[]) == 0
	assert len(h.compiles) == 2                                   # full universe, once
	agg = json.loads((base / "aggregate.json").read_text())
	assert agg["counters"] == {"cnt": 7}
	assert (base / "projections.json").is_file()                 # per-fixture retained for seeding


def test_promote_unexpected_flip_fails_without_mutation(tmp_path, monkeypatch):
	names = ["a", "b"]
	dirs = _mk_fixtures(tmp_path, names)
	# handoff expects a=5,b=2 but the fresh compile also flips b to 9
	h = Harness(monkeypatch, tmp_path, dirs, {"a": (True, {"cnt": 5}), "b": (True, {"cnt": 9})})
	base = _make_baseline(tmp_path, dirs, {"a": {"cnt": 1}, "b": {"cnt": 2}}, [])
	before = (base / "aggregate.json").read_bytes()
	_write_handoff(_check.HANDOFF_PATH, dirs, {"a": {"cnt": 5}, "b": {"cnt": 2}}, [])
	assert _check.run_promote(base, jobs=2, extra=[]) == 1
	assert (base / "aggregate.json").read_bytes() == before      # baseline untouched
	assert (_check.ACTUAL_DIR / "aggregate.json").is_file()      # fresh actual retained separately


def test_promote_stale_handoff_fails_not_baseline_fallback(tmp_path, monkeypatch):
	names = ["a", "b"]
	dirs = _mk_fixtures(tmp_path, names)
	h = Harness(monkeypatch, tmp_path, dirs, {"a": (True, {"cnt": 1}), "b": (True, {"cnt": 2})})
	base = _make_baseline(tmp_path, dirs, {"a": {"cnt": 1}, "b": {"cnt": 2}}, [])
	# handoff describes a DIFFERENT universe (an extra fixture) -> stale
	universe = _audit._universe_dict(dirs, [])
	stale = {
		"schema_version": _check.HANDOFF_SCHEMA_VERSION,
		"origin": {"work_dir": "x", "toolchain_composite": "0" * 64, "run_snapshot_composite": "0" * 64},
		"universe": {"inclusion_rule": universe["inclusion_rule"],
		             "fixtures": universe["fixtures"] + [{"name": "zzz", "sha256": _hx("zzz")}],
		             "excluded": universe["excluded"],
		             "compiled_ok": ["a", "b", "zzz"], "failed": []},
		"projections": {"a": {"cnt": 1}, "b": {"cnt": 2}, "zzz": {"cnt": 1}},
		"counters": {"cnt": 4}, "observed": ["a", "b", "zzz"], "projected": [],
	}
	_check.HANDOFF_PATH.write_text(json.dumps(stale))
	assert _check.run_promote(base, jobs=2, extra=[]) == 2       # stale handoff = error
	assert h.compiles == []                                      # failed before compiling


def test_promote_malformed_handoff_fails(tmp_path, monkeypatch):
	names = ["a", "b"]
	dirs = _mk_fixtures(tmp_path, names)
	h = Harness(monkeypatch, tmp_path, dirs, _projs(names))
	base = _make_baseline(tmp_path, dirs, {"a": {"cnt": 1}, "b": {"cnt": 2}}, [])
	_check.HANDOFF_PATH.write_text("{ not json")
	assert _check.run_promote(base, jobs=2, extra=[]) == 2
	assert h.compiles == []


def test_promote_start_finish_mismatch_aborts(tmp_path, monkeypatch):
	names = ["a", "b"]
	dirs = _mk_fixtures(tmp_path, names)
	h = Harness(monkeypatch, tmp_path, dirs, {"a": (True, {"cnt": 1}), "b": (True, {"cnt": 2})})
	h.finish_seed = "tc-SHIFTED"
	base = _make_baseline(tmp_path, dirs, {"a": {"cnt": 1}, "b": {"cnt": 2}}, [])
	assert _check.run_promote(base, jobs=2, extra=[]) == 2


def test_promote_hard_gate_fails(tmp_path, monkeypatch):
	gate = _audit.HARD_GATES[0]
	names = ["a"]
	dirs = _mk_fixtures(tmp_path, names)
	Harness(monkeypatch, tmp_path, dirs, {"a": (True, {gate: 1})})
	base = _make_baseline(tmp_path, dirs, {"a": {gate: 1}}, [])
	_write_handoff(_check.HANDOFF_PATH, dirs, {"a": {gate: 1}}, [])
	assert _check.run_promote(base, jobs=2, extra=[]) == 1       # nonzero hard gate


# ── consistency + CLI discipline ─────────────────────────────────────

def test_expectation_consistency_rejects_bad_merge(tmp_path, monkeypatch):
	dirs = _mk_fixtures(tmp_path, ["a", "b"])
	Harness(monkeypatch, tmp_path, dirs, _projs(["a", "b"]))
	# projections merge to cnt=3 but universe/counters claim cnt=99
	universe = _audit._universe_dict(dirs, [])
	handoff = {
		"schema_version": _check.HANDOFF_SCHEMA_VERSION,
		"origin": {"work_dir": "x", "toolchain_composite": "0" * 64, "run_snapshot_composite": "0" * 64},
		"universe": {"inclusion_rule": universe["inclusion_rule"], "fixtures": universe["fixtures"],
		             "excluded": universe["excluded"], "compiled_ok": ["a", "b"], "failed": []},
		"projections": {"a": {"cnt": 1}, "b": {"cnt": 2}},
		"counters": {"cnt": 99},          # inconsistent
		"observed": ["a", "b"], "projected": [],
	}
	_check.HANDOFF_PATH.write_text(json.dumps(handoff))
	base = _make_baseline(tmp_path, dirs, {"a": {"cnt": 1}, "b": {"cnt": 2}}, [])
	assert _check.run_promote(base, jobs=2, extra=[]) == 2


def test_reject_nonpositive_jobs():
	assert _check.main(["--promote", "-j", "0"]) == 2
	assert _check.main(["somedir", "-j", "-1"]) == 2


def test_promote_rejects_work_dir_arg():
	assert _check.main(["somedir", "--promote"]) == 2


def test_infra_error_is_exit_2_no_traceback(tmp_path, monkeypatch):
	dirs = _mk_fixtures(tmp_path, ["a"])
	h = Harness(monkeypatch, tmp_path, dirs, _projs(["a"]))
	def _boom(extra):
		raise _check.InfraError("clang vanished")
	monkeypatch.setattr(_check, "_toolchain", _boom)
	assert _check.run_check(tmp_path / "work", select=set(), jobs=2, extra=[], baseline=None) == 2


def test_staged_install_leaves_no_staging_dir(tmp_path, monkeypatch):
	names = ["a"]
	dirs = _mk_fixtures(tmp_path, names)
	Harness(monkeypatch, tmp_path, dirs, {"a": (True, {"cnt": 5})})
	base = _make_baseline(tmp_path, dirs, {"a": {"cnt": 1}}, [])
	_write_handoff(_check.HANDOFF_PATH, dirs, {"a": {"cnt": 5}}, [])
	assert _check.run_promote(base, jobs=2, extra=[]) == 0
	assert not (base.parent / f".{base.name}.staging").exists()
	# post-install bundle reloads + validates exactly (incl per-fixture projections)
	installed = _check._read_baseline(base)
	assert installed["counters"] == {"cnt": 5} and installed["projections"] == {"a": {"cnt": 5}}


# ── exact handoff-schema validator ───────────────────────────────────

def _valid_handoff() -> dict:
	return {
		"schema_version": _check.HANDOFF_SCHEMA_VERSION,
		"origin": {"work_dir": "w", "toolchain_composite": "a" * 64, "run_snapshot_composite": "b" * 64},
		"universe": {"inclusion_rule": "r",
		             "fixtures": [{"name": "a", "sha256": _hx("a")}, {"name": "b", "sha256": _hx("b")}],
		             "excluded": [], "compiled_ok": ["a"], "failed": ["b"]},
		"projections": {"a": {"cnt": 3}},
		"counters": {"cnt": 3},
		"observed": ["a", "b"], "projected": [],
	}


def test_valid_handoff_accepted():
	assert _check._validate_handoff(_valid_handoff()) is None


@pytest.mark.parametrize("mutate,needle", [
	(lambda h: h.update(extra=1), "key set"),
	(lambda h: h["origin"].update(toolchain_composite="short"), "hex"),
	(lambda h: h["origin"].pop("work_dir"), "key set"),
	(lambda h: h["universe"].update(extra=1), "universe has the wrong key set"),
	(lambda h: h["universe"].update(compiled_ok=["a", "a"]), "duplicate"),
	(lambda h: h["universe"].update(fixtures=[{"name": "a"}]), "sha256"),
	(lambda h: h["universe"].update(failed=[]), "exhaustive"),
	(lambda h: h.update(observed=["a", "b"], projected=["a"]), "overlap"),
	(lambda h: h.update(observed=["a"], projected=[]), "exhaustive"),
	(lambda h: h.update(observed=["a", "a", "b"]), "duplicate"),
	(lambda h: h.update(counters={"cnt": 99}), "merge"),
	(lambda h: h["projections"].update(b={"cnt": 1}), "compiled_ok bucket"),
	(lambda h: h.update(observed="a,b"), "list of strings"),
])
def test_invalid_handoffs_rejected(mutate, needle):
	h = _valid_handoff()
	mutate(h)
	problem = _check._validate_handoff(h)
	assert problem is not None and needle in problem


def test_handoff_malformed_nested_does_not_raise():
	h = _valid_handoff()
	h["universe"]["compiled_ok"] = [123]        # non-string bucket entry
	assert isinstance(_check._validate_handoff(h), str)   # returns error, never raises


# ── fail-closed new-format baseline ──────────────────────────────────

def test_read_baseline_corrupt_projections_fails_closed(tmp_path):
	dirs = _mk_fixtures(tmp_path, ["a"])
	base = _make_baseline(tmp_path, dirs, {"a": {"cnt": 1}}, [])
	(base / "projections.json").write_text("{ not json")
	assert _check._read_baseline(base) is None


def test_read_baseline_invalid_projections_fails_closed(tmp_path):
	dirs = _mk_fixtures(tmp_path, ["a"])
	base = _make_baseline(tmp_path, dirs, {"a": {"cnt": 1}}, [])
	(base / "projections.json").write_text(json.dumps({"a": {"cnt": True}}))  # bool value
	assert _check._read_baseline(base) is None


def test_read_baseline_corrupt_fingerprint_fails_closed(tmp_path):
	dirs = _mk_fixtures(tmp_path, ["a"])
	base = _make_baseline(tmp_path, dirs, {"a": {"cnt": 1}}, [])
	(base / "fingerprint.json").write_text("{ not json")
	assert _check._read_baseline(base) is None


def test_absent_new_format_files_are_legacy(tmp_path):
	"""Only an ABSENT file is legacy — a legacy baseline still loads."""
	dirs = _mk_fixtures(tmp_path, ["a"])
	base = tmp_path / "legacy"
	(base / "audit").mkdir(parents=True)
	_audit._emit_run(base, _audit._universe_dict(dirs, []), ["a"], [], {"cnt": 1}, 0.0, 1)
	b = _check._read_baseline(base)
	assert b is not None and b["projections"] is None and b["toolchain"] is None


def test_promote_upgrades_legacy_baseline(tmp_path, monkeypatch):
	"""A legacy baseline (no projections) is never 'equal' — promotion installs to
	give it per-fixture projections for fast clean-clone seeding."""
	dirs = _mk_fixtures(tmp_path, ["a"])
	Harness(monkeypatch, tmp_path, dirs, {"a": (True, {"cnt": 1})})
	base = tmp_path / "legacy"
	(base / "audit").mkdir(parents=True)
	_audit._emit_run(base, _audit._universe_dict(dirs, []), ["a"], [], {"cnt": 1}, 0.0, 1)
	assert not (base / "projections.json").exists()
	assert _check.run_promote(base, jobs=2, extra=[]) == 0     # no handoff -> baseline expectation
	assert (base / "projections.json").is_file()               # upgraded
	assert (base / "fingerprint.json").is_file()


# ── select typos + infra guard ───────────────────────────────────────

def test_select_typo_rejected_before_compiling(tmp_path, monkeypatch):
	dirs = _mk_fixtures(tmp_path, ["a", "b"])
	h = Harness(monkeypatch, tmp_path, dirs, _projs(["a", "b"]))
	assert _check.run_check(tmp_path / "work", select={"nope"}, jobs=2, extra=[], baseline=None) == 2
	assert h.compiles == []


def test_main_infra_oserror_is_exit_2(monkeypatch):
	def _boom(*a, **k):
		raise OSError("disk gone")
	monkeypatch.setattr(_check, "run_promote", _boom)
	assert _check.main(["--promote"]) == 2


def test_bad_driftc_args_is_exit_2_not_traceback():
	# an unmatched quote must be a controlled exit 2, not a shlex traceback
	assert _check.main(["--promote", "--driftc-args", "'unterminated"]) == 2
