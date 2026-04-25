#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 3A Task #5 triage aggregator.

Reads per-case stderr logs from `build/ownership-ledger/triage/triage-raw/`,
parses the JSON records emitted with the `[drift:ownership_ledger] `
prefix, deduplicates by (site, fn_name, program_point, local) so a
record seen in N cases is counted once per unique decision, then
buckets every record into exactly ONE of:

  1. per_field_gap            — site 2 records on partial-move scrutinees
  2. droppolicy_approximation — driver-side has_drop divergence cases
  3. path_dependent           — `classification == "path_dependent"`
  4. semantic_equivalent      — `classification == "semantic_equivalent"`
  5. moved_locals_return_path — site 1 (scope_drop) says "local
                                moved" at a scope-drop cursor on a
                                path where the ledger sees the local
                                still Live.
                                Original diagnosis (bucket name
                                "implicit_return_move_gap", retained
                                as a legacy alias in the filter below)
                                suspected a missing MIR ownership edge
                                for LoadLocal+Return.  The Phase 4
                                Return-as-move lattice enhancement
                                closed that theoretical gap and its
                                unit-tested carrier shapes — but the
                                residual records observed in practice
                                are a different disagreement class:
                                HIR's `_moved_locals` is function-wide
                                / path-insensitive, so site 1 reports
                                "moved" on paths where the local is
                                actually still Live (typically error
                                arms that return an error value, not
                                the owned local).  3C drop-flag
                                insertion cured the RUNTIME leak; the
                                TELEMETRY disagreement persists until
                                site 1 queries the ledger instead of
                                `_moved_locals`.  NOT a site leak and
                                NOT a ledger lattice bug.
  6. real_disagreement        — true site-vs-ledger disagreement after
                                buckets 1–5 are stripped.  Must be
                                empty before 3B begins.

