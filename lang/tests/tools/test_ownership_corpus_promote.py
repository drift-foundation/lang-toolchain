# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Teeth for tools/drift_corpus_promote.py — the reviewed-baseline
promotion tool.

The tool MATERIALIZES an already-approved promotion; it must never be
able to approve its own input.  Pinned here:
  * dry-run by default: all checks run, NOTHING is written;
  * --apply writes ONLY the four reviewed-baseline files and then
    passes the exact post-write zero-delta comparison;
  * fail-closed on: run-dir not matching the approval (never selects
    a run itself), stale predecessor (baseline changed since
    approval), candidate artifact hash mismatch, unexpected universe
    change, unexplained counter delta, nonzero hard gate, malformed
    approval JSON;
  * unrelated files in the baseline dir are left untouched.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]

spec = importlib.util.spec_from_file_location(
	"drift_corpus_promote", ROOT / "tools" / "drift_corpus_promote.py")
promote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(promote)


def sha(p: Path) -> str:
	return hashlib.sha256(p.read_bytes()).hexdigest()


def write_side(d: Path, counters: dict, fixtures: list, compiled: list,
               failed: list, per_fixture: dict | None = None) -> None:
	"""per_fixture: {name: {counter: value}} — when given, audit/ jsonl
	files with EXACTLY ONE aggregate record each are emitted (their
	sums must equal `counters` for residual-zero worlds)."""
	d.mkdir(parents=True, exist_ok=True)
	if per_fixture is not None:
		(d / "audit").mkdir(exist_ok=True)
		for name, cnt in per_fixture.items():
			rec = {"record": "aggregate", **cnt}
			(d / "audit" / f"{name}.jsonl").write_text(
				f"[audit] {json.dumps(rec)}\n")
	(d / "aggregate.json").write_text(json.dumps(
		{"counters": counters, "fixtures_compiled": len(compiled)},
		indent=2, sort_keys=True))
	(d / "manifest.json").write_text(json.dumps({
		"universe": {
			"inclusion_rule": "test-rule",
			"fixtures": fixtures,
			"excluded": [{"name": "skipme", "reason": "declares x"}],
			"compiled_ok": sorted(compiled),
			"failed": sorted(failed),
		},
		"environment": {"driftc_version": "0.0.1", "abi": "22",
		                 "tool_version": "1.7.1"},
	}, indent=2, sort_keys=True))
	(d / "metadata.json").write_text(json.dumps(
		{"started_unix": 123.0, "duration_s": 1.0, "jobs": 1,
		 "repo_root": "x", "python": "3"}, indent=2, sort_keys=True))


@pytest.fixture()
def world(tmp_path: Path):
	"""baseline (predecessor), candidate run (one added fixture, one
	counter delta), and a valid approval file."""
	base = tmp_path / "baseline"
	run = tmp_path / "run"
	fixtures_b = [{"name": "a", "sha256": "1" * 64},
	              {"name": "bad", "sha256": "2" * 64}]
	write_side(base, {"events": 100, "fns": 10, "unclassified": 0},
	           fixtures_b, ["a"], ["bad"],
	           per_fixture={"a": {"events": 100, "fns": 10}})
	fixtures_n = fixtures_b + [{"name": "newfix", "sha256": "3" * 64}]
	write_side(run, {"events": 120, "fns": 10, "unclassified": 0},
	           fixtures_n, ["a", "newfix"], ["bad"],
	           per_fixture={"a": {"events": 100, "fns": 10},
	                        "newfix": {"events": 20}})
	# a retained predecessor RUN whose aggregate+manifest byte-equal
	# the live baseline (identity requirement of --draft)
	pred_run = tmp_path / "pred-run"
	pred_run.mkdir()
	for name in ("aggregate.json", "manifest.json", "metadata.json"):
		(pred_run / name).write_bytes((base / name).read_bytes())
	(pred_run / "audit").mkdir()
	(pred_run / "audit" / "a.jsonl").write_bytes(
		(base / "audit" / "a.jsonl").read_bytes())
	approval = tmp_path / "approval.json"
	approval.write_text(json.dumps({
		"approval": "ownership-corpus-promotion",
		"status": "approved",
		"approved_by": "tester",
		"date": "2026-07-26",
		"predecessor": {
			"aggregate_sha256": sha(base / "aggregate.json"),
			"manifest_sha256": sha(base / "manifest.json"),
			"metadata_sha256": sha(base / "metadata.json"),
		},
		"candidate": {
			"run_dir": str(run),
			"aggregate_sha256": sha(run / "aggregate.json"),
			"manifest_sha256": sha(run / "manifest.json"),
			"metadata_sha256": sha(run / "metadata.json"),
		},
		"expected_universe": {
			"compiled_added": ["newfix"], "compiled_removed": [],
			"failed_added": [], "failed_removed": [],
			"prehash_changes": [],
			"compiled_count": 2, "failed_count": 1, "excluded_count": 1,
		},
		"expected_counter_deltas": {"events": 20},
		"baseline_md": {
			"title": "test baseline",
			"predecessor_description": "the previous test baseline",
			"attribution": "events +20 from newfix (reviewed).",
		},
	}, indent=2))
	return base, run, approval


