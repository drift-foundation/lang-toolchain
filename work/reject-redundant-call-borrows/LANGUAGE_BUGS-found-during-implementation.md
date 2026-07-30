# LANGUAGE_BUGs found and fixed during reject-redundant-call-borrows (2026-07-29)

All handled regression-first inside this slice (same subsystem the feature
was already changing). The first two are members of the SAME defect family as
the e8d fn-pointer miscompile fixed separately in 0.33.91/453a2f52: **a call
family whose argument checking neither borrows nor rejects, so ill-typed
arguments reach lowering.** The W0 shared argument policy + the typed-mode
totality validator are the structural close-out for that class — any future
family that forgets to classify its borrow arguments now fails validation
instead of miscompiling. Bug #3 is a separate trait-resolution key-canonical-
ization defect surfaced by the round-3 pins, not a call-checking-family
member. Bugs #4 and #5 are spurious-rejection defects in the rule's own
classification layers (nested-index borrow suppression; param_index
authority in the shared classifier), found by round-5 review analysis and
by the first-ever run of the driver shard respectively — both fixed
regression-first.

## 1. Associated-function bare `&T` argument miscompile (e8d, assoc flavor)

- **Repro (pre-fix, demonstrated live before the wiring landed):**
  `Util::measure(s)` with `pub fn measure(s: &String)` in an `implement`
  block — the type checker ACCEPTED the bare call and clang rejected the
  emitted module: `'%.t2' defined with type '%DriftString' but expected
  'ptr'`. Explicit `Util::measure(&s)` compiled and ran (exit 5).
- **Subsystem:** `checker/call_resolver.py`, the qualified-static call
  resolution (`resolve_nonvariant_qualified_static_call` /
  `resolve_qualified_member_ufcs`) and its shared record point in
  `resolve_call_expr`'s qualified-member branch — candidate viability
  (`args_match_params`) accepted `T` at `&T` but no path synthesized the
  borrow before `record_call_info`.
- **Regression-first evidence:** the pinning fixture
  `lang/tests/codegen/e2e/autoborrow_bare_assoc_fn/` encodes the exact
  pre-fix repro; the failure (clang `%DriftString` vs `ptr`) was
  demonstrated on the pre-wiring tree (probe `w6b_assoc`, session
  transcript 2026-07-29) before the fix landed. The explicit-form
  rejection case is `redundant_arg_borrow_assoc_rejected` (created with
  the negative-fixture batch).
- **Fix:** W0 wiring at the qualified-member record point — template mask
  from `signatures_by_id` (DIRECT targets) or the trait decl's param
  type_exprs (TRAIT targets), UFCS `self` slot exempt; same
  `apply_autoborrow_args` engine as every other family.
- **refactor_triggers.md scan (mandatory):** reviewed 2026-07-29 — no
  registered trigger matches (nearest entries concern borrow-checker
  walker consolidation and implicit-move classification, not
  call-resolution argument-check gaps). No escalation; the W0 validator
  is the structural remedy for the class.

## 2. `Array<T>.extend()` accepted a mismatched source element type

- **Repro (pre-fix):** `var dest: Array<Int>; val wrong: Array<String>;
  dest.extend(wrong)` — accepted by the checker and EXECUTED (exit 3:
  String payloads appended into an Int array; memory-unsafe). Both bare
  and explicit spellings affected; pre-existing (not introduced by the
  rule — found while fixing D2 review finding 2, which required the
  extend formal to come from the receiver's element type instead of the
  actual argument).
- **Subsystem:** `checker/call_resolver.py`, the Array intrinsic method
  branch (`push`/`extend`/… arm) — arity and element-Copy were checked,
  the source's element type never was.
- **Regression-first evidence:** fixture
  `lang/tests/codegen/e2e/array_extend_elem_mismatch_rejected/` was
  created first and confirmed FAILING (0 compile errors; ran to exit 3)
  on the pre-fix tree, then the check landed and the fixture pins
  `Array<T>.extend() source element type mismatch` at typecheck.
