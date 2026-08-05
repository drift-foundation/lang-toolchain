# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Verify is the single fresh authority: candidate publication + bootstrap + lock.

Contract (finding-corpus-verify-candidate-unification +
finding-corpus-fast-fail-promotion): the discovery-to-install workflow
performs exactly ONE full-universe compile — `verify`'s, which publishes
the digest-sealed schema-2 promotion candidate as a side effect of EVERY
complete, stable, zero-hard-gate fresh observation (exact matches
included); `promote` is fast-or-fail validation/install with ZERO compiles
and ZERO builds (passive identity probe).  A previous candidate is
invalidated at verify start, so no failed/aborted run leaves a stale
candidate attributed to it;
hard-gate and exit-2 runs publish nothing.  An ABSENT reviewed baseline is
the bootstrap case (maximal valid drift → initial candidate); a PRESENT but
malformed baseline still fails closed.  One coarse advisory lock serializes
check/verify/promote.
"""
from __future__ import annotations

import fcntl
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _load_sibling(name: str):
	spec = importlib.util.spec_from_file_location(
		f"corpus_candidate_{name}", Path(__file__).parent / f"{name}.py")
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	return mod


_t = _load_sibling("test_drift_corpus_check")
_check = _t._check
_audit = _t._audit
Harness = _t.Harness
_mk_fixtures = _t._mk_fixtures
_make_baseline = _t._make_baseline
_write_handoff = _t._write_handoff
_projs = _t._projs
_fp = _t._fp
_baseline_files = _t._baseline_files


def _candidate():
	assert _check.HANDOFF_PATH.exists(), "expected a published candidate"
	h = json.loads(_check.HANDOFF_PATH.read_text())
	assert _check._validate_handoff(h) is None, _check._validate_handoff(h)
	return h


def test_exact_match_publishes_candidate_of_this_run(tmp_path, monkeypatch):
	# Exit 0 AND a valid candidate attributed to THIS run: verification
	# with candidate-publication side effects (promotion unnecessary).
	dirs = _mk_fixtures(tmp_path, ["a", "b"])
	Harness(monkeypatch, tmp_path, dirs, {"a": (True, {"cnt": 1}), "b": (True, {"cnt": 2})})
	base = _make_baseline(tmp_path, dirs, {"a": {"cnt": 1}, "b": {"cnt": 2}}, [])
	before = _baseline_files(base)
	assert _check.run_verify(base, jobs=2, extra=[]) == 0
	assert _baseline_files(base) == before
	h = _candidate()
	assert sorted(h["projections"]) == ["a", "b"]
	assert h["observed"] == ["a", "b"] and h["projected"] == []
	# Attribution: the origin composites are this run's snapshot, so
	# promote's staleness checks accept it against the SAME tree state.
	universe = _audit._universe_dict(dirs, [])
	snap = _check._snapshot(_t._tc_for("tc0"), universe)
	assert h["origin"]["run_snapshot_composite"] == snap["composite"]


def test_valid_drift_publishes_candidate_and_retains_actual(tmp_path, monkeypatch):
	dirs = _mk_fixtures(tmp_path, ["a"])
	Harness(monkeypatch, tmp_path, dirs, {"a": (True, {"cnt": 5})})
	base = _make_baseline(tmp_path, dirs, {"a": {"cnt": 1}}, [])
	before = _baseline_files(base)
	assert _check.run_verify(base, jobs=2, extra=[]) == 1
	assert _baseline_files(base) == before
	assert (_check.ACTUAL_DIR / "aggregate.json").is_file()
	h = _candidate()
	assert h["projections"] == {"a": {"cnt": 5}}
	# Candidate equals the retained actual's projections (one observation,
	# two artifacts).
	actual = json.loads((_check.ACTUAL_DIR / "projections.json").read_text())
	assert h["projections"] == actual


def test_drift_then_promote_one_full_run_total(tmp_path, monkeypatch):
	# The ONE-full-run workflow: verify compiles the universe once; fast
	# promote validates and installs the candidate with ZERO fixture
	# compiles.  No intervening check, no reproduction compile.
	dirs = _mk_fixtures(tmp_path, ["a", "b"])
	h = Harness(monkeypatch, tmp_path, dirs, {"a": (True, {"cnt": 7}), "b": (True, {"cnt": 2})})
	base = _make_baseline(tmp_path, dirs, {"a": {"cnt": 1}, "b": {"cnt": 2}}, [])
	assert _check.run_verify(base, jobs=2, extra=[]) == 1
	assert sorted(h.compiles) == ["a", "b"]              # THE full run
	h.compiles.clear()
	assert _check.run_promote(base, extra=[]) == 0
	assert h.compiles == []                              # fast promote: zero
	installed = json.loads((base / "projections.json").read_text())
	assert installed == {"a": {"cnt": 7}, "b": {"cnt": 2}}


def test_hard_gate_drift_retains_actual_no_candidate(tmp_path, monkeypatch):
	dirs = _mk_fixtures(tmp_path, ["a"])
	Harness(monkeypatch, tmp_path, dirs, {"a": (True, {"cnt": 1, "unclassified": 3})})
	base = _make_baseline(tmp_path, dirs, {"a": {"cnt": 1}}, [])
	assert _check.run_verify(base, jobs=2, extra=[]) == 1
	assert (_check.ACTUAL_DIR / "aggregate.json").is_file()
	assert not _check.HANDOFF_PATH.exists()              # never promotable


@pytest.mark.parametrize("stale", ["valid-different", "malformed"])
def test_begin_invalidate_kills_stale_candidate_on_abort(tmp_path, monkeypatch, stale):
	# An exit-2 run (unstable start/finish snapshot) must not leave a
	# pre-existing candidate that appears to describe it.
	dirs = _mk_fixtures(tmp_path, ["a"])
	h = Harness(monkeypatch, tmp_path, dirs, {"a": (True, {"cnt": 1})})
	h.finish_seed = "tc-SHIFTED"
	base = _make_baseline(tmp_path, dirs, {"a": {"cnt": 1}}, [])
	if stale == "valid-different":
		_write_handoff(_check.HANDOFF_PATH, dirs, {"a": {"cnt": 99}}, [])
	else:
		_check.HANDOFF_PATH.write_text("{ not json")
	assert _check.run_verify(base, jobs=2, extra=[]) == 2
	assert not _check.HANDOFF_PATH.exists()


def test_match_replaces_stale_candidate_with_this_runs(tmp_path, monkeypatch):
	# A stale pre-existing candidate is never left masquerading: after an
	# exact-match verify the candidate on disk is THIS run's observation.
	dirs = _mk_fixtures(tmp_path, ["a", "b"])
	Harness(monkeypatch, tmp_path, dirs, {"a": (True, {"cnt": 1}), "b": (True, {"cnt": 2})})
	base = _make_baseline(tmp_path, dirs, {"a": {"cnt": 1}, "b": {"cnt": 2}}, [])
	_write_handoff(_check.HANDOFF_PATH, dirs, {"a": {"cnt": 99}, "b": {"cnt": 2}}, [])
	assert _check.run_verify(base, jobs=2, extra=[]) == 0
	h = _candidate()
	assert h["projections"] == {"a": {"cnt": 1}, "b": {"cnt": 2}}    # not the stale 99


def test_absent_baseline_bootstrap_emits_initial_candidate(tmp_path, monkeypatch):
	# Bootstrap: absent baseline = maximal valid drift -> exit 1 + valid
	# initial candidate; the fast promote then installs it with zero
	# compiles.
	dirs = _mk_fixtures(tmp_path, ["a"])
	h = Harness(monkeypatch, tmp_path, dirs, {"a": (True, {"cnt": 4})})
	base = tmp_path / "nonexistent-baseline"
	assert _check.run_verify(base, jobs=2, extra=[]) == 1
	cand = _candidate()
	assert cand["projections"] == {"a": {"cnt": 4}}
	h.compiles.clear()
	assert _check.run_promote(base, extra=[]) == 0
	assert h.compiles == []                              # fast promote: zero
	assert json.loads((base / "projections.json").read_text()) == {"a": {"cnt": 4}}


def test_malformed_baseline_still_fails_closed_no_candidate(tmp_path, monkeypatch):
	# Present-but-corrupt is DAMAGE, not bootstrap: exit 2, no candidate.
	dirs = _mk_fixtures(tmp_path, ["a"])
	Harness(monkeypatch, tmp_path, dirs, {"a": (True, {"cnt": 1})})
	base = _make_baseline(tmp_path, dirs, {"a": {"cnt": 1}}, [])
	(base / "aggregate.json").write_text("{ not json")
	assert _check.run_verify(base, jobs=2, extra=[]) == 2
	assert not _check.HANDOFF_PATH.exists()


def test_corpus_lock_is_exclusive(tmp_path, monkeypatch):
	monkeypatch.setattr(_check, "LOCK_PATH", tmp_path / "corpus.lock")
	with _check._corpus_lock():
		probe = open(tmp_path / "corpus.lock", "w")
		with pytest.raises(OSError):
			fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
		probe.close()
	probe2 = open(tmp_path / "corpus.lock", "w")
	fcntl.flock(probe2, fcntl.LOCK_EX | fcntl.LOCK_NB)      # released after exit
	fcntl.flock(probe2, fcntl.LOCK_UN)
	probe2.close()


def test_retired_fresh_flag_is_rejected():
	# The retired lane is GONE, not silently accepted: argparse rejects
	# the unknown flag (SystemExit 2) on every lane.
	for argv in (["--fresh"], ["--verify", "--fresh"], ["--promote", "--fresh"]):
		with pytest.raises(SystemExit) as e:
			_check.main(argv)
		assert e.value.code == 2


def test_early_failure_still_invalidates_stale_candidate(tmp_path, monkeypatch):
	# Begin-invalidate is the FIRST run_verify action: even a toolchain
	# probe failure (before discovery snapshotting completes) removes a
	# pre-existing candidate, so no exit-2 run leaves one attributed to it.
	dirs = _mk_fixtures(tmp_path, ["a"])
	Harness(monkeypatch, tmp_path, dirs, {"a": (True, {"cnt": 1})})
	def _boom(extra):
		raise _check.InfraError("toolchain probe failed (synthetic)")
	monkeypatch.setattr(_check, "_toolchain", _boom)
	base = _make_baseline(tmp_path, dirs, {"a": {"cnt": 1}}, [])
	_write_handoff(_check.HANDOFF_PATH, dirs, {"a": {"cnt": 99}}, [])
	assert _check.run_verify(base, jobs=2, extra=[]) == 2
	assert not _check.HANDOFF_PATH.exists()


@pytest.mark.parametrize("artifact", ["projections.json", "fingerprint.json",
                                      "metadata.json", "BASELINE.md"])
def test_partial_baseline_is_damage_not_bootstrap(tmp_path, monkeypatch, artifact):
	# A directory holding ANY recognized baseline artifact — core or
	# auxiliary — is partial/corrupt baseline STATE: fail closed (exit 2,
	# no candidate), never a bootstrap discovery run.
	dirs = _mk_fixtures(tmp_path, ["a"])
	Harness(monkeypatch, tmp_path, dirs, {"a": (True, {"cnt": 1})})
	base = tmp_path / "partial-baseline"
	base.mkdir()
	(base / artifact).write_text("{}")
	assert _check.run_verify(base, jobs=2, extra=[]) == 2
	assert not _check.HANDOFF_PATH.exists()


def test_baseline_path_that_is_a_file_fails_closed(tmp_path, monkeypatch):
	# An EXISTING non-directory baseline path can never hold artifacts, so
	# the no-children probe must not misread it as bootstrap: exit 2, no
	# candidate.
	dirs = _mk_fixtures(tmp_path, ["a"])
	Harness(monkeypatch, tmp_path, dirs, {"a": (True, {"cnt": 1})})
	base = tmp_path / "baseline-as-file"
	base.write_text("not a directory")
	assert _check.run_verify(base, jobs=2, extra=[]) == 2
	assert not _check.HANDOFF_PATH.exists()


def test_dangling_baseline_symlink_fails_closed(tmp_path, monkeypatch):
	# Path.exists follows symlinks: a DANGLING baseline symlink must read
	# as damage (lexically something is there), never bootstrap.
	dirs = _mk_fixtures(tmp_path, ["a"])
	Harness(monkeypatch, tmp_path, dirs, {"a": (True, {"cnt": 1})})
	base = tmp_path / "baseline-dangling"
	base.symlink_to(tmp_path / "no-such-target")
	assert _check.run_verify(base, jobs=2, extra=[]) == 2
	assert not _check.HANDOFF_PATH.exists()


def test_dangling_artifact_symlink_fails_closed(tmp_path, monkeypatch):
	# Same at the artifact level: a baseline dir whose only content is a
	# dangling recognized-artifact symlink is partial state, not absence.
	dirs = _mk_fixtures(tmp_path, ["a"])
	Harness(monkeypatch, tmp_path, dirs, {"a": (True, {"cnt": 1})})
	base = tmp_path / "baseline-partial-link"
	base.mkdir()
	(base / "manifest.json").symlink_to(tmp_path / "no-such-target")
	assert _check.run_verify(base, jobs=2, extra=[]) == 2
	assert not _check.HANDOFF_PATH.exists()


def test_promote_preserves_verify_run_metadata(tmp_path, monkeypatch):
	# Metadata provenance: a DELAYED promote installs verify's measured
	# duration/start/jobs verbatim — the human review gap never leaks into
	# the baseline's metadata.
	dirs = _mk_fixtures(tmp_path, ["a"])
	Harness(monkeypatch, tmp_path, dirs, {"a": (True, {"cnt": 9})})
	base = _make_baseline(tmp_path, dirs, {"a": {"cnt": 1}}, [])
	assert _check.run_verify(base, jobs=2, extra=[]) == 1
	cand_meta = json.loads(_check.HANDOFF_PATH.read_text())["run_meta"]
	assert _check.run_promote(base, extra=[]) == 0
	installed_meta = json.loads((base / "metadata.json").read_text())
	assert installed_meta == cand_meta


def test_tree_change_after_verify_fails_fast_no_compile(tmp_path, monkeypatch):
	# Staleness: any toolchain/universe change after verify makes promote
	# fail BEFORE any fixture compile, baseline byte-identical, with the
	# diagnostic instructing a new verify.
	dirs = _mk_fixtures(tmp_path, ["a"])
	h = Harness(monkeypatch, tmp_path, dirs, {"a": (True, {"cnt": 5})})
	base = _make_baseline(tmp_path, dirs, {"a": {"cnt": 1}}, [])
	assert _check.run_verify(base, jobs=2, extra=[]) == 1
	h.seed = "tc-MOVED"                                 # compiler changed after verify
	h.compiles.clear()
	before = _baseline_files(base)
	assert _check.run_promote(base, extra=[]) == 2
	assert h.compiles == []
	assert _baseline_files(base) == before


def test_promote_rejects_explicit_jobs():
	# --promote never compiles: an explicitly supplied worker count is a
	# clear rejection, not a silently ignored option.
	assert _check.main(["--promote", "-j", "4"]) == 2
	assert _check.main(["--promote", "--jobs", "4"]) == 2


def test_passive_fingerprint_never_builds_and_matches_building(monkeypatch):
	# Cold-cache deterministic: FIRST take the ordinary BUILDING
	# fingerprint (this prebuilds the archive through the production
	# authority, so the test never depends on a warmed cache), THEN
	# install exploding monkeypatches on both build entry points and take
	# the PASSIVE fingerprint.  It must never build — and it must yield
	# the EXACT same composite as the building fingerprint on the
	# unchanged tree (the identity fast promotion compares).
	import lang.language_runtime as _lr
	building = _fp.toolchain_fingerprint(ROOT, extra_args=())
	def _boom(*a, **k):
		raise AssertionError("passive path reached the runtime BUILD authority")
	monkeypatch.setattr(_fp, "prebuild_runtime", _boom)
	monkeypatch.setattr(_lr, "build_runtime_archive", _boom)
	passive = _fp.toolchain_fingerprint_passive(ROOT, extra_args=())
	assert passive["kind"] == "toolchain"
	assert passive["components"] == building["components"]
	assert passive["composite"] == building["composite"]


def test_passive_identity_ignores_archive_mtime(tmp_path, monkeypatch):
	# Staleness is CONTENT identity, not cache mtime: a content-identical
	# COPY of the archive with a far-past mtime, served from a PRIVATE
	# cache tree (never the shared runtime cache), yields the exact same
	# passive identity — deployed/read-only archives promote fine.
	import os as _os
	import shutil as _sh
	import lang.language_runtime as _lr
	building = _fp.toolchain_fingerprint(ROOT, extra_args=())
	real_root = _lr.runtime_archive_cache_root(ROOT)
	real_archive = _lr.runtime_archive_path(ROOT, variant="default")
	rel = real_archive.relative_to(real_root)
	priv_root = tmp_path / "runtime-cache"
	priv_archive = priv_root / rel
	priv_archive.parent.mkdir(parents=True)
	_sh.copy2(real_archive, priv_archive)
	st = priv_archive.stat()
	_os.utime(priv_archive, (st.st_atime, st.st_mtime - 10_000))   # far past
	monkeypatch.setattr(_lr, "runtime_archive_cache_root", lambda root: priv_root)
	monkeypatch.setattr(_lr, "runtime_archive_path",
	                    lambda root, *, variant: priv_root / rel)
	passive = _fp.toolchain_fingerprint_passive(ROOT, extra_args=())
	assert passive["components"] == building["components"]
	assert passive["composite"] == building["composite"]


def test_passive_identity_unhashable_archive_fails_closed(tmp_path, monkeypatch):
	# Existing-but-unhashable (racing/unreadable) artifact: fail closed —
	# a None identity must never join the composite, and no build occurs.
	import lang.language_runtime as _lr
	def _boom(*a, **k):
		raise AssertionError("passive path reached the runtime BUILD authority")
	monkeypatch.setattr(_lr, "build_runtime_archive", _boom)
	monkeypatch.setattr(_fp, "_file_sha256", lambda p: None)
	with pytest.raises(RuntimeError, match="could not be hashed"):
		_fp.resolve_runtime_identity(ROOT, extra_args=())


def test_promote_never_calls_building_toolchain(tmp_path, monkeypatch):
	# Wiring pin: promote's identity comes from the PASSIVE probe only —
	# the building `_toolchain` explodes if promote touches it, and the
	# promotion still succeeds from a valid current candidate.
	dirs = _mk_fixtures(tmp_path, ["a"])
	h = Harness(monkeypatch, tmp_path, dirs, {"a": (True, {"cnt": 5})})
	base = _make_baseline(tmp_path, dirs, {"a": {"cnt": 1}}, [])
	assert _check.run_verify(base, jobs=2, extra=[]) == 1
	def _boom(extra):
		raise AssertionError("promote reached the BUILDING toolchain probe")
	monkeypatch.setattr(_check, "_toolchain", _boom)
	h.compiles.clear()
	assert _check.run_promote(base, extra=[]) == 0
	assert h.compiles == []


def test_promote_missing_runtime_artifact_fails_closed(tmp_path, monkeypatch):
	# MISSING runtime artifact: promote must FAIL (baseline
	# byte-identical, zero compiles, no build) and request a verify —
	# exercised through the REAL passive probe with the artifact path
	# pointed at nothing.  (Stale CONTENT is a composite mismatch, pinned
	# separately via the identity tests.)
	import lang.language_runtime as _lr
	_real_passive = _check._toolchain_passive        # BEFORE the harness mocks it
	dirs = _mk_fixtures(tmp_path, ["a"])
	h = Harness(monkeypatch, tmp_path, dirs, {"a": (True, {"cnt": 5})})
	base = _make_baseline(tmp_path, dirs, {"a": {"cnt": 1}}, [])
	assert _check.run_verify(base, jobs=2, extra=[]) == 1
	# Real passive probe (undo the harness mock; keeps the InfraError
	# wrapper -> controlled exit 2) + missing artifact.
	monkeypatch.setattr(_check, "_toolchain_passive", _real_passive)
	monkeypatch.setattr(_lr, "runtime_archive_path",
	                    lambda root, *, variant: tmp_path / "no-such-archive.a")
	def _boom(*a, **k):
		raise AssertionError("promote reached the runtime BUILD authority")
	monkeypatch.setattr(_lr, "build_runtime_archive", _boom)
	h.compiles.clear()
	before = _baseline_files(base)
	assert _check.run_promote(base, extra=[]) == 2
	assert h.compiles == []
	assert _baseline_files(base) == before


def test_promote_rejects_invalid_metadata_candidate_at_boundary(tmp_path, monkeypatch):
	# P2 boundary proof: a RESEALED candidate with invalid metadata values
	# is rejected by run_promote itself — controlled exit 2, no passive
	# identity probe, baseline bytes unchanged — so malformed metadata can
	# never reach _emit_run/installation.
	dirs = _mk_fixtures(tmp_path, ["a"])
	Harness(monkeypatch, tmp_path, dirs, {"a": (True, {"cnt": 5})})
	base = _make_baseline(tmp_path, dirs, {"a": {"cnt": 1}}, [])
	assert _check.run_verify(base, jobs=2, extra=[]) == 1
	h = json.loads(_check.HANDOFF_PATH.read_text())
	h["run_meta"]["jobs"] = 0                             # invalid value
	payload = {k: v for k, v in h.items() if k != "payload_sha256"}
	import hashlib as _hl
	h = {**payload, "payload_sha256": _hl.sha256(
		_fp.canonical_json(payload).encode()).hexdigest()}   # deliberate reseal
	_check.HANDOFF_PATH.write_text(json.dumps(h))
	def _no_probe(extra):
		raise AssertionError("candidate validation must fail BEFORE the identity probe")
	monkeypatch.setattr(_check, "_toolchain_passive", _no_probe)
	before = _baseline_files(base)
	assert _check.run_promote(base, extra=[]) == 2
	assert _baseline_files(base) == before