def run_tool(run, approval, base, apply=False):
	argv = [str(run), str(approval), "--baseline-dir", str(base)]
	if apply:
		argv.append("--apply")
	try:
		return promote.main(argv)
	except SystemExit as e:
		return int(e.code or 0)


def snapshot(d: Path) -> dict:
	return {p.name: sha(p) for p in sorted(d.iterdir()) if p.is_file()}


def test_dry_run_checks_pass_and_write_nothing(world):
	base, run, approval = world
	before = snapshot(base)
	assert run_tool(run, approval, base) == 0
	assert snapshot(base) == before, "dry-run must not write"


def test_apply_writes_only_baseline_files_and_zero_delta(world, tmp_path):
	base, run, approval = world
	unrelated = base / "NOTES.txt"
	unrelated.write_text("do not touch")
	assert run_tool(run, approval, base, apply=True) == 0
	# the three artifacts now equal the candidate's
	for name in ("aggregate.json", "manifest.json", "metadata.json"):
		assert sha(base / name) == sha(run / name)
	md = (base / "BASELINE.md").read_text()
	assert "test baseline" in md and "events +20" in md.replace("`", "")
	# reviewer identity/date are NOT duplicated in the baseline —
	# they come from Git history (stated in the provenance row)
	assert "Git history" in md
	assert unrelated.read_text() == "do not touch", "unrelated file touched"


def test_wrong_run_dir_rejected(world, tmp_path):
	base, run, approval = world
	other = tmp_path / "other-run"
	other.mkdir()
	assert run_tool(other, approval, base) == 1


def test_stale_predecessor_rejected(world):
	base, run, approval = world
	agg = json.loads((base / "aggregate.json").read_text())
	agg["counters"]["events"] = 101  # baseline moved since approval
	(base / "aggregate.json").write_text(json.dumps(agg, indent=2, sort_keys=True))
	assert run_tool(run, approval, base) == 1


def test_candidate_hash_mismatch_rejected(world):
	base, run, approval = world
	man = json.loads((run / "manifest.json").read_text())
	man["environment"]["driftc_version"] = "0.0.2"
	(run / "manifest.json").write_text(json.dumps(man, indent=2, sort_keys=True))
	assert run_tool(run, approval, base) == 1


def _approval_edit(approval: Path, run: Path, mutate) -> None:
	"""edit the CANDIDATE data, re-pin its hashes in the approval, and
	apply `mutate` to the approval dict — used to isolate one check."""
	app = json.loads(approval.read_text())
	app["candidate"]["aggregate_sha256"] = sha(run / "aggregate.json")
	app["candidate"]["manifest_sha256"] = sha(run / "manifest.json")
	app["candidate"]["metadata_sha256"] = sha(run / "metadata.json")
	mutate(app)
	approval.write_text(json.dumps(app, indent=2))


def test_unexplained_counter_delta_rejected(world):
	base, run, approval = world
	agg = json.loads((run / "aggregate.json").read_text())
	agg["counters"]["fns"] = 11  # a delta the approval does not explain
	(run / "aggregate.json").write_text(json.dumps(agg, indent=2, sort_keys=True))
	_approval_edit(approval, run, lambda app: None)  # hashes re-pinned; deltas not
	assert run_tool(run, approval, base) == 1


def test_unexpected_universe_addition_rejected(world):
	base, run, approval = world
	man = json.loads((run / "manifest.json").read_text())
	man["universe"]["compiled_ok"] = sorted(
		man["universe"]["compiled_ok"] + ["sneaky"])
	man["universe"]["fixtures"].append({"name": "sneaky", "sha256": "4" * 64})
	(run / "manifest.json").write_text(json.dumps(man, indent=2, sort_keys=True))
	_approval_edit(approval, run, lambda app: None)
	assert run_tool(run, approval, base) == 1


