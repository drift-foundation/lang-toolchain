# regex-engine-allocation-removal: corpus delta attribution.
# Compares a fresh measurement run against the reviewed-baseline and
# attributes EVERY counter delta to residual zero:
#   * universe: expects exactly +1 fixture (std_regex_view_offsets_
#     alternation) and hash deltas only where fixtures were
#     intentionally touched (none besides the addition);
#   * per-fixture counter deltas: expects one MODAL delta shared by
#     (nearly) all pre-existing fixtures — the uniform stdlib
#     contribution of the std.regex rewrite — plus individually
#     attributed outliers (fixtures whose own source exercises
#     std.regex) and the new fixture's own contribution;
#   * totals must reconcile: sum(per-fixture deltas) + new-fixture
#     contribution == aggregate delta on EVERY counter, residual 0;
#   * hard gates must be zero in the new run.
from __future__ import annotations

import json
import sys
from collections import Counter as Tally
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BASELINE = ROOT / "lang" / "tests" / "ownership_corpus" / "reviewed-baseline"


def load_fixture_counters(run_dir: Path, names: list[str]) -> dict[str, dict[str, int]]:
	"""FAIL-CLOSED loader: every fixture must yield EXACTLY ONE
	well-formed aggregate record; malformed JSON, a missing record, or
	multiple records reject the run (review blocker 3)."""
	out = {}
	for name in names:
		f = run_dir / "audit" / f"{name}.jsonl"
		if not f.exists():
			raise SystemExit(f"ATTRIBUTION FAIL-CLOSED: missing audit file for {name}")
		aggs = []
		for lineno, line in enumerate(f.read_text().splitlines(), 1):
			start = line.find("{")
			if start < 0:
				raise SystemExit(
					f"ATTRIBUTION FAIL-CLOSED: {name}:{lineno}: no JSON object on line")
			try:
				rec = json.loads(line[start:])
			except json.JSONDecodeError as e:
				raise SystemExit(
					f"ATTRIBUTION FAIL-CLOSED: {name}:{lineno}: malformed JSON ({e})")
			if rec.get("record") == "aggregate":
				aggs.append(rec)
		if len(aggs) != 1:
			raise SystemExit(
				f"ATTRIBUTION FAIL-CLOSED: {name}: expected exactly 1 aggregate "
				f"record, found {len(aggs)}")
		out[name] = {k: v for k, v in aggs[0].items()
		             if k != "record" and isinstance(v, int)}
	return out


# The attribution this script certifies (measured, reviewed): the
# uniform per-fixture stdlib delta of the regex rewrite, and the new
# pin fixture's own contribution.
EXPECTED_MODAL = (("c3_moveout_owned", -3), ("events", -3),
                  ("site_class:moveout_expansion", -3))
EXPECTED_NEW_FIXTURE = "std_regex_view_offsets_alternation"
EXPECTED_NEW_CONTRIB = {
	"c1_agree": 1100, "c1_path_dependent": 22,
	"c3_moveout_flag_guarded": 5, "c3_moveout_owned": 2118,
	"c3_moveout_unreachable_block": 2, "c3_moveout_zero_safe": 15,
	"events": 3144, "fns": 1254, "pre_post_verdict_drift": 52,
	"site_class:materialized_lastuse_release": 669,
	"site_class:moveout_expansion": 2140,
	"site_class:overwrite_release": 261,
	"site_class:scope_exit_release": 74,
}