- **refactor_triggers.md scan (mandatory):** same scan as above — no
  registered trigger matches ("Drop-aware RawBuffer/Ptr write variants"
  concerns lowering-side write helpers, not checker-side argument
  typing). No escalation.

## 3. Parameterized-trait qualified call with a REFERENCE type argument failed impl lookup

- **Repro (pre-fix):** `implement Taker<&String> for Sink { … }` then
  `Taker<&String>::take(k, &s)` → `no implementation for trait 'Taker' on
  receiver Sink` — while the byte-identical shape at `Taker<Int>` resolved.
  Found while writing the round-2 W0 trait-path pins (the "(c)" case), first
  misattributed to a generic receiver-lookup gap; isolating probes narrowed it
  to the reference TYPE ARGUMENT.
- **Subsystem:** `traits/world.py::normalize_type_key` — a `Ref`/`RefMut`
  TypeKey has no home module, but normalization stamped the CALLER's module
  onto module-less non-builtin keys, so the call-side obligation key diverged
  from the impl-registration key. (`fn` keys had the same exposure.)
- **Regression-first evidence:** fixture
  `lang/tests/codegen/e2e/trait_qualified_ref_type_arg_impl_lookup/` created
  first and confirmed FAILING (the exact diagnostic above), then the
  normalize exemption for `Ref`/`RefMut`/`fn` landed and the fixture pins
  exit 0.
- **refactor_triggers.md scan (mandatory):** re-scanned 2026-07-29 — no
  registered trigger covers trait-key normalization; no escalation.

## 4. Nested-HIndex borrow: only the OUTER index was copy-suppressed

- **Repro (pre-fix, round-5 review prediction confirmed by probe):**
  `peek(make_matrix()[0][0])` with `peek(x: &Handle)` and
  `make_matrix() -> Array<Array<Handle>>` → the INNER index hop reached the
  shallow checker's element-copy gate and rejected with
  `cannot copy value of type 'Array' (use move <expr>)` — while the
  single-index form `_peek(mk()[i])` compiled and ran.
- **Subsystem:** `lang/driftc/checker/__init__.py`, the shallow
  `_infer_expr_type` HBorrow arm — the round-4 parity layer (new in this
  slice) suppressed the index-copy check ONLY for `expr.subject` itself,
  not for deeper HIndex nodes on the projection chain; recursion into the
  inner index hit the copy gate at the HIndex value-read arm. (MIR's
  lifted chain already handled repeated index hops — the gap was
  checker-side only.)
- **Regression-first evidence:** the probe (`scratchpad/nested_idx`, two
  call sites) was confirmed FAILING with the exact diagnostic above before
  the fix landed; post-fix it compiles AND runs with correct element
  values through both hops. Pinned in-corpus as
  `borrow_chained_ref_projection_noncopy` SECTION D
  (`_peek(mk_matrix()[0][0])` → 7, `[0][1]` → 9; exit-22/23 guards).
- **Fix:** the suppression now walks the ENTIRE borrow-subject projection
  spine — every `HIndex` reached through `HIndex.subject`/`HField.subject`
  hops is suppressed (try/finally-scoped); expressions INSIDE `[...]` are
  never walked, so index-expression value reads keep their copy checks.
- **refactor_triggers.md scan (mandatory):** re-scanned 2026-07-30 — no
  registered trigger covers shallow-checker copy-gate suppression; no
  escalation.

## 5. Boxed-callback interface dispatch rejected the legal generic-by-value borrow spelling