def test_hard_gate_nonzero_rejected(world):
	base, run, approval = world
	agg = json.loads((run / "aggregate.json").read_text())
	agg["counters"]["unclassified"] = 1
	(run / "aggregate.json").write_text(json.dumps(agg, indent=2, sort_keys=True))
	def add_delta(app):
		app["expected_counter_deltas"]["unclassified"] = 1
	_approval_edit(approval, run, add_delta)
	assert run_tool(run, approval, base) == 1


def test_malformed_approval_rejected(world):
	base, run, approval = world
	app = json.loads(approval.read_text())
	del app["expected_universe"]
	approval.write_text(json.dumps(app))
	assert run_tool(run, approval, base) == 1
	approval.write_text("{not json")
	assert run_tool(run, approval, base) == 1


def test_zero_delta_in_expected_deltas_rejected(world):
	base, run, approval = world
	def add_zero(app):
		app["expected_counter_deltas"]["fns"] = 0
	_approval_edit(approval, run, add_zero)
	assert run_tool(run, approval, base) == 1


def _edit_approval(approval: Path, mutate) -> None:
	app = json.loads(approval.read_text())
	mutate(app)
	approval.write_text(json.dumps(app, indent=2))


def test_legacy_status_fields_are_inert(world):
	"""Authority is the FILENAME.  Legacy status/approved_by/date
	fields (or their absence, or placeholder values) neither enable
	nor block anything."""
	base, run, approval = world
	# approval.json with status "pending" and a placeholder reviewer:
	# STILL approved (filename wins); fields are inert history
	_edit_approval(approval, lambda a: a.update(
		status="pending", approved_by="PENDING-REVIEW (someone)"))
	assert run_tool(run, approval, base, apply=True) == 0


def test_missing_status_fields_fine(world):
	base, run, approval = world
	def drop(a):
		for k in ("status", "approved_by", "date"):
			a.pop(k, None)
	_edit_approval(approval, drop)
	assert run_tool(run, approval, base) == 0


def test_draft_filename_is_pending(world, tmp_path):
	"""approval-DRAFT.json: dry-run allowed, --apply refused; the exact
	rename to approval.json enables apply."""
	base, run, approval = world
	d = tmp_path / "state-dir"
	d.mkdir()
	draft = d / "approval-DRAFT.json"
	draft.write_text(approval.read_text())
	approval.unlink()  # keep exactly one approval file in play
	assert run_tool(run, draft, base) == 0             # dry-run fine
	assert run_tool(run, draft, base, apply=True) == 1  # pending by name
	# the reviewer's ONLY mutation: the rename
	final = d / "approval.json"
	draft.rename(final)
	assert run_tool(run, final, base, apply=True) == 0


def test_alternate_filename_fails_closed(world, tmp_path):
	base, run, approval = world
	for name in ("approval-v2.json", "APPROVAL.json", "approval-final.json"):
		alt = tmp_path / name
		alt.write_text(approval.read_text())
		assert run_tool(run, alt, base) == 1, name
		assert run_tool(run, alt, base, apply=True) == 1, name
		alt.unlink()


def test_both_files_present_fails_closed(world):
	base, run, approval = world
	sibling = approval.parent / "approval-DRAFT.json"
	sibling.write_text(approval.read_text())
	assert run_tool(run, approval, base) == 1
	assert run_tool(run, approval, base, apply=True) == 1
	assert run_tool(run, sibling, base) == 1


def test_zero_valued_counter_key_addition_rejected(world):
	base, run, approval = world
	agg = json.loads((run / "aggregate.json").read_text())
	agg["counters"]["brand_new_gate"] = 0   # zero-valued schema change
	(run / "aggregate.json").write_text(json.dumps(agg, indent=2, sort_keys=True))
	_approval_edit(approval, run, lambda app: None)
	assert run_tool(run, approval, base) == 1
	# explicitly approving the schema change makes it pass
	_approval_edit(approval, run,
	               lambda app: app.update(counter_keys_added=["brand_new_gate"]))
	assert run_tool(run, approval, base) == 0


def test_counter_key_removal_rejected(world):
	base, run, approval = world
	agg = json.loads((run / "aggregate.json").read_text())
	del agg["counters"]["fns"]              # fns was zero-delta; removal
	(run / "aggregate.json").write_text(json.dumps(agg, indent=2, sort_keys=True))
	_approval_edit(approval, run, lambda app: None)
	assert run_tool(run, approval, base) == 1


