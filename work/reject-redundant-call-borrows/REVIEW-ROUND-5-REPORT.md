# Round-5 report — nested-index parity, load-bearing pins, negatives, doc close-out (2026-07-30)

All four round-5 findings resolved. Compiler delta this round is ONE hunk
(the shallow checker's suppression walk); everything else is tests, pins,
and documentation. Verification statuses below; the snapshot gate chain is
still running per the round-5 directive, and the FINAL chain relaunches on
the frozen tree after it completes.

## 1. Nested HIndex borrowing — LANGUAGE_BUG-process fix (regression first)

- **Failing repro confirmed pre-fix** (exact predicted shape):
  `peek(make_matrix()[0][0])` with `peek(x: &Handle)` →
  `cannot copy value of type 'Array' (use move <expr>)` on the INNER index
  (probe `scratchpad/nested_idx`, two sites, both failing).
- **Fix** (`lang/driftc/checker/__init__.py`, HBorrow arm): the suppression
  now walks the ENTIRE borrow-subject projection spine — every `HIndex`
  encountered through `HIndex.subject` / `HField.subject` hops is added to
  `suppress_index_copy_check_expr_ids` (try/finally-scoped, as before).
  Expressions INSIDE `[...]` are never walked — an index expression is a
  genuine value read and keeps its copy checks (the review's exclusion
  requirement; structural: the walk never descends into `.index`).
- **MIR side needed nothing**: repeated index hops were already supported
  by the round-4 lifted chain; post-fix the repro compiles AND runs with
  correct element values through both hops.
- **Pinned**: `borrow_chained_ref_projection_noncopy` SECTION D
  (`_peek(mk_matrix()[0][0])` → 7, `[0][1]` → 9; exits 22-23), narrative
  and expected.json updated. Fixture runs `ok`/0.
- **refactor_triggers.md**: re-scanned — no matching trigger. Recorded as
  numbered LANGUAGE_BUG **#4** in the ledger (close-out round: repro,
  failing regression, subsystem, fix, trigger result), counted in
  doc/history.md ("Four LANGUAGE_BUGs found and fixed by the rule's own
  tripwires") and in the combined announcement.

## 2. Same-head inference pin made load-bearing

- Accepted the finding: `Carrier<T>` has no Borrow impl and the general
  `_infer` declared-ref peel serves it independently, so the old row could
  pass with the preference broken.
- **Reworked** to the suggested discriminating shape:
  `inspect<T>(a: &core.Arc<T>)` with a bare `Arc<Int>` argument — Arc's
  competing `Borrow<T>` view (`Arc<Int>.borrow() → &Int`) makes the row
  fail unless plain auto-borrow wins for same-head.
- **Load-bearing PROVEN empirically**: with the head-match preference
  branch disabled (`if False and …`), the row FAILS (1 failed in 7.7s);
  restored, it passes (restoration verified by grep). The mismatched-head
  row (`conc.lock(Arc<Mutex<Int>>)` → Borrow-trait view) is unchanged.
- File: `lang/tests/driver/test_autoborrow_reresolution_pins.py` — 4/4.

## 3. Owned-base HIndex: mandatory negative + ICE determination

- **Negative companion landed**:
  `test_autoborrow_receiver_place.py::
  test_method_receiver_mut_through_rvalue_index_rejects_cleanly` — BOTH
  flavors in one module: `mk_handles()[0].bump()` (owned base; mutating a
  temporary) and `w.handles_ref()[0].bump()` (`&mut` through a shared-ref
  base). Close-out tightening: asserts the `borrow requires an addressable
  place; bind to a local first` diagnostic on EACH distinct source line
  (owned-base and shared-ref-base call sites separately — duplicate
  diagnostics from one call cannot mask the other path) AND the ABSENCE of
  any ICE-shaped message. The
  validator's `base_owned` arm independently hard-excludes `is_mut`
  (owned admission requires a shared borrow), so the checker rejection is
  backed by a lowering-side guard. File: 11/11.
- **`_peek(mk()[1])` ICE determination: transient unpublished state — no
  ledger number** (the numbered #4 slot went to the nested-HIndex bug in
  the close-out round). Probe evidence recorded in the ledger
  ("Considered and determined NOT a LANGUAGE_BUG" section): the identical
  probe compiled with the CERTIFIED 0.33.91 toolchain rejects with the
  clean pre-rule diagnostic (`cannot copy value of type 'Handle'`,
  E-AUTO-e0f26505) — the ICE window existed only between two in-branch
  edits (shallow suppression landed, owned-base MIR admission not yet) and
  was never reachable from any published compiler. Trigger scan: no match.

## 4. Documentation close-out

- `borrow_chained_ref_projection_noncopy/expected.json` — description
  rewritten to the four-section dual-route reality (A: stage1 bindings;
  B: bare args via the MIR twin; C: owned-base index; D: nested index).
- `type_checker.py::_ultimate_base_is_rvalue_call` docstring — now states
  HField/HIndex/deref-at-base scope and that the synthesized receiver
  borrow lowers through the MIR twin, NOT stage1 (the stage1 claim was the
  round-5-flagged error).
- `hir_to_mir.py` — all four flagged regions rewritten: the
  `_lift_rvalue_ref_base_for_borrow` header (field/index/deref + owned
  bases), the "Supported chain shape" base clause (owned-base admission
  rule), the emitter step-kind comment (three kinds), and the defensive
  else-arm comment ("three step kinds").
- `REVIEW-ROUND-4-REPORT.md` items 2 and 4 — updated from IN PROGRESS to
  DONE with the round-5 additions folded in.
- `D5-test-changes.md` driver-additions list — now names
  `test_autoborrow_reresolution_pins.py` and the negative companion row.

## Verification and sequencing

- Round-5 probes: nested-index repro compiles+runs; owned/shared `&mut`
  negatives reject cleanly; chained fixture (A/B/C/D) runs `ok`/0;
  receiver-place file 11/11; reresolution pins 4/4 with the load-bearing
  break/restore check.
- Full feature batch (17 files, 16-way) rerunning on the final tree at
  time of writing — result lands in PROGRESS.md.
- Snapshot gate chain (launched pre-round-5): phase 1 perf GREEN
  (second consecutive clean serial run); later phases proceed as snapshot
  evidence only — round-5 landed compiler + hashed-fixture changes
  mid-flight, so per the round-5 directive it is NOT promotable.
- FINAL chain (perf → corpus audit → memcheck → ASAN) relaunches on the
  frozen post-round-5 tree when the snapshot chain completes; its audit
  produces the promotion package (universe 1,269 → 1,292, the 23
  enumerated additions, zero unexpected flips expected — SECTION-C/D and
  expected.json edits are content deltas on already-modified fixtures).
- Announcement update to final-tree certification happens when the final
  chain is green.

## What to review (round-5 code delta)

- `lang/driftc/checker/__init__.py` — the spine-walk suppression hunk
  (only compiler change this round). Review question: the walk covers
  HIndex/HField hops; deref (`*`) does not appear inside argument
  projection spines post-normalize (deref-at-base is consumed by the
  borrow itself) — confirm no spine shape with an interior HUnary needs
  coverage.
- `lang/tests/codegen/e2e/borrow_chained_ref_projection_noncopy/` —
  SECTION D + mk_matrix builder + expected.json.
- `lang/tests/driver/test_autoborrow_reresolution_pins.py` — Arc-based
  same-head row.
- `lang/tests/driver/test_autoborrow_receiver_place.py` — negative
  companion row.
- `work/…/LANGUAGE_BUGS-found-during-implementation.md` — the
  determination section.

## Close-out round (2026-07-30, post-round-5 review)

1. Nested-HIndex promoted to numbered **LANGUAGE_BUG #4** in the ledger
   (repro, failing regression, subsystem, fix, trigger scan);
   doc/history.md updated three→FOUR rule-tripwire bugs (the head-match
   inference sentence there was corrected in the same pass); the combined
   announcement's tripwire sentence now names the spurious-rejection
   fourth bug.
2. Mutable-receiver negative tightened to per-source-line assertions (see
   §3 above) — 1/1 green.
