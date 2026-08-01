# bare-temp-field-projection-uaf — research/plan

Status: FIX IMPLEMENTED, VERIFIED, review rounds 1–6 folded in (see
PROGRESS.md). This file is the research/design record; where it and
PROGRESS.md disagree, PROGRESS.md (and history.md's 0.33.94 entry) is
authoritative. Two matrix items changed against the original design
after empirical checks: (a) MRB — explicit `bump(&mut mk().root)`
actually yields `E_MUT_RVALUE_ARG_BINDING_REQUIRED`, not
`E_REDUNDANT_ARG_BORROW` as speculated below; (b) PROV — the bare-vs-
explicit IR byte-identity premise was REFUTED and replaced by an A/B
lowering-route ownership-parity gate (see the revised §246). A new
value-control-flow rejection (ternary/match rvalue borrows) was added
that this design did not originally enumerate.
Classification: LANGUAGE_BUG, RELEASE-BLOCKING memory unsafety
(teardown-time double free / UAF).
Origin: drift-query announce
/tmp/drift-announce/2026-07-31T071855Z-driftquery-bare-temp-field-projection-uaf.md
(driftc 0.33.92, git ff1bc2b2, cert run 20260731-053507).
Kept SEPARATE from the toolchain-meta-stamps release-gate run.

## 1. Confirmed failure (current tree)

The 26-line heap-backed repro (`peek(mk().root)`, std.core only)
reproduces here on the CURRENT candidate:
- base (no sanitizer): `[drift:contract] String flags: reserved bit
  set`, SIGABRT, exit 134 — the double-release signature.
- `peek(&mk().root)` (the pre-0.33.91 sound spelling): rejected with
  `E_REDUNDANT_ARG_BORROW` — no accepted spelling of this call shape is
  sound today.
- Hoisted control `val pr = mk(); peek(pr.root)`: runs, rc 0.

Variant probes (this tree):
| Shape | Result |
|---|---|
| `peek(mk().root)` — shared, one field hop | UAF (exit 134) |
| `peek(mk().mid.leaf)` — shared, NESTED field | UAF (exit 134) |
| `peek(mk()[0])` — shared, INDEX hop | SOUND (rc 0) |
| `bump(mk().root)` at `&mut Node` | REJECTED ("borrow requires an addressable place; bind to a local first") |
| `val pr = mk(); peek(pr.root)` | SOUND (rc 0) |

The index case being sound is the key diagnostic: the correct lowering
shape (materialize the base ONCE, project an address to the leaf)
already exists and is exercised — the bug is that owned pure-FIELD
chains are routed to a different, unsound path.

## 2. Root cause (exact subsystem)

Subsystem: **MIR ownership lowering — field-projection of an owned
rvalue at a checker-synthesized HBorrow** (`stage2/hir_to_mir.py`).
Two drop registrations of the SAME leaf backing.

Path for `peek(mk().root)`:

1. reject-redundant-call-borrows: the checker wraps the bare argument
   in `HBorrow(subject = HField(HCall mk(), "root"), source_written=False)`.
2. `_visit_expr_HBorrow` (line 3192): subject is not an `HPlaceExpr`
   (it is rooted at an rvalue call), so it tries
   `_lift_rvalue_ref_base_for_borrow`.
3. `_lift_rvalue_ref_base_for_borrow` / `_validate_lifted_chain`
   (line 3277/3414): admits an OWNED base **only for index-bearing
   chains** (comment line 3346-3354). A pure-FIELD owned chain returns
   None — deliberately, to keep the pin
   `autoborrow_owned_rvalue_field_method_unchanged`. So the lift
   REFUSES `mk().root`.
4. Fallback (line 3269): `_materialize_owned_temp_for_borrow(ty=Node,
   value = lambda: lower_expr(mk().root))` — allocate a drop-registered
   temp of type Node (**slot B**), store `lower_expr(mk().root)` into
   it, return its address.
5. `lower_expr(mk().root)` → `_visit_expr_HField` (line 3831):
   `source_is_owned_rvalue` is True (not a ref-load, not an HVar, PR is
   destructible), so it materializes the PR source into a
   drop-registered `__field_src_` temp (**slot A**, line 4054-4057) —
   correct, to release PR's un-read fields — then emits
   `StructGetField(dest, subject=PR, field=root)`: `dest` is a SHALLOW
   bitcopy alias of slot A's `root` (same String/Array backing).
6. Line 4080-4090: `subject_is_alias = source_is_owned_rvalue` is True,
   and the `if subject_is_alias:` at line 4089 (a SEPARATE `if`, not an
   `elif`) DOES fire — `dest` IS correctly marked via
   `_mark_ref_alias_if_non_bitcopy`, i.e. added to `_ref_field_temps`.
   The marking is present and correct.
7. **The actual gap:** slot B is built by
   `_materialize_owned_temp_for_borrow` →
   `_materialize_owned_temp` (line 912), whose `StoreLocal(local, val)`
   (line 969) stores `dest` **without calling `_copy_if_ref_alias`**.
   Every OTHER ownership-transfer boundary (struct/variant ctor,
   return, variable binding, call args) calls `_copy_if_ref_alias`
   (line 858) to deep-copy a `_ref_field_temps` alias before taking
   ownership; the borrow-materialization fallback is the ONE consumer
   that omits it. So the marked-but-uncopied shallow alias is stored
   verbatim into slot B and registered for drop.

Result: **slot A (PR)** drops `root.text` + `root.children` at scope
exit, and **slot B (Node)** — holding the same aliased `root` backing,
never deep-copied — drops `root.text` + `root.children` AGAIN. Double
free → `String flags: reserved bit set` / heap-use-after-free.

The reviewer's specific hypothesis is CONFIRMED verbatim: *the
projected field is shallow-loaded into a separately droppable temp
(slot B) while the owning mk() result remains registered for teardown
(slot A).*

Why the index case is sound: `_lift_rvalue_ref_base_for_borrow` admits
index-bearing owned chains, materializes the base `mk()` ONCE, and does
`AddrOfArrayElem` — a pure ADDRESS projection, no second owned copy, no
leaf bitcopy. Nothing is double-registered.

Why the `&mut` case is safe: the mut path never reaches this fallback —
it is rejected earlier ("borrow requires an addressable place"). NOTE:
that message is NOT `E_MUT_RVALUE_ARG_BINDING_REQUIRED`; the plan
reconciles the two (§5).

## 3. HIR/MIR difference (the three requested comparisons)

- **(1) `peek(mk().root)` — failing accepted form:** HBorrow over
  `HField(mk(), root)`, `source_written=False`. MIR: slot A
  (`__field_src_*` : PR, drop-registered) + slot B (`__borrow_tmp*` :
  Node, drop-registered) BOTH release the same leaf. `dest` is in
  `_ref_field_temps` (marked), but slot B's `StoreLocal` never runs
  `_copy_if_ref_alias`, so the alias is stored owned. Two
  `_register_drop_local` calls, one backing, no intervening deep copy.
- **(2) Programmatic explicit-borrow baseline, `source_written=False`:**
  identical HIR shape to (1) once `source_written` is cleared (the A/B
  gate's mechanism) → identical buggy MIR. The bug is provenance-
  INDEPENDENT: it is the lowering of the borrow-of-owned-rvalue-field
  shape, not a source-written artifact. (This is why
  `test_rvalue_arg_temp_drop_ab.py`'s current root-rvalue A/B pin does
  NOT catch it — its subject is a root rvalue with a custom destructor,
  never a field/index projection carrying real owned String/Array.)
- **(3) `val x = mk(); peek(x.root)` — sound control:** `x` is a
  drop-registered local; `x.root` is a field read of a PLACE (HVar),
  so `source_is_owned_rvalue` is False and the borrow lowers via
  `_lower_addr_of_place` → `AddrOfField` against `x`'s address. ONE
  owner (`x`), pure address projection, single drop. No slot B.

## 4. Fix direction (for report-back approval)

Preferred — **admit owned rvalue bases with field projections in
`_lift_rvalue_ref_base_for_borrow`**, mirroring the already-sound index
path: materialize the base rvalue into ONE drop-registered temp, then
walk the field projection with `AddrOfField` to the leaf and return
that address. This removes slot B entirely — the borrow points into the
single materialized base's storage, dropped exactly once, with no leaf
bitcopy and no `_copy_if_ref_alias` question. The index and
`&w.get().handle` shapes already prove this lowering is sound and
byte-stable.

Two constraints this fix must satisfy — both are P1:

- **Base generality (finding #2, resolved toward ACCEPTANCE).**
  `_validate_lifted_chain` accepts ONLY `HCall`/`HMethodCall`/`HInvoke`
  bases (line ~3472), while stage1's corresponding
  `_split_lift_place_chain` already handles arbitrary safe rvalue
  bases. Shared borrows of rvalues are established language behavior,
  so the fix GENERALIZES the lift to safe constructor / block / ternary
  bases (`Wrapper(...).field`, `(cond ? a : b).field`) — matching
  stage1 — rather than rejecting them. Checker rejection is reserved
  ONLY as an emergency fail-closed measure for a base shape the lift
  genuinely cannot lower soundly (none identified). Row CTF (§6) is a
  heap-owning CONSTRUCTOR-field pin that expects ACCEPT-and-drop-once;
  do not ship the call-only lift and call it done.

- **Method-receiver pin (finding #3).** The refusal was added to keep
  `autoborrow_owned_rvalue_field_method_unchanged` —
  `make_inner().handle.peek()`, an owned-field METHOD RECEIVER, which
  DOES go through this helper (its own comments require the old
  fallback). It is NOT a distinct lowering shape. Its `Handle { raw:
  Int }` carries no owned state, so it never exercised the double free —
  it pins an *implementation contract* ("owned base must not lift") that
  the preferred fix invalidates. With explicit permission to edit
  tests: REWRITE it as a semantic compile/run/DROP pin — `Handle`
  carrying a real `String` with an observable destructor, asserting the
  method returns the right value and the payload is released exactly
  once (base + ASan) — preserving BEHAVIOR while dropping the stale
  "must not lift" mechanism assertion.

Rejected fallback (was listed as "secondary"; withdrawn per finding
#1): "add the missing ref-alias mark in `_visit_expr_HField`" — the
mark is NOT missing (§2 step 6). The only surgical alternative would be
to call `_copy_if_ref_alias` inside the borrow-materialization
fallback, but that DEEP-COPIES the leaf for what is syntactically a
borrow — semantically wrong (a borrow must not copy) and it leaves the
redundant slot B. The address-projection fix above is the correct
repair; this is recorded only to close the option explicitly.

Compiler-internal ownership-lowering change: expected next compiler
version, **ABI 22 unchanged** (no boundary shape change — reassess only
if the fix alters a runtime/boundary contract, not anticipated).

NO stdlib or downstream masking. The hoisted-binding form stays a
CONTROL in the matrix, never the fix.

## 5. `&mut` policy reconciliation

Two distinct `&mut` source spellings, distinct diagnostics — the matrix
pins both, and does NOT conflate them:
- `bump(&mut mk().root)` (real source-written `&mut`) → the redundancy
  rule fires: `E_REDUNDANT_ARG_BORROW`. Pinned as a separate
  redundancy-rule sanity check (the `&` is still "redundant" spelling
  by the rule's own terms, even though the underlying temp is mutable).
- bare `bump(mk().root)` at `&mut Node` → currently REJECTED with
  "borrow requires an addressable place; bind to a local first" (the
  non-canonical-&mut / addressable-place gate), NOT
  `E_MUT_RVALUE_ARG_BINDING_REQUIRED`.

The matrix must:
- pin that mutable rvalue FIELD/INDEX projections are REJECTED
  bind-first (a mutable borrow of temp-derived storage has no argument
  spelling, consistent with the plain mutable-rvalue rule) — do NOT
  assume they should COMPILE;
- reconcile the bare-form diagnostic: decide whether it should surface
  `E_MUT_RVALUE_ARG_BINDING_REQUIRED` (uniform user message) or keep
  the addressable-place message, and pin whichever is chosen;
- assert diagnostic-EQUIVALENCE only between the bare form and its
  programmatic `source_written=False` baseline (§ IR-identity) — never
  between the bare and the explicit `&mut` spelling (those differ:
  bind-first vs `E_REDUNDANT_ARG_BORROW`).

## 6. Proposed bounded ownership matrix (enumerated rows)

Regression-first: every EXPECTED-RED row must be OBSERVED red on this
tree BEFORE the fix (only CF and NF are observed red so far, via §1
probes); it becomes "confirmed RED" in this table only after that
observation. Then the fix flips each GREEN. Ownership rows carry
heap-owned payloads (String / Array<struct-with-String> /
Destructible); one row maps to `core.string_from_utf8_bytes` as a
representative refcounted producer (the fired-trigger requirement); and
ONE explicitly-labelled static-literal row is a MASK CONTROL — a static
String masks ownership defects because releasing its STATIC BACKING is
a no-op, so it must stay sound but proves NOTHING about ownership.
Extend `test_rvalue_arg_temp_drop_ab.py` for the programmatic /
provenance / IR-identity rows; the semantic run rows live as e2e
fixtures. Lane legend: B=base run, A=ASan, M=memcheck, T=alloc-track.

| # | Row | Shape | Payload | Expect | State | Lanes |
|---|---|---|---|---|---|---|
| CF  | call-field (the bug) | `peek(mk().root)` | heap String + Array | accept, drop-once | CONFIRMED RED (observed) | B A M T |
| NF  | nested field | `peek(mk().mid.leaf)` | heap String | accept, drop-once | CONFIRMED RED (observed) | B A |
| CTF | constructor-field (#2, → accept) | `peek(Wrapper(inner=mk_inner()).inner)` | heap String + Array | accept, drop-once | expected RED; must be observed before the fix | B A M T |
| MIX | mixed field/index | `peek(mk().kids[0])` (field then index) | heap String | accept, drop-once | expected RED; must be observed before the fix | B A M T |
| SFU | refcounted-producer row | `peek(wrap(core.string_from_utf8_bytes(ptr, len)).field)` (real buffer pointer + length) | String from `core.string_from_utf8_bytes` | accept, drop-once | expected RED; must be observed before the fix | B A M T |
| IDX | pure-index (already sound) | `peek(mk()[0])` | heap String | accept, drop-once (regression guard) | sound control (observed rc 0) | B A |
| HOI | hoisted control (NOT a workaround) | `val x = mk(); peek(x.root)` | heap String + Array | accept, drop-once | sound control (observed rc 0) | B A |
| PROV| A/B lowering-route ownership parity (was "IR-identity"; byte-identity REFUTED) | bare + programmatic `source_written=False` explicit baseline of CF and IDX | heap String (+ Array) | identical result + scope-end timing + exactly-one-drop (NOT byte-identical IR); structural pin on bare CF (§ below) | GREEN (implemented) | B A M (A/B gate) |
| MRC | method receiver (rewritten pin #3) | `make_inner().handle.peek()`, Handle carries String+destructor | heap String | value correct, payload dropped once | expected sound; observe before fix (new pin) | B A |
| THR | throwing edge | CF's borrow passed to a `throws` call that throws | heap String + Array | unwind cleanup drops once (no leak/double-free) | expected RED; must be observed before the fix | B A M T |
| LIT | static-literal MASK CONTROL (not an ownership proof) | `peek(mk_lit().root)` with `text = "static"` | static-literal String (release is a no-op) | accept, sound (no spurious drop) | expected sound; observe before fix (new control) | B A |
| MUF | &mut field rejection | `bump(mk().root)` at `&mut Node` | — | REJECT bind-first (pinned diagnostic, §5) | rejection pin | B (compile-only) |
| MUI | &mut index rejection | `bump(mk()[0])` at `&mut Node` | — | REJECT bind-first | rejection pin | B (compile-only) |
| MRB | explicit `&mut` mutable-rvalue sanity | `bump(&mut mk().root)` (real source-written) | — | REJECT `E_MUT_RVALUE_ARG_BINDING_REQUIRED` (the ACTUAL diagnostic; NOT `E_REDUNDANT_ARG_BORROW` as first speculated) | mutable-rvalue pin | B (compile-only) |
| TFR | ternary field projection (value-control-flow) | `peek((cond ? a : b).root)` | heap String + Array | REJECT `E_PROJECTED_RVALUE_ARG_BINDING_REQUIRED` bind-first | rejection pin (new) | B (compile-only) |
| TWR | whole ternary borrow | `peek(cond ? a : b)` | heap String + Array | REJECT `E_PROJECTED_RVALUE_ARG_BINDING_REQUIRED` bind-first | rejection pin (new) | B (compile-only) |
| TBF | droppable-root ternary, BITCOPY field | `peek_int((cond ? a : b).count)` (PR owns String; `.count: Int`) | heap String on root | REJECT bind-first — guard keys on ROOT `has_drop`, not the leaf's bitcopy-ness | rejection pin (round-7 hole) | B (compile-only) + code pin |
| TBS | bitcopy-root ternary (over-rejection control) | `read(cond ? 1 : 2)` at `&Int` | none (Int, no drop) | ACCEPT + run rc 0 | sound control (round-7) | B (run) |
| TEX | explicit `&` of ternary projection | `peek(&(cond ? a : b).root)` | heap String | REJECT bind-first `E_PROJECTED_RVALUE_ARG_BINDING_REQUIRED` — NOT `E_REDUNDANT_ARG_BORROW` (no "pass directly" fix-it) | classifier pin (round-7) | B (compile-only) + code pin |

Round-7 predicate note: the value-control-flow rejection keys on the
ROOT type's drop-ness (`type_table.has_drop`), never the projected
field's `is_bitcopy` — the double-registered owner is the root. The
source-written spelling is caught in the W0 redundancy classifier via a
new stage1 provenance flag `HBorrow.materialized_rvalue_cfv` (stage1
materializes the ternary into a temp before the checker runs, so the
CFV kind must be stamped at materialization time). Diagnostic CODES are
pinned in `lang/tests/driver/test_cfv_rvalue_borrow_codes.py` (the e2e
runner asserts only message + phase).

Round-8 predicate note: a ROOT type that still contains a type variable
is FAIL-CLOSED (hazardous) — `has_drop(TypeVar)` caches False, and the
generic body is checked pristinely (empirically: a `pick<T>` borrowing
`c ? move x : move y` rejects for both `String` and `Int`
instantiations), so a generic CFV borrow would otherwise slip through and
double-free when instantiated droppable. Both hazard helpers OR in
`has_typevar(root_ty)` and use `type_expr(..., used_as_value=False)`
(classification queries, no value-use side effects). The rejection class
`policy_class = "cfv_rvalue_binding"` is in the W0 totality fail-loud set
(typed_validator) and its unit table.

alloc-track rows: CF, CTF, MIX, SFU, THR (the leak-precise ownership
rows). In this codebase alloc-track == valgrind memcheck (definite-leak
+ UAF, multi-pass so a premature free corrupts a later pass); there is
no separate malloc-count harness. memcheck/alloc-track is exercised via
the e2e runner's memcheck lane on the ownership fixtures and via the
A/B-parity gate's multi-pass valgrind on CF/IDX.

### A/B lowering-route ownership parity (PROV row — REVISED, byte-identity refuted)

**Original design (DriftQuery ask #2) asked for bare-vs-explicit LLVM-IR
byte-identity. That premise was empirically REFUTED and is NOT the
gate.** The bare spelling `peek(mk().root)` receives its synthetic
`HBorrow(source_written=False)` from the checker AFTER stage1, so it is
lowered by the MIR rvalue-base lift (`__borrow_tmp`); the explicit
`peek(&mk().root)` with the flag cleared is normalized by stage1's
`BorrowMaterializeRewriter` (`__tmp_borrow`). Two different lowering
subsystems emit DIFFERENT — both memory-safe — IR. This is by design,
not a contract failure; the earlier 0.33.91 note that the surviving
spelling was "IR byte-identical" overstated the guarantee (corrected in
history.md). The real promise is SEMANTIC parity.

Replacement gate (implemented in `test_rvalue_arg_temp_drop_ab.py`):
- **CF (field) and IDX (index) A/B parity:** compile AND RUN the bare
  spelling and the programmatic `source_written=False` explicit baseline
  of each shape; assert identical observable result, scope-end drop
  TIMING, and exactly-one-drop, under base + ASan + memcheck. CF uses a
  Destructible leaf with a `&mut` drop counter (precise mid==pass /
  after==pass+1 timing); IDX uses an owning `Array<Holder>` with a heap
  String and leans on ASan/memcheck for exactly-once (an array element
  cannot carry a `&mut` counter in v1). Both loop three passes so a
  premature free corrupts a later pass — this IS the alloc-track lane
  (alloc-track == valgrind memcheck in this codebase; there is no
  separate malloc-count harness).
- **Structural pin (bare CF, not whole-IR):** assert the bare route
  materializes exactly ONE owning base (`alloca %Struct_main_Wrap…`)
  and address-projects the leaf into it (`getelementptr … i32 0, 0`
  feeding `@peek`), with NO second owned leaf temp
  (`alloca %Struct_main_Leaf…` count 0).
- **`&mut` — two SOURCE spellings pinned SEPARATELY (no manufactured
  equivalence):**
  - bare `bump(mk().root)` → "borrow requires an addressable place; bind
    to a local first" (addressable-place bind-first gate).
  - explicit `bump(&mut mk().root)` → `E_MUT_RVALUE_ARG_BINDING_REQUIRED`
    (the mutable-rvalue diagnostic; NOT `E_REDUNDANT_ARG_BORROW` as this
    plan originally speculated).
  We do NOT equate the bare form with a programmatic `source_written=
  False` bypass: stage1 has already reshaped the explicit spelling, so
  such an equivalence would be an artifact, not a contract.

## 7. Trigger decisions

Framing: this is a **post-completion coverage defect in the ALREADY-
UNIFIED String ownership architecture**. The whole "Unify String/Arc
ownership" refactor shipped (Scope A classification + centralized alias
contract 0.33.75; B-arch ledger stakes 0.33.79; string_arc endgame /
deletion 0.33.87; Scope B `DriftRcBytes` representation 0.33.88 /
ABI 22). The centralized contract is present and correct — the
borrow-temp materialization fallback simply VIOLATED it: it accepted an
alias-marked (`_ref_field_temps`) projection as an OWNED value instead
of address-projecting through its owner / deep-copying at the transfer
boundary via `_copy_if_ref_alias`.

- **String ownership-authoring conformance matrix (§ doc/refactor_
  triggers.md:582): FIRES.** This is a String/Array double-free rooted
  in field/index projection lowering + owned-temp drop registration /
  `_ref_field_temps` classification — squarely the recurring-defect
  class that trigger names. Deliverable is the bounded producer ×
  projection × mode × exit matrix above with centralized
  droppable-set/aliasing classification, NOT a one-cell emitted-release
  patch.

- **Unify String/Arc ownership (§ doc/refactor_triggers.md): DOES NOT
  FIRE — the entire refactor (Scope A AND Scope B) already SHIPPED**
  (0.33.75 → 0.33.88 / ABI 22; the registry entry is now marked
  COMPLETED). This defect does not trigger Scope A, Scope B, or any new
  String/Arc rewrite, because all of those projects have landed.
  String's classification is correct everywhere (retain-Copy +
  needs-drop) and the centralized alias helpers exist; the bug is a
  coverage hole where one consumer (the borrow-temp fallback) bypassed
  `_copy_if_ref_alias`. The generalized address-projection fix RESTORES
  the existing architecture's contract — it does not reopen or
  re-trigger the unification work. (A genuinely new String-
  representation project, if ever desired, would need its own registry
  entry with new triggers.)

## 8. Migration-guidance correction (versioned, closeout)

The reject-redundant-call-borrows note "every site is a one-token
deletion" gains a SECOND exception beyond mutable temporaries — and
unlike that one it is SILENT and memory-unsafe. Stated precisely and
versioned:

- **0.33.91–0.33.93 (affected):** deleting an argument `&` is UNSAFE
  when the remaining expression is a **FIELD projection** from an owned
  temporary (`f(producer().field)`, incl. nested and
  constructor-produced bases) — demonstrated double-free/UAF. The
  **pure-INDEX** case (`f(producer()[i])`) is already SOUND on these
  versions (tested) and needs no exception. For the affected field
  sites on these versions, use the bind-first spelling
  `val x = producer(); f(x.field)`. (drift-query's 28
  `normalize_module(parse_module(…).root)` sites are exactly the field
  case.)
- **After the fixed compiler ships:** direct bare field projection
  `f(producer().field)` is AGAIN the valid, sound spelling — NOT a
  permanent bind-first exception. The migration note should say the
  field-projection hazard is bounded to 0.33.91–0.33.93 and resolved in
  the fix release, so downstreams don't carry a needless workaround
  forward.

Update history / effective-drift migration guidance at closeout with
this version-bounded framing.
