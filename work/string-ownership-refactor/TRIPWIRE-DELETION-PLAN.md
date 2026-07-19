# Tripwire-deletion slice — plan (APPROVED 2026-07-18, corrections folded)

Status: APPROVED with one blocking correction (T4) and decisions on
all open items — implementation proceeding. Rev 2 of this document;
rev 1 was the report-only checkpoint.

## 0. Precondition status

The deletion condition from RELEASE-ARM-TRIPWIRE-DESIGN.md §8 — "one
clean cert cycle with zero firings" — is MET:

- 0.33.84 / ABI 21 certified 2026-07-18 with all three tripwire
  families armed (release-arm, 4a store-arm dead-stakes, 4b central
  funnel) and zero firings through the full suite.
- The one production firing in the tripwires' lifetime (TLR-8,
  `"lit" + move s`, drift-workflows staging vs 0.33.83) was fixed
  IN-TREE before cert; 0.33.84 is the first clean cycle and includes
  the fix.
- EXTERNAL PRECONDITION: MET (maintainer, 2026-07-18). Certified run
  `20260719-001008-drift-lang-99a68ee` exercised drift-workflows
  `0251b24`; staging plus normal/debug test, stress, and perf all
  passed with ZERO tripwire log matches.

## 1. Approved scope (maintainer, 2026-07-18)

1. Delete the `_note_use` release-arm condition/body and
   `_release_arm_tripwire`.
2. Delete all three 4a proven-String store branches plus the central
   4b `_ensure_owned` tripwire; remove `_dead_stake_tripwire` once
   unused. Preserve move paths and untyped pass-through behavior.
3. Retire `SITE_CLASS_TEMP_LASTUSE_RELEASE` like retired C4 /
   destructor_self: keep the historical constant + note, remove it
   from the closed enumeration and the counted-only live-event
   handling.
4. Retire only tripwire-specific tests; preserve TLR-8 and memcheck
   semantic regressions.
5. Update the intake resolution(s) with the certified run and deletion
   outcome.

Acceptance: every counter +0 vs `build/tmp/cleanup-tripwire`,
standalone memcheck, reporter/stage2/guardrails batteries, identical
universe. No ABI bump. No refactor trigger blocks this cleanup.

## 2. Exact deletion inventory

All line refs against 99a68ee1 (0.33.84).

`lang/driftc/stage2/string_arc.py`:

- **Release arm** — `_note_use`'s
  `use_counts[val] == 0 and val in owned_values and val not in
  live_out` branch (≈1758-1775, condition + comment + call) and the
  `_release_arm_tripwire` def (≈1320-1348). The consume branch above
  it is untouched; `_note_use` ends after it.
- **4a store branches** — the three proven-String
  `_dead_stake_tripwire` calls in the StoreLocal (≈2153), StoreRef
  (≈2204), ArrayIndexStore (≈2240) fallback arms, with their Slice-4a
  comments.
- **4b central funnel** — `_ensure_owned`'s terminal
  `_dead_stake_tripwire` call + the Slice-4b comment block (≈1258-1280);
  the proven-String fall-through becomes `return val`, joining the
  untyped pass-through. All move/owned pre-checks above it untouched.
- **`_dead_stake_tripwire` def** (≈1282-1318) — deleted once the four
  call sites are gone.
- **Doc surfaces**: module doc + `insert_string_arc` pipeline-
  precondition comment (≈1007) reworded — the precondition itself is
  unchanged (production runs the materialization pass first; bare use
  only in tests that document why it is safe), but the stated
  consequence changes from "trips the arm" to "family temps get no
  last-use release" (see §4).

`lang/driftc/stage2/ownership_ledger_reporter.py`:

- `SITE_CLASS_TEMP_LASTUSE_RELEASE` removed from
  `STRING_ARC_SITE_CLASSES` (≈356) and from finalize's `_counted_only`
  set (≈839). Constant kept; its comment block (≈329-333) rewritten to
  the destructor_self retirement form: historical parsing only, arm
  deleted this slice, any future `note()` with the tag → UNTAGGED
  (already a hard corpus gate).
- The `materialized_lastuse_release` comment's "the shim retired with
  the release-arm tripwire" sentence gains "; the arm itself was
  deleted <this slice>".

`lang/driftc/driftc.py`:

- **KEPT**: the string_arc boundary wrap (≈8210) — generic
  AssertionError → clean `internal:` diagnostic containment for the
  whole phase, not tripwire-specific. Only its comment updates (drop
  "the dead-stake tripwires foremost"). See tweak T2.

`tools/drift_corpus_audit.py`:

