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
	out = {}
	for name in names:
		f = run_dir / "audit" / f"{name}.jsonl"
		agg: dict[str, int] = {}
		for line in f.read_text().splitlines():
			start = line.find("{")
			if start < 0:
				continue
			try:
				rec = json.loads(line[start:])
			except json.JSONDecodeError:
				continue
			if rec.get("record") != "aggregate":
				continue
			for k, v in rec.items():
				if k != "record" and isinstance(v, int):
					agg[k] = agg.get(k, 0) + v
		out[name] = agg
	return out


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

	# sanity: the baseline RUN dir matches the checked-in baseline
	br_agg = json.loads((base_run / "aggregate.json").read_text())["counters"]
	assert br_agg == base_agg, "baseline run dir != checked-in baseline aggregate"

	print("== universe ==")
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

	new_fixture_contrib = {}
	for name in added_ok:
		cnt = load_fixture_counters(run_dir, [name])[name]
		new_fixture_contrib[name] = cnt
		print(f"new fixture {name}: {cnt}")

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
