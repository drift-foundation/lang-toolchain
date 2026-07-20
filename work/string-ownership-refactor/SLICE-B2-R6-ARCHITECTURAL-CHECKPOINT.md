# Slice B2 — remaining R6 (site-3 + site-4): REPORT-ONLY ARCHITECTURAL CHECKPOINT

Branch `string-arc-endgame-cleanup-authority`; chunk on the single
0.33.87 / ABI 21 endgame candidate. **No implementation authorized.**
This document exists because B2 hits a measured-premise mismatch and a
non-trivial pipeline-placement decision — both listed report-only
checkpoint conditions.

Code facts below are from a direct read of the current tree
(`string_arc.py`, `cleanup_authoring.py`, `hir_to_mir.py`,
`cleanup` ledger driver in `driftc.py`); file:line anchors inline.

---

## 1. Scope

B2 = every remaining R6 responsibility still owned by `string_arc.py`:

| sub-site | population (corpus 924) | authority model TODAY |
|---|---|---|
| **site-3** Return-boundary destructible sweep | **1,088** drops | structural/dataflow sweep **filtered by** ledger verdicts |
| **site-4** drop-before-overwrite (StoreLocal into destructible) | **14** | pure Tier-1 ledger authority (`verdict_at`) |
| nullsafe-destructible StoreLocal sub-case | (subset, unmeasured) | unconditional, no ledger |

Removing all three is what lets `string_arc.py` eventually be deleted
(R3/R4/R8/R5/R1 leave in C/D).

---

## 2. Site characterization — the distinction that governs everything

### 2.1 Site-3 is NOT pure ledger authority

`_drop_all_destructibles` (string_arc.py:415) at each Return
(call @1802) iterates `sorted(destructible_locals)` and drops each
local that is **in `initialized_at_return`** and **not in
`skip_cleanup_locals`**. The emission decision is *set membership*,
not a `verdict_at` call. But those two sets are seeded by ledger
verdicts computed just before the sweep:

- `initialized_at_return` (@1688) = `assigned_in` ∪ `store_defs(block)`
  ∪ `store_defs(entry)` — a **definite-assignment dataflow fixpoint**
  (@507-530), PLUS a **ledger `PATH_DEPENDENT` zero-storage widening**
  (@1706-1719): a zero-storage-drop-safe destructible that the ledger
  reports `PATH_DEPENDENT` at the return point is *added* so its
  PHI-zeroed storage is harmlessly dropped on uninit paths.
- `skip_cleanup_locals` (@1454-1749) = `moved_out ∪ explicitly_dropped`,
  plus **ledger `MUST_NOT_DROP` folding** for destructibles (@1597-1612)
  and strings (@1648-1662), plus **`_is_flag_managed`** locals
  (@1746-1749, these are cleanup_authoring's flag-guarded
  responsibility — site-3 must skip to avoid double-drop).

So site-3 is **structural/dataflow authoring filtered by ledger
verdicts.** Any relocation must reproduce BOTH the dataflow and the
ledger-filter, not just a `verdict_at` lookup.

Per-local emission `_drop_destructible_local` (@398) emits, verbatim:
`LoadLocal(tmp) → ZeroValue(zero) → StoreLocal(local, zero)
[synthetic_zero_back=True, @409-411] → DropValue(tmp)`.

### 2.2 Why 1,068 of 1,088 have no CleanupHook (measured-premise root cause)

HIR→MIR **deliberately** does not scope-register the locals site-3
catches:

- **Error binders** (`catch e {…}`): `_visit_stmt_HTry`
  (hir_to_mir.py:9896-9908) and `_visit_expr_HTryExpr` (@8515-8527)
  `ensure_local` + type + `StoreLocal` the binder but call **no
  `_register_drop_local`** — flagged `# materialize-audit: allow
  consumed`. The binder's drop is hand-authored inline on only *some*
  edges: fall-through (`if terminator is None` @9923/8543) and
  rethrow (a single-candidate `CleanupHook` in `_visit_stmt_HThrow`
  @9635-9642). An **early `return` out of a catch arm** hits neither —
  that is the "no hook at all" class.
- **Discard temps** (`__discard.t*`): minted name-only by
  `_canonical_local` (@1191-1194) with no registration; the pure
  expr-statement discard path emits an inline `DropValue` (@8840) with
  no local at all.

The load-bearing invariant (cleanup_authoring.py:350-393): **a local
is invisible to cleanup_authoring unless `_register_drop_local`'d onto
a live scope before an exit hook fires.** No registration → not a
`CleanupHook` candidate → `verdict_at` never called → no authored drop.
**Site-3 is the Return-boundary safety net for exactly these
deliberately-unregistered locals.** The 20 `CA_EMITS_ELSEWHERE` cases
have a hook at a *different* point (e.g. a normal scope exit) but not
at the Return where site-3 fires.