- **NO CHANGE, tool stays v1.6.0.** The four retain site-class hard
  gates stay (they guard against emission bypass / direct audit notes
  — post-deletion they are the primary corpus guard). No new gate
  needed for `temp_lastuse_release`: removal from the closed set means
  a resurrected tag counts UNTAGGED, which is already gated
  (destructor_self precedent, verified in the 4b pin).

Intake folders (§6): both updated, neither deleted.

## 3. Tweaks vs the approved scope (confirm)

- **T1 — store-arm collapse.** At all three 4a sites the move arm and
  the fallback arm are IDENTICAL post-deletion (same store append,
  same `_note_use(val, consume=True)`); the tripwire was the only
  difference. Plan: collapse each if/else to the single unconditional
  form, with a comment recording the transfer semantics (staking is
  owned upstream by string_stakes; the store consumes its source
  once). Behavior-identical by inspection; `_can_move_owned_once`
  keeps its other callers. Alternative (keep the empty if/else shape)
  preserves diff locality but leaves a two-arm structure whose arms
  are equal — recommend the collapse.
- **T2 — driver boundary wrap kept, one containment pin kept.** The
  wrap is generic phase containment and stays (harmless, and
  string_arc still has non-tripwire asserts upstream of it). The
  2026-07-13 review made wrap containment a MANDATORY user-facing
  contract pin; deleting every in-tree assertion source doesn't
  delete the contract. Plan: retire the release-arm driver pin, and
  GENERALIZE `test_tripwire_surfaces_as_clean_internal_diagnostic`
  (already injection-based) into the wrap-containment pin — injected
  AssertionError → clean `internal:` diagnostic, no IR, no traceback —
  with tripwire wording removed. This is the one deliberate deviation
  from "retire only tripwire-specific tests": the pin is
  boundary-wrap-specific, not tripwire-specific.
- **T3 — `test_tlr4_nonfamily_calls_stay_out` rework, not retire.**
  It doubles as the non-family tripwire carrier. The stay-out half
  (can-throw / info-less / throw-indirect calls never materialized by
  the pass) is a live pass contract and stays; the tripwire-carrier
  half retires with the arm.
- **T4 — CORRECTED (maintainer, blocking): the CopyValue/MoveOut
  `recognized_released` guards are themselves DEAD once the release
  arm is gone, and both DELETE in this slice.** The rev-1 reasoning
  was wrong: removing the guards cannot produce a second release —
  the recognition arm copies the pass-materialized release through
  without consulting `owned_values`, and while the re-owned state MAY
  propagate (AssignSSA copies owned membership) and affect branch
  selection (`_can_move_owned_once` reads it), every affected branch
  is output-equivalent — `_ensure_owned` is identity, the store paths
  are unconditional, and `_note_use` only changes bookkeeping — so no
  branch can author another instruction or release.
  Therefore: delete the guard condition from the CopyValue (~2042)
  and MoveOut (~1977) rewrite-loop re-add arms (String-typed re-add
  becomes unconditional; MoveOut keeps its move_only mark), rewrite
  both comments, and RETIRE `test_tlr6_copyvalue_guard_teeth` and
  `test_tlr8_moveout_guard_teeth` (their subject is the guard). The
  TLR-6/8 family, cross-block, end-to-end, and memcheck regressions
  are all preserved.
  FLAGGED, NOT ACTED ON: the ConstString (~2009) and
  StringFrom*/Concat (~2016) re-add guards are the same shape and go
  dead by the same argument; they are KEPT per the correction's
  letter — maintainer's call whether they join now or fall with
  string_arc's endgame deletion.
- **T5 — exemption comments reworded, not removed.** The four bare-
  caller files (test_string_arc_return_swap,
  test_drop_before_overwrite_swap, test_string_arc_recursive_type_
  guard, test_move_from_ref_string_arc_contract) keep their
  why-bare-use-is-safe comments (no family producers / consumed
  before drain) with the tripwire framing dropped.
- **T6 — historical docstrings untouched.** The ~15 "single-config
  since the release-arm tripwire retired the arc-only leg" notes in
  the TLR pins describe why config-A died and remain true as history;
  only LIVE references to the arm (helper docstring at ≈59-75, ≈134,
  pipeline-precondition wording) are edited.

## 4. Failure-mode conversion (the honest cost, for the record)

Deletion converts fail-closed ICEs into silent misbehavior for any
OUT-OF-CORPUS defect shape (the class TLR-8 proved can exist):

| Guard | Today on a defect | After deletion |
|---|---|---|
| release arm | clean ICE + intake pointer | missing last-use release → LEAK (memcheck-visible, no corruption) |
| 4a store arms / 4b funnel | clean ICE + intake pointer | unowned proven-String stored without stake → over-release class (UAF) on a shape that must ALSO evade string_stakes |

