# Ownership-corpus reviewed-baseline PROMOTION tool.
#
# Materializes an ALREADY-APPROVED promotion; it cannot approve or
# bless its own input.  The review stamp lives in a separate APPROVAL
# FILE that pins, ahead of time: the predecessor baseline's artifact
# hashes, the candidate run's artifact hashes, the exact expected
# universe change, and the exact expected counter deltas.  The tool
# fails closed on ANY divergence.
#
# Usage:
#   drift_corpus_promote.py <run-dir> <approval-file> [--apply]
#                           [--baseline-dir DIR]
#   drift_corpus_promote.py <candidate-run-dir> <promotion-record-dir>
#                           --draft [--predecessor-run <run-dir>]
#                           [--promotions-dir DIR] [--baseline-dir DIR]
#
#   --draft: PROMOTION-RECORD generation — creates a durable,
#     self-contained record directory:
#         <record>/approval-DRAFT.json
#         <record>/predecessor/{aggregate,manifest,metadata,
#                                fixture-counters}.json
#         <record>/candidate/{aggregate,manifest,metadata,
#                              fixture-counters}.json
#     predecessor artifacts are copies of the LIVE baseline.  The
#     predecessor's per-fixture evidence comes from the CHECKED-IN
#     record chain: the one approved record under --promotions-dir
#     whose candidate/ is byte-equal to the live baseline (it IS the
#     run behind it), its fixture-counters verified against the hash
#     its own approval pinned.  A clone of the repo therefore carries
#     everything a draft needs; retained run dirs do not survive one.
#     --predecessor-run is the BOOTSTRAP escape hatch for a baseline
#     predating record-keeping: a retained raw-log run dir byte-equal
#     to the live baseline (aggregate + manifest), from which the
#     counters are extracted directly.
#     Candidate fixture-counters.json is the COMPACT extraction of
#     the one aggregate record per compiled fixture from the run's
#     raw audit logs (the raw logs need not be preserved).  The
#     approval draft pins every evidence hash and carries the
#     machine-computed attribution_facts (modal delta, outliers,
#     new-fixture contributions); promotion later RE-PROVES residual
#     zero from the checked-in evidence.  The generated draft is
#     COMPLETE — including baseline_md, composed mechanically from
#     the recorded facts.  State is the FILENAME ONLY: the reviewer
#     approves by renaming approval-DRAFT.json to approval.json (Git
#     records identity/date); --apply refuses the DRAFT name.
#
#   * DRY-RUN BY DEFAULT: prints every check and what would be
#     written; --apply is required to touch the baseline.
#   * Never selects "latest", never generates a corpus: the run dir
#     is explicit AND hash-pinned by the approval file.
#   * Writes ONLY the four reviewed-baseline files: aggregate.json,
#     manifest.json, metadata.json (copied verbatim from the
#     candidate) and BASELINE.md (regenerated from the approval).
#   * After writing, performs the EXACT zero-delta comparison
#     (drift_corpus_audit._compare --require-zero-delta semantics)
#     between the new baseline and the candidate run; a nonzero
#     result is a hard failure.
#   * NEVER called by `just test`, `just certify`, or
#     run-all-tests.sh — promotion is a reviewed, manual act.
#
# Approval-file schema (JSON; every field required unless noted):
# APPROVAL STATE IS THE EXACT FILENAME (reviewer identity and date
# come from Git history — the commit that renames the file):
#   approval-DRAFT.json  = pending  — dry-run allowed, --apply refused
#   approval.json        = approved — --apply allowed
#   any other filename, or BOTH files present in one directory,
#   fails closed.
# The reviewer's ONLY mutation is the rename; no JSON edits.
# Legacy records may carry status/approved_by/date fields — they are
# INERT historical data; authority comes only from the filename.
#
# {
#   "approval": "ownership-corpus-promotion",
#   "predecessor": {"aggregate_sha256": ..., "manifest_sha256": ...,
#                    "metadata_sha256": ...},
#   "candidate": {"run_dir": "<exact path>", "aggregate_sha256": ...,
#                  "manifest_sha256": ..., "metadata_sha256": ...},
#   "expected_universe": {
#     "compiled_added": [...], "compiled_removed": [...],
#     "failed_added": [...], "failed_removed": [...],
#     "prehash_changes": [...],
#     "compiled_count": N, "failed_count": N, "excluded_count": N},
#   "expected_counter_deltas": {"<counter>": <exact delta>, ...}
#     — EXACTLY the nonzero deltas; any unexplained or mismatched
#       delta fails,
#   "counter_keys_added": [...], "counter_keys_removed": [...]
#     (optional, default []) — the counter-key SETS must otherwise be
#       identical; a key appearing/disappearing (even zero-valued)
#       is a schema change and must be explicitly approved here,
#   "baseline_md": {"title": ..., "predecessor_description": ...,
#                    "attribution": ...}  — markdown fragments for the
#       regenerated BASELINE.md
# }
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / "lang" / "tests" / "ownership_corpus" / "reviewed-baseline"
ARTIFACTS = ("aggregate.json", "manifest.json", "metadata.json")


def _load_audit_tool():
	spec = importlib.util.spec_from_file_location(
		"drift_corpus_audit", Path(__file__).resolve().parent / "drift_corpus_audit.py")
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	return mod


def fail(msg: str) -> "NoReturn":
	print(f"PROMOTE FAIL-CLOSED: {msg}", file=sys.stderr)
	sys.exit(1)


def sha256_file(p: Path) -> str:
	return hashlib.sha256(p.read_bytes()).hexdigest()