### 2.3 Site-4 is pure ledger authority with a live proof obligation

Main site-4 path (string_arc.py:851-945): StoreLocal into
`destructible_locals` → `verdict_at((block.name, _instr_idx),
local, needs_drop)` on ledger A → `MUST_DROP` emits
`_drop_destructible_local` before passing the store through.
Critically:

- **`_instr_idx` is the enumerate index over the ORIGINAL
  `block.instructions`** — ledger A is keyed to original indices, and
  string_arc builds `new_instrs` separately while querying original
  indices. This is why alignment "just works" today.
- **`PATH_DEPENDENT → RuntimeError` (@908-922)** is a *proof
  obligation*, not defensive code: the lattice is asserted never to
  yield MaybeUninit at an overwrite point (100% agreement across 1031
  cases). Any relocation MUST retain this tripwire and the exact
  original-index alignment, or the proof is silently voided.
- Missing-ledger → `RuntimeError` (@881). Nullsafe sub-case
  (@847-850) is unconditional (no ledger).

---

## 3. Candidate architectures

### Option A — dedicated destructible-cleanup MIR pass (relocation)

Extract site-3 + site-4 (+ nullsafe sub-case) into a new sibling MIR
pass (working name `destructible_cleanup`), structurally analogous to
B1's `overwrite_cleanup`, at string_arc's current pipeline slot.

- Site-4 and site-3 both query ledger A using the **same
  enumerate-original-index / build-new_instrs** pattern string_arc
  uses today → verdicts are byte-identical, alignment preserved by
  construction, tripwire carried verbatim.
- Site-3's dataflow (`assigned_in`/`store_defs`) and ledger-filter
  (`MUST_NOT_DROP` fold, `PATH_DEPENDENT` widen, `_is_flag_managed`)
  move verbatim; the sweep and `sorted(destructible_locals)` order are
  preserved → **destruction order identical.**
- **HIR→MIR untouched** (pure MIR pass).

### Option B — creation-site lifetime-hook model (consolidation)

Register error-binders and discard-temps via `_register_drop_local`
/ `_materialize_owned_temp` at their creation sites in HIR→MIR. The
existing scope-exit `_emit_scope_cleanup_hook` sweeps then include
them as candidates, and the **existing** `cleanup_authoring` pass
authors their drops against the ledger. Site-3 is deleted with **no
replacement pass**; the inline binder drops (@9918-9927/8538-8547) and
the single-candidate throw hook (@9635-9642) are retired, since the
ledger's `MOVED_OUT`/`MUST_NOT_DROP` verdict already suppresses
redundant drops on consumed edges.

- The infrastructure exists and is the *intended* mechanism — this is
  the architecturally "correct" end-state (single cleanup authority).
- BUT it **expands HIR→MIR ownership semantics**: error-binders and
  discard-temps become first-class scope-registered locals (today they
  are deliberately outside ownership tracking). It requires preserving
  the `MoveOut`-before-transfer discipline on every propagation edge
  (@9873-9887) so `verdict_at` returns skip/`MUST_NOT_DROP` on
  moved-out paths — i.e. the special-case markers become *invariants
  the new registration must not violate*.
- **Blast radius is the error-binder/unwind path** — historically the
  densest bug cluster in this compiler (throw-unwind Destructible
  drop; typed-catch binder into ctor field double-free; match-arm
  `move` binder zeroed variant; VT-capture atexit UAF; cb_drop phantom
  destroy of moved-out captures). Changing what gets scope-registered
  here reopens all of it.
