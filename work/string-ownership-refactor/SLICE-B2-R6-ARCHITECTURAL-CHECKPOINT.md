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
- **Anonymous `catch _` binders** (the `__discard*` slots in this
  population — CORRECTED 2026-07-20 per maintainer): a `catch _` has
  no binding id and fallback `"_"`, so the catch-binder path calls
  `_canonical_local(None, "_")` → `__discard{n}` (@1188-1191) and
  **deliberately omits `_register_drop_local`**, exactly like a named
  error binder. These are **part of the error-binder population**, not
  a separate expression-statement-temp mechanism.
  - NOT this population: the pure expression-statement discard path
    (`_visit_stmt_HExprStmt`, inline `DropValue` @8840) creates **no
    local at all**, so it cannot populate site-3; and an ordinary
    `HLet(name="_")` carries a real binding id → flows through
    `_register_drop_local` (@9017) and IS hooked. Neither is a site-3
    source. **There is no separate "discard-temp" owned-local
    population** — every `__discard*` slot in site-3 is an anonymous
    `catch _` binder.
- **One non-binder outlier**: the census is **1,087 ERROR-kind + 1
  STRUCT** (SLICE-B §3.1), so the population is *not* exclusively
  catch/error binders. The single `STRUCT`-typed site-3 drop is
  identified and explained in §2.4 below.

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

### 2.4 The one non-error site-3 drop (STRUCT outlier) — identified

The census kind-split is **1,087 ERROR + 1 STRUCT** — so site-3's
population is NOT exclusively catch/error binders. Measured (temporary
site-3 probe over the 924-fixture corpus; total reproduces **1,088**
exactly, instrumentation proven inert: run counters byte-identical to
flagret, compiled-OK set identical), the single STRUCT event is:

- **fixture** `closures_share_capture_arc_generic`
- **fn** `__lambda_main_0_0`, **local** `app`, **type** `Arc`
  (`Arc<T>` is `STRUCT`-kind)

Mechanism: the immediate lambda
`(| | captures(share app) => { val a = app.get(); return a.read(); })()`
**`share`-captures** `app: Arc` — the closure env carries a shared Arc
stake, and inside the lambda body `app` is **materialized as a
destructible struct local**. It is initialized at the lambda's
`return`, not moved out, so site-3's sweep drops it (releasing the
captured refcount).

**Why this matters architecturally:** site-3 is a **general
destructible-Return sweep**, not merely an error-binder safety net. A
binder-only registration (deferred Option B) would NOT cover this
`share`-captured Arc lambda-local — it is a **second live site-3-only
authority class** (already realized, not a hypothetical future
trigger). The Option-B refactor-trigger entry is corrected accordingly:
its scope now covers ALL current site-3-only lifetime classes (catch
binders + immediate-lambda MOVE/SHARE capture locals), and the future
triggers are recast to a semantic defect in a known class or a
**third/new** category. Under the APPROVED Option A relocation the full
structural sweep moves verbatim, so this case is covered identically
(it is one of the 1,088 planned site-3 decisions, dropped in `sorted`
order). **Bijection pin:** the Arc event MUST remain in the 1,088
site-3 bijection during B2+C; STOP if its exact release placement
changes.

**Immediate vs callback capture ownership asymmetry** (recorded for the
B2+C cleanup inventory; verified in `hir_to_mir.py`):

- **Callback** MOVE/SHARE captures `continue` early in
  `_emit_lambda_capture_prologue` (@5759-5767) — **no body local is
  created**; the heap env field is the +1 owner and is dropped by the
  callback env-drop thunk.
- **Immediate-lambda** MOVE/SHARE captures do NOT take that early exit:
  they load the env field into a body local (@5768-5769) but
  deliberately skip `_register_drop_local` (@5776). The **stack**
  immediate env has no independently registered / drop-authored owner,
  so **site-3's Return drop is the live release authority** for that
  captured stake.
- Consequence: the generic comment at `hir_to_mir.py:5770-5776` — which
  says MOVE/SHARE body locals are "released by the env drop thunk" and
  registering would double-drop — is **overbroad**: it holds for
  callbacks but NOT for immediate lambdas (which have no thunk; site-3
  releases). This comment must be **retargeted in B2+C** to pin the
  immediate/callback distinction. This is an **authority-comment
  correction only** — NOT approval to change HIR→MIR ownership behavior.

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

