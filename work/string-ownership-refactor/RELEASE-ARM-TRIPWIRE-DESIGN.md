# `_note_use` release-arm tripwire — design checkpoint (report only)

Status: DESIGN/CHECKPOINT ONLY — no implementation.  This is the
cert-cycle guard before deleting the dead arm; NO deletion in this
slice.

## 1. Reference state — confirmed

Committed TLR-7 reference (a1fa8f59, `build/tmp/cleanup-tlr7`, exit 0):
`site_class:temp_lastuse_release` is ABSENT from the aggregate (17
counters — the class vanished, i.e. exactly 0 corpus-wide);
materialized_lastuse_release 618,744 = the entire lifetime population;
events 2,772,052; all nine hard gates zero.  Working tree clean at the
commit.

## 2. The exact branch (string_arc.py:1698–1733)

Inside `_note_use(val, *, consume)` — reached only with
`consume=False` (the consume path returns at 1694–1697):

    if use_counts[val] == 0 and val in owned_values \
            and val not in live_out.get(block.name, set()):
        if _audit is not None:
            _tlr_cls = (...)            # the TLR-1 option-B shim
            _audit.note(...)
        new_instrs.append(M.StringRelease(value=val))   # line 1731
        owned_values.discard(val)
        move_only_values.discard(val)

The branch CONDITION is precisely "an owned String temp just drained
to zero non-consumingly, is dead after this block, and nothing
pre-materialized its release" — post-TLR-7 that is an ownership-
accounting hole by definition: every measured population (family
producers, all CFG shapes) is pass-authored and recognition-suppressed
before this arm can fire.

## 3. Decision: tripwire the WHOLE branch body, not just the append

Replace the entire body (audit note + shim + append + discards) with a
structured failure.  Rationale:
- The condition itself is the defect signal.  Releasing (old behavior)
  would MASK the hole; noting-then-raising is pointless bookkeeping on
  a doomed path (the 4a lesson: only the genuinely-dead arm trips, and
  the whole body of THIS arm is the dead part — the guard condition
  stays as the trigger).
- The TLR-1 shim RETIRES with the body: its classification job ended
  when the last family migrated; it currently only classifies arc-only
  unit runs, and those runs stop being a valid configuration (§5).
- The two `discard` calls are unreachable post-raise; no state
  maintenance is needed on a failing compile.

## 4. Tripwire payload and failure path

New closure `_release_arm_tripwire(val)` (sibling of
`_dead_stake_tripwire`, distinct message class):

    string_arc release-arm tripwire [lastuse_release_arm]:
    fn '<func.name>', block '<block.name>'[<_audit_point idx>],
    value '<val>', producer=<type(producers_fnwide.get(val)).__name__>
    (family=<is_materialized_release_family_producer(...)>),
    use_count=0, consume=False,
    live_out=<val in live_out.get(block.name, set())>.
    The in-pass last-use release arm is corpus-zero after TLR-7
    (every family is pass-materialized and recognition-suppressed);
    a firing means either a STALE UNMIGRATED family temp (pass or
    recognition defect) or a NON-FAMILY owned producer reaching a
    non-consuming drain (new emission shape needing family review).
    File issues/string-arc-release-arm-tripwire/ with the compiling
    source and this full message.

- `producer` resolves via `producers_fnwide` (in scope; the family
  flag distinguishes the two firing classes in the payload itself).
- `use_count=0` / `consume=False` / `live_out=False` are implied by
  the branch condition but recorded explicitly so the structured
  message is self-contained.
- AssertionError → the existing driver boundary wrap
  (phase="string_arc") → clean `internal: string ownership stake
  contract failure` diagnostic; operators never see a traceback —
  identical containment to 4a/4b.
- New intake doc `issues/string-arc-release-arm-tripwire/description.md`
  mirroring the dead-stake intake (which uses the same filename).

## 5. The hidden cost this checkpoint surfaces: the arc-only pin
configuration dies with the arm

Nearly every existing reporter pin runs `insert_string_arc` WITHOUT
the materialization pass (the A/B pins' config-A leg, the conformance
pin's live-pass half, the TLR-1 shim pin, the teeth pins' operand
temps).  Those funcs contain family temps draining in-block → they
exercise the release arm today → EVERY such pin would fire the
tripwire.  This is not collateral damage; it is the correct semantics:
after this slice, "insert_string_arc without the pass" is no longer a
valid pipeline configuration for MIR containing family temps.

Required battery migration (the bulk of the slice):
- A shared test helper (`_run_pipeline(func, ...)`: materialize →
  attach ledger → insert_string_arc) replaces the bare
  `_attach_ledger + insert_string_arc` pairs.
- A/B byte-identity pins collapse to SINGLE-CONFIG assertions: their
  historical job (prove the pass reproduces in-pass emission) is done
  and un-testable once the in-pass author is gone; the surviving
  assertions are the pass's output layout (release positions by
  draining-instruction shape — already asserted in the B legs),
  recognition/audit counters (materialized counts, zero temp_lastuse),
  and idempotence.  The A legs and `agg_a == agg_b` comparisons are
  deleted, with the pin docstrings recording the retirement rationale.
