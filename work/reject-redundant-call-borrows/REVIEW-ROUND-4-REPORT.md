# Round-4 report — corpus-audit flip fixes + review corrections (2026-07-29)

Written mid-gate-chain per request, to jump-start code review. The gate chain
(perf → corpus audit → memcheck → ASAN) is running on this snapshot; per the
review directive it is treated as snapshot evidence only — the FINAL audit
reruns after the corrections below (hashed fixture sources/descriptions
changed).

## Part 1 — the 16 corpus flips (report-mode audit, dryrun 1) and their fixes

The first report-mode audit surfaced 16 `compiled_ok → failed` flips where D5
predicted zero. Root-caused into four classes; every fix verified by
compiling+running all 16 fixtures (expected exits) plus the feature's
101-test driver batch.

### Class A — projection-chain auto-borrow parity (3 fixtures)

`borrow_chained_ref_projection_noncopy`, `ref_array_jsonnode_usage_matrix`,
`effective_drift_emitter_example` (the last also had Class C).

Bare `f(chain()[i])` / `f(*f())` at a declared-ref formal — the spellings the
sweep produces — hit two layers that only ever saw *materialized* borrows:

1. **`lang/driftc/checker/__init__.py` `_infer_expr_type` HBorrow arm
   (~:1914)** — inferring through a checker-synthesized
   `HBorrow(subject=HIndex)` fired the indexed-element Copy check.
   Source-written borrows never hit this arm live because stage1
   `borrow_materialize` rewrites them into place-exprs first; synthesized
   borrows are created AFTER stage1. Fix: suppress the index-copy check for
   the borrow subject (`suppress_index_copy_check_expr_ids`), exactly
   mirroring the existing HField-projection-through-index arm above it —
   borrowing an element is not copying it.

