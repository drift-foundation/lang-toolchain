# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Triage-bucket classification for ownership-ledger observe records.

Single source of truth for the `bucket_for` function consumed by:
  - `tools/ownership_observe/aggregate_triage.py` — the CLI aggregator
    that walks `build/ownership-ledger/triage/triage-raw/*.log` and
    writes `build/ownership-ledger/triage/triage.md`.
  - `lang/tests/stage2/test_match_cleanup_per_field_observe.py` — pins
    the per-field-vs-whole-local routing rule.

Buckets (in K's first-match-wins order):
  0   drop_flag_owned         — site defers to Phase 3C drop-flag ownership
  0b  moved_unconditional     — Phase 4 step 2 reason tag (skip)
  0c  unknown_type            — Phase 4 step 2 silent-skip surface
  0d  per_field_still_disagrees — per-field record (non-empty field_path)
                                 with non-agree classification
  1   per_field_gap           — whole-local match_cleanup record with a
                                field-related reason (back-compat)
  3   path_dependent          — explicit ledger verdict
  4   semantic_equivalent     — Tombstoned drop
  2   droppolicy_approximation — Copy-trait short-circuit / DV carrier
                                 heuristic
  5   implicit_return_move_gap — site-1 over-reports `moved` while
                                 ledger sees Live (legacy bucket name;
                                 cured at runtime by 3C drop flags)
  6   real_disagreement       — gate-blocking; everything else with
                                 non-agree classification
"""
from __future__ import annotations


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
	# 5. moved_locals_return_path (filter key kept as
	#    "implicit_return_move_gap" for stability — the legacy name
	#    reflected the original diagnosis).  Detection: site 1 says no
	#    drop with reason "moved", ledger sees the local as Live.
	#    Post-Phase-4-Return-as-move, the LoadLocal+Return theoretical
	#    gap is closed and the residual records come from a different
	#    class: HIR's `_moved_locals` over-reports "moved" on paths
	#    where the local is still Live (typically error arms that
	#    return an error value, not the owned local).  Not a site leak,
	#    not a ledger lattice bug; cured at runtime by 3C drop flags;
	#    telemetry clears only when site 1 consults the ledger instead
	#    of `_moved_locals`.  Do not let it pollute bucket 6.
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