Register error binders (named, and anonymous `catch _` which lower to
`__discard*` slots) via `_register_drop_local` / `_materialize_owned_temp`
at their creation sites in HIR→MIR. The
existing scope-exit `_emit_scope_cleanup_hook` sweeps then include
them as candidates, and the **existing** `cleanup_authoring` pass
authors their drops against the ledger. Site-3 is deleted with **no
replacement pass**; the inline binder drops (@9918-9927/8538-8547) and
the single-candidate throw hook (@9635-9642) are retired, since the
ledger's `MOVED_OUT`/`MUST_NOT_DROP` verdict already suppresses
redundant drops on consumed edges.

- The infrastructure exists and is the *intended* mechanism — this is
  the architecturally "correct" end-state (single cleanup authority).
- BUT it **expands HIR→MIR ownership semantics**: error binders
  (including anonymous `catch _`) become first-class scope-registered
  locals (today they are deliberately outside ownership tracking). It
  requires preserving
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

### Semantic choice — DECIDED (maintainer 2026-07-20T223728Z)

**Adopt Option A's RELOCATION SEMANTICS. Defer Option B.** Do NOT
expand HIR→MIR catch/unwind ownership during the string-arc endgame.
Option B (creation-site lifetime registration) is filed as a separate
design project in `doc/refactor_triggers.md` (§7 below). The rest of
this document describes the APPROVED realization, which is NOT the
standalone-pass sketch originally proposed under "Option A."

**Rejected realization (do not build):** a standalone
`destructible_cleanup` pass that mutates MIR, marks ledger A dirty,
and forces a rebuilt ledger A′ for a residual `string_arc`. C is the
next ledger-consuming work and would immediately remove/re-plan that
rebuild, so the transient sequence is wasted machinery.

---

## 4. APPROVED architecture — combined B2+C chunk