def test_inclusion_rule_change_rejected(world):
	base, run, approval = world
	man = json.loads((run / "manifest.json").read_text())
	man["universe"]["inclusion_rule"] = "different-rule"
	(run / "manifest.json").write_text(json.dumps(man, indent=2, sort_keys=True))
	_approval_edit(approval, run, lambda app: None)
	assert run_tool(run, approval, base) == 1


def test_excluded_change_always_rejected(world):
	base, run, approval = world
	man = json.loads((run / "manifest.json").read_text())
	man["universe"]["excluded"] = [{"name": "skipme", "reason": "NEW reason"}]
	(run / "manifest.json").write_text(json.dumps(man, indent=2, sort_keys=True))
	_approval_edit(approval, run, lambda app: None)
	assert run_tool(run, approval, base) == 1
	# excluded_changed=true is itself rejected as unsupported
	_approval_edit(approval, run, lambda app: app["expected_universe"].update(
		excluded_changed=True))
	assert run_tool(run, approval, base) == 1


def test_universe_integrity_partition_overlap_rejected(world):
	base, run, approval = world
	man = json.loads((run / "manifest.json").read_text())
	man["universe"]["failed"] = sorted(man["universe"]["failed"] + ["a"])
	(run / "manifest.json").write_text(json.dumps(man, indent=2, sort_keys=True))
	_approval_edit(approval, run, lambda app: None)
	assert run_tool(run, approval, base) == 1


def test_universe_integrity_orphan_fixture_rejected(world):
	base, run, approval = world
	man = json.loads((run / "manifest.json").read_text())
	man["universe"]["fixtures"].append({"name": "orphan", "sha256": "9" * 64})
	(run / "manifest.json").write_text(json.dumps(man, indent=2, sort_keys=True))
	_approval_edit(approval, run, lambda app: None)
	assert run_tool(run, approval, base) == 1


def test_malformed_corpus_data_rejected(world):
	base, run, approval = world
	(run / "manifest.json").write_text("{not json at all")
	_approval_edit(approval, run, lambda app: None)  # re-pin hash of the junk
	assert run_tool(run, approval, base) == 1
	# structurally-invalid universe (fixture entry missing sha256)
	man = {"universe": {"inclusion_rule": "test-rule",
	                     "fixtures": [{"name": "a"}],
	                     "excluded": [], "compiled_ok": ["a"], "failed": []},
	       "environment": {}}
	(run / "manifest.json").write_text(json.dumps(man, indent=2, sort_keys=True))
	_approval_edit(approval, run, lambda app: None)
	assert run_tool(run, approval, base) == 1


def test_staging_residue_fails_closed(world):
	base, run, approval = world
	(base / ".promote-staging.tmp").mkdir()
	assert run_tool(run, approval, base, apply=True) == 1


def test_promotion_not_wired_into_gates():
	"""The promote recipe must remain ABSENT from `just test`,
	`just certify`, and run-all-tests.sh (wiring-isolation pin)."""
	justfile = (ROOT / "justfile").read_text()
	# the only mentions of the tool live in its own recipe block
	promote_recipe = justfile.index("ownership-corpus-promote RUN_DIR")
	test_line = [l for l in justfile.splitlines()
	             if l.startswith("test:")][0]
	certify_line = [l for l in justfile.splitlines()
	                if l.startswith("certify:")][0]
	assert "promote" not in test_line
	assert "promote" not in certify_line
	# every mention of the tool lives inside the promote recipe's own
	# section (from its header comment to the next section header)
	section_start = justfile.index(
		"# ── Ownership CORPUS baseline PROMOTION")
	section_end = justfile.index(
		"# ── Ownership CORPUS certification gate", section_start)
	section = justfile[section_start:section_end]
	assert justfile.count("drift_corpus_promote.py") == \
		section.count("drift_corpus_promote.py"), \
		"the promote tool is referenced outside its own recipe section"
	assert section.count("drift_corpus_promote.py") >= 1
	assert promote_recipe > 0
	runner = ROOT / "run-all-tests.sh"
	if runner.exists():
		assert "promote" not in runner.read_text(), \
			"run-all-tests.sh must never invoke promotion"