def main(run_dir_s: str, baseline_run_dir_s: str) -> int:
	"""baseline_run_dir: a retained PRE-rewrite run dir with per-fixture
	audit files (the checked-in baseline has only aggregates), used for
	per-fixture attribution; its aggregate must equal the checked-in
	reviewed-baseline aggregate."""
	run_dir = Path(run_dir_s)
	base_run = Path(baseline_run_dir_s)

	base_agg = json.loads((BASELINE / "aggregate.json").read_text())["counters"]
	base_man = json.loads((BASELINE / "manifest.json").read_text())["universe"]
	new_agg = json.loads((run_dir / "aggregate.json").read_text())["counters"]
	new_man = json.loads((run_dir / "manifest.json").read_text())["universe"]

	# fail-closed: the baseline RUN dir must be IDENTICAL to the
	# checked-in baseline — aggregate AND manifest (universe identity)
	br_agg = json.loads((base_run / "aggregate.json").read_text())["counters"]
	if br_agg != base_agg:
		raise SystemExit("ATTRIBUTION FAIL-CLOSED: baseline run dir aggregate "
		                 "!= checked-in baseline")
	br_man = json.loads((base_run / "manifest.json").read_text())["universe"]
	if br_man != base_man:
		raise SystemExit("ATTRIBUTION FAIL-CLOSED: baseline run dir manifest "
		                 "!= checked-in baseline manifest")

	print("== universe (FAIL-CLOSED expectations — RESUME-STATUS §5.3) ==")
	b_ok, n_ok = set(base_man["compiled_ok"]), set(new_man["compiled_ok"])
	b_f, n_f = set(base_man["failed"]), set(new_man["failed"])
	added_ok = sorted(n_ok - b_ok)
	removed_ok = sorted(b_ok - n_ok)
	print(f"compiled_ok: {len(b_ok)} -> {len(n_ok)}  added={added_ok}  removed={removed_ok}")
	print(f"failed: {len(b_f)} -> {len(n_f)}  delta_add={sorted(n_f-b_f)}  delta_rm={sorted(b_f-n_f)}")
	b_hash = {f["name"]: f["sha256"] for f in base_man["fixtures"]}
	n_hash = {f["name"]: f["sha256"] for f in new_man["fixtures"]}
	changed = sorted(n for n in b_hash if n in n_hash and b_hash[n] != n_hash[n])
	print(f"content-hash changes among pre-existing fixtures: {changed}")
	universe_errs = []
	if added_ok != [EXPECTED_NEW_FIXTURE]:
		universe_errs.append(
			f"additions must be EXACTLY [{EXPECTED_NEW_FIXTURE}], got {added_ok}")
	if removed_ok:
		universe_errs.append(f"removals present: {removed_ok}")
	if b_f != n_f:
		universe_errs.append(
			f"failed population changed: +{sorted(n_f-b_f)} -{sorted(b_f-n_f)}")
	b_exc = sorted((e["name"], e.get("reason", "")) for e in base_man.get("excluded", []))
	n_exc = sorted((e["name"], e.get("reason", "")) for e in new_man.get("excluded", []))
	if b_exc != n_exc:
		universe_errs.append(
			f"excluded population changed (name+reason): "
			f"only_base={sorted(set(b_exc)-set(n_exc))} "
			f"only_new={sorted(set(n_exc)-set(b_exc))}")
	if changed:
		universe_errs.append(f"pre-existing fixture hashes changed: {changed}")
	if universe_errs:
		for e in universe_errs:
			print(f"UNIVERSE VIOLATION: {e}")
		print("\nATTRIBUTION: FAILED (universe fail-closed)")
		return 1

	shared = sorted(b_ok & n_ok)
	print(f"\n== per-fixture attribution over {len(shared)} shared compiled fixtures ==")
	old_cnt = load_fixture_counters(base_run, shared)
	new_cnt = load_fixture_counters(run_dir, shared)
	keys = sorted(set(base_agg) | set(new_agg))

	deltas = {}
	for name in shared:
		d = {}
		for k in keys:
			dv = new_cnt[name].get(k, 0) - old_cnt[name].get(k, 0)
			if dv:
				d[k] = dv
		deltas[name] = tuple(sorted(d.items()))

	tally = Tally(deltas.values())
	modal, modal_n = tally.most_common(1)[0]
	print(f"MODAL delta ({modal_n}/{len(shared)} fixtures): {dict(modal)}")
	outliers = {n: d for n, d in deltas.items() if d != modal}
	print(f"outliers ({len(outliers)}):")
	for n, d in sorted(outliers.items()):
		beyond = {k: v - dict(modal).get(k, 0) for k, v in dict(d).items()}
		beyond = {k: v for k, v in beyond.items() if v}
		missing = {k: -v for k, v in dict(modal).items() if k not in dict(d)}
		print(f"  {n}: beyond-modal {beyond}  missing-modal {missing}")
	# FAIL-CLOSED attribution expectations (review blocker 3): the
	# modal delta must be EXACTLY the reviewed stdlib delta, on EVERY
	# shared fixture (zero outliers)
	if modal != EXPECTED_MODAL:
		raise SystemExit(
			f"ATTRIBUTION FAIL-CLOSED: modal delta {dict(modal)} != expected "
			f"{dict(EXPECTED_MODAL)}")
	if modal_n != len(shared):
		raise SystemExit(
			f"ATTRIBUTION FAIL-CLOSED: modal delta on {modal_n}/{len(shared)} "
			f"fixtures — outliers present")
	if outliers:
		raise SystemExit(
			f"ATTRIBUTION FAIL-CLOSED: {len(outliers)} outlier fixtures: "
			f"{sorted(outliers)[:5]}...")

	new_fixture_contrib = {}
	for name in added_ok:
		cnt = load_fixture_counters(run_dir, [name])[name]
		new_fixture_contrib[name] = cnt
		print(f"new fixture {name}: {cnt}")
	got = {k: v for k, v in new_fixture_contrib.get(EXPECTED_NEW_FIXTURE, {}).items() if v}
	if got != EXPECTED_NEW_CONTRIB:
		raise SystemExit(
			f"ATTRIBUTION FAIL-CLOSED: new-fixture contribution differs from "
			f"the reviewed expectation:\n  got      {got}\n  expected "
			f"{EXPECTED_NEW_CONTRIB}")

	print("\n== reconciliation (residual must be ZERO on every counter) ==")
	residual_ok = True
	for k in keys:
		agg_delta = new_agg.get(k, 0) - base_agg.get(k, 0)
		per_fix = sum(dict(d).get(k, 0) for d in deltas.values())
		newf = sum(c.get(k, 0) for c in new_fixture_contrib.values())
		residual = agg_delta - per_fix - newf
		flag = "" if residual == 0 else "  <-- RESIDUAL NONZERO"
		if residual != 0:
			residual_ok = False
		print(f"{k:44s} agg{agg_delta:+12d} = per-fixture{per_fix:+12d} + new{newf:+10d}  residual {residual}{flag}")

	hard = [k for k in keys if k.startswith("hard_gate") and new_agg.get(k, 0)]
	print(f"\nhard gates nonzero: {hard if hard else 'NONE'}")
	print("\nATTRIBUTION:", "RESIDUAL ZERO" if residual_ok else "FAILED")
	return 0 if residual_ok and not hard else 1


if __name__ == "__main__":
	sys.exit(main(sys.argv[1], sys.argv[2]))