def load_json(p: Path, what: str):
	if not p.exists():
		fail(f"{what} missing: {p}")
	try:
		return json.loads(p.read_text())
	except json.JSONDecodeError as e:
		fail(f"{what} malformed JSON: {p}: {e}")


def require(cond: bool, msg: str) -> None:
	if not cond:
		fail(msg)


DRAFT_NAME = "approval-DRAFT.json"
APPROVED_NAME = "approval.json"


def approval_state(approval_path: Path) -> str:
	"""Authority is the EXACT filename.  Both-present is ambiguous and
	fails closed; any other name fails closed."""
	name = approval_path.name
	require(name in (DRAFT_NAME, APPROVED_NAME),
	        f"approval filename must be exactly {DRAFT_NAME!r} (pending) or "
	        f"{APPROVED_NAME!r} (approved); got {name!r}")
	sibling = approval_path.parent / (
		APPROVED_NAME if name == DRAFT_NAME else DRAFT_NAME)
	require(not sibling.exists(),
	        f"ambiguous approval state: both {DRAFT_NAME} and "
	        f"{APPROVED_NAME} present in {approval_path.parent}")
	return "approved" if name == APPROVED_NAME else "pending"


def validate_approval(app: dict) -> None:
	require(isinstance(app, dict) and app.get("approval") == "ownership-corpus-promotion",
	        "approval file: wrong or missing 'approval' kind")
	# status/approved_by/date are NOT part of the schema: legacy
	# records may carry them as inert historical data; authority is
	# the filename, and reviewer identity/date come from Git history.
	for side in ("predecessor", "candidate"):
		blk = app.get(side)
		require(isinstance(blk, dict), f"approval file: missing '{side}' block")
		for k in ("aggregate_sha256", "manifest_sha256", "metadata_sha256"):
			require(isinstance(blk.get(k), str) and len(blk[k]) == 64,
			        f"approval file: {side}.{k} must be a sha256 hex digest")
	require(isinstance(app["candidate"].get("run_dir"), str),
	        "approval file: candidate.run_dir missing")
	if "attribution_facts" in app:
		facts = app["attribution_facts"]
		require(isinstance(facts, dict)
		        and isinstance(facts.get("modal"), dict)
		        and isinstance(facts.get("modal_fixture_count"), int)
		        and isinstance(facts.get("outliers", {}), dict)
		        and isinstance(facts.get("new_fixture_contributions"), dict),
		        "approval file: malformed attribution_facts")
		for side in ("predecessor", "candidate"):
			require(isinstance(app[side].get("fixture_counters_sha256"), str)
			        and len(app[side]["fixture_counters_sha256"]) == 64,
			        f"approval file: {side}.fixture_counters_sha256 required "
			        f"when attribution_facts present")
	uni = app.get("expected_universe")
	require(isinstance(uni, dict), "approval file: expected_universe missing")
	for k in ("compiled_added", "compiled_removed", "failed_added",
	          "failed_removed", "prehash_changes"):
		require(isinstance(uni.get(k), list),
		        f"approval file: expected_universe.{k} must be a list")
	for k in ("compiled_count", "failed_count", "excluded_count"):
		require(isinstance(uni.get(k), int),
		        f"approval file: expected_universe.{k} must be an int")
	require(uni.get("excluded_changed") in (None, False),
	        "approval file: excluded-population changes are NOT supported "
	        "by this tool (excluded_changed must be absent or false)")
	deltas = app.get("expected_counter_deltas")
	require(isinstance(deltas, dict), "approval file: expected_counter_deltas missing")
	for k in ("counter_keys_added", "counter_keys_removed"):
		require(isinstance(app.get(k, []), list),
		        f"approval file: {k} must be a list when present")
	for k, v in deltas.items():
		require(isinstance(v, int) and v != 0,
		        f"approval file: expected_counter_deltas[{k!r}] must be a "
		        f"NONZERO int (zero deltas are implicit)")
	md = app.get("baseline_md")
	require(isinstance(md, dict), "approval file: baseline_md missing")
	for k in ("title", "predecessor_description", "attribution"):
		require(isinstance(md.get(k), str) and md[k],
		        f"approval file: baseline_md.{k} missing")


def extract_fixture_counters(run_dir: Path, compiled: list) -> dict:
	"""Compact per-fixture evidence: EXACTLY ONE well-formed aggregate
	record per compiled fixture, reduced to its integer counters.
	Fail-closed on malformed/missing/multiple records."""
	out = {}
	for name in sorted(compiled):
		f = run_dir / "audit" / f"{name}.jsonl"
		require(f.exists(), f"extraction: missing audit file for {name}")
		aggs = []
		for lineno, line in enumerate(f.read_text().splitlines(), 1):
			start = line.find("{")
			require(start >= 0, f"extraction: {name}:{lineno}: no JSON object")
			try:
				rec = json.loads(line[start:])
			except json.JSONDecodeError as e:
				fail(f"extraction: {name}:{lineno}: malformed JSON ({e})")
			if rec.get("record") == "aggregate":
				aggs.append(rec)
		require(len(aggs) == 1,
		        f"extraction: {name}: expected exactly 1 aggregate record, "
		        f"found {len(aggs)}")
		out[name] = {k: v for k, v in sorted(aggs[0].items())
		             if k != "record" and isinstance(v, int)}
	return {"record": "fixture-counters", "fixtures": out}