Order is critical (per K's directive): a record is classified by the
FIRST bucket it matches, so a `match_cleanup` record that is also
`path_dependent` lands in bucket 1.  Bucket 6 is the gate-blocker.

Output: writes `build/ownership-ledger/triage/triage.md` with the bucket sizes,
per-bucket samples, and a verdict on the gate.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
# Policy: transient artifacts under `build/`, never in `work/` or `lang/`.
_BUILD_ROOT = REPO / "build/ownership-ledger/triage"
RAW_DIR = _BUILD_ROOT / "triage-raw"
OUT = _BUILD_ROOT / "triage.md"
PREFIX = "[drift:ownership_ledger] "


def load_records():
	"""Yield (case_name, record_dict) for every parsed record."""
	for log in sorted(RAW_DIR.glob("*.log")):
		case = log.stem
		try:
			text = log.read_text(errors="replace")
		except Exception:
			continue
		for line in text.splitlines():
			if not line.startswith(PREFIX):
				continue
			payload = line[len(PREFIX):]
			try:
				yield case, json.loads(payload)
			except json.JSONDecodeError:
				continue


from .buckets import bucket_for

def main() -> int:
	parser = argparse.ArgumentParser(
		prog="tools.ownership_observe.aggregate_triage",
		description=(
			"Aggregate ownership-ledger observe records produced by "
			"run_observe.py into a bucketed triage report.  Reads "
			f"{RAW_DIR.relative_to(REPO)}/*.log; writes "
			f"{OUT.relative_to(REPO)}.  Exits non-zero iff bucket 6 "
			"(real_disagreement) is non-empty."
		),
	)
	parser.parse_args()
	bucket_counts: dict[str, int] = defaultdict(int)
	bucket_samples: dict[str, list[dict]] = defaultdict(list)
	bucket_unique_keys: dict[str, set] = defaultdict(set)
	total = 0
	cases_with_records = set()
	for case, rec in load_records():
		total += 1
		cases_with_records.add(case)
		b = bucket_for(rec)
		# Dedup per bucket by decision key.
		key = (rec.get("site"), rec.get("fn_name"), tuple(rec.get("program_point", [])), rec.get("local"))
		if key in bucket_unique_keys[b]:
			continue
		bucket_unique_keys[b].add(key)
		bucket_counts[b] += 1
		if len(bucket_samples[b]) < 5:
			bucket_samples[b].append(rec)

	gate_block = bucket_counts.get("real_disagreement", 0)
	lines: list[str] = []
	lines.append("# Phase 3A Task #5 Triage")
	lines.append("")
	lines.append(f"Source: `build/ownership-ledger/triage/triage-raw/*.log`")
	lines.append(f"Cases producing records: {len(cases_with_records)}")
	lines.append(f"Total records (incl. duplicates across cases): {total}")
	lines.append(f"Total UNIQUE decisions (deduped by site+fn+point+local): {sum(bucket_counts.values())}")
	lines.append("")
	lines.append("## Bucket counts (unique decisions)")
	lines.append("")
	lines.append("| # | Bucket | Count | Notes |")
	lines.append("|---|---|---|---|")
	lines.append(f"| 0 | drop_flag_owned | {bucket_counts.get('drop_flag_owned', 0)} | Site defers to Phase 3C drop-flag ownership for this scope-exit |")
	lines.append(f"| 0b | moved_unconditional | {bucket_counts.get('moved_unconditional', 0)} | Phase 4 step 2 — move in same scope as declaration; legacy-correct skip |")
	lines.append(f"| 0c | unknown_type | {bucket_counts.get('unknown_type', 0)} | Phase 4 step 2 — local with no recorded type; silent skip surfaces here |")
	lines.append(f"| 0d | per_field_still_disagrees | {bucket_counts.get('per_field_still_disagrees', 0)} | Phase 4 step 3b — per-field record (non-empty field_path) where ledger and site disagree; gates 3c (chain-aware tightening required if dominated by VariantGetFieldAddr noise) |")
	lines.append(f"| 1 | per_field_gap | {bucket_counts.get('per_field_gap', 0)} | Defer to 3B (per-field tracking) |")
	lines.append(f"| 2 | droppolicy_approximation | {bucket_counts.get('droppolicy_approximation', 0)} | Quarantined — 3B must NOT consume `has_drop` |")
	lines.append(f"| 3 | path_dependent | {bucket_counts.get('path_dependent', 0)} | Direct input to 3C design |")
	lines.append(f"| 4 | semantic_equivalent | {bucket_counts.get('semantic_equivalent', 0)} | Tolerated (Tombstoned drop = no-op) |")
	lines.append(f"| 5 | implicit_return_move_gap | {bucket_counts.get('implicit_return_move_gap', 0)} | Site 1 over-reports `moved` on paths where ledger sees Live.  Name is legacy — the original LoadLocal+Return gap is closed (Phase 4 Return-as-move); residual is path-insensitive `_moved_locals` over-reports, 3C-handled at runtime.  NOT a site leak. |")
	lines.append(f"| 6 | real_disagreement | **{gate_block}** | Gate-blocking: must be empty before 3B |")
	lines.append(f"|   | (agree)          | {bucket_counts.get('agree', 0)} | — |")
	lines.append("")
	if gate_block == 0:
		lines.append("## Gate verdict: ✅ Bucket 6 is empty — 3A→3B verdict-disagreement gate is satisfied.")
	else:
		lines.append(f"## Gate verdict: ❌ Bucket 6 has {gate_block} records — 3B is BLOCKED until each is resolved.")
	lines.append("")
	for b in ("drop_flag_owned", "moved_unconditional", "unknown_type", "per_field_still_disagrees", "per_field_gap", "droppolicy_approximation", "path_dependent", "semantic_equivalent", "implicit_return_move_gap", "real_disagreement"):
		samples = bucket_samples.get(b, [])
		if not samples:
			continue
		lines.append(f"## Samples — `{b}`")
		lines.append("")
		lines.append("```json")
		for s in samples:
			lines.append(json.dumps(s, sort_keys=True))
		lines.append("```")
		lines.append("")
	OUT.write_text("\n".join(lines) + "\n")
	print(f"Wrote {OUT}")
	print(f"Bucket 5 (real_disagreement): {gate_block}")
	return 0 if gate_block == 0 else 1


if __name__ == "__main__":
	raise SystemExit(main())