- **Destruction order changes** from site-3's `sorted(destructible_
  locals)` to cleanup_authoring's candidate order (reversed scope
  stack, then reversed locals) — must be proven equivalent or benign.

### Site-4 disposition (decoupled from site-3)

Site-4 is instruction-local ledger authority, unlike site-3's sweep.
It cannot go into B1's `overwrite_cleanup` as-is: that pass runs
*after* string_arc with **no ledger**, and site-4 needs ledger A at
original indices. Under Option A it rides the same
`destructible_cleanup` pass (ledger A, aligned). Under Option B it
still needs a ledger-bearing home — it does **not** dissolve into
creation-site hooks (it is an overwrite drop, not a scope-exit drop),
so even Option B leaves site-4 as ledger authority in a dedicated
pass. **=> site-4 → Option-A-style relocation regardless.**

---

## 4. Pipeline placement (explicit)

String_arc still has ledger consumers during B2 (C/D not yet done):
its String scope-exit `MUST_NOT_DROP` folding (@1648-1662) reads
ledger A. So B2 cannot assume string_arc is ledger-free.

| placement | ledger consequence | verdict |
|---|---|---|
| **B2 pass BEFORE string_arc** | B2 mutates MIR → **invalidates ledger A** → string_arc's remaining String consumers read stale → **forced rebuild** before string_arc; rebuild shifts indices, needs proof it preserves string_arc's String decisions | viable but adds a rebuild + a String-decision-preservation proof |
| **B2 pass AFTER string_arc** | string_arc already inserted String releases and **shifted indices** + marked ledger dirty → a fresh **ledger B** has DIFFERENT indices than site-4's original alignment → **breaks the "exact original (block,idx)" requirement** and voids the PathDependent proof | **REJECTED for site-4** |
| **B2 pass AT string_arc's slot, on ledger A, original-index query** (Option A realization) | consumes ledger A exactly as string_arc does today (original-index keying); marks dirty after; string_arc-residue then needs **one rebuilt ledger A′** | **RECOMMENDED**; alignment + tripwire preserved by construction; cost = +1 ledger rebuild/function |

The inescapable compile-time cost of *separating* R6 from string_arc:
today site-3/site-4 and the String work share one pass on one ledger A;
splitting them introduces **one additional `build_and_attach_ledger`
per function** between the destructible pass and the string_arc
residue (until C/D dissolve string_arc, at which point the ledger
sequencing is re-planned wholesale). Ledger build is O(instructions);
924-fixture corpus impact should be measured before commit, not
assumed negligible.

---

## 5. Comparison matrix

| axis | Option A (relocation) | Option B (creation-site hooks) |
|---|---|---|
| **destruction order** | identical (`sorted(destructible_locals)`, same sequence) | changes to hook candidate order — must prove equivalent |
| **duplicate Return emissions** | risk only if site-3 not removed from string_arc atomically with the new pass — same discipline B1 used | risk if site-3 sweep still covers now-registered binders — must remove site-3 coverage atomically with registration |
| **compile-time rebuild cost** | +1 ledger rebuild/function (measure on corpus) | no new MIR pass; cleanup_authoring processes ~1,068 more candidates + HIR→MIR registration (marginal) — but no extra full rebuild |
| **HIR→MIR semantic expansion** | **none** (pure MIR pass) | **yes** — binders/discard-temps become scope-registered; retires inline drops + throw hook; MoveOut-before-transfer becomes an invariant |
| **blast radius** | bounded MIR cleanup pass | error-binder/unwind path — historically highest bug density |
| **site-4 alignment + tripwire** | preserved by construction (ledger A, original indices) | site-4 still needs Option-A relocation anyway |
| **endgame fit (deletes string_arc?)** | yes — R6 leaves; string_arc.py deletable after C/D | yes for site-3; site-4 still needs a pass |
| **proof obligation** | bounded: pass reproduces today's sets/verdicts at same ledger/indices; corpus +0 | broad: unwind-edge ownership correctness across all catch/throw/discard shapes; memcheck-heavy |

---

## 6. Recommendation

**Option A for both site-3 and site-4**, realized as one
`destructible_cleanup` MIR pass at string_arc's current slot consuming
ledger A with original-index queries, then a rebuilt ledger A′ for the
string_arc residue. Reasons:

1. It is the only option that preserves site-4's exact-alignment +
   PathDependent proof obligation *by construction*.
2. Site-4 needs a ledger-bearing relocation under **either** option, so
   Option B does not save the site-4 work — it only adds the HIR→MIR
   risk for site-3.
3. It keeps B2 a bounded, MIR-only, corpus-+0-provable relocation
   consistent with B1 — the endgame's job is to *remove R6 from
   string_arc so the file can die*, not to re-architect error-binder
   ownership.
4. Option B touches the compiler's highest-risk area (error-unwind
   ownership) for no endgame-required benefit.

**Option B is a legitimate future consolidation** (fold error-binders
and discard-temps into first-class scope registration, delete site-3
outright) but should be its **own project** — design-first, memcheck-
gated, sequenced against the known unwind bug cluster — **not gated on
or bundled into the string-arc endgame.** Recorded as a forward thread.

---

## 7. Open questions for the maintainer (decide before implementation)

1. **A vs B for site-3**: adopt Option A (relocate, recommended) and
   file Option B as a separate future project? Or invest B now?
2. **Ledger-rebuild cost**: acceptable to add one ledger rebuild/
   function for the B2 window (until C/D re-plan ledger sequencing), or
   should B2 wait and be co-designed with C/D's ledger plan to avoid a
   transient rebuild that C/D removes anyway?
3. **Nullsafe sub-case**: ride the same `destructible_cleanup` pass
   (unconditional, no ledger) — any objection?
4. **Pass identity for the endgame**: is a standalone
   `destructible_cleanup` pass the intended final home, or should R6
   land somewhere already planned for C/D (avoiding a pass that C/D
   would immediately reshuffle)?

## 8. STOP

Report-only. No implementation, no code changes. Awaiting decision on
§7.
