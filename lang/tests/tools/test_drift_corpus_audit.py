# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Pins for tools/drift_corpus_audit.py (cleanup slice 1b).

The tool is the acceptance instrument for every ownership slice, so its
own contract is pinned: comparable artifacts are stable and volatile-free,
determinism is byte-level, universe mismatches are loud, and hard gates
fail the comparison.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TOOL = ROOT / "tools" / "drift_corpus_audit.py"

# Small, stable fixtures with `module m;`-style mains that compile fast.
SUBSET = "hello_drift,algo_swap_sanity,array_string"

# Volatile KEY names that must never appear in the comparable artifact
# (checked structurally against key names — counter names like
# c1_path_dependent legitimately contain these substrings as values).
VOLATILE_KEYS = ("time", "duration", "pid", "started", "repo_root", "jobs", "python")


def _run(out: Path, *extra: str) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		[sys.executable, str(TOOL), "--out", str(out), "-j", "3",
		 "--only", SUBSET, *extra],
		cwd=ROOT, capture_output=True, text=True, timeout=900,
	)


def test_aggregate_is_stable_and_volatile_free(tmp_path: Path) -> None:
	r = _run(tmp_path / "run1")
	assert r.returncode == 0, r.stderr[-1500:]
	agg_path = tmp_path / "run1" / "aggregate.json"
	agg = json.loads(agg_path.read_text())
	assert agg["fixtures_compiled"] >= 2, agg
	counters = agg["counters"]
	assert counters, "no counters aggregated"
	assert list(counters.keys()) == sorted(counters.keys()), "keys not sorted"
	# The comparable artifact is EXACTLY {counters, fixtures_compiled},
	# all-int counter values, and no volatile key anywhere.
	assert set(agg.keys()) == {"counters", "fixtures_compiled"}, agg.keys()
	assert all(isinstance(v, int) for v in counters.values())
	for key in list(agg.keys()) + list(counters.keys()):
		for vol in VOLATILE_KEYS:
			assert vol != key.lower(), f"volatile key {key!r} in aggregate.json"
	# Manifest carries the universe identity; metadata carries the volatile.
	manifest = json.loads((tmp_path / "run1" / "manifest.json").read_text())
	assert manifest["universe"]["fixtures"], manifest
	names = [f["name"] for f in manifest["universe"]["fixtures"]]
	assert names == sorted(names)
	meta = json.loads((tmp_path / "run1" / "metadata.json").read_text())
	assert "duration_s" in meta and "started_unix" in meta


def test_determinism_and_self_baseline(tmp_path: Path) -> None:
	r1 = _run(tmp_path / "run1")
	assert r1.returncode == 0, r1.stderr[-1500:]
	r2 = _run(tmp_path / "run2", "--baseline", str(tmp_path / "run1"))
	assert r2.returncode == 0, f"{r2.stdout}\n{r2.stderr[-1500:]}"
	# Byte-identical comparable artifacts across runs.
	a1 = (tmp_path / "run1" / "aggregate.json").read_bytes()
	a2 = (tmp_path / "run2" / "aggregate.json").read_bytes()
	assert a1 == a2, "aggregate.json not byte-identical across identical runs"
	m1 = (tmp_path / "run1" / "manifest.json").read_bytes()
	m2 = (tmp_path / "run2" / "manifest.json").read_bytes()
	assert m1 == m2, "manifest.json not byte-identical across identical runs"
	# The delta table shows all-zero deltas.
	for line in r2.stdout.splitlines():
		if line and not line.startswith("counter") and not line.startswith("compiled"):
			assert line.rstrip().endswith("+0"), f"nonzero delta on identical runs: {line}"


def test_inclusion_rule_and_exclusions_recorded(tmp_path: Path) -> None:
	"""Multi-unit fixtures (module_paths/c_sources) are excluded BY RULE
	and the manifest records both the rule and each exclusion reason —
	the universe claim cannot silently drift from the real e2e universe."""
	subset = SUBSET + ",abi_entrypoint_cross_module_call"
	r = subprocess.run(
		[sys.executable, str(TOOL), "--out", str(tmp_path / "run1"), "-j", "3",
		 "--only", subset],
		cwd=ROOT, capture_output=True, text=True, timeout=900,
	)
	assert r.returncode == 0, r.stderr[-1500:]
	manifest = json.loads((tmp_path / "run1" / "manifest.json").read_text())
	uni = manifest["universe"]
	assert "inclusion_rule" in uni
	assert "module_paths" in uni["inclusion_rule"] and "main.drift" in uni["inclusion_rule"]
	excl = {e["name"]: e["reason"] for e in uni["excluded"]}
	assert "abi_entrypoint_cross_module_call" in excl, uni["excluded"]
	assert "module_paths" in excl["abi_entrypoint_cross_module_call"]
	included = {f["name"] for f in uni["fixtures"]}
	assert "abi_entrypoint_cross_module_call" not in included


