# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Focused pins for the STATIC ownership-corpus preflight
(`just ownership-corpus-preflight` -> tools/drift_corpus_audit.py --preflight).

The preflight compares the current corpus universe to the reviewed baseline
WITHOUT compiling.  It must surface every category of KNOWN universe drift —
inclusion-rule change, included add/remove/content-change, exclusion
add/remove/reason-change, and included<->excluded transitions — and must reject
a baseline whose own partition (included / excluded / compiled_ok / failed) is
malformed.  It must NOT claim to detect compiler-result flips.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
	"drift_corpus_audit", ROOT / "tools" / "drift_corpus_audit.py")
_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tool)


def _uni(fixtures, excluded=(), rule=None, compiled_ok=None, failed=None):
	u = {
		"inclusion_rule": _tool.INCLUSION_RULE if rule is None else rule,
		"fixtures": [{"name": n, "sha256": h} for n, h in fixtures],
		"excluded": [{"name": n, "reason": r} for n, r in excluded],
	}
	if compiled_ok is not None:
		u["compiled_ok"] = list(compiled_ok)
	if failed is not None:
		u["failed"] = list(failed)
	return u


# ── _preflight_compare ───────────────────────────────────────────────

def test_detects_added_removed_content_changed():
	base = _uni([("a", "h1"), ("b", "h2"), ("c", "h3")])
	cur = _uni([("a", "h1"), ("b", "HHH"), ("d", "h4")])  # b changed, c removed, d added
	rep = _tool._preflight_compare(base, cur)
	assert rep["included_added"] == ["d"]
	assert rep["included_removed"] == ["c"]
	assert rep["content_changed"] == ["b"]
	assert _tool._preflight_has_drift(rep)


def test_detects_exclusion_add_remove_reason_change():
	base = _uni([("a", "h1")], excluded=[("x", "declares module_paths"), ("y", "declares c_sources")])
	cur = _uni([("a", "h1")], excluded=[("x", "declares module_paths+c_sources"), ("z", "unparseable expected.json")])
	rep = _tool._preflight_compare(base, cur)
	assert rep["excluded_added"] == ["z"]
	assert rep["excluded_removed"] == ["y"]
	assert any("x:" in s for s in rep["exclusion_reason_changed"])
	assert _tool._preflight_has_drift(rep)


def test_detects_included_excluded_transitions():
	# `a` was included, now excluded; `x` was excluded, now included.
	base = _uni([("a", "h1"), ("b", "h2")], excluded=[("x", "declares c_sources")])
	cur = _uni([("b", "h2"), ("x", "hX")], excluded=[("a", "declares module_paths")])
	rep = _tool._preflight_compare(base, cur)
	assert rep["included_to_excluded"] == ["a"]
	assert rep["excluded_to_included"] == ["x"]
	assert _tool._preflight_has_drift(rep)


def test_detects_inclusion_rule_change():
	base = _uni([("a", "h1")])
	cur = _uni([("a", "h1")], rule="a DIFFERENT inclusion rule")
	rep = _tool._preflight_compare(base, cur)
	assert "inclusion_rule_changed" in rep
	assert _tool._preflight_has_drift(rep)


def test_clean_when_identical():
	base = _uni([("a", "h1"), ("b", "h2")], excluded=[("x", "declares c_sources")])
	cur = _uni([("a", "h1"), ("b", "h2")], excluded=[("x", "declares c_sources")])
	rep = _tool._preflight_compare(base, cur)
	assert not _tool._preflight_has_drift(rep)


# ── _baseline_partition_errors ───────────────────────────────────────

def test_partition_ok_for_well_formed_baseline():
	base = _uni([("a", "h1"), ("b", "h2")], excluded=[("x", "r")],
	            compiled_ok=["a"], failed=["b"])
	assert _tool._baseline_partition_errors(base) == []


def test_partition_rejects_included_excluded_overlap():
	base = _uni([("a", "h1")], excluded=[("a", "r")], compiled_ok=["a"], failed=[])
	errs = _tool._baseline_partition_errors(base)
	assert any("BOTH included and excluded" in e for e in errs)


def test_partition_rejects_compiled_ok_failed_overlap():
	base = _uni([("a", "h1")], compiled_ok=["a"], failed=["a"])
	errs = _tool._baseline_partition_errors(base)
	assert any("BOTH compiled_ok and failed" in e for e in errs)


def test_partition_rejects_non_partition_of_included():
	# `b` is included but in neither compiled_ok nor failed; `z` is in
	# compiled_ok but not included.
	base = _uni([("a", "h1"), ("b", "h2")], compiled_ok=["a", "z"], failed=[])
	errs = _tool._baseline_partition_errors(base)
	assert any("absent from compiled_ok" in e for e in errs)
	assert any("absent from included set" in e for e in errs)


# ── _run_preflight exit codes ────────────────────────────────────────

def _write_baseline(tmp_path: Path, universe: dict) -> Path:
	d = tmp_path / "baseline"
	d.mkdir()
	(d / "manifest.json").write_text(json.dumps({"universe": universe}))
	return d


def test_run_preflight_exit_0_on_match(tmp_path, monkeypatch):
	uni = _uni([("a", "h1")], compiled_ok=["a"], failed=[])
	base_dir = _write_baseline(tmp_path, uni)
	monkeypatch.setattr(_tool, "_static_universe", lambda only=None: _uni([("a", "h1")]))
	assert _tool._run_preflight(base_dir) == 0


