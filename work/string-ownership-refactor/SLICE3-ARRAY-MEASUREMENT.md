# Slice 3 — Array release-elision MEASUREMENT (report-only)

Status: MEASUREMENT REPORT — per plan, EXPLICIT STOP: no implementation in this
slice regardless of the numbers. Branch `refactor/string-authority-cleanup`,
tree = 0.33.82/ABI 21 + Slice 2 Part 2.

## 1. Instrumentation (reporter-side, inert by construction)

- `string_arc`: at the Return-boundary sweep, each Array local about to be
  dropped by `_drop_all_arrays` (i.e. in `array_locals` and NOT in
  `skip_cleanup_locals`) is recorded via the new
  `StringArcAudit.note_array_drop(subject, point, needs_drop)` — same boundary
  convention as `note_return_boundary`, `needs_drop` from
  `DropPolicy(ty).needs_drop` at the note site.
- Reporter: array drops live in a SEPARATE inventory (never
  `StringStakeEvent`s), so the string `events` counter and every C1–C3
  comparison are untouched by construction. Per drop, finalize emits:
  `site_class:scope_exit_arraydrop`, `arraydrop_state:<raw>`,
  `arraydrop_verdict:<classify(raw, needs_drop)>`. Counted-only — never
  divergent, never a gate.
- Out of scope (deliberate): the drop-before-overwrite array drop
  (`StoreLocal` path in string_arc) is not measured; only the return-boundary
  sweep, per plan.

Pin: `test_arraydrop_measurement_mix_and_inertness` (mix over
live/moved_out/uninit + drop-free type folding + `events == 0` inertness).
Batteries: reporter 13/13; stage2 + slice-1 guardrails 337/337.

## 2. Inertness proof (corpus)

Run `build/tmp/cleanup-slice3` vs the Slice 2 reference
(`build/tmp/cleanup-part2`), tool v1.4.0, exit 0: universe identical, and
EVERY pre-existing counter +0 — including `events` (2,775,744), all C3
classes, and all string site classes. The instrumentation is corpus-inert
exactly as designed.

## 3. The mix (924 fixtures, 156,308 swept drops)

| counter | value | share |
|---|---|---|
| site_class:scope_exit_arraydrop | 156,308 | 100% |
| arraydrop_state:uninit | 141,391 | 90.5% |
| arraydrop_state:moved_out | 10,297 | 6.6% |
| arraydrop_state:maybe_uninit | 4,620 | 3.0% |
| arraydrop_state:live | 0 | 0% |
| arraydrop_state:tombstoned | 0 | 0% |
| arraydrop_verdict:must_not_drop | 151,688 | 97.0% |
| arraydrop_verdict:path_dependent | 4,620 | 3.0% |
| arraydrop_verdict:must_drop | **0** | 0% |

**Zero LIVE, zero MUST_DROP, corpus-wide.** Every array drop the return-
boundary sweep emits today is either provably dead (97%) or path-dependent
(3%).

## 4. Why live = 0 is structural, not luck (probe-verified)

A trivially-live array at return (`var arr = []; arr.push(..); return 0;`)
was probed: it produces NO sweep events. The MIR shows why — live arrays
never reach the sweep:

- `cleanup_authoring` (3C) owns their scope-exit drops: inline
  `MoveOut + DropValue` before the Return. That MoveOut puts the local into
  string_arc's own `moved_out_locals` path-dataflow, and the Return branch
  starts with `skip_cleanup_locals |= moved_out_locals` — so the sweep skips
  it (and the drop already happened, correctly).
- Return-source arrays are skipped by the alias walk (return-by-move).

So the sweep is a LEGACY BACKSTOP that fires only where string_arc's own
block-path tracking (`moved_in`) cannot prove the local dead — and the
lattice now proves that every one of those 156,308 emissions is a no-op
(the 10,297 ledger-MOVED_OUT ones quantify exactly the legacy-tracking vs
lattice precision gap; the 141,391 uninit ones are paths that never
initialized the local at all).

## 5. Projected elision win

- Unconditional: 151,688 drops/corpus (97%) elidable at MUST_NOT_DROP
  boundaries — the exact strings-release-elision fold, applied to
  `array_locals`. Each elided drop removes a Load+Zero+Store+DropValue quad
  plus a runtime element-walking drop call on zeroed/never-written storage
  (~600k dead instructions corpus-wide at the observed volume).
- PATH_DEPENDENT (4,620, 3%): keep today's unconditional null-safe drop in a
  first slice (mirroring the strings elision decision), or extend the
  zero-safe argument later (zeroed Array = null buffer / len 0 → element
  walk is vacuous). Not required for the main win.
- With both, the sweep goes to zero and becomes deletable — but that is
  string_arc-deletion-campaign (Slice 4) territory, not this measurement.

## 6. Safety notes (Array-specific)

- MUST_NOT_DROP-only elision cannot skip a real drop anywhere in the corpus:
  live = 0 at the sweep means there is NO case where the sweep is
  load-bearing for a live value.
- The historical blocker (0.27.145 memcheck regression; the "authority
  boundary" comment at the destructible consultation) was the STRING
  return-retain wrap making MOVED_OUT verdicts wrong post-rewrite. Arrays
  have no analogous late retain-wrap at return (return-by-move only), and
  the B-arch precondition that unlocked strings (every stake ledger-visible)
  has no array counterpart to violate — but the implementation slice must
  re-verify this explicitly (pin: array return-source shapes under memcheck)
  before folding arrays into the ledger consultation.
- Element-walking drops: elision only removes walks the lattice proves
  vacuous; guarded/authored drops (cleanup_authoring) are untouched.
- Guardrail direction per plan: MUST_NOT_DROP-only, PATH_DEPENDENT keeps its
  unconditional drop; memcheck lanes in-gate from the start.

## 7. Recommendation

**GO** — implement Array release-elision as its own future slice (it is an
emission change: needs its own predicted-delta acceptance —
`site_class:scope_exit_arraydrop` 156,308 → 4,620 (path-dependent retained),
all string counters byte-identical, memcheck in-gate). Natural sequencing:
it shares the acceptance instrument and the elision fold shape with the
recorded flag-refined-ledger future slice, but does not depend on it.

## 8. STOP

Per plan, explicit stop — no implementation in this slice. Measurement
artifacts: `build/tmp/cleanup-slice3` (universe identical to cleanup-part2).