2. **`lang/driftc/stage2/hir_to_mir.py` `_lift_rvalue_ref_base_for_borrow` /
   `_validate_lifted_chain` (~:3266-3480)** — the MIR-side twin of stage1's
   `_split_lift_place_chain` supported ONLY HField hops; HIndex subjects fell
   through to whole-expression materialization → `NotImplementedError("array
   index read requires Copy element type")`. Fix: the validator/emitter now
   also accept **index hops** (emit `LoadRef` of the array value +
   `AddrOfArrayElem`, byte-mirroring `_lower_addr_of_place`'s HPlaceIndex arm)
   and an **explicit deref directly on the base call** (`&*f()` — an
   address-level no-op; the call's `&T` result IS the borrow). The
   validator's atomicity contract is preserved: hop validation is pure, and
   index expressions lower inline at emit time exactly as the place path
   does.

### Class B — stale-arg-types idempotency in `_apply_autoborrow_args` (7 fixtures)

`for_in_byvalue_{constshare_local,copy_local_reuse,variant_iterable}`,
`for_in_distinct_generic_insts_around_const_share_loop`,
`for_in_iterable_ownership_matrix`, `for_in_nc_loop_then_generic_box_loop`,
`for_iter_json_expect_array`.

The new assoc/trait-qualified record point re-enters
`_apply_autoborrow_args` on re-resolution passes with **stale `arg_types`
against the already-mutated `args` list** (for-in desugar calls
`Iterable::iter(...)` through exactly this path). Two coercion branches were
not idempotent under that mismatch (`lang/driftc/type_checker.py`):

- the **symmetric `&T→T`** branch (~:3338) wrapped a SECOND deref around the
  deref/const_share node it had synthesized on the prior pass → "deref
  requires a reference value" (the for-in byvalue family);
- the **nested `&&T→&T`** branch (~:3410) wrapped a second deref that
  dereffed INTO the pointee → "cannot copy value of type
  'Array<std.json.JsonNode>'" (the `&users_ref`-shaped fixtures).

Fix, both branches: skip the coercion when the slot's node **already types to
the formal** (structural check via `type_expr` re-entry / recorded type; node
markers were tried first and do NOT survive `normalize.py` rebuilds, which is
documented in the guard comments). The existing borrow `synth_cache`
already covered the third branch (borrow synthesis) — these two were the
uncovered siblings.

### Class C — Borrow-inference head selection (5 fixtures)

`callback_arc_mutex_full_mutation`, `callback_move_capture_{arc_lifetime,
nested_callback,replace_state}`, `effective_drift_emitter_example`.

The round-earlier "typevar-inner prefers plain auto-borrow" rule in
`_borrow_infer_arg_types` (`lang/driftc/checker/call_resolver.py` ~:4988)
was too broad: `conc.lock<T>(m: &Mutex<T>)` called with an
`Arc<Mutex<Counter>>` argument NEEDS the Borrow-TRAIT view
(`Arc.borrow() → &Mutex<Counter>`, T:=Counter); wrapping the raw arg as
`&Arc<Mutex<Counter>>` pinned E-INFER-CONFLICT. Fix: the preference now
requires **head compatibility** — a bare-typevar inner always auto-borrows
(`&T` binds T:=arg), a structured inner auto-borrows only when the argument's
head constructor matches (struct/variant base-id equality; same-kind for
ARRAY/FUNCTION). Mismatched heads retain the Borrow-trait rewrite. The two
shapes that motivated the original preference (`wait_until(guard: &mut
MutexGuard<T>)` ← `MutexGuard<Counter>`; `_publish_or_drop(state:
&Arc<Mutex<ResultState<T>>>)` ← `Arc<Mutex<ResultState<X>>>`) are both
same-head and keep the plain-borrow path.

### Class D — om generator regen gap (2 fixtures)

`om_local_assign_token`, `om_return_value_token`: two emitted-template sites
in `lang/tests/codegen/e2e/__ownership_matrix__/_gen.py` were missed by the
earlier om regen (`token_site_local_assign` :1281 `make_token(&mut
dst_sess)`; `token_site_return_value` :1267 `produce_<shape>(&mut sess)`).
Also, the sweep had edited one GENERATED fixture directly and broke its
indentation — regen owns these files. Both template sites bared, all 51
om_* fixtures regenerated from the generator; 51/51 compile+run+exit-match
(16-way sweep).

## Part 2 — perf gate: triaged, NO code change

All three `test_std_json_parse_perf_gate` rows pass under the mandated
protocol. The morning's allocation/scaling failures cleared with the
oracle-fragment sweep; the residual bands failure (tiny_arr absΔ, malformed
ratio+absΔ) was reproduced as **self-inflicted CPU contention** — the gate
had been launched in the background while a 16-way pytest batch ran in the
foreground (the exact contention mode the module docstring forbids). Clean
serial rerun on the idle box: `1 passed in 142s`, all shapes inside bands
with margin (e.g. request ratio 1.16 vs band 1.34). The gate chain re-runs
perf first, serially, before any parallel phase.

## Part 3 — review corrections (this round's mandate)

1. **Projection fixture dual coverage — DONE.**
   `borrow_chained_ref_projection_noncopy` now has SECTION A (explicit
   borrows in non-argument bindings → the original stage1
   `_split_lift_place_chain` HField/HIndex/DEREF regression, exits 11-15)
   and SECTION B (the same chains bare in argument position → checker-
   synthesized HBorrow → MIR lifted-chain, exits 1-5); narrative rewritten to
   describe both routes and why they are distinct. Compiles, runs "ok"/0.

2. **HIndex receiver contract — DONE (parity completed).** All four
   contradictory sites reconciled by COMPLETING receiver parity (the
   direction the old comments themselves mandated):
   `_ultimate_base_is_rvalue_call` widened to HIndex + deref-at-base; the
   MIR lifted chain additionally admits OWNED bases for index-bearing
   shared chains (drop-registered temp; pure-field owned chains keep the
   fallback pinned by `autoborrow_owned_rvalue_field_method_unchanged`);
   receiver e2e extended with n4/n5/n6 compile+run pins; the `_rejects`
   driver test flipped to `_accepts` per its own docstring; docstrings and
   narrative comments rewritten to the real contract. Round 5 added the
   nested-HIndex spine suppression (`peek(make_matrix()[0][0])`, SECTION D
   pin), the `&mut`-through-rvalue-index negative companion
   (`test_method_receiver_mut_through_rvalue_index_rejects_cleanly`, with
   an ICE-absence assertion), and the transient-ICE determination recorded
   in the LANGUAGE_BUGS ledger (certified-toolchain probe evidence).

3. **D5 single authoritative enumeration — DONE.** 23 additions = 15
   `failed` + 8 `compiled_ok`; `trait_qualified_ref_type_arg_impl_lookup` is
   enumerated compiled_ok #23 (the "non-enumerated" tier is retired);
   universe statements are uniformly 1,269 → 1,292; the round-2 report and
   PROGRESS log entries carry explicit supersession notes rather than silent
   rewrites. LANGUAGE_BUGS ledger intro corrected: "The first two" belong to
   the e8d call-checking family; bug #3 is a separate trait-resolution
   key-canonicalization defect.

4. **Pins for the two subtle fixes — DONE.**
   `lang/tests/driver/test_autoborrow_reresolution_pins.py`, 4 compile+RUN
   rows: (a) both stale-arg-types idempotency branches (minimized for-in
   Copy-iterable and `&&Array` carriers, designated e2e run-carriers named
   in docstrings); (b) BOTH head-selection directions — the round-5 rework
   made the same-head row load-bearing via `inspect<T>(a: &Arc<T>)` with a
   bare `Arc<Int>` (Arc's competing `Borrow<T>` view discriminates;
   verified by disabling the preference → row FAILS → restored → passes),
   and mismatched-head `conc.lock(Arc<Mutex<Int>>)` retains the
   Borrow-trait view.

## Part 4 — what to review (diff inventory, compiler side)

- `lang/driftc/checker/__init__.py` — HBorrow-arm index-copy suppression
  (one hunk, try/finally mirror of the HField arm).
- `lang/driftc/stage2/hir_to_mir.py` — lifted-chain validator/emitter
  extension (hop peel loop, index/deref validation, index emit arm,
  docstring). Review against `_lower_addr_of_place`'s HPlaceIndex arm for
  emit parity, and the atomicity contract (no emission before validation
  passes).
- `lang/driftc/type_checker.py` — two idempotency guards inside
  `_apply_autoborrow_args` (search "Idempotency"). Review question: the
  structural check re-enters `type_expr` on an already-typed node — confirm
  no diagnostic duplication (none observed across the fixture corpus; the
  re-entry is value-position-free).
- `lang/driftc/checker/call_resolver.py` — head-match refinement in
  `_borrow_infer_arg_types` (one hunk). Review question: VARIANT head check
  uses `get_variant_instance` guarded by hasattr — confirm preferred API.
- `lang/tests/codegen/e2e/__ownership_matrix__/_gen.py` — two template
  lines; fixtures regenerated (om_* dirs show as content deltas, not flips).
- `lang/tests/codegen/e2e/borrow_chained_ref_projection_noncopy/main.drift`
  — dual-coverage restoration (Part 3.1).

## Status / next

Gate chain phases: perf (done, green), corpus audit dryrun-2 (running),
memcheck, ASAN. Remaining work: review items 2 and 4, then the FINAL corpus
audit rerun on the corrected tree and the combined announcement update to
final-tree certification (replacing the earlier zero-delta fn-pointer
checkpoint wording). No git writes; commits stay with you.