def resolve_predecessor_record(promotions_dir: Path,
                               baseline_dir: Path) -> Path:
	"""Locate THE approved checked-in promotion record whose candidate
	is byte-equal to the live baseline — that record's candidate IS
	the run behind the baseline, and a clone carries it."""
	require(promotions_dir.is_dir(),
	        f"promotions dir not found: {promotions_dir} — for a "
	        f"baseline predating record-keeping, pass --predecessor-run "
	        f"(raw-log bootstrap path)")
	base_agg = (baseline_dir / "aggregate.json").read_bytes()
	base_man = (baseline_dir / "manifest.json").read_bytes()
	matches = []
	for rec in sorted(p for p in promotions_dir.iterdir() if p.is_dir()):
		cand = rec / "candidate"
		if not ((cand / "aggregate.json").exists()
		        and (cand / "manifest.json").exists()):
			continue
		if ((cand / "aggregate.json").read_bytes() == base_agg
				and (cand / "manifest.json").read_bytes() == base_man):
			matches.append(rec)
	require(len(matches) == 1,
	        f"expected exactly ONE checked-in record whose candidate "
	        f"byte-equals the live baseline, found {len(matches)}"
	        + (f": {sorted(m.name for m in matches)}" if matches else
	           f" under {promotions_dir} — for a baseline predating "
	           f"record-keeping, pass --predecessor-run"))
	rec = matches[0]
	require((rec / APPROVED_NAME).exists(),
	        f"record {rec.name!r} matches the live baseline but is not "
	        f"approved ({APPROVED_NAME} missing) — only an APPLIED "
	        f"promotion's record is authoritative")
	return rec


def load_record_fixture_counters(rec: Path, compiled: list) -> dict:
	"""Predecessor per-fixture evidence from the checked-in record,
	verified against the hash its own approval pinned."""
	app = load_json(rec / APPROVED_NAME, f"record {rec.name} approval")
	fc_path = rec / "candidate" / "fixture-counters.json"
	require(fc_path.exists(),
	        f"record {rec.name} missing candidate fixture-counters.json")
	want = app.get("candidate", {}).get("fixture_counters_sha256")
	require(isinstance(want, str) and len(want) == 64,
	        f"record {rec.name} approval lacks "
	        f"candidate.fixture_counters_sha256")
	got = sha256_file(fc_path)
	require(got == want,
	        f"record {rec.name} fixture-counters sha256 {got[:16]}... "
	        f"!= pinned {want[:16]}...")
	data = load_json(fc_path, f"record {rec.name} fixture-counters")
	require(data.get("record") == "fixture-counters",
	        f"record {rec.name} fixture-counters wrong record kind")
	require(sorted(data.get("fixtures", {})) == list(compiled),
	        f"record {rec.name} fixture-counters cover a different "
	        f"compiled set than the live baseline")
	return data


def verify_attribution(app: dict, approval_dir: Path,
                       base_agg: dict, new_agg: dict,
                       added_ok: list, removed_ok: list) -> None:
	"""Re-prove the per-fixture attribution from the COMPACT checked-in
	evidence: hash-pinned fixture-counters on both sides, exact modal
	delta on every shared fixture, exact approved outliers, exact
	new-fixture contributions, and RESIDUAL ZERO against the aggregate
	deltas."""
	facts = app["attribution_facts"]
	sides = {}
	for side in ("predecessor", "candidate"):
		fc_path = approval_dir / side / "fixture-counters.json"
		require(fc_path.exists(), f"attribution: missing {fc_path}")
		got = sha256_file(fc_path)
		want = app[side]["fixture_counters_sha256"]
		require(got == want,
		        f"attribution: {side} fixture-counters sha256 {got[:16]}... "
		        f"!= pinned {want[:16]}...")
		data = load_json(fc_path, f"{side} fixture-counters")
		require(data.get("record") == "fixture-counters",
		        f"attribution: {side} fixture-counters wrong record kind")
		sides[side] = data["fixtures"]
	shared = sorted(set(sides["predecessor"]) & set(sides["candidate"]))
	modal = {k: int(v) for k, v in facts["modal"].items()}
	outliers_expected = {n: {k: int(v) for k, v in d.items()}
	                     for n, d in facts.get("outliers", {}).items()}
	outliers_found = {}
	for name in shared:
		old_c = sides["predecessor"][name]
		new_c = sides["candidate"][name]
		delta = {}
		for k in set(old_c) | set(new_c):
			dv = new_c.get(k, 0) - old_c.get(k, 0)
			if dv:
				delta[k] = dv
		if delta != modal:
			outliers_found[name] = delta
	require(outliers_found == outliers_expected,
	        f"attribution: outliers differ from approval:\n"
	        f"  found    {outliers_found}\n  approved {outliers_expected}")
	require(len(shared) - len(outliers_found) == int(facts["modal_fixture_count"]),
	        f"attribution: modal fixture count "
	        f"{len(shared) - len(outliers_found)} != approved "
	        f"{facts['modal_fixture_count']}")
	new_contribs = {}
	for name in added_ok:
		require(name in sides["candidate"],
		        f"attribution: added fixture {name} missing from candidate "
		        f"fixture-counters")
		new_contribs[name] = {k: v for k, v in
		                      sides["candidate"][name].items() if v}
	facts_new = {n: {k: int(v) for k, v in d.items()}
	             for n, d in facts["new_fixture_contributions"].items()}
	require(new_contribs == facts_new,
	        f"attribution: new-fixture contributions differ:\n"
	        f"  found    {new_contribs}\n  approved {facts_new}")
	removed_contribs = {}
	for name in removed_ok:
		require(name in sides["predecessor"],
		        f"attribution: removed fixture {name} missing from "
		        f"predecessor fixture-counters")
		removed_contribs[name] = {k: v for k, v in
		                          sides["predecessor"][name].items() if v}
	facts_removed = {n: {k: int(v) for k, v in d.items()}
	                 for n, d in facts.get(
	                     "removed_fixture_contributions", {}).items()}
	require(removed_contribs == facts_removed,
	        f"attribution: removed-fixture contributions differ:\n"
	        f"  found    {removed_contribs}\n  approved {facts_removed}")
	# RESIDUAL ZERO: aggregate delta == modal*count + outlier deltas +
	# new-fixture contributions, per counter
	bc, nc = base_agg["counters"], new_agg["counters"]
	for k in sorted(set(bc) | set(nc)):
		agg_delta = nc.get(k, 0) - bc.get(k, 0)
		explained = modal.get(k, 0) * int(facts["modal_fixture_count"])
		for d in outliers_found.values():
			explained += d.get(k, 0)
		for d in new_contribs.values():
			explained += d.get(k, 0)
		for d in removed_contribs.values():
			explained -= d.get(k, 0)
		require(agg_delta == explained,
		        f"attribution: RESIDUAL NONZERO on {k}: aggregate "
		        f"{agg_delta:+d} vs explained {explained:+d}")
	print(f"attribution OK: modal on {facts['modal_fixture_count']} shared "
	      f"fixtures, {len(outliers_found)} outliers, "
	      f"{len(new_contribs)} new / {len(removed_contribs)} removed "
	      f"fixtures, residual ZERO on every counter")