def test_nonempty_out_dir_refused(tmp_path: Path) -> None:
	"""Reusing a non-empty --out could aggregate STALE audit files as
	current results — the tool must fail fast (exit 2), not merge."""
	out = tmp_path / "run1"
	(out / "audit").mkdir(parents=True)
	(out / "audit" / "stale.jsonl").write_text("[]\n")
	r = _run(out)
	assert r.returncode == 2, f"expected refusal exit 2, got {r.returncode}"
	assert "refusing to reuse non-empty --out" in r.stderr


def test_universe_mismatch_is_loud(tmp_path: Path) -> None:
	r1 = _run(tmp_path / "run1")
	assert r1.returncode == 0
	r2 = subprocess.run(
		[sys.executable, str(TOOL), "--out", str(tmp_path / "run2"), "-j", "2",
		 "--only", "hello_drift,algo_swap_sanity",
		 "--baseline", str(tmp_path / "run1")],
		cwd=ROOT, capture_output=True, text=True, timeout=900,
	)
	assert r2.returncode == 2, f"expected universe-mismatch exit 2, got {r2.returncode}"
	assert "UNIVERSE MISMATCH" in r2.stderr


def test_hard_gate_failure(tmp_path: Path) -> None:
	"""Nonzero hard gates fail BOTH modes: the new side of a comparison
	(exit 1 via _compare) and — review round 3 — standalone baseline
	acquisition (the helper is shared; a reference baseline must never
	be acquirable with a gate regression)."""
	r1 = _run(tmp_path / "run1")
	assert r1.returncode == 0
	r2 = _run(tmp_path / "run2")
	assert r2.returncode == 0
	sys.path.insert(0, str(ROOT / "tools"))
	import importlib
	mod = importlib.import_module("drift_corpus_audit")
	# Clean self-comparison passes; clean counters have no gate failures.
	assert mod._compare(tmp_path / "run1", tmp_path / "run2") == 0
	clean = json.loads((tmp_path / "run2" / "aggregate.json").read_text())["counters"]
	assert mod._hard_gate_failures(clean) == []
	# Gate regression fails the comparison path...
	agg_path = tmp_path / "run2" / "aggregate.json"
	agg = json.loads(agg_path.read_text())
	agg["counters"]["unclassified"] = 3
	agg_path.write_text(json.dumps(agg, indent=2, sort_keys=True) + "\n")
	rc = mod._compare(tmp_path / "run1", tmp_path / "run2")
	assert rc == 1, "hard-gate regression did not fail the comparison"
	# ...and the SAME helper flags it for standalone acquisition.
	assert mod._hard_gate_failures(agg["counters"]) == ["unclassified"]


def test_universe_mismatch_dominates_gate_failure(tmp_path: Path) -> None:
	"""Review round 4: with BOTH a universe mismatch and a tampered hard
	gate, exit 2 (mismatch) must dominate — deltas and gate checks over
	different universes are meaningless."""
	r1 = _run(tmp_path / "run1")
	assert r1.returncode == 0
	r2 = subprocess.run(
		[sys.executable, str(TOOL), "--out", str(tmp_path / "run2"), "-j", "2",
		 "--only", "hello_drift,algo_swap_sanity"],
		cwd=ROOT, capture_output=True, text=True, timeout=900,
	)
	assert r2.returncode == 0
	agg_path = tmp_path / "run2" / "aggregate.json"
	agg = json.loads(agg_path.read_text())
	agg["counters"]["unclassified"] = 7
	agg_path.write_text(json.dumps(agg, indent=2, sort_keys=True) + "\n")
	sys.path.insert(0, str(ROOT / "tools"))
	import importlib
	mod = importlib.import_module("drift_corpus_audit")
	rc = mod._compare(tmp_path / "run1", tmp_path / "run2")
	assert rc == 2, f"universe mismatch must dominate the gate failure, got {rc}"
