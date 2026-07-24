# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Negative teeth for the ownership-corpus certification gate
(`just ownership-corpus-check` → `tools/drift_corpus_audit.py
--baseline <checked-in> --require-zero-delta`).

The 51-fixture ownership MATRIX (`just ownership-matrix-check`, part of
`just test`) and the 924-fixture ownership CORPUS (this gate, run
exactly once from `just certify`) are DISTINCT certification gates —
see lang/tests/ownership_corpus/certified-baseline/BASELINE.md.

These teeth run the tool's COMPARISON stage against synthetic run
directories (no fixture compiles — cheap), proving the recipe fails
closed on every mandated divergence class:

  * universe drift                             → exit 2
  * nonzero counter delta (zero-delta mode)    → exit 1
    (and documents WHY the mode exists: the plain --baseline
    comparison alone does NOT fail on it)
  * counter key missing from the new run       → exit 1
  * unexpected new counter key                 → exit 1
  * hard-gate counter nonzero                  → exit 1
  * missing / corrupt baseline data            → exit 2

plus sanity pins on the checked-in certified baseline itself and on
the justfile wiring (corpus in `certify` exactly once, never in
`test`).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BASELINE = ROOT / "lang" / "tests" / "ownership_corpus" / "certified-baseline"

_spec = importlib.util.spec_from_file_location(
	"drift_corpus_audit", ROOT / "tools" / "drift_corpus_audit.py")
_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tool)


_UNIVERSE = {
	"compiled_ok": ["fx_a", "fx_b"],
	"excluded": [],
	"failed": ["fx_c"],
	"fixtures": [
		{"name": "fx_a", "sha256": "aa"},
		{"name": "fx_b", "sha256": "bb"},
		{"name": "fx_c", "sha256": "cc"},
	],
	"inclusion_rule": "synthetic",
}


def _write_run(dirpath: Path, counters: dict, universe: dict | None = None,
               *, corrupt_aggregate: bool = False, omit_aggregate: bool = False) -> Path:
	dirpath.mkdir(parents=True, exist_ok=True)
	(dirpath / "manifest.json").write_text(json.dumps(
		{"universe": universe if universe is not None else _UNIVERSE}))
	if corrupt_aggregate:
		(dirpath / "aggregate.json").write_text("{not json")
	elif not omit_aggregate:
		(dirpath / "aggregate.json").write_text(json.dumps({"counters": counters}))
	return dirpath


_CLEAN = {"events": 10, "fns": 4, "site_class:overwrite_release": 6}


def test_identical_runs_pass_zero_delta(tmp_path: Path) -> None:
	base = _write_run(tmp_path / "base", dict(_CLEAN))
	new = _write_run(tmp_path / "new", dict(_CLEAN))
	assert _tool._compare(base, new, require_zero_delta=True) == 0


def test_universe_drift_fails(tmp_path: Path) -> None:
	base = _write_run(tmp_path / "base", dict(_CLEAN))
	drifted = dict(_UNIVERSE)
	drifted["compiled_ok"] = ["fx_a"]
	drifted["failed"] = ["fx_b", "fx_c"]
	new = _write_run(tmp_path / "new", dict(_CLEAN), universe=drifted)
	assert _tool._compare(base, new, require_zero_delta=True) == 2
	# universe dominates even without zero-delta mode
	assert _tool._compare(base, new) == 2


def test_nonzero_delta_fails_only_in_zero_delta_mode(tmp_path: Path) -> None:
	"""The mandated policy gap: plain --baseline comparison PRINTS a
	nonzero delta but passes; --require-zero-delta fails it."""
	base = _write_run(tmp_path / "base", dict(_CLEAN))
	changed = dict(_CLEAN)
	changed["site_class:overwrite_release"] += 1
	new = _write_run(tmp_path / "new", changed)
	assert _tool._compare(base, new) == 0, "documenting the plain-mode gap"
	assert _tool._compare(base, new, require_zero_delta=True) == 1