One **B2+C** implementation chunk (not two). The organizing idea: a
single frozen decision plan computed from the ORIGINAL MIR before any
mutation, shared by both the Return authority (site-3 + C's R3/R4) and
the Overwrite authority (nullsafe + site-4).

### 4.1 Frozen ledger-A decision plan (computed once, before mutation)

Compute an **immutable, fail-closed per-function decision plan** from
the original MIR and a fresh **ledger A**, BEFORE any B2/C mutation.
The plan:

- records an **original-anchor record** per decision with: (a) original
  block + original numerical index, (b) the original object
  identity/reference, (c) expected instruction/terminator kind, local,
  and type/operand relationship, (d) consumed state;
- is a plain per-function plan object keyed by **original object
  identity PLUS `(block, original_index)`** — NOT dynamic MIR attributes
  on instructions (the transient-attribute anti-pattern from B1 debt #2
  is explicitly disallowed here).

**Plan-time proof coordinate vs consumption-time location (CRITICAL — the
contract S1 must encode, per maintainer 2026-07-21T001349Z):**
`(block, original_index)` is the **immutable proof coordinate** for the
ledger-A query, validated *when the plan is built*. It must NOT also be
required to equal the instruction's numerical index at CONSUMPTION —
Return emissions and earlier overwrite emissions legitimately shift
current indices. At consumption, the emitter validates:

- the **exact object** is still present **once in the same block**;
- its expected **semantic fields/relationships** (kind, local,
  type/operand) are **unchanged**;
- **original-anchor relative order is preserved**.

A **changed current numerical index is ALLOWED**. FAIL CLOSED on:
disappearance, duplication, movement to another block, replacement,
reordered original anchors, wrong local/type/operand, or unconsumed/
orphan plan entries. (If an implementation ever needs numerical-index
equality at consumption, ALL consumers must run in ONE traversal of the
original snapshot before any mutation — otherwise the object-identity +
same-block-once + relative-order contract is the rule.) **Teeth both
sides:** legitimate insertions BEFORE an anchor must NOT invalidate it;
moving/replacing/duplicating the anchor MUST fail closed.

Because the plan is frozen against original coordinates, later mutation
cannot invalidate the verdicts it carries — this is how **site-4
preserves original-index authority even though its final emission
lives with overwrite cleanup** (the ledger is read once, at planning
time, at the original index; emission is a plan lookup, not a fresh
`verdict_at` on a shifted ledger). The `PATH_DEPENDENT → RuntimeError`
tripwire moves into the PLANNING step (still on ledger A, original
index) and is retained verbatim.

**STOP condition:** if the anchor lifecycle cannot be preserved without
a ledger rebuild or dynamic MIR metadata, stop and report.

### 4.2 Return authority (site-3 + R3/R4)

A narrow Return/scope cleanup authority consumes the planned **site-3**
decisions together with C's **R3/R4** String Return/scope decisions.
Site-3 keeps, unchanged:

- its structural definite-assignment calculation
  (`assigned_in`/`store_defs`);
- its ledger filters (`MUST_NOT_DROP` fold, `_is_flag_managed`);
- its `PATH_DEPENDENT` zero-storage widening;
- its `sorted(destructible_locals)` **destruction order**.

It is **not** reduced to a raw ledger lookup — the structural/dataflow
authoring is planned, then emitted.

**Return-authority choreography (S3/S5 — maintainer 2026-07-21T001349Z):**
S3 and S5 must NOT become two independently-wired Return rewriters over
the same original Return anchor — once S3 replaces/mutates a Return, S5
can no longer truthfully validate the original anchor. Discipline
(preferred): **build and unit-test the site-3 emitter in S3 but DELAY
its production wiring until S5**, when a SINGLE unified Return-authority
traversal consumes site-3 + R3/R4 decisions **atomically** in one
coordinated Return rewrite. (Transitional alternative: `string_arc`
delegates site-3 emission to the new module at its existing Return
point while preserving the original Return object, then S5 replaces the
delegation with the unified authority.) Final production shape: compute
the complete plan ONCE → consume every Return decision in ONE
coordinated Return rewrite → then overwrite cleanup consumes StoreLocal
anchors. **The Return rewrite MUST preserve every original non-Return
instruction object and original-anchor relative order**, so the
overwrite plan (site-4 + nullsafe StoreLocal anchors) remains valid at
consumption.

### 4.3 Overwrite authority (nullsafe + site-4) → `overwrite_cleanup`

`overwrite_cleanup` (B1's pass) remains the final instruction-local
overwrite authority. Into it move:

- the **unconditional null-safe destructible overwrite** (today
  string_arc.py:847-850) — placed here because it is an instruction-
  local overwrite, NOT because it shares `_drop_destructible_local`
  with the Return sweep today;
- **site-4** as a **consumer of the precomputed ledger-A decision**
  (not a fresh/shifted `verdict_at`), retaining the
  `PATH_DEPENDENT → RuntimeError` proof-obligation tripwire (fired at
  planning time on ledger A / original index).

### 4.4 One coordinated late-cleanup phase; no transient rebuild

Keep analysis/planning and emission in **narrow modules behind one
coordinated late-cleanup phase**. Do NOT replace `string_arc` with
another monolith. R8 and the rest of C use the **same original-MIR
planning window**. **After B2+C, no residual ledger consumer forces an
intermediate rebuild.** R5/R1/final deletion remain **D**, on the same
0.33.87 / ABI-21 line.

Rebuild-cost note (corrects the earlier premise): the driver already
narrows rebuilds to mutated functions, and the approved design adds
**zero transient rebuilds** — planning reads ledger A once; emission
consumes the frozen plan. **Ledger-build counts are a gate** (measure:
zero additional builds vs the pre-B2+C pipeline), not an accepted cost.

---

## 5. Comparison matrix — approved semantics (A) vs deferred (B)

The comparison is between the approved **A relocation semantics** and
the deferred **B creation-site model** — NOT the rejected standalone
realization.

| axis | A semantics (APPROVED, via frozen plan) | B creation-site hooks (DEFERRED) |
|---|---|---|
| **destruction order** | identical (`sorted(destructible_locals)`) | changes to hook candidate order — must prove equivalent |
| **duplicate Return emissions** | fail-closed: plan consumed exactly once; site-3 removed from string_arc atomically | risk if site-3 still covers now-registered binders |
| **ledger rebuilds** | **zero transient** (plan read once on ledger A; gated) | none new, but HIR→MIR registration + more hook candidates |
| **HIR→MIR semantic expansion** | **none** | **yes** — binders become scope-registered; retires inline drops + throw hook |
| **blast radius** | bounded late-cleanup planning/emission | error-binder/unwind path — highest bug density |
| **site-4 alignment + tripwire** | preserved: planned on ledger A at original index; tripwire at planning | site-4 still needs A-style planning anyway |
| **endgame fit** | R6 leaves; string_arc.py deletable in D; no monolith | site-3 dissolves, but site-4 still needs planning |
| **proof obligation** | bijective plan + corpus +0 + zero-rebuild gate | broad unwind-edge correctness; memcheck-heavy |

---

## 6. Migration baselines & acceptance (frozen before edits)

| population | baseline | source |
|---|---|---|
| site-3 Return destructible drops | **1,088** | §3 measurement |
| site-4 drop-before-overwrite | **14** | corpus counter |
| null-safe destructible overwrite | **133,998** (§6.1) | measured, new counter |
| R3 scope-exit String release | **68,562** | corpus counter |
| materialized last-use release | **618,744** | corpus counter |

Acceptance for the B2+C chunk (focused gates + end static delta;
NOT a release/cert boundary):

- every production aggregate counter **+0**; all hard gates zero;
- **zero additional ledger builds** vs the pre-B2+C pipeline; no
  stale-ledger reads;
- site-3 sorted destruction order, site-4 original-index verdicts +
  tripwire, String return-alias safety, and TLR-8 recognition/counter
  placement all **exact**;
- focused/memcheck gate covers: error-binder early-return, catch/
  rethrow, `_` catch binder, null-safe first-store/overwrite, site-4
  reassignment, return-alias, and the existing 0.27.145 carriers.

### 6.1 Null-safe overwrite measurement (DONE — before editing)

Measured on the 924-fixture corpus via a temporary env-gated probe at
the string_arc.py:847-850 nullsafe branch (StoreLocal into
`nullsafe_destructible_locals`), per-compile-summed (the same counting
convention as site-4=14 and overwrite_release=233,519). Instrumentation
proven **inert**: the instrumented run's aggregate counters are
byte-identical to flagret and the compiled-OK set matches exactly
(924, zero symmetric difference). Probe reverted byte-identically after.

| metric | value |
|---|---|
| **total null-safe overwrite events** (per-compile-summed) | **133,998** |
| marked-synthetic (`synthetic_zero_back`) events | **0** (user stores only — the marked-provenance skip that B1 established will exclude zero exactly here too) |
| distinct functions | **295** |
| distinct types (by `TypeId`) | **225** |
| distinct `(fn, local, type)` sites | **2,014** |

Population is **disjoint** from site-4: the nullsafe check (@847-850)
`continue`s before the site-4 branch (@851), so site-4's 14 are all
NON-nullsafe destructibles. It is also disjoint from R7 (array locals).

Migration contract: the relocated author in `overwrite_cleanup`
emits **one** null-safe drop per event → an **independent
counter/bijection** must track exactly **133,998** during migration
(0 marked-synthetic excluded), even if the measurement surface is
retired afterward. This is the "one-for-one planned author" the review
requires.

---

## 7. DECISIONS (closed) + deferred Option B

Closed by maintainer 2026-07-20T223728Z:

1. **Site-3 semantics** = Option A relocation; Option B deferred to
   `doc/refactor_triggers.md`.
2. **No transient rebuild** — frozen ledger-A plan shared across B2+C;
   zero additional ledger builds (gated).
3. **Null-safe** overwrites are IN this chunk, placed with
   `overwrite_cleanup`; population measured first (§6.1).
4. **No standalone permanent `destructible_cleanup` phase** — narrow
   planning/emission modules behind one coordinated cleanup phase:
   Return authority (site-3 + R3/R4), Overwrite authority (nullsafe +
   site-4 precomputed verdicts).

**Option B refactor-trigger** recorded in `doc/refactor_triggers.md`.
Its scope covers ALL current site-3-only lifetime classes — named +
anonymous `catch _` binders AND immediate-lambda MOVE/SHARE capture
locals (the latter already a live class, per §2.4, NOT a future
trigger). Recast forcing shapes: a semantic leak/double-drop/ordering
bug in EITHER known class; a **third/new** category of unregistered
owned local needing a Return sweep; or a language feature requiring
these lifetimes to participate in normal scope-order semantics. Scope
when triggered: inline binder/throw-cleanup retirement, a creation-site
drop authority for immediate-lambda captures (+ the immediate/callback
distinction), MoveOut-before-transfer proofs, ordering, unwind+capture
memcheck coverage, removal of the site-3 safety net.

**B1 cleanup debts** (from SLICE-B §10) are carried into this B2+C
chunk: (1) `_validate` occurrence hardening, (2) removal of transient
MIR attributes before final output, (3) remaining StoreRef authority
prose retarget in `test_mut_struct_string_field_self_concat.py`,
(4) removal of the unused `mutated` local in `overwrite_cleanup.py`.

**B2+C cleanup inventory (new):** (5) retarget the overbroad
authority comment at `hir_to_mir.py:5770-5776` — it claims MOVE/SHARE
body locals are released by the env drop thunk (true for callbacks,
false for immediate lambdas, where site-3 is the release authority);
pin the immediate/callback distinction per §2.4. **Authority-comment
correction only — no HIR→MIR ownership behavior change.**

## 8. STATUS

Conditional GO for the combined **B2+C** chunk. Implementation is
authorized once these report-only amendments are recorded AND the §6.1
null-safe measurement lands — no further architecture checkpoint.
During implementation, STOP only for: a failed invariant, an
unexpected counter/emission population, a LANGUAGE_BUG, or an
ABI/runtime implication. B1 must be committed as the recovery point
first (maintainer's git action).
