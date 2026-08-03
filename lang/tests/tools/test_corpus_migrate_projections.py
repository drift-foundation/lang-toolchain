# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Pins for the one-time projections.json migration
(tools/corpus_migrate_projections.py): it lands per-fixture projections from an
already-approved candidate ONLY when all four proofs hold, and aborts otherwise
without writing.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _load(name: str):
	spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	return mod


_mig = _load("corpus_migrate_projections")


def _setup(tmp_path, *, manifest, aggregate, fixtures):
	base = tmp_path / "baseline"
	base.mkdir()
	(base / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
	(base / "aggregate.json").write_text(json.dumps(aggregate, indent=2, sort_keys=True) + "\n")
	cand = tmp_path / "cand"
	cand.mkdir()
	cm = cand / "manifest.json"
	ca = cand / "aggregate.json"
	fc = cand / "fixture-counters.json"
	# candidate manifest/aggregate byte-identical to the live baseline by default
	cm.write_bytes((base / "manifest.json").read_bytes())
	ca.write_bytes((base / "aggregate.json").read_bytes())
	fc.write_text(json.dumps({"fixtures": fixtures}))
	return base, cm, ca, fc


_MANIFEST = {"universe": {"inclusion_rule": "r", "excluded": [],
                          "fixtures": [{"name": "a", "sha256": "h"}, {"name": "b", "sha256": "h"}],
                          "compiled_ok": ["a"], "failed": ["b"]}}
_AGG = {"counters": {"cnt": 3}}
_FIX = {"a": {"cnt": 3}}


def test_migration_lands_projections_when_all_proofs_hold(tmp_path):
	base, cm, ca, fc = _setup(tmp_path, manifest=_MANIFEST, aggregate=_AGG, fixtures=_FIX)
	assert _mig.migrate(fc, cm, ca, base) == 0
	proj = json.loads((base / "projections.json").read_text())
	assert proj == {"a": {"cnt": 3}}


def test_abort_on_candidate_manifest_mismatch(tmp_path):
	base, cm, ca, fc = _setup(tmp_path, manifest=_MANIFEST, aggregate=_AGG, fixtures=_FIX)
	cm.write_text(json.dumps({"universe": {"different": True}}))
	assert _mig.migrate(fc, cm, ca, base) == 2
	assert not (base / "projections.json").exists()


def test_abort_on_candidate_aggregate_mismatch(tmp_path):
	base, cm, ca, fc = _setup(tmp_path, manifest=_MANIFEST, aggregate=_AGG, fixtures=_FIX)
	ca.write_text(json.dumps({"counters": {"cnt": 999}}))
	assert _mig.migrate(fc, cm, ca, base) == 2
	assert not (base / "projections.json").exists()


def test_abort_when_keys_not_equal_compiled_ok(tmp_path):
	# projection has an extra fixture not in compiled_ok
	base, cm, ca, fc = _setup(tmp_path, manifest=_MANIFEST, aggregate=_AGG,
	                          fixtures={"a": {"cnt": 3}, "zzz": {"cnt": 0}})
	assert _mig.migrate(fc, cm, ca, base) == 2
	assert not (base / "projections.json").exists()


def test_abort_when_failed_fixture_has_projection(tmp_path):
	# OVERLAPPING partition: 'b' is in BOTH compiled_ok and failed, so projection
	# keys == compiled_ok (proof 2 passes) yet a failed fixture carries a
	# projection (proof 3 must fire).
	manifest = {"universe": {"inclusion_rule": "r", "excluded": [],
	                         "fixtures": [{"name": "a", "sha256": "h"}, {"name": "b", "sha256": "h"}],
	                         "compiled_ok": ["a", "b"], "failed": ["b"]}}
	base, cm, ca, fc = _setup(tmp_path, manifest=manifest, aggregate=_AGG,
	                          fixtures={"a": {"cnt": 3}, "b": {"cnt": 0}})
	# keys {a,b} == compiled_ok {a,b}; merge {cnt:3} == aggregate; only the
	# failed-intersection proof can reject this.
	rc = _mig.migrate(fc, cm, ca, base)
	assert rc == 2
	assert not (base / "projections.json").exists()


def test_abort_when_merge_not_aggregate(tmp_path):
	base, cm, ca, fc = _setup(tmp_path, manifest=_MANIFEST, aggregate={"counters": {"cnt": 999}},
	                          fixtures=_FIX)
	# candidate aggregate must match baseline aggregate first; make them both 999
	ca.write_bytes((base / "aggregate.json").read_bytes())
	# now merge of {a:{cnt:3}} = 3 != 999
	assert _mig.migrate(fc, cm, ca, base) == 2
	assert not (base / "projections.json").exists()
