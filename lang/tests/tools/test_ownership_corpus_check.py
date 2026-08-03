# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Negative teeth for the ownership-corpus comparison engine
(drift_corpus_audit._compare) that `just ownership-corpus-promote` relies
on, plus wiring pins for the two public recipes.

The 51-fixture ownership MATRIX (`just ownership-matrix-check`, part of
`just test`) and the full ownership-audit CORPUS (`just ownership-corpus-
promote`, run once from `just certify`) are DISTINCT certification gates —
see lang/tests/ownership_corpus/reviewed-baseline/BASELINE.md.

These teeth run the COMPARISON stage against synthetic run directories
(no fixture compiles — cheap), proving it fails closed on every mandated
divergence class:

  * universe drift                             → exit 2
  * nonzero counter delta (zero-delta mode)    → exit 1
    (and documents WHY the mode exists: the plain --baseline
    comparison alone does NOT fail on it)
  * counter key missing from the new run       → exit 1
  * unexpected new counter key                 → exit 1
  * hard-gate counter nonzero                  → exit 1
  * missing / corrupt baseline data            → exit 2

plus sanity pins on the checked-in reviewed baseline itself and on
the justfile wiring (corpus in `certify` exactly once, never in
`test`).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
BASELINE = ROOT / "lang" / "tests" / "ownership_corpus" / "reviewed-baseline"


def _load(name: str):
	spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	return mod


_tool = _load("drift_corpus_audit")
_check = _load("drift_corpus_check")


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


# ── Checked-in baseline sanity + wiring pins ───────────────────

def test_reviewed_baseline_is_well_formed() -> None:
	"""The checked-in reviewed baseline must be a complete, self-contained,
	hard-gate-clean run: machine files present and consistent, universe partition
	intact, provenance recorded.  It is produced ONLY by
	`drift_corpus_check.py --promote` (a fresh compile that exactly reproduced a
	reviewed developer projection); the Git commit is the approval."""
	agg = json.loads((BASELINE / "aggregate.json").read_text())
	man = json.loads((BASELINE / "manifest.json").read_text())
	uni = man["universe"]
	assert isinstance(agg["counters"], dict) and agg["counters"], "counters present"
	assert not _tool._hard_gate_failures(agg["counters"]), "every hard gate at zero"
	assert _tool._baseline_partition_errors(uni) == [], "universe partition intact"
	assert uni["inclusion_rule"], "inclusion rule recorded"
	assert (BASELINE / "metadata.json").is_file(), "provenance metadata present"
	assert (BASELINE / "BASELINE.md").is_file(), "provenance doc present"


def test_reviewed_baseline_carries_migrated_projections() -> None:
	"""The one-time migration must have landed projections.json, and it must be
	internally consistent with the manifest/aggregate (keys == compiled_ok,
	per-fixture merge == aggregate) so clean-clone seeding and per-fixture verify
	exactness work."""
	assert (BASELINE / "projections.json").is_file(), "per-fixture projections.json"
	b = _check._read_baseline(BASELINE)
	assert b is not None and b["projections"] is not None
	assert set(b["projections"]) == set(b["compiled_ok"]), "keys == compiled_ok"
	assert _tool._merge_counters(b["projections"].values()) == b["counters"], "merge == aggregate"


def test_reviewed_baseline_has_fingerprint() -> None:
	"""The promoted golden baseline carries the run fingerprint (a valid run
	snapshot) that verify's exact-fingerprint no-op check and full provenance
	depend on."""
	assert (BASELINE / "fingerprint.json").is_file(), "run fingerprint.json"
	snap = _check._fp.read_fingerprint(BASELINE / "fingerprint.json")
	assert snap["kind"] == "run_snapshot"
	assert _check._read_baseline(BASELINE)["toolchain"] is not None


def test_baseline_md_does_not_name_deleted_promotion_tooling() -> None:
	"""Clean break: the reviewed baseline's provenance doc must not reference the
	removed promote tool / promotions record chain (historical prose belongs in
	doc/history.md)."""
	md = (BASELINE / "BASELINE.md").read_text()
	for gone in ("drift_corpus_promote.py", "promotions/", "approval-DRAFT.json"):
		assert gone not in md, f"BASELINE.md still names removed tooling: {gone!r}"


def test_justfile_public_recipe_contract() -> None:
	"""Contract pins on the PUBLIC recipes only: `just certify` verifies (never
	promotes), and `just test` does not invoke the corpus."""
	justfile = (ROOT / "justfile").read_text()
	# three public recipes; no gate/preflight recipe
	assert "ownership-corpus-check *ARGS" in justfile
	assert "ownership-corpus-verify *ARGS" in justfile
	assert "ownership-corpus-promote *ARGS" in justfile
	assert "ownership-corpus-gate" not in justfile
	assert "ownership-corpus-preflight" not in justfile
	# certify depends on VERIFY exactly once — never promote (verify never writes)
	certify_line = next(l for l in justfile.splitlines() if l.startswith("certify:"))
	deps = certify_line.split(":", 1)[1].split()
	assert deps.count("ownership-corpus-verify") == 1
	assert "ownership-corpus-promote" not in deps
	certify_recipe = justfile[justfile.index("certify:"):].split("\n\n")[0]
	assert "promote" not in certify_recipe
	# the corpus is NOT a dependency of `just test`; the matrix stays in test
	test_line = next(l for l in justfile.splitlines() if l.startswith("test:"))
	assert "ownership-corpus" not in test_line
	assert "ownership-matrix-check" in test_line



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