- The TLR-1 shim pin retires entirely (its subject is deleted); its
  emission-position assertions fold into the surviving single-config
  pins.
- Conformance pins keep the calculator half unchanged and run the
  live-pass half through `_run_pipeline`.

## 6. New pins (the guard's own teeth)

Three pins; the first two are unit-level (AssertionError match on
`lastuse_release_arm`), the third is the mandatory end-to-end
containment check:
- **stale unmigrated family temp**: a ConstString temp draining
  non-consumingly, `insert_string_arc` called WITHOUT the pass (the
  exact configuration the tripwire exists to catch — a pass/recognition
  regression manifests as this);
- **truly non-family owned temp**: a StringRetain-produced temp (owned
  via the StringRetain arm, NOT a family member) draining
  non-consumingly — pass runs and correctly materializes nothing; the
  arm fires on the non-family producer; payload's family=False
  distinguishes it.
- **driver-level diagnostic pin (MANDATORY — review amendment)**: an
  end-to-end compile through the driver whose MIR reaches the arm,
  asserting the user-facing failure is the clean
  `internal: string ownership stake contract failure` diagnostic with
  the `lastuse_release_arm` payload — never a Python traceback.  The
  §4 containment promise is a user-facing contract; unit-level
  AssertionError coverage does not test the boundary wrap, so this pin
  is REQUIRED (4a precedent).

## 6a. Pipeline-precondition documentation (review amendment)

`string_arc.py`'s module doc still presents `insert_string_arc` as a
standalone pass.  The slice must document the new precondition at BOTH
surfaces:
- module doc: in production, `materialize_lastuse_releases` MUST run
  before `insert_string_arc` (the driver's cleanup_authoring loop does
  this); with the release arm fail-closed, bare `insert_string_arc` on
  MIR containing family temps that drain non-consumingly TRIPS by
  design;
- a comment at `insert_string_arc` itself: direct unit use is valid
  ONLY for tests that intentionally avoid the arm (no family temps, or
  all consumed/live-out/pre-materialized) or intentionally REACH it
  (the tripwire pins).

## 6b. Direct-caller sweep (review amendment) — ALL files, pre-classified

`insert_string_arc` is called directly from FIVE test files.  Scanned
and classified now so nothing surfaces late:
- `test_string_arc_audit_reporter.py` — the battery migration of §5
  (many family temps reach the arm today; migrates to `_run_pipeline`).
- `test_move_from_ref_string_arc_contract.py` — ONE family temp
  (`t_str`, ConstString) but it is CONSUMED by a ConstructVariant
  String field → never reaches the arm.  SAFE AS-IS.
- `test_string_arc_return_swap.py` (6 calls),
  `test_drop_before_overwrite_swap.py` (7),
  `test_string_arc_recursive_type_guard.py` (4) — construct NO family
  producers at all (zero ConstString/Concat/StringFrom/CopyValue/Exc
  instructions).  SAFE AS-IS.
The slice must (a) re-verify all four non-reporter files run green
under the armed tripwire, and (b) add a one-line classification
comment near each file's insert_string_arc usage stating why it is
exempt from the `_run_pipeline` precondition (per §6a's unit-use
rule), so future MIR additions to those tests re-confront the
decision.

## 6c. `_ensure_owned` release half — removed (review finding)

Review caught a SECOND in-pass temp_lastuse emission surface: the
release bookkeeping inside `_ensure_owned` (before its 4b dead-stake
tripwire).  Since slice 4b, any proven-String value entering
`_ensure_owned` unconditionally raises at the dead-stake tripwire one
statement later — the release half could only execute EN ROUTE TO THE
RAISE (the TLR measurement's "dead-in-effect" corollary), and its
audit note polluted the record of a doomed compile.  The slice REMOVES
it (behavior change: none — every path through it already raised),
making the fail-closed claim complete: `_note_use`'s arm is
`_release_arm_tripwire`; `_ensure_owned`'s proven-String funnel is
`_dead_stake_tripwire`; no live emission site tags
SITE_CLASS_TEMP_LASTUSE_RELEASE.

## 7. Acceptance (vs cleanup-tlr7)

- EVERY counter +0 — materialized stays 618,744 (recognition arm
  unaffected), events unchanged (the shim retirement removes no
  production notes: the arm was corpus-dead), temp_lastuse stays
  absent/0; universe identical; all nine hard gates zero.
- The tripwire firing on ANY corpus fixture is the stop trigger (it
  would mean TLR-7's coverage claim is wrong — stop and report, do not
  ship).
- FULL memcheck STANDALONE stays in gate (emission is untouched in
  production; the gate is regression insurance for the pin-battery
  migration).
- Batteries: reporter (migrated), stage2, ledger-cache guardrails.

## 8. After this slice (not in it)

One clean cert cycle with zero firings → delete the arm together with
the 4a'/4b' tripwire branches (the standing deletion inventory), and
retire `SITE_CLASS_TEMP_LASTUSE_RELEASE` from the closed enumeration
(historical-aggregate compatibility note, as with retired-C4).

## 9. STOP

Awaiting review before implementation.