def test_missing_counter_key_fails(tmp_path: Path) -> None:
	base = _write_run(tmp_path / "base", dict(_CLEAN))
	shrunk = dict(_CLEAN)
	del shrunk["fns"]
	new = _write_run(tmp_path / "new", shrunk)
	assert _tool._compare(base, new, require_zero_delta=True) == 1


def test_unexpected_new_counter_key_fails(tmp_path: Path) -> None:
	base = _write_run(tmp_path / "base", dict(_CLEAN))
	grown = dict(_CLEAN)
	grown["surprise_counter"] = 1
	new = _write_run(tmp_path / "new", grown)
	assert _tool._compare(base, new, require_zero_delta=True) == 1


def test_hard_gate_nonzero_fails(tmp_path: Path) -> None:
	gate = _tool.HARD_GATES[0]
	base_counters = dict(_CLEAN)
	base = _write_run(tmp_path / "base", base_counters)
	bad = dict(_CLEAN)
	bad[gate] = 1
	new = _write_run(tmp_path / "new", bad)
	assert _tool._compare(base, new, require_zero_delta=True) == 1
	# gates fail even without zero-delta mode
	assert _tool._compare(base, new) == 1


def test_corrupt_baseline_fails_closed(tmp_path: Path) -> None:
	base = _write_run(tmp_path / "base", dict(_CLEAN), corrupt_aggregate=True)
	new = _write_run(tmp_path / "new", dict(_CLEAN))
	assert _tool._compare(base, new, require_zero_delta=True) == 2


def test_missing_baseline_data_fails_closed(tmp_path: Path) -> None:
	base = _write_run(tmp_path / "base", dict(_CLEAN), omit_aggregate=True)
	new = _write_run(tmp_path / "new", dict(_CLEAN))
	assert _tool._compare(base, new, require_zero_delta=True) == 2
	assert _tool._compare(tmp_path / "nonexistent", new, require_zero_delta=True) == 2


def test_zero_delta_requires_baseline_flagging() -> None:
	rc = _tool.main(["--out", "/nonexistent-unused", "--require-zero-delta"])
	assert rc == 2


# ── Checked-in baseline sanity + wiring pins ─────────────────────────


def test_certified_baseline_is_complete_and_clean() -> None:
	agg = json.loads((BASELINE / "aggregate.json").read_text())
	man = json.loads((BASELINE / "manifest.json").read_text())
	assert isinstance(agg["counters"], dict) and agg["counters"], "counters present"
	assert not _tool._hard_gate_failures(agg["counters"]), (
		"the certified baseline must have every hard gate at zero"
	)
	uni = man["universe"]
	assert uni["inclusion_rule"], "verbatim inclusion rule recorded"
	assert len(uni["compiled_ok"]) == 924, "the certified 924-fixture universe"
	assert (BASELINE / "metadata.json").is_file(), "provenance metadata checked in"
	readme = (BASELINE / "BASELINE.md").read_text()
	for needle in ("0.33.87", "ABI 21", "3d48b7f0", "drift_corpus_audit.py",
	               "Generation command", "NEVER regenerates"):
		assert needle in readme, f"BASELINE.md must record provenance: {needle!r}"


def test_justfile_wiring_corpus_once_in_certify_never_in_test() -> None:
	justfile = (ROOT / "justfile").read_text()
	# recipe exists and points at the checked-in baseline in zero-delta mode
	assert "ownership-corpus-check:" in justfile
	assert "--require-zero-delta" in justfile
	assert "lang/tests/ownership_corpus/certified-baseline" in justfile
	# certify: exactly one independent corpus dependency, and the recipe
	# never references the private pre-handoff runner
	# (run-all-tests.sh — a separate entrypoint).
	certify_line = next(l for l in justfile.splitlines() if l.startswith("certify:"))
	assert certify_line.split(":", 1)[1].split().count("ownership-corpus-check") == 1
	certify_idx = justfile.index("certify:")
	certify_recipe = justfile[certify_idx:].split("\n\n")[0]
	assert "run-all" not in certify_recipe
	# the corpus is NOT a dependency of `just test` (run-all-tests.sh
	# runs test twice — the corpus must not run twice)
	test_line = next(l for l in justfile.splitlines() if l.startswith("test:"))
	assert "ownership-corpus-check" not in test_line
	# ...and the two ownership gates remain distinct: the matrix stays in test.
	assert "ownership-matrix-check" in test_line