- **Repro (pre-fix, found by the FIRST-EVER driver-shard lane run):**
  `Callback1<&mut Scope, R>.call(&mut s)` — policy-matrix row 10, the
  release-notes' own "still doing real work" example — fired
  `E_REDUNDANT_ARG_BORROW` ("redundant borrow for parameter 'a: &mut
  CStringScope'"). First seen live at `stdlib/std/ffi/ffi.drift:428`
  (`with_cstring_scope`'s `body.call(&mut s)`) via
  `test_b5_ffi_api_teeth`; 36 driver tests / 74 diagnostics in the lane
  reduced to this class. The pre-existing Fn1 pin never caught it because
  it specializes F to a THIN function — D8-exempt before this path.
- **Subsystem:** `checker/call_resolver.py::declared_ref_formal` — the
  shared W0 classifier's `param_index` exemption was gated on
  `name is None`, but `param_index` is AUTHORITATIVE regardless of
  name/args (core/generic_type_expr.py): builtin `Callback*` schemas
  carry `name=""` on their param refs, so their generic slots read as
  DECLARED `&mut`/`&` formals at every interface-dispatch callsite.
- **Regression-first evidence:**
  `lang/tests/driver/test_callback_iface_generic_ref_param_exemption.py`
  (4 full compile/run rows: {direct Callback1 value, `require F is Fn1`
  wrapper instantiated with a boxed callback} × {&mut, &}) written first
  and confirmed FAILING with the exact diagnostic (4/4), then the
  classifier fix landed: `param_index is not None` short-circuits before
  any name/args inspection. Post-fix 4/4 compile AND run. Unit rows added
  to `test_declared_ref_formal_classifier.py` for all three producer
  shapes (name None / name "" / residual name outside the generic set).
- **Rejected first attempt (review):** passing owner+schema type-param
  NAMES into the W2 mask — did not fix the repro and would over-exempt
  unrelated names on inherited interfaces; reverted before the real fix.
- **refactor_triggers.md scan (mandatory):** re-scanned 2026-07-30 — no
  registered trigger covers W0 classifier semantics; no escalation.

## Considered and determined NOT a LANGUAGE_BUG: owned-base index bare-arg ICE (round-5 item 3)

- **Candidate:** during round-4's receiver-parity work, the bare spelling
  `_peek(mk()[1])` (owned-returning call base + index hop at a declared `&T`
  formal) died in MIR lowering with
  `NotImplementedError("array index read requires Copy element type; borrow
  not supported in v1")` — an ICE shape.
- **Determination: transient unpublished implementation state, no ledger
  entry.** The ICE window existed ONLY between two in-branch edits: after the
  shallow checker's HBorrow-over-HIndex copy-suppression landed and before
  the MIR lifted chain's owned-base admission landed. On every PUBLISHED
  compiler the spelling rejects cleanly upstream.
- **Probe evidence (2026-07-30):** the same probe source compiled with the
  CERTIFIED 0.33.91 toolchain
  (`~/opt/drift/certified/current/toolchain/bin/driftc`) rejects with the
  clean pre-rule diagnostic `cannot copy value of type 'Handle' (use move
  <expr>)` [E-AUTO-e0f26505] — no ICE reachable from published state. On the
  fixed branch tree the spelling compiles and RUNS
  (`borrow_chained_ref_projection_noncopy` SECTION C, exit-21 guard).
- **Negative companion (mandatory, landed):** `&mut self` methods through an
  rvalue-base index chain reject upstream with `borrow requires an
  addressable place; bind to a local first` for BOTH the owned base
  (`mk_handles()[0].bump()`) and the shared-ref base
  (`w.handles_ref()[0].bump()`) — pinned (with an ICE-absence assertion) in
  `test_autoborrow_receiver_place.py::
  test_method_receiver_mut_through_rvalue_index_rejects_cleanly`; the
  validator's `base_owned` arm additionally hard-excludes `is_mut`.
- **refactor_triggers.md scan:** re-scanned 2026-07-30 for the round-5
  nested-HIndex suppression fix and this candidate — no registered trigger
  covers shallow-checker copy-gate suppression or MIR borrow-chain lifting;
  no escalation.