def check_universe_integrity(side: str, u: dict) -> None:
	"""Shared fixture-integrity validator (draft AND promotion paths):
	unique fixture names, duplicate-free and disjoint partitions, and
	fixtures == compiled_ok ∪ failed."""
	names = [f["name"] for f in u["fixtures"]]
	require(len(names) == len(set(names)),
	        f"{side} universe: duplicate fixture names")
	cset, fset = set(u["compiled_ok"]), set(u["failed"])
	require(len(u["compiled_ok"]) == len(cset)
	        and len(u["failed"]) == len(fset),
	        f"{side} universe: duplicate entries in partition lists")
	require(not (cset & fset),
	        f"{side} universe: compiled_ok and failed overlap: "
	        f"{sorted(cset & fset)}")
	require(set(names) == cset | fset,
	        f"{side} universe: fixtures != compiled_ok ∪ failed "
	        f"(orphans: {sorted(set(names) ^ (cset | fset))})")


def generate_draft(audit, run_dir: Path, baseline_dir: Path,
                   record_dir: Path, predecessor_run: Path,
                   promotions_dir: Path) -> int:
	"""Build the durable promotion record (facts only)."""
	require(not record_dir.exists(),
	        f"promotion record already exists (non-overwriting): {record_dir}")
	# Predecessor evidence: the checked-in record chain by default
	# (clone-sufficient); --predecessor-run is the raw-log bootstrap
	# escape hatch for a baseline predating record-keeping.
	pred_record = None
	if predecessor_run is None:
		pred_record = resolve_predecessor_record(promotions_dir, baseline_dir)
	for name in ARTIFACTS:
		require((baseline_dir / name).exists(), f"baseline missing {name}")
		require((run_dir / name).exists(), f"candidate missing {name}")
		if predecessor_run is not None:
			require((predecessor_run / name).exists(),
			        f"predecessor run missing {name}")
	base_man = load_json(baseline_dir / "manifest.json", "baseline manifest")
	new_man = load_json(run_dir / "manifest.json", "candidate manifest")
	base_agg = load_json(baseline_dir / "aggregate.json", "baseline aggregate")
	new_agg = load_json(run_dir / "aggregate.json", "candidate aggregate")
	if predecessor_run is not None:
		# bootstrap-path identity: the run must BE the run behind the
		# live baseline (aggregate + manifest byte-equal)
		require((predecessor_run / "aggregate.json").read_bytes()
		        == (baseline_dir / "aggregate.json").read_bytes(),
		        "--predecessor-run aggregate != live baseline aggregate")
		require((predecessor_run / "manifest.json").read_bytes()
		        == (baseline_dir / "manifest.json").read_bytes(),
		        "--predecessor-run manifest != live baseline manifest")
	try:
		audit._validate_universe_schema("baseline", base_man["universe"])
		audit._validate_universe_schema("candidate", new_man["universe"])
		audit._validate_counters_schema("baseline", base_agg["counters"])
		audit._validate_counters_schema("candidate", new_agg["counters"])
	except (ValueError, KeyError, TypeError) as e:
		fail(f"schema validation: {e}")
	gates = audit._hard_gate_failures(new_agg["counters"])
	require(not gates, f"candidate hard gates nonzero: {gates}")
	bu, nu = base_man["universe"], new_man["universe"]
	for side, u in (("baseline", bu), ("candidate", nu)):
		check_universe_integrity(side, u)
	require(bu["inclusion_rule"] == nu["inclusion_rule"],
	        "inclusion_rule changed — the promotion tool does not support "
	        "this; no draft written")
	b_exc = sorted((e["name"], e.get("reason", "")) for e in bu["excluded"])
	n_exc = sorted((e["name"], e.get("reason", "")) for e in nu["excluded"])
	require(b_exc == n_exc,
	        "excluded population changed — the promotion tool does not "
	        "support this; no draft written")
	b_ok, n_ok = set(bu["compiled_ok"]), set(nu["compiled_ok"])
	b_f, n_f = set(bu["failed"]), set(nu["failed"])
	b_hash = {f["name"]: f["sha256"] for f in bu["fixtures"]}
	n_hash = {f["name"]: f["sha256"] for f in nu["fixtures"]}
	bc, nc = base_agg["counters"], new_agg["counters"]
	deltas = {k: nc.get(k, 0) - bc.get(k, 0)
	          for k in sorted(set(bc) | set(nc))
	          if nc.get(k, 0) - bc.get(k, 0)}

	# compact per-fixture evidence (fail-closed): record chain, or raw
	# logs on the bootstrap path
	if pred_record is not None:
		pred_fc = load_record_fixture_counters(pred_record, sorted(b_ok))
	else:
		pred_fc = extract_fixture_counters(predecessor_run, sorted(b_ok))
	cand_fc = extract_fixture_counters(run_dir, sorted(n_ok))

	# attribution facts: per-fixture deltas over shared fixtures
	from collections import Counter as _Tally
	shared = sorted(b_ok & n_ok)
	per_fix = {}
	for name in shared:
		oc, ncs = pred_fc["fixtures"][name], cand_fc["fixtures"][name]
		d = {k: ncs.get(k, 0) - oc.get(k, 0)
		     for k in set(oc) | set(ncs)
		     if ncs.get(k, 0) - oc.get(k, 0)}
		per_fix[name] = tuple(sorted(d.items()))
	tally = _Tally(per_fix.values())
	modal_t, _n = tally.most_common(1)[0] if per_fix else ((), 0)
	modal = dict(modal_t)
	outliers = {name: dict(d) for name, d in per_fix.items() if d != modal_t}
	new_contribs = {name: {k: v for k, v in
	                        cand_fc["fixtures"][name].items() if v}
	                for name in sorted(n_ok - b_ok)}
	removed_contribs = {name: {k: v for k, v in
	                            pred_fc["fixtures"][name].items() if v}
	                    for name in sorted(b_ok - n_ok)}

	# materialize the record
	(record_dir / "predecessor").mkdir(parents=True)
	(record_dir / "candidate").mkdir()
	for name in ARTIFACTS:
		shutil.copyfile(baseline_dir / name, record_dir / "predecessor" / name)
		shutil.copyfile(run_dir / name, record_dir / "candidate" / name)
	(record_dir / "predecessor" / "fixture-counters.json").write_text(
		json.dumps(pred_fc, indent=1, sort_keys=True) + "\n")
	(record_dir / "candidate" / "fixture-counters.json").write_text(
		json.dumps(cand_fc, indent=1, sort_keys=True) + "\n")

	cand_dir = record_dir / "candidate"
	# COMPLETE baseline_md, mechanically composed from the recorded
	# facts — the reviewer's only mutation is the rename.
	env = new_man.get("environment", {})
	pred_env = base_man.get("environment", {})
	pred_meta = load_json(
		(pred_record / "candidate" if pred_record is not None
		 else predecessor_run) / "metadata.json",
		"predecessor metadata")
	if modal:
		modal_txt = (", ".join(f"{k} {v:+d}" for k, v in sorted(modal.items()))
		             + f" on all {len(shared) - len(outliers)} shared fixtures")
	else:
		modal_txt = f"no shared-fixture drift ({len(shared)} fixtures unchanged)"
	outlier_txt = (f"{len(outliers)} outlier fixture(s): "
	               + "; ".join(f"{n} {dict(d)}" for n, d in sorted(outliers.items()))
	               if outliers else "zero outliers")
	new_txt = ("; ".join(f"{n} contributes {d}" for n, d in
	           sorted(new_contribs.items()))
	           if new_contribs else "no new fixtures")
	removed_txt = ("; ".join(f"{n} withdrew {d}" for n, d in
	               sorted(removed_contribs.items()))
	               if removed_contribs else "no removed fixtures")
	auto_md = {
		"title": (f"The checked-in reference for `just ownership-corpus-check` "
		          f"— candidate driftc {env.get('driftc_version', '?')} / "
		          f"ABI {env.get('abi', '?')}: {len(n_ok)} compiled, "
		          f"{len(n_f)} compile-failed, {len(nu['excluded'])} "
		          f"rule-excluded."),
		"predecessor_description": (
			f"The prior reviewed baseline (driftc "
			f"{pred_env.get('driftc_version', '?')} / ABI "
			f"{pred_env.get('abi', '?')}; origin run started_unix "
			f"{pred_meta.get('started_unix', '?')}), preserved verbatim in "
			f"this record's predecessor/ directory; earlier chain in the Git "
			f"history of reviewed-baseline/BASELINE.md."),
		"attribution": (
			f"Machine attribution_facts in this approval, re-proven from the "
			f"record's fixture-counters on every dry-run and apply: modal "
			f"delta {modal_txt}; {outlier_txt}; {new_txt}; {removed_txt}.  "
			f"Residual zero on every counter; hard gates zero."),
	}
	draft = {
		"approval": "ownership-corpus-promotion",
		"predecessor": {
			"aggregate_sha256": sha256_file(record_dir / "predecessor" / "aggregate.json"),
			"manifest_sha256": sha256_file(record_dir / "predecessor" / "manifest.json"),
			"metadata_sha256": sha256_file(record_dir / "predecessor" / "metadata.json"),
			"fixture_counters_sha256": sha256_file(
				record_dir / "predecessor" / "fixture-counters.json"),
		},
		"candidate": {
			"run_dir": str(cand_dir),
			"aggregate_sha256": sha256_file(cand_dir / "aggregate.json"),
			"manifest_sha256": sha256_file(cand_dir / "manifest.json"),
			"metadata_sha256": sha256_file(cand_dir / "metadata.json"),
			"fixture_counters_sha256": sha256_file(
				cand_dir / "fixture-counters.json"),
		},
		"expected_universe": {
			"compiled_added": sorted(n_ok - b_ok),
			"compiled_removed": sorted(b_ok - n_ok),
			"failed_added": sorted(n_f - b_f),
			"failed_removed": sorted(b_f - n_f),
			"prehash_changes": sorted(
				n for n in b_hash if n in n_hash and b_hash[n] != n_hash[n]),
			"compiled_count": len(n_ok),
			"failed_count": len(n_f),
			"excluded_count": len(nu["excluded"]),
		},
		"expected_counter_deltas": deltas,
		"counter_keys_added": sorted(set(nc) - set(bc)),
		"counter_keys_removed": sorted(set(bc) - set(nc)),
		"attribution_facts": {
			"modal": modal,
			"modal_fixture_count": len(shared) - len(outliers),
			"outliers": outliers,
			"new_fixture_contributions": new_contribs,
			"removed_fixture_contributions": removed_contribs,
		},
		"baseline_md": auto_md,
	}
	(record_dir / DRAFT_NAME).write_text(
		json.dumps(draft, indent=2) + "\n")
	print(f"PROMOTION RECORD written (complete; pending by filename): {record_dir}")
	print(f"  modal {modal} on {len(shared) - len(outliers)}/{len(shared)} "
	      f"shared fixtures; {len(outliers)} outliers; "
	      f"{len(new_contribs)} new fixtures")
	print(f"Reviewer approves by RENAMING {DRAFT_NAME} to {APPROVED_NAME} — "
	      f"no JSON edits; identity/date are recorded by the Git commit.")
	return 0