def test_run_preflight_exit_1_on_drift(tmp_path, monkeypatch):
	uni = _uni([("a", "h1")], compiled_ok=["a"], failed=[])
	base_dir = _write_baseline(tmp_path, uni)
	monkeypatch.setattr(_tool, "_static_universe", lambda only=None: _uni([("a", "h1"), ("b", "h2")]))
	assert _tool._run_preflight(base_dir) == 1


def test_run_preflight_exit_2_on_broken_partition(tmp_path, monkeypatch):
	uni = _uni([("a", "h1")], compiled_ok=["a"], failed=["a"])  # overlap
	base_dir = _write_baseline(tmp_path, uni)
	monkeypatch.setattr(_tool, "_static_universe", lambda only=None: _uni([("a", "h1")]))
	assert _tool._run_preflight(base_dir) == 2


def test_run_preflight_exit_2_on_malformed_baseline(tmp_path):
	d = tmp_path / "baseline"
	d.mkdir()
	(d / "manifest.json").write_text("{ not valid json")
	assert _tool._run_preflight(d) == 2


# ── the real reviewed baseline's partition is intact (sanity) ────────

def test_real_reviewed_baseline_partition_is_intact():
	base = json.loads((ROOT / "lang" / "tests" / "ownership_corpus"
	                   / "reviewed-baseline" / "manifest.json").read_text())["universe"]
	assert _tool._baseline_partition_errors(base) == []


# ── review findings: single authority, lazy scratch, --only, read failures ──

def test_static_universe_uses_single_universe_dict_authority():
	"""Finding 4a: _static_universe builds via the shared _universe_dict."""
	fixtures, excluded = _tool._discover_fixtures(None)
	assert _tool._static_universe(None) == _tool._universe_dict(fixtures, excluded)


def test_write_run_consumes_universe_dict_authority(tmp_path, monkeypatch):
	"""Finding 4b: the FULL-run manifest emission must consume the SAME
	_universe_dict authority — not re-inline the universe shape.  Monkeypatch the
	authority to a sentinel and prove _write_run's manifest is built from it."""
	sentinel = {"inclusion_rule": "SENTINEL_RULE",
	            "fixtures": [{"name": "z", "sha256": "hZ"}], "excluded": []}
	monkeypatch.setattr(_tool, "_universe_dict", lambda fixtures, excluded: dict(sentinel))
	run_dir = tmp_path / "run"
	(run_dir / "audit").mkdir(parents=True)
	_tool._write_run(run_dir, fixtures=[], excluded=[], compiled_ok=["a"],
	                 failed=["b"], counters={}, started=0.0, jobs=1)
	universe = json.loads((run_dir / "manifest.json").read_text())["universe"]
	assert universe["inclusion_rule"] == "SENTINEL_RULE"
	assert universe["fixtures"] == [{"name": "z", "sha256": "hZ"}]
	# _write_run adds only the compile partition on top of the authority.
	assert universe["compiled_ok"] == ["a"] and universe["failed"] == ["b"]


def test_scratch_dir_not_created_at_import(tmp_path):
	"""Finding 7: importing the tool (as the compile-free preflight does) must NOT
	create the scratch/session directory.  Fresh subprocess import against a
	nonexistent scratch root, asserting nothing is created."""
	import os
	import subprocess
	import sys as _sys
	scratch = tmp_path / "should_not_exist_scratch"
	env = dict(os.environ)
	env["DRIFT_TMP_ROOT"] = str(scratch)
	env["PYTHONPATH"] = str(ROOT)
	code = (
		"import importlib.util\n"
		f"spec = importlib.util.spec_from_file_location('dca', {str(ROOT / 'tools' / 'drift_corpus_audit.py')!r})\n"
		"m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
		"assert m._scratch_base_cached is None, 'scratch cache populated at import'\n"
	)
	r = subprocess.run([_sys.executable, "-c", code], env=env, cwd=str(ROOT),
	                   capture_output=True, text=True)
	assert r.returncode == 0, r.stderr
	assert not scratch.exists(), "importing the tool created the scratch directory"


def test_run_preflight_rejects_only_via_cli(tmp_path):
	"""Finding 6: --only is meaningless for a full-universe preflight — refuse it."""
	uni = _uni([("a", "h1")], compiled_ok=["a"], failed=[])
	base_dir = _write_baseline(tmp_path, uni)
	rc = _tool.main(["--preflight", "--baseline", str(base_dir), "--only", "a,b"])
	assert rc == 2


def test_run_preflight_current_tree_read_failure_is_caught(tmp_path, monkeypatch):
	"""Finding 5: a current-tree read failure must exit 2 cleanly, not traceback."""
	uni = _uni([("a", "h1")], compiled_ok=["a"], failed=[])
	base_dir = _write_baseline(tmp_path, uni)

	def _boom(only=None):
		raise OSError("disk gone")
	monkeypatch.setattr(_tool, "_static_universe", _boom)
	assert _tool._run_preflight(base_dir) == 2


def test_run_preflight_current_tree_value_error_is_caught(tmp_path, monkeypatch):
	"""Finding 1: a non-OSError read/parse failure (e.g. UnicodeDecodeError, a
	ValueError subclass, from a non-UTF-8 expected.json) must also exit 2."""
	uni = _uni([("a", "h1")], compiled_ok=["a"], failed=[])
	base_dir = _write_baseline(tmp_path, uni)

	def _boom(only=None):
		raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
	monkeypatch.setattr(_tool, "_static_universe", _boom)
	assert _tool._run_preflight(base_dir) == 2