Surviving guards: the four retain site-class HARD gates + UNTAGGED +
c3_moveout_not_owned in the corpus tool; the full memcheck suite incl.
the TLR-6/7/8 rows; the audit reporter under DRIFT_STRING_ARC_AUDIT=1
(unchanged). Note these are corpus/CI-time guards, not compile-time —
that is the accepted trade of this slice, per the standing plan
(RELEASE-ARM-TRIPWIRE-DESIGN.md §8), and the endgame remains deleting
string_arc.py entirely.

## 5. Test battery disposition

RETIRE (9 tests — 7 original inventory + 2 per the T4 correction):
test_dead_store_value_stake_tripwire_fires;
test_dead_call_arg_stake_tripwire_fires;
test_dead_value_position_stake_tripwire_fires;
test_dead_return_site3_stake_tripwire_fires;
test_release_arm_tripwire_stale_family_temp;
test_release_arm_tripwire_nonfamily_producer;
test_release_arm_tripwire_driver_diagnostic;
test_tlr6_copyvalue_guard_teeth; test_tlr8_moveout_guard_teeth.
Plus the `_expect_tripwire` helper (not a test).

GENERALIZE: test_tripwire_surfaces_as_clean_internal_diagnostic → the
wrap-containment pin (T2). Per maintainer: must explicitly assert the
string_arc phase / `internal:` diagnostic, EMPTY IR, and no
traceback.

REWORK: test_tlr4_nonfamily_calls_stay_out (T3);
reporter-battery helper docstrings; four exemption comments (T5).

KEEP UNCHANGED: all TLR-1..8 family/cross-block/CFG/loop pins; the
destructor_self-is-UNTAGGED pin (now also the retirement guard for
temp_lastuse); test_tlr8_move_operand_concat_end_to_end; ALL memcheck
rows (test_move_operand_concat_release, test_copyvalue_lastuse_release,
test_crossblock_lastuse_release, the full suite); the corpus-tool pin
battery (no tool change); ledger-cache guardrails.

Expected battery deltas: reporter 60 → 51 (9 retired, 1 generalized
in place — maintainer-confirmed arithmetic); stage2/guardrails counts
otherwise unchanged.

## 6. Docs & intake updates

- `issues/string-arc-release-arm-tripwire/RESOLUTION.md`: closing
  entry — 0.33.84 certified 2026-07-18 with the arm armed and zero
  firings; deletion executed this slice; folder retained as the TLR-8
  historical record.
- `issues/string-arc-dead-stake-tripwire/`: NEW RESOLUTION.md — never
  fired on any corpus, staging, or cert run across its lifetime
  (armed 2026-07-13/14); branches deleted this slice after the clean
  0.33.84 cycle. Folder retained.
- string_arc module doc / insert_string_arc precondition comment per
  §2; reporter constant comments per §2.

## 7. Acceptance

- Corpus: `--baseline build/tmp/cleanup-tripwire`, tool v1.6.0 —
  universe identical 924/344/49, EVERY counter +0 (17 aggregate keys;
  temp_lastuse_release stays absent; materialized 618,744; events
  2,772,052), all nine hard gates zero, exit 0. NOTE: this run doubles
  as the missing post-TLR-8 corpus reference — TLR-8 is provably
  corpus-invisible (zero `+ move` concat sites in the toolchain
  universe), so +0 is the prediction, and any drift is a finding.
- STANDALONE full memcheck (expected 102 + 1 skip — no rows retire).
- Batteries: reporter (post-retirement count), stage2 full,
  ledger-cache guardrails 24/24.
- Any tripwire-shaped failure during the work (e.g. a battery test
  that only passed BECAUSE an arm raised) = stop and report.

## 8. Decisions on record (maintainer, 2026-07-18)

1. External precondition MET — certified run
   `20260719-001008-drift-lang-99a68ee` × drift-workflows `0251b24`,
   staging + normal/debug test + stress + perf, zero tripwire log
   matches (§0).
2. Version: NO compiler bump, NO ABI bump; ABI remains 21.
3. T1, T2, T3, T5, T6 approved (T2's generalized pin must assert
   phase/`internal:` diagnostic, empty IR, no traceback). T4
   corrected per §3 — guards delete, both teeth tests retire.
4. Count correction accepted: 7 original retires + 2 T4 retires;
   reporter collection 60 → 51.
5. Still open (flagged, non-blocking): the same-argument ConstString /
   StringFrom*/Concat re-add guards (§3 T4 note) — kept this slice.

## 9. GO

Approved; implementation in flight. Acceptance per §7.