def main(argv=None) -> int:
	ap = argparse.ArgumentParser(
		description="Materialize an APPROVED ownership-corpus baseline "
		            "promotion (dry-run by default)")
	ap.add_argument("run_dir", help="the approved retained corpus run dir")
	ap.add_argument("approval_file", help="reviewed approval JSON")
	ap.add_argument("--apply", action="store_true",
	                help="actually write the baseline (default: dry-run)")
	ap.add_argument("--draft", action="store_true",
	                help="promotion-record generation: build a durable "
	                     "record dir at <approval-file> (must not exist) "
	                     "instead of promoting")
	ap.add_argument("--predecessor-run", default=None,
	                help="(--draft only) BOOTSTRAP escape hatch: retained "
	                     "raw-log predecessor run dir, for a baseline "
	                     "predating record-keeping (default: predecessor "
	                     "evidence comes from the checked-in record chain)")
	ap.add_argument("--promotions-dir", default=None,
	                help="(--draft only) checked-in promotion-records dir "
	                     "(default: <baseline-dir>/../promotions)")
	ap.add_argument("--baseline-dir", default=str(DEFAULT_BASELINE))
	args = ap.parse_args(argv)

	audit = _load_audit_tool()
	run_dir = Path(args.run_dir)
	baseline_dir = Path(args.baseline_dir)
	approval_path = Path(args.approval_file)

	if args.draft:
		require(not args.apply, "--draft and --apply are mutually exclusive")
		return generate_draft(audit, run_dir, baseline_dir, approval_path,
		                      Path(args.predecessor_run)
		                      if args.predecessor_run else None,
		                      Path(args.promotions_dir)
		                      if args.promotions_dir
		                      else baseline_dir.parent / "promotions")

	app = load_json(approval_path, "approval file")
	validate_approval(app)

	# ── approval state = EXACT FILENAME ─────────────────────────────
	state = approval_state(approval_path)
	if args.apply:
		require(state == "approved",
		        f"--apply requires the approved filename "
		        f"({APPROVED_NAME!r}); {approval_path.name!r} is pending — "
		        f"the reviewer approves by RENAMING it")
		for k, v in app["baseline_md"].items():
			require("<<" not in v,
			        f"--apply refuses incomplete baseline_md.{k} "
			        f"(draft placeholder present)")
	elif state == "pending":
		print(f"NOTE: {DRAFT_NAME} — pending; dry-run only.  The reviewer "
		      f"approves by renaming to {APPROVED_NAME} (identity/date are "
		      f"recorded by the Git commit)")

	# ── the run dir is EXPLICIT and must match the approval ─────────
	require(str(run_dir) == app["candidate"]["run_dir"],
	        f"run dir {run_dir} != approved candidate run_dir "
	        f"{app['candidate']['run_dir']} (this tool never selects runs)")

	# ── live-baseline state: predecessor (proposed transition) OR
	#    candidate (auditing an already-promoted checkout) ───────────
	def _matches(block: dict) -> bool:
		for name in ARTIFACTS:
			p = baseline_dir / name
			if not p.exists():
				return False
			key = name.split(".")[0] + "_sha256"
			if sha256_file(p) != block[key]:
				return False
		return True

	audit_mode = False
	predecessor_src = baseline_dir
	if _matches(app["predecessor"]):
		pass  # proposed transition: live baseline == approved predecessor
	elif _matches(app["candidate"]):
		audit_mode = True
		require(not args.apply,
		        "--apply refused: the live baseline ALREADY matches the "
		        "candidate (this checkout is post-promotion; dry-run audit "
		        "only)")
		# the comparison predecessor comes from the promotion RECORD
		# (the live baseline IS the candidate now); its artifacts must
		# match the approval's predecessor pins
		predecessor_src = approval_path.parent / "predecessor"
		require(predecessor_src.is_dir(),
		        "AUDIT MODE needs the promotion record's predecessor/ "
		        "directory next to the approval")
		for name in ARTIFACTS:
			got = sha256_file(predecessor_src / name)
			want = app["predecessor"][name.split(".")[0] + "_sha256"]
			require(got == want,
			        f"AUDIT MODE: record predecessor {name} sha256 "
			        f"{got[:16]}... != approval pin {want[:16]}...")
		print("AUDIT MODE: live baseline already matches the candidate — "
		      "verifying the recorded promotion against the record's "
		      "predecessor")
	else:
		fail("STALE PREDECESSOR: the live baseline matches neither the "
		     "approved predecessor nor the candidate — the baseline changed "
		     "since this approval was written")
	# ── artifact identity: candidate ────────────────────────────────
	for name in ARTIFACTS:
		p = run_dir / name
		require(p.exists(), f"candidate run missing {name}")
		key = name.split(".")[0] + "_sha256"
		got = sha256_file(p)
		want = app["candidate"][key]
		require(got == want,
		        f"CANDIDATE MISMATCH: run {name} sha256 {got[:16]}... != "
		        f"approved {want[:16]}...")
	print(f"artifact identity OK: predecessor ({baseline_dir}) and "
	      f"candidate ({run_dir}) match the approval hashes")

	# ── load + schema-validate both sides (reuse the audit tool) ────
	base_man = load_json(predecessor_src / "manifest.json", "predecessor manifest")
	new_man = load_json(run_dir / "manifest.json", "candidate manifest")
	base_agg = load_json(predecessor_src / "aggregate.json", "predecessor aggregate")
	new_agg = load_json(run_dir / "aggregate.json", "candidate aggregate")
	try:
		audit._validate_universe_schema("baseline", base_man["universe"])
		audit._validate_universe_schema("candidate", new_man["universe"])
		audit._validate_counters_schema("baseline", base_agg["counters"])
		audit._validate_counters_schema("candidate", new_agg["counters"])
	except (ValueError, KeyError, TypeError) as e:
		fail(f"schema validation: {e}")

	bu, nu = base_man["universe"], new_man["universe"]
	uni = app["expected_universe"]

	# ── universe INTEGRITY (shared validator, both sides) ───────────
	for side, u in (("baseline", bu), ("candidate", nu)):
		check_universe_integrity(side, u)

	# ── inclusion rule must be UNCHANGED (gap 3) ────────────────────
	require(bu["inclusion_rule"] == nu["inclusion_rule"],
	        "universe: inclusion_rule changed — not supported by this tool")

	# ── exact universe confirmation ─────────────────────────────────
	b_ok, n_ok = set(bu["compiled_ok"]), set(nu["compiled_ok"])
	b_f, n_f = set(bu["failed"]), set(nu["failed"])
	require(sorted(n_ok - b_ok) == sorted(uni["compiled_added"]),
	        f"universe: compiled additions {sorted(n_ok - b_ok)} != approved "
	        f"{sorted(uni['compiled_added'])}")
	require(sorted(b_ok - n_ok) == sorted(uni["compiled_removed"]),
	        f"universe: compiled removals {sorted(b_ok - n_ok)} != approved")
	require(sorted(n_f - b_f) == sorted(uni["failed_added"]),
	        f"universe: failed additions {sorted(n_f - b_f)} != approved")
	require(sorted(b_f - n_f) == sorted(uni["failed_removed"]),
	        f"universe: failed removals {sorted(b_f - n_f)} != approved")
	require(len(n_ok) == uni["compiled_count"],
	        f"universe: compiled count {len(n_ok)} != approved {uni['compiled_count']}")
	require(len(n_f) == uni["failed_count"],
	        f"universe: failed count {len(n_f)} != approved {uni['failed_count']}")
	require(len(nu["excluded"]) == uni["excluded_count"],
	        f"universe: excluded count {len(nu['excluded'])} != approved")
	b_exc = sorted((e["name"], e.get("reason", "")) for e in bu["excluded"])
	n_exc = sorted((e["name"], e.get("reason", "")) for e in nu["excluded"])
	require(b_exc == n_exc,
	        "universe: excluded population changed (name+reason) — "
	        "excluded changes are not supported by this tool")
	b_hash = {f["name"]: f["sha256"] for f in bu["fixtures"]}
	n_hash = {f["name"]: f["sha256"] for f in nu["fixtures"]}
	changed = sorted(n for n in b_hash if n in n_hash and b_hash[n] != n_hash[n])
	require(changed == sorted(uni["prehash_changes"]),
	        f"universe: pre-existing hash changes {changed} != approved "
	        f"{sorted(uni['prehash_changes'])}")
	print("universe OK: matches the approved expectation exactly")

	# ── counter-key SCHEMA identity (gap 2) ─────────────────────────
	bc, nc = base_agg["counters"], new_agg["counters"]
	keys_added = sorted(set(nc) - set(bc))
	keys_removed = sorted(set(bc) - set(nc))
	require(keys_added == sorted(app.get("counter_keys_added", [])),
	        f"counter schema: keys added {keys_added} != approved "
	        f"{sorted(app.get('counter_keys_added', []))} — a new counter "
	        f"key (even zero-valued) is a schema change requiring explicit "
	        f"approval")
	require(keys_removed == sorted(app.get("counter_keys_removed", [])),
	        f"counter schema: keys removed {keys_removed} != approved "
	        f"{sorted(app.get('counter_keys_removed', []))}")

	# ── exact counter deltas (no unexplained delta survives) ────────
	actual = {}
	for k in sorted(set(bc) | set(nc)):
		d = nc.get(k, 0) - bc.get(k, 0)
		if d:
			actual[k] = d
	expected = dict(app["expected_counter_deltas"])
	require(actual == expected,
	        f"counter deltas differ from the approval:\n"
	        f"  actual   {actual}\n  approved {expected}")
	print(f"counter deltas OK: {len(actual)} nonzero deltas match the "
	      f"approval exactly")

	# ── hard gates ──────────────────────────────────────────────────
	gates = audit._hard_gate_failures(nc)
	require(not gates, f"candidate hard gates nonzero: {gates}")
	print("hard gates OK: all zero")

	# ── per-fixture attribution from checked-in compact evidence ────
	if "attribution_facts" in app:
		verify_attribution(app, approval_path.parent, base_agg, new_agg,
		                   sorted(n_ok - b_ok), sorted(b_ok - n_ok))

	# ── BASELINE.md content (regenerated from the approval) ─────────
	md = app["baseline_md"]
	env = new_man.get("environment", {})
	meta = load_json(run_dir / "metadata.json", "candidate metadata")
	baseline_md = f"""# Reviewed ownership-corpus baseline

{md['title']}

## Provenance

| field | value |
|---|---|
| origin run | retained run dir `{run_dir}`; promoted from the RETAINED artifacts without a rerun |
| driftc / ABI | **{env.get('driftc_version', 'unknown')}** / **ABI {env.get('abi', '?')}** |
| corpus tool | v{env.get('tool_version', '?')} |
| run started_unix | {meta.get('started_unix', '?')} |
| universe | {len(n_ok)} compiled / {len(n_ok) + len(n_f)} discovered ({len(n_f)} compile-failed, {len(nu['excluded'])} rule-excluded) |
| promotion | drift_corpus_promote.py under approval `{approval_path.name}` (full sha256 {sha256_file(approval_path)}); reviewer identity and date are recorded by Git history — the commit that renamed approval-DRAFT.json to approval.json and landed this promotion |

## Predecessor

{md['predecessor_description']}

## Approved deltas and attribution

Counter deltas vs the predecessor (exact, per the approval):

{chr(10).join(f'* `{k}`: {v:+d}' for k, v in sorted(expected.items())) if expected else '* (none — zero-delta promotion)'}

{md['attribution']}

## Update policy

This baseline changes ONLY through `tools/drift_corpus_promote.py`
under a reviewed approval file — dry-run by default, `--apply`
required, artifact hashes pinned on both sides, exact universe and
counter-delta expectations enforced, hard gates zero, and a
post-write exact zero-delta comparison.  Certification NEVER
regenerates or re-blesses it; the promote tool is never invoked by
`just test`, `just certify`, or run-all-tests.sh.  Process
documentation: doc/ownership-corpus-gate.md.
"""

	if not args.apply:
		print("\nDRY-RUN (no --apply): all checks PASSED.  Would write:")
		for name in ARTIFACTS:
			print(f"  {baseline_dir / name}  <- {run_dir / name}")
		print(f"  {baseline_dir / 'BASELINE.md'}  (regenerated, "
		      f"{len(baseline_md)} bytes)")
		return 0

	# ── APPLY (gap 4): stage → validate staged → replace with
	#    rollback protection ─────────────────────────────────────────
	staging = baseline_dir / ".promote-staging.tmp"
	backup = baseline_dir / ".promote-backup.tmp"
	require(not staging.exists() and not backup.exists(),
	        f"staging/backup residue present ({staging.name}/{backup.name}) "
	        f"— a previous promotion was interrupted; inspect and remove "
	        f"manually before retrying")
	staging.mkdir()
	try:
		for name in ARTIFACTS:
			shutil.copyfile(run_dir / name, staging / name)
		(staging / "BASELINE.md").write_text(baseline_md)
		# validate the STAGED baseline before touching the real one
		rc = audit._compare(staging, run_dir, require_zero_delta=True)
		require(rc == 0,
		        f"STAGED baseline failed the exact zero-delta comparison "
		        f"(rc {rc}) — nothing was replaced")
		backup.mkdir()
		for name in ARTIFACTS + ("BASELINE.md",):
			if (baseline_dir / name).exists():
				shutil.copyfile(baseline_dir / name, backup / name)
		try:
			import os
			for name in ARTIFACTS + ("BASELINE.md",):
				os.replace(staging / name, baseline_dir / name)
		except BaseException:
			# rollback: restore every backed-up file
			for name in ARTIFACTS + ("BASELINE.md",):
				if (backup / name).exists():
					shutil.copyfile(backup / name, baseline_dir / name)
			raise
	finally:
		shutil.rmtree(staging, ignore_errors=True)
		shutil.rmtree(backup, ignore_errors=True)
	print(f"baseline written: {baseline_dir}")

	# ── post-write exact zero-delta comparison ──────────────────────
	rc = audit._compare(baseline_dir, run_dir, require_zero_delta=True)
	require(rc == 0,
	        f"POST-WRITE zero-delta comparison FAILED (rc {rc}) — baseline "
	        f"and candidate diverge after promotion")
	print("post-write exact zero-delta comparison: OK")
	print("PROMOTION COMPLETE — commit the baseline change with the candidate.")
	return 0


if __name__ == "__main__":
	sys.exit(main())
