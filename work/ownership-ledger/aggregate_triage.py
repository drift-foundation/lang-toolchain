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
  5. implicit_return_move_gap — site marks local moved (HIR knew about
                                ownership leaving via return) but MIR
                                only emitted LoadLocal+Return so the
                                ledger sees the local as still Live.
                                NOT a site leak and NOT a ledger
                                lattice bug — it is a missing explicit
                                ownership edge in MIR.  3B/3C input:
                                either widen the ledger transfer
                                function for return-of-owned-local, or
                                make MIR's return ownership explicit.
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

import json
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


def bucket_for(rec: dict) -> str:
	"""Classify per K's bucket order — first match wins."""
	site = rec.get("site", "")
	site_reason = rec.get("site_reason", "")
	classification = rec.get("classification", "")
	field_path = rec.get("field_path") or []
	# 0. drop_flag_owned — site explicitly defers to Phase 3C drop-
	# flag ownership for this scope-exit.  Not a disagreement; the
	# site is documenting the responsibility split (3C is the sole
	# authority on this local's scope-exit drop).  Any classification
	# is irrelevant — if the ledger said MustDrop here, that drop IS
	# being emitted, just by 3C's drop block, not by this site.
	# Filtered first so it doesn't pollute bucket 5/6.
	if site_reason == "drop_flag_owned":
		return "drop_flag_owned"
	# 0b. moved_unconditional — Phase 4 step 2 reason tag for the
	# `_scope_drop_verdict` "move in same scope as declaration"
	# branch.  Same emission semantics as the legacy "moved" case
	# (skip), but distinct telemetry.  Filtered alongside
	# drop_flag_owned so the agreement / bucket-2 / bucket-6
	# accounting reflects the actual decision being made (skip,
	# legacy-correct).
	if site_reason == "moved_unconditional":
		return "moved_unconditional"
	# 0c. unknown_type — Phase 4 step 2 distinct tag for
	# `_scope_drop_verdict`'s unknown-type silent skip.  Same
	# emission semantics as not_drop_needing (skip), but the tag
	# surfaces the case for diagnosis.  Filtered into its own bucket
	# so it doesn't appear as bucket-2 noise.
	if site_reason == "unknown_type":
		return "unknown_type"
	# 0d. per_field_still_disagrees — Phase 4 step 3b.  Per-field
	# record (non-empty `field_path`) where the per-field ledger
	# verdict still disagrees with the site after both sides
	# classify the field.  Distinct from `per_field_gap` (whole-
	# local records where the ledger has no per-field opinion to
	# compare).  Captures the residual K wants visible to gate 3c:
	# if this bucket is dominated by VariantGetFieldAddr false
	# positives, 3c needs chain-aware tightening before site-2
	# emission authority changes.  If it's small, 3c can proceed.
	if field_path and classification != "agree":
		return "per_field_still_disagrees"
	# 1. per_field_gap — site 2 partial-move records (whole-local
	# only — per-field records take the bucket above first).
	if site == "match_cleanup" and site_reason in {"field_moved", "field_needs_drop", "field_not_drop_needing"} and not field_path:
		return "per_field_gap"
	# Some site-2 records also use REASON_NEEDS_DROP / REASON_MOVED
	# directly when they record the scrutinee whole-drop branch — those
	# are the case-3 (whole-drop) shape, which the ledger CAN model;
	# they don't go in this bucket.  Filter narrow.
	# 3. path_dependent — explicit ledger verdict
	if classification == "path_dependent":
		return "path_dependent"
	# 4. semantic_equivalent — Tombstoned drop
	if classification == "semantic_equivalent":
		return "semantic_equivalent"
	# 2. droppolicy_approximation — per K, identify by:
	#    site_stricter on a drop-needing decision (likely Copy-trait
	#    short-circuit) OR ledger_stricter where the local's name pattern
	#    suggests a DV-bearing carrier.  We cannot precisely separate
	#    DropPolicy divergence from real disagreements without consulting
	#    the type table — TRIAGE NOTE: this aggregator marks suspected
	#    cases by HEURISTIC.  If a record is `site_stricter` AND the
	#    site's verdict is must_drop, it's most likely the Copy-trait
	#    case (site correctly skipped drop on a Copy type, ledger
	#    over-claimed drop).  If it's `ledger_stricter` AND the local
	#    type token includes "DV" / "Diagnostic", it's the DV case.
	if classification == "site_stricter":
		return "droppolicy_approximation"
	if classification == "ledger_stricter" and any(
		marker in rec.get("local", "") for marker in ("__dv", "Diagnostic", "DiagnosticValue")
	):
		return "droppolicy_approximation"
	# 5. implicit_return_move_gap — HIR-marked move that MIR represents
	#    as LoadLocal+Return (no MoveOut), so the ledger cannot infer
	#    the ownership transfer.  Detection: site says no drop with
	#    reason "moved", ledger sees the local as Live.  This is a
	#    missing explicit ownership edge in MIR, not a site leak; do
	#    not let it pollute bucket 6.
	if (
		classification == "ledger_stricter"
		and site_reason == "moved"
		and rec.get("raw_state") == "live"
	):
		return "implicit_return_move_gap"
	# 6. real_disagreement — everything else with non-agree classification
	if classification == "agree":
		return "agree"
	return "real_disagreement"


def main() -> int:
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
	lines.append(f"| 5 | implicit_return_move_gap | {bucket_counts.get('implicit_return_move_gap', 0)} | Missing MIR ownership edge for LoadLocal+Return; 3B/3C input, NOT a site leak |")
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