def test_draft_generation_records_facts(world, tmp_path):
	base, run, approval = world
	record = tmp_path / "record"
	rc = run_tool_draft(run, record, base)
	assert rc == 0
	# durable record layout
	for side in ("predecessor", "candidate"):
		for name in ("aggregate.json", "manifest.json", "metadata.json",
		             "fixture-counters.json"):
			assert (record / side / name).exists(), (side, name)
	out = record / "approval-DRAFT.json"
	draft = json.loads(out.read_text())
	# no status/identity fields: filename is the state; Git records
	# the reviewer identity/date
	for k in ("status", "approved_by", "date"):
		assert k not in draft, f"draft must not carry {k}"
	assert draft["expected_universe"]["compiled_added"] == ["newfix"]
	assert draft["expected_counter_deltas"] == {"events": 20}
	assert draft["counter_keys_added"] == []
	# baseline_md must be COMPLETE — the reviewer's only mutation is
	# the rename
	for k, v in draft["baseline_md"].items():
		assert v and "<<" not in v and "HUMAN REVIEW" not in v, (k, v)
	assert "newfix" in draft["baseline_md"]["attribution"]
	# machine attribution facts: unchanged modal, one new fixture
	facts = draft["attribution_facts"]
	assert facts["modal"] == {} and facts["modal_fixture_count"] == 1
	assert facts["outliers"] == {}
	assert facts["new_fixture_contributions"] == {"newfix": {"events": 20}}
	# the record's own candidate dir is the promotion run-dir; the
	# dry-run RE-PROVES attribution from the checked-in evidence
	cand = record / "candidate"
	assert run_tool(cand, out, base) == 0
	# --apply refused purely by the DRAFT filename...
	assert run_tool(cand, out, base, apply=True) == 1
	# ...and the exact rename enables it with no JSON edits
	final = record / "approval.json"
	out.rename(final)
	assert run_tool(cand, final, base, apply=True) == 0


def test_attribution_evidence_tamper_rejected(world, tmp_path):
	base, run, approval = world
	record = tmp_path / "record2"
	assert run_tool_draft(run, record, base) == 0
	out = record / "approval-DRAFT.json"
	cand = record / "candidate"
	fc = cand / "fixture-counters.json"
	data = json.loads(fc.read_text())
	data["fixtures"]["newfix"]["events"] = 21
	fc.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
	# hash pin catches the tamper
	assert run_tool(cand, out, base) == 1
	# re-pinning the hash exposes the attribution residual instead
	app = json.loads(out.read_text())
	app["candidate"]["fixture_counters_sha256"] = sha(fc)
	out.write_text(json.dumps(app, indent=2))
	assert run_tool(cand, out, base) == 1


def test_audit_mode_after_promotion(world, tmp_path):
	base, run, approval = world
	record = tmp_path / "record3"
	assert run_tool_draft(run, record, base) == 0
	out = record / "approval-DRAFT.json"
	cand = record / "candidate"
	# approve by the RENAME alone — no JSON edits
	final = record / "approval.json"
	out.rename(final)
	assert run_tool(cand, final, base, apply=True) == 0
	# post-promotion checkout: dry-run runs in AUDIT MODE...
	assert run_tool(cand, final, base) == 0
	# ...and --apply refuses (already promoted)
	assert run_tool(cand, final, base, apply=True) == 1


def test_draft_never_overwrites(world, tmp_path):
	base, run, approval = world
	out = tmp_path / "record-exists"
	out.mkdir()
	assert run_tool_draft(run, out, base) == 1


def test_draft_refuses_hard_gate_candidate(world):
	base, run, approval = world
	agg = json.loads((run / "aggregate.json").read_text())
	agg["counters"]["unclassified"] = 1
	(run / "aggregate.json").write_text(json.dumps(agg, indent=2, sort_keys=True))
	out = run.parent / "gen3-record"
	assert run_tool_draft(run, out, base) == 1
	assert not out.exists()


def test_draft_refuses_excluded_change(world):
	base, run, approval = world
	man = json.loads((run / "manifest.json").read_text())
	man["universe"]["excluded"] = [{"name": "skipme", "reason": "changed"}]
	(run / "manifest.json").write_text(json.dumps(man, indent=2, sort_keys=True))
	out = run.parent / "gen4-record"
	assert run_tool_draft(run, out, base) == 1
	assert not out.exists()


def test_apply_refuses_incomplete_baseline_md(world):
	base, run, approval = world
	_edit_approval(approval, lambda a: a["baseline_md"].update(
		attribution="<<placeholder left in an edited draft>>"))
	assert run_tool(run, approval, base, apply=True) == 1


