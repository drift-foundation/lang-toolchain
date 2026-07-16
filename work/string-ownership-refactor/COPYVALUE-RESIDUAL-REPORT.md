# CopyValue residual checkpoint (report only)

Status: REPORT ONLY — no implementation.  Prerequisite state: TLR-5
accepted (`build/tmp/cleanup-tlr5`: temp_lastuse_release 18,493 =
CopyValue 11,095 + cross-block 7,398; all nine hard gates zero).

## 1. Provenance split — measured, lossless

Instrumentation: `_tm_origin` tags at EVERY CopyValue emit site
(string_stakes' `_stake` + all ten hir_to_mir sites), shim split
`TM_cv_{origin}`; scratch run `build/tmp/cv-measure` (exit 0, universe
identical 924/344/49, events 2,772,052 unchanged, materialized 600,251
unchanged, plain temp_lastuse exactly the cross-block 7,398);
restoration via stored reverse edits (14 edits reversed, zero
`TM_`/`_tm_` refs, battery 47/47).  No `untagged` bucket — the origin
attribute survived to string_arc for every instance.

| emit site | count |
|---|---|
| **hir_to_mir `array_elem_copy` (bounds-checked `arr[i]` value read)** | **9,246** |
| **hir_to_mir `array_elem_field_copy` (`arr[i].field` read)** | **1,849** |
| string_stakes (B-arch call-arg/value-position stakes) | 0 |
| ref_deref_copy / match_scrut_dual_owner / match_binder_field | 0 |
| deref_value_copy / array_elem_copy(4270) / store_value_copy | 0 |
| raw_elem_cheap_copy / array_index_elem_copy / array_index_store_copy | 0 |
| **sum** | **11,095 — lossless** |

## 2. The B-arch stake question — answered: ZERO released-unused stakes

The original TLR measurement annotated this bucket "CopyValue
(string_stakes stakes!)" — that attribution is now DISPROVEN.  Every
`.stake<n>` CopyValue is CONSUMED at its anchoring call/value position
(that is its purpose — string_arc moves it), so stakes never reach the
last-use release arm.  The stake machinery has no precision problem in
this population; the historical note is corrected by this report.

## 3. The two real populations, classified per the rubric

### 3a. `array_elem_field_copy` — 1,849: REAL OWNERSHIP → migrate

Lowering shape (`hir_to_mir.py` ~3777): `arr[i].field` reads through
`AddrOfArrayElem → AddrOfField → LoadRef` — a BORROWED field view with
no +1 — then `CopyValue` materializes the owned copy (codegen: String
CopyValue lowers to `drift_string_retain`).  The copy is semantically
NECESSARY under current boundary contracts: the view borrows the
array's element storage, and using it past any array mutation would be
a use-after-free.  When the copy's last use is non-consuming, its
release is REAL ownership cleanup — exactly the materialized-release
family shape.  Classification: **migrate**.

### 3b. `array_elem_copy` — 9,246: real ownership NOW; eliminable churn LATER

Lowering shape (~4065, bounds-checked `arr[i]` value read):

    ok_block:  %d = ArrayIndexLoadUnchecked(...)   ; owned +1 (B-arch-1d)
               StoreLocal(__tmp, %d)               ; consumed into hidden local
    join:      %loaded = LoadLocal(__tmp)          ; borrowed VIEW
               %copy = CopyValue(%loaded)          ; owned +1 (the residual)

Two stacked owners: the hidden `__tmp` local keeps the extraction +1
(released by its scope cleanup) AND the CopyValue takes a second +1.
Classification is two-layer:
- The RELEASE of `%copy` is real ownership cleanup today → **migrate
  now** (mechanical, same acceptance ladder — no reason to hold the
  release-authority migration hostage to a lowering redesign).
- The COPY itself is **eliminable churn at the source**: `__tmp` is a
  compiler-internal single-purpose local, so the join block could
  `MoveOut(__tmp)` instead of `LoadLocal + CopyValue` — transferring
  the extraction +1 directly, deleting one retain+release pair per
  element read AND `__tmp`'s scope release.  That is a LOWERING slice
  with its own design gate (drop-flag interaction for `__tmp`,
  loop-reuse zero-back, PHASE-1-RESIDUAL classification comment at the
  site) and a DIFFERENT corpus signature (events DECREASE — CopyValue
  count, release count, and scope-exit releases all drop; est. order
  −9,246 copies and a matching release reduction, exact numbers
  measured at that slice's own checkpoint).  Not blocked by, and not
  blocking, the migration.

No "necessary churn due to boundary contracts" bucket remains: 3a is
necessary-but-real-ownership (migrate), 3b's churn is eliminable (not
boundary-forced).

## 4. Proposed paths and exact expected corpus deltas

**Path A — TLR-6 migration slice (recommended next):** add
`M.CopyValue` to the UNCONDITIONAL family membership in
`is_materialized_release_family_producer` (String-typed-dest condition
stays caller-side, as always).  Safety: a String CopyValue dest is an
unconditional +1 owner (codegen retain); `.stake` CopyValues are
consumed at their anchors → never qualify → string_stakes unaffected
(measured zero); all other CopyValue sites measured zero at last-use.

**REQUIRED SUPPRESSION (review amendment, 2026-07-16): the CopyValue
owned-registration arm needs the recognized guard.**  Unlike Call and
Exc* (prepass-only registration, covered by the per-block
`owned_values -= recognized_released` subtraction), CopyValue has a
LIVE rewrite-loop re-add arm (`string_arc.py` ~1898:
`elif isinstance(instr, M.CopyValue): if _is_string_tid(instr.ty):
owned_values.add(instr.dest)`).  The subtraction runs BEFORE the
rewrite loop, so without a guard the arm re-adds the recognized temp
and `_note_use` emits a SECOND release at the drain (the
pre-materialized release having been copied through).  The slice MUST
add the same `if instr.dest not in recognized_released:` guard the
ConstString and StringFrom*/StringConcat arms carry — this is the
TLR-2b owned_defs lesson repeating at the arm level, and it gets a
dedicated TEETH pin: a CopyValue temp with a pre-materialized release
after its last non-consuming use → exactly ONE release survives in the
output MIR and zero temp_lastuse_release — a pin that FAILS if the
predicate is extended without the arm guard.
- `temp_lastuse_release` 18,493 → **7,398** (−11,095; per-site
  sub-check 9,246 + 1,849);
- `materialized_lastuse_release` 600,251 → **611,346** (+11,095);
- sum conserved; every other counter +0 (incl. events); universe
  identical; gates zero; full memcheck STANDALONE.
- Pins per the established ladder: CopyValue-family A/B byte-identity
  (view-source copy shape), multi-use/consumed carriers, cross-block
  stays out, misplaced/duplicated CopyValue release trips — and the
  out-of-contract SHAPE carrier migrates a THIRD time (CopyValue joins
  the family): next non-member carrier is a StringRetain-produced
  temp.  Heap memcheck row: `Array<String>` element reads compared
  non-consumingly (both sites live).
- After Path A the ONLY temp_lastuse population is cross-block 7,398 —
  the `_note_use` release-arm tripwire becomes reachable once the
  cross-block lifetime analysis lands.

**Path B — element-read churn elimination (separate, later):** the
§3b MoveOut lowering change.  Report-first slice with its own
measurement (its corpus signature moves `events`, CopyValue counts,
scope_exit_release, and — post-Path-A — materialized counts DOWN;
"everything +0 except the enumerated decreases" acceptance).  Ordering:
AFTER Path A (the migration keeps the release authority uniform while
the lowering slice shrinks the population underneath it).

**Cross-block `none` (7,398): OUT OF SCOPE here**, reaffirmed — needs
the per-block→cross-block lifetime analysis flagged since the first
measurement.

## 5. STOP

Awaiting review.  Recommended sequence: Path A as TLR-6 (one reviewed
slice, exact ±11,095), then the cross-block design gate, with Path B
as an independent optimization proposal once the release ladder is
closed.