def test_run_all_contains_exactly_one_corpus_invocation() -> None:
	"""run-all-tests.sh (the maintainer's private, untracked
	pre-handoff runner) must run the corpus exactly once before its two
	`just test` passes.  Skipped when the private file is absent from
	this checkout."""
	import pytest
	run_all = ROOT / "run-all-tests.sh"
	if not run_all.is_file():
		pytest.skip("run-all-tests.sh (private maintainer runner) not present")
	text = run_all.read_text()
	assert text.count("ownership-corpus-check") == 1, (
		"run-all-tests.sh must invoke the ownership corpus exactly once"
	)
	assert text.count("just test") == 2, "the two-mode full suite"


def test_malformed_universe_shapes_fail_closed(tmp_path: Path) -> None:
	"""Universe entries that do not match the comparison schema (wrong
	container types, missing keys, malformed fixture entries) exit 2 —
	never traceback."""
	base = _write_run(tmp_path / "base", dict(_CLEAN))
	shapes = [
		"not-a-dict",
		{},  # all keys missing
		{**_UNIVERSE, "compiled_ok": "fx_a"},              # not a list
		{**_UNIVERSE, "failed": [1, 2]},                   # not strings
		{**_UNIVERSE, "fixtures": "nope"},                 # not a list
		{**_UNIVERSE, "fixtures": [{"name": "fx_a"}]},     # sha256 missing
	]
	for i, bad in enumerate(shapes):
		new = _write_run(tmp_path / f"new{i}", dict(_CLEAN), universe=bad)
		assert _tool._compare(base, new, require_zero_delta=True) == 2, f"shape {i}: {bad!r}"


def test_non_integer_counter_values_fail_closed(tmp_path: Path) -> None:
	base = _write_run(tmp_path / "base", dict(_CLEAN))
	for i, bad_val in enumerate(["7", 3.5, None, True, [1]]):
		bad = dict(_CLEAN)
		bad["events"] = bad_val
		new = _write_run(tmp_path / f"newv{i}", bad)
		assert _tool._compare(base, new, require_zero_delta=True) == 2, f"value {bad_val!r}"


def test_malformed_baseline_side_also_fails_closed(tmp_path: Path) -> None:
	"""Schema validation covers the BASELINE side too, not just the new
	run."""
	bad_base = _write_run(tmp_path / "base", dict(_CLEAN),
		universe={**_UNIVERSE, "fixtures": [{"sha256": "aa"}]})
	new = _write_run(tmp_path / "new", dict(_CLEAN))
	assert _tool._compare(bad_base, new, require_zero_delta=True) == 2


def test_malformed_inclusion_rule_and_excluded_fail_closed(tmp_path: Path) -> None:
	"""inclusion_rule must be a string and excluded a list of
	{name, reason} string records — both participate in universe
	identity, so malformed shapes exit 2."""
	base = _write_run(tmp_path / "base", dict(_CLEAN))
	shapes = [
		{**_UNIVERSE, "inclusion_rule": 7},                       # not a string
		{**_UNIVERSE, "excluded": "nope"},                        # not a list
		{**_UNIVERSE, "excluded": [{"name": "fx_x"}]},            # reason missing
		{**_UNIVERSE, "excluded": [{"name": 1, "reason": "r"}]},  # name not a string
	]
	for i, bad in enumerate(shapes):
		new = _write_run(tmp_path / f"newx{i}", dict(_CLEAN), universe=bad)
		assert _tool._compare(base, new, require_zero_delta=True) == 2, f"shape {i}: {bad!r}"
	# and a well-formed excluded record passes
	good = {**_UNIVERSE, "excluded": [{"name": "fx_x", "reason": "declares module_paths"}]}
	okb = _write_run(tmp_path / "okbase", dict(_CLEAN), universe=good)
	okn = _write_run(tmp_path / "oknew", dict(_CLEAN), universe=good)
	assert _tool._compare(okb, okn, require_zero_delta=True) == 0