def run_tool_draft(run, out, base):
	pred_run = base.parent / "pred-run"
	try:
		return promote.main([str(run), str(out), "--draft",
		                     "--predecessor-run", str(pred_run),
		                     "--baseline-dir", str(base)])
	except SystemExit as e:
		return int(e.code or 0)


def test_draft_refuses_duplicate_fixture_names(world):
	base, run, approval = world
	man = json.loads((run / "manifest.json").read_text())
	man["universe"]["fixtures"].append({"name": "a", "sha256": "5" * 64})
	(run / "manifest.json").write_text(json.dumps(man, indent=2, sort_keys=True))
	out = run.parent / "dup-record"
	assert run_tool_draft(run, out, base) == 1
	assert not out.exists()


def test_draft_refuses_partition_overlap(world):
	base, run, approval = world
	man = json.loads((run / "manifest.json").read_text())
	man["universe"]["failed"] = sorted(man["universe"]["failed"] + ["a"])
	(run / "manifest.json").write_text(json.dumps(man, indent=2, sort_keys=True))
	out = run.parent / "ovl-record"
	assert run_tool_draft(run, out, base) == 1
	assert not out.exists()


def test_draft_refuses_orphan_fixture(world):
	base, run, approval = world
	man = json.loads((run / "manifest.json").read_text())
	man["universe"]["fixtures"].append({"name": "orphan", "sha256": "9" * 64})
	(run / "manifest.json").write_text(json.dumps(man, indent=2, sort_keys=True))
	out = run.parent / "orp-record"
	assert run_tool_draft(run, out, base) == 1
	assert not out.exists()


def test_baseline_md_records_full_approval_hash(world):
	base, run, approval = world
	assert run_tool(run, approval, base, apply=True) == 0
	md = (base / "BASELINE.md").read_text()
	assert sha(approval) in md, \
		"BASELINE.md must record the FULL approval sha256"


def test_recipes_run_focused_teeth_automatically():
	"""Workflow pin: after a successful tool run, the promote recipe
	must run BOTH the promotion-mechanics suite AND the live-baseline
	sanity test (in that order, after the tool); the draft recipe must
	run the mechanics suite.  No manual pytest step between promotion
	and commit."""
	justfile = (ROOT / "justfile").read_text()

	def body_of(recipe: str) -> str:
		start = justfile.index(recipe)
		rest = justfile[start:]
		body_end = rest.find("\n\n")
		return rest[:body_end if body_end > 0 else len(rest)]

	promote = body_of("ownership-corpus-promote RUN_DIR")
	assert "test_ownership_corpus_promote.py" in promote
	assert ("test_ownership_corpus_check.py::"
	        "test_reviewed_baseline_matches_approved_promotion") in promote, \
		"promote recipe must run the live-baseline sanity test"
	tool_at = promote.index("drift_corpus_promote.py")
	assert tool_at < promote.index("test_ownership_corpus_promote.py") \
		< promote.index("test_reviewed_baseline_matches_approved_promotion"), \
		"both checks must run AFTER the tool, sanity after mechanics"

	draft = body_of("ownership-corpus-promotion-draft CAND_RUN")
	assert "test_ownership_corpus_promote.py" in draft
	assert draft.index("drift_corpus_promote.py") \
		< draft.index("test_ownership_corpus_promote.py")


# ── predecessor from the CHECKED-IN record chain ─────────────────────
# A clone carries no build/tmp runs: the only predecessor evidence that
# survives cloning is the promotions record whose candidate byte-equals
# the live baseline.  --draft without --predecessor-run must consume
# exactly that, fail-closed on every divergence.


def make_chain_record(base: Path, promotions: Path,
                      name: str = "0.0.1-prior") -> Path:
	"""An APPROVED record whose candidate IS the live baseline."""
	rec = promotions / name
	cand = rec / "candidate"
	cand.mkdir(parents=True)
	for art in ("aggregate.json", "manifest.json", "metadata.json"):
		(cand / art).write_bytes((base / art).read_bytes())
	fc = promote.extract_fixture_counters(base, ["a"])
	fc_path = cand / "fixture-counters.json"
	fc_path.write_text(json.dumps(fc, indent=1, sort_keys=True) + "\n")
	(rec / "approval.json").write_text(json.dumps({
		"approval": "ownership-corpus-promotion",
		"candidate": {"fixture_counters_sha256": sha(fc_path)},
	}, indent=2))
	return rec


def run_tool_draft_chain(run, out, base, promotions):
	try:
		return promote.main([str(run), str(out), "--draft",
		                     "--promotions-dir", str(promotions),
		                     "--baseline-dir", str(base)])
	except SystemExit as e:
		return int(e.code or 0)


def test_draft_from_record_chain(world, tmp_path):
	"""Record-chain draft == raw-log draft, end to end through --apply."""
	base, run, approval = world
	promotions = tmp_path / "promotions"
	make_chain_record(base, promotions)
	record = tmp_path / "chain-record"
	assert run_tool_draft_chain(run, record, base, promotions) == 0
	draft = json.loads((record / "approval-DRAFT.json").read_text())
	# identical facts to the raw-log bootstrap path
	raw = tmp_path / "raw-record"
	assert run_tool_draft(run, raw, base) == 0
	raw_draft = json.loads((raw / "approval-DRAFT.json").read_text())
	for k in ("predecessor", "expected_universe", "expected_counter_deltas",
	          "counter_keys_added", "counter_keys_removed",
	          "attribution_facts"):
		assert draft[k] == raw_draft[k], k
	# the chain-drafted record promotes cleanly after the rename
	out = record / "approval-DRAFT.json"
	final = record / "approval.json"
	out.rename(final)
	assert run_tool(record / "candidate", final, base, apply=True) == 0


def test_draft_chain_no_matching_record_rejected(world, tmp_path):
	base, run, approval = world
	promotions = tmp_path / "promotions"
	# a record exists but its candidate does NOT match the live baseline
	rec = make_chain_record(base, promotions)
	agg = rec / "candidate" / "aggregate.json"
	data = json.loads(agg.read_text())
	data["counters"]["events"] = 99
	agg.write_text(json.dumps(data, indent=2, sort_keys=True))
	record = tmp_path / "chain-record"
	assert run_tool_draft_chain(run, record, base, promotions) == 1
	assert not record.exists()


def test_draft_chain_missing_promotions_dir_rejected(world, tmp_path):
	base, run, approval = world
	record = tmp_path / "chain-record"
	assert run_tool_draft_chain(run, record, base,
	                            tmp_path / "no-such-dir") == 1
	assert not record.exists()


def test_draft_chain_unapproved_record_rejected(world, tmp_path):
	"""A matching but DRAFT-state record is not authoritative."""
	base, run, approval = world
	promotions = tmp_path / "promotions"
	rec = make_chain_record(base, promotions)
	(rec / "approval.json").rename(rec / "approval-DRAFT.json")
	record = tmp_path / "chain-record"
	assert run_tool_draft_chain(run, record, base, promotions) == 1
	assert not record.exists()


def test_draft_chain_ambiguous_records_rejected(world, tmp_path):
	base, run, approval = world
	promotions = tmp_path / "promotions"
	make_chain_record(base, promotions, "0.0.1-prior")
	make_chain_record(base, promotions, "0.0.1-duplicate")
	record = tmp_path / "chain-record"
	assert run_tool_draft_chain(run, record, base, promotions) == 1
	assert not record.exists()


def test_draft_chain_tampered_counters_rejected(world, tmp_path):
	"""The record's own approval hash-pins its fixture-counters."""
	base, run, approval = world
	promotions = tmp_path / "promotions"
	rec = make_chain_record(base, promotions)
	fc = rec / "candidate" / "fixture-counters.json"
	data = json.loads(fc.read_text())
	data["fixtures"]["a"]["events"] = 101
	fc.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
	record = tmp_path / "chain-record"
	assert run_tool_draft_chain(run, record, base, promotions) == 1
	assert not record.exists()


def test_draft_chain_wrong_fixture_set_rejected(world, tmp_path):
	"""Counters must cover exactly the baseline's compiled set."""
	base, run, approval = world
	promotions = tmp_path / "promotions"
	rec = make_chain_record(base, promotions)
	fc = rec / "candidate" / "fixture-counters.json"
	data = json.loads(fc.read_text())
	data["fixtures"]["renamed"] = data["fixtures"].pop("a")
	fc.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
	# re-pin the hash so ONLY the set check can reject
	app = json.loads((rec / "approval.json").read_text())
	app["candidate"]["fixture_counters_sha256"] = sha(fc)
	(rec / "approval.json").write_text(json.dumps(app, indent=2))
	record = tmp_path / "chain-record"
	assert run_tool_draft_chain(run, record, base, promotions) == 1
	assert not record.exists()


def test_draft_explicit_predecessor_run_still_bootstraps(world, tmp_path):
	"""--predecessor-run bypasses the chain (baselines predating
	record-keeping) — even with NO promotions dir in sight."""
	base, run, approval = world
	record = tmp_path / "boot-record"
	assert run_tool_draft(run, record, base) == 0


# ── removed-fixture attribution (0.33.93 promotion surfaced this) ────
# The first removal-bearing promotion failed apply-time re-proof:
# attribution modeled added fixtures but never SUBTRACTED removed ones.
# Pinned here: a removal world drafts removed_fixture_contributions,
# re-proves residual zero through --apply, and a tampered/mismatched
# removed set fails closed.


def _removal_world(tmp_path: Path):
	"""Baseline carries compiled fixtures a+gone; the run keeps a
	(unchanged) and REMOVES gone (counters must be subtracted)."""
	base = tmp_path / "baseline"
	run = tmp_path / "run"
	fixtures_b = [{"name": "a", "sha256": "1" * 64},
	              {"name": "gone", "sha256": "9" * 64},
	              {"name": "bad", "sha256": "2" * 64}]
	write_side(base, {"events": 130, "fns": 12, "unclassified": 0},
	           fixtures_b, ["a", "gone"], ["bad"],
	           per_fixture={"a": {"events": 100, "fns": 10},
	                        "gone": {"events": 30, "fns": 2}})
	fixtures_n = [{"name": "a", "sha256": "1" * 64},
	              {"name": "bad", "sha256": "2" * 64}]
	write_side(run, {"events": 100, "fns": 10, "unclassified": 0},
	           fixtures_n, ["a"], ["bad"],
	           per_fixture={"a": {"events": 100, "fns": 10}})
	pred_run = tmp_path / "pred-run"
	pred_run.mkdir()
	for name in ("aggregate.json", "manifest.json", "metadata.json"):
		(pred_run / name).write_bytes((base / name).read_bytes())
	(pred_run / "audit").mkdir()
	for fx in ("a", "gone"):
		(pred_run / "audit" / f"{fx}.jsonl").write_bytes(
			(base / "audit" / f"{fx}.jsonl").read_bytes())
	return base, run, pred_run


def _main_rc(argv: list) -> int:
	try:
		return promote.main(argv)
	except SystemExit as e:
		return int(e.code or 0)


def test_removed_fixture_attribution_round_trip(tmp_path) -> None:
	base, run, pred_run = _removal_world(tmp_path)
	record = tmp_path / "removal-record"
	rc = _main_rc([str(run), str(record), "--draft",
	                   "--predecessor-run", str(pred_run),
	                   "--baseline-dir", str(base)])
	assert rc == 0
	draft = json.loads((record / "approval-DRAFT.json").read_text())
	assert draft["expected_universe"]["compiled_removed"] == ["gone"]
	assert draft["attribution_facts"]["removed_fixture_contributions"] == {
		"gone": {"events": 30, "fns": 2}}
	assert draft["expected_counter_deltas"] == {"events": -30, "fns": -2}
	# Approve by rename; apply must RE-PROVE residual zero WITH the
	# removed contribution subtracted.
	(record / "approval-DRAFT.json").rename(record / "approval.json")
	rc = _main_rc([str(record / "candidate"),
	                   str(record / "approval.json"),
	                   "--baseline-dir", str(base), "--apply"])
	assert rc == 0


def test_removed_fixture_facts_tamper_fails_closed(tmp_path) -> None:
	base, run, pred_run = _removal_world(tmp_path)
	record = tmp_path / "removal-record2"
	assert _main_rc([str(run), str(record), "--draft",
	                     "--predecessor-run", str(pred_run),
	                     "--baseline-dir", str(base)]) == 0
	# Drop the removed entry from the facts — the pre-fix bug shape.
	app_path = record / "approval-DRAFT.json"
	app = json.loads(app_path.read_text())
	app["attribution_facts"]["removed_fixture_contributions"] = {}
	app_path.write_text(json.dumps(app, indent=2))
	(record / "approval-DRAFT.json").rename(record / "approval.json")
	rc = _main_rc([str(record / "candidate"),
	                   str(record / "approval.json"),
	                   "--baseline-dir", str(base), "--apply"])
	assert rc == 1
