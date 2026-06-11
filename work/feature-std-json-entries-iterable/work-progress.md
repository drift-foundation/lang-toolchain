# std.json entries iteration / for-in compiler work

**Status:** IN PROGRESS (2026-06-10)

This is the restart-safe ledger for std.json Slice 2B and every compiler
defect exposed while making `for entry in node.entries()` work. Do not treat
the top `history.md` entry as proof that this work is complete until every
open item below is closed and the consolidated gates pass.

## Goal

Keep the existing API:

```drift
pub fn entries(self: &JsonNode) nothrow -> JsonEntriesIter
```

Make `JsonEntriesIter` implement `Iterable` so callers can write:

```drift
for entry in node.entries() {
	// entry.key / entry.value are borrowed
}
```

The iterator remains a variant. Do not reshape it into a struct to avoid a
compiler defect.

## Non-negotiable process

- Every language/toolchain defect is a `LANGUAGE_BUG`.
- Pin a minimal failing regression before its root-cause fix.
- Do not add stdlib workarounds or for-in-only semantic checks.
- Do not defer a known soundness or ownership defect merely because it was
  pre-existing.
- Scan `doc/refactor_triggers.md` when beginning each newly classified bug.
- Behavior-changing compiler fixes bump `DRIFTC_VERSION`; bump ABI only if a
  compiler/runtime boundary signature, layout, or calling convention changes.

## Defect ledger

### LB-1: by-value variant `Iterable` receives a pointer

**State:** root-caused; implementation and regressions present; final
consolidated verification still required.

**Symptom:** `for x in <variant rvalue>` selected `Iterable<Src,...> for Src`
but passed the shared-borrow pointer to an `iter(self: Src)` parameter. LLVM
expected the variant value and clang rejected the pointer/value mismatch.

**Root cause:** for-in always lowered its source through a shared borrow.
Struct/array ABIs obscured the defect because their by-value representation is
pointer-based; variants expose it.

**Required behavior:**

- Prefer a compatible borrow-mode `Iterable for &Src` / `&mut Src`.
- Copy/ConstShare bound locals may be converted from `&Src` to an owned value
  without consuming the local.
- A non-Copy compiler-owned rvalue temporary may move into a by-value
  `iter(self: Src)`.
- A non-Copy user local requires explicit `move`.
- Concrete MIR-bound `for_iter` / `for_next` calls must have DIRECT targets.

**Regressions present:**

- `lang/tests/codegen/e2e/for_in_byvalue_variant_iterable`
- `lang/tests/codegen/e2e/for_in_iterable_ownership_matrix`
- `lang/tests/codegen/e2e/for_in_rvalue_borrow_only_iterable`
- `lang/tests/driver/test_for_in_byvalue_variant_iterable.py`
- `lang/tests/driver/test_for_in_marker_and_selection.py`

### LB-2: for-in ownership marker lost during HIR normalization

**State:** fixed and structurally pinned; final consolidated verification
still required.

**Symptom:** `HBorrow.for_iter_owned_temp=True`, set on the compiler-owned
rvalue temporary, reached method resolution as `False`.

**Root cause:** HIR reconstructors rebuilt `HBorrow` manually and dropped
new fields.

**Fix direction used:** reconstruct with `dataclasses.replace` in
`place_canonicalize`, `borrow_materialize`, and `ast_to_hir._rename_expr`.

**Pinned invariant:** `for_iter_owned_temp` and `origin="for_iter"` survive
normalization, and the subject remains the compiler-created
`__for_iterable` place.

### LB-3: repeated for-in binder name produces SSA load-before-store

**State:** fixed and pinned; do not leave it described as out of scope.

**Symptom:**

```drift
for x in a { ... }
for x in a { ... }
```

failed with `SSA: load before store for local 'x__b<id>'`.

**Root cause:** match-binder stores used the raw source name while reads used
the binding-id-canonical MIR local.

**Fix direction used:** canonicalize the binder destination from
`arm.binder_ids` and use that identity consistently for local declaration,
typing, drop registration, and stores.

**Regressions present:**

- `lang/tests/codegen/e2e/for_in_loop_var_reuse`
- `lang/tests/driver/test_for_in_loop_var_reuse_mir.py`

### LB-4: for-in method resolution duplicated and weakened central resolution

**State:** active rewrite; most behavior works, but it is not complete until
LB-5 and LB-6 close.

**Problem:** the old `for_iter` path discarded central method resolution and
rescanned signatures. The scan lacked the full requirement, receiver
compatibility, ambiguity, substitution, visibility, and auto-deref behavior.

**Current design:**

- Use `resolve_method_call` as the only candidate resolver.
- Add a hard required-trait constraint for `Iterable` /
  `SinglePassIterator`; `traits_in_scope` alone is not a trait-only filter.
- Compare canonical full `TraitKey` identity, including package identity.
  Module/name comparison is insufficient.
- Exclude inherent and unrelated-trait methods during candidate collection.
- Preserve the selected declaration, signature, substitutions, receiver
  rewrites, and diagnostics.
- For a concrete impl, produce a DIRECT target from the selected
  declaration's `fn_id`; never reconstruct the signature manually.
- A MIR-bound result with no concrete implementation is an internal resolver
  contract failure.

**Known regressions that must remain green:**

- nested `&Array` / `&&Array` receiver shapes:
  `for_iter_ref_array_local`, `for_iter_json_expect_array`,
  `ref_array_jsonnode_usage_matrix`
- inherent `iter()` does not satisfy for-in
- unrelated-trait `iter()` does not satisfy for-in
- non-Copy bound local is rejected
- explicit `move` succeeds
- borrow-only `Array<String>` rvalue remains borrow-iterated

### LB-5: central receiver matching lacks secondary `&T -> T` coercion

**State:** OPEN.

**Symptom:** a bound local of Copy variant type with only
`iter(self: V)` is presented to resolution as `&V`; central receiver
compatibility rejects it before the existing ref-to-value HIR rewrite can be
applied.

**Required fix:** add a central secondary receiver-coercion phase, not a
for-in resolver retry:

1. Rank exact and borrow-compatible receiver candidates first.
2. Only when none match, consider `&T` / `&mut T` to by-value `T`.
3. Permit it only when `T` is Copy or proves ConstShare.
4. Record the coercion in HIR with `rewrite_ref_to_value`.
5. Preserve overload preference for exact borrowed receivers.

This is not ordinary auto-deref: non-Copy ConstShare values require the
ownership-producing implicit `const_share()` rewrite.

**Required regression:** Copy bound local can be iterated twice and yields the
same result both times, proving the local was not consumed. Also add a direct
non-for-in by-value method receiver regression so the shared resolver behavior
is pinned independently.

### LB-6 investigation: generic trait-impl `require` applicability

**State:** ORIGINAL REPRO INVALID; a replacement non-Copy repro is required
before this can be classified as a LANGUAGE_BUG.

**Original claim:** a direct call such as `b.next()` on `Box<String>`
compiled even though the selected generic trait impl is:

```drift
implement<T> SinglePassIterator<T> for Box<T> require T is Copy
```

That expectation was wrong: Drift explicitly implements `Copy for String`.
`String` is an ARC handle whose source-level copy retains the backing value.
Therefore `T=String` satisfies `T is Copy`, and accepting `Box<String>` is
correct.

**Trace result:** the concrete `main` call does reach the shared candidate
require filter. It does not take the early `receiver_is_type_param` branch.
The filter recovers the `require T is Copy` expression, then correctly accepts
the candidate through its Copy shortcut because
`type_table.is_copy(String) == True`.

No compiler fix is justified from the `Box<String>` result.

**Required replacement test:** use a genuinely non-Copy `T`, for example a
user-defined owning struct with no Copy impl or `Array<String>`. First assert
independently that `copy_status(T) is False`, then test both:

- direct `b.next()` on `Box<T>`;
- for-in over `Box<T>`.

If either is accepted, classify the result as LB-6 and follow the
regression-first sequence:

1. Add a minimal direct-method regression independent of for-in.
2. Confirm current behavior incorrectly accepts the non-Copy type.
3. Capture candidate, require expression, receiver arguments, impl
   substitution, final prover substitution, and proof status.
4. Find and fix the first broken invariant in shared generic trait-impl
   applicability machinery.
5. Confirm direct call and for-in both reject the non-Copy instantiation with a
   typecheck-phase error.
6. Confirm `Box<Int>` succeeds through full compile/run.

Do not add a for-in-specific requirement check. If the genuinely non-Copy
negative case already rejects correctly, close this investigation as
not-a-bug.

## Rejected approaches

- Converting `JsonEntriesIter` from a variant to a struct.
- Unconditionally moving every for-in rvalue at AST/HIR desugaring time; this
  breaks borrow-only non-Copy rvalues such as `Array<String>`.
- Moving user locals implicitly.
- A global method-name/signature scan for `iter`.
- Treating `traits_in_scope=[Iterable]` as a hard candidate constraint.
- Keeping a TRAIT CallInfo for concrete MIR-bound for-in calls.
- Retrying all of resolution from for-in to implement Copy ref-to-value.
- Adding a for-in-only `require` check.
- Deferring LB-3 or any confirmed replacement LB-6 as
  unrelated/pre-existing.

## std.json Slice 2B surface

`JsonEntriesIter` remains a self-iterating variant implementing:

```drift
SinglePassIterator<HashMapItemRef<String, JsonNode>>
Iterable<JsonEntriesIter, HashMapItemRef<String, JsonNode>, JsonEntriesIter>
```

Behavior remains:

- borrowed keys and values, no cloning;
- unspecified HashMap iteration order;
- non-object and empty object both produce zero entries;
- manual `.next()` requires `SinglePassIterator` in trait scope;
- `for entry in node.entries()` should work directly;
- early iterator and early loop exits must be leak-clean.

Relevant tests:

- `lang/tests/codegen/e2e/std_json_entries_iter_behavior`
- `lang/tests/codegen/e2e/std_json_entries_for_in`

## Current changed areas

- `lang/driftc/checker/call_resolver.py`
- `lang/driftc/stage1/ast_to_hir.py`
- `lang/driftc/stage1/borrow_materialize.py`
- `lang/driftc/stage1/hir_nodes.py`
- `lang/driftc/stage1/place_canonicalize.py`
- `lang/driftc/stage2/hir_to_mir.py`
- `stdlib/std/json/json.drift`
- docs/history/version and the regressions listed above

The worktree is uncommitted. Preserve unrelated user changes.

### LB-7: ConstShare structural synthesis missing from the test-helper pipeline

**State:** RESOLVED.

**Symptom:** a ConstShare-only (non-Copy) by-value `Iterable` bound to a local
(`for x in v`) compiled + ran correctly via the CLI (`--entry`: exit 12,
memcheck-clean) but FAILED the strict codegen e2e runner with
`no matching method 'iter' for receiver Ref<CS>` — the secondary `&T -> T`
coercion (LB-5) silently skipped because `ConstShare(CS)` proved **false** in the
runner's build path.

**Root cause (pipeline convergence):** post-link ConstShare *structural
synthesis* (`synthesize_const_share_phase1`) — which derives the ConstShare impl
for composition-rule types and registers its `const_share` method — runs in the
CLI driver but NOT in the test-helper pipeline (`compile_stubbed_funcs`, used by
`compile_to_llvm_ir_for_tests`). So user types deriving ConstShare proved it in
the CLI world but not the test world. Two follow-on defects surfaced while
wiring it: (a) the synthesizer was handed the wrong callable registry
(`semantic_world.callable_registry`) while `check_function` resolves against the
LOCAL registry → the synthesized `const_share` resolved to Unknown; (b) the
test-helper registry arrives FROZEN.

**Fix:** extracted one shared idempotent helper
`_run_post_link_const_share_synthesis` (driftc.py) invoked by BOTH the CLI and
`compile_stubbed_funcs` (before its impl-index build, pre-typecheck). It passes
the LOCAL registry, temporarily unfreezes it for the run, and builds a closed
visibility map for the proof world. Idempotence is split: **semantic synthesis**
is run-once per `TypeTable` (the synthesizer skips already-covered types), while
**registry population** is gated per-registry (`_const_share_hydrated`) so a
prepared `TypeTable` reused with a fresh registry still hydrates. No refactor
trigger matches; internal pipeline correction → `DRIFTC_VERSION` bump, ABI
unchanged.

**Regression:** e2e `for_in_byvalue_constshare_local` now passes BOTH gates —
strict runner (zero diagnostics, exit 12, memcheck-clean) AND `--entry` (12).

### Resolution status (LB-1 .. LB-7)

- **LB-1 .. LB-4:** RESOLVED (codegen marker + normalization preservation + match-binder MIR identity + central trait-constrained resolution with DIRECT targets).
- **LB-5:** RESOLVED — central secondary `&T -> T` receiver coercion via a provenance-gated unwrap of the compiler-synthesized implicit borrow to a fresh binding read (Copy) / implicit `const_share` (ConstShare-only); explicit `&v` never converts. The candidate-local effective receiver type is established from a single conversion `type_expr` and used through the continuation.
- **LB-6:** CLOSED as INVALID — `String` IS Copy, so `Box<String>` was a bad probe; with a genuinely non-Copy `T` (`Box<Array<String>>`) the `require T is Copy` impl is correctly rejected on both direct `.next()` and for-in. No defect.
- **LB-7:** RESOLVED (above).
- **LB-8:** RESOLVED (below).
- Required-trait matching uses canonical full `TraitKey` (package_id + module + name).

### LB-8: implicit `const_share` insertion desynced callsite_ids from the per-callsite instantiation map (cross-loop generic for-in mistargeting)

**State:** RESOLVED.

**Symptom:** a NON-Copy (move/const_share-path) for-in loop FOLLOWED by a
generic by-value `Box<T>` for-in loop miscompiled.  The generic Box loop's
`iter()` callsite lowered to a MIR `Call` targeting `Box<T>::…::next__inst…`
instead of `Box<T>::…::iter…`; LLVM rejected the IR
(`'%tN' defined with type '%Variant_main_Box…' but expected 'ptr'` — the iter
position received the Box VALUE where `next(self: &mut Box<T>)` wanted a ptr).
Combination-dependent: `box_only`, `box_then_box`, and `cc_then_box` (Copy
variant + generic) all PASS; only a preceding **non-Copy ConstShare** loop
breaks the following generic loop.  The smallest failing combination is the
dedicated regression.

**Root cause (first broken invariant):** `type_checker.check_function`'s
`_alloc_callsite_id` seeds its high-water mark from `_max_callsite_id(body)`,
which used a hand-rolled `__dict__` walker that did NOT descend into statement
children of nested blocks (`HLet`/`HLoop` produced by the for-in desugaring).
For a body whose calls all live inside for-in blocks, it found no callsite_id
and returned -1, so allocation started at **0**.  The secondary `&T -> T`
receiver coercion (LB-5) for the non-Copy loop synthesizes an implicit
`const_share` HMethodCall and allocates it callsite_id **0** — colliding with an
existing call.  `_record_call_info`'s collision path then reassigned it and the
collision **cascaded +1 through every later call's callsite_id**.  But the
per-callsite instantiation map (`instantiations_by_callsite_id`, recorded during
resolution keyed by the pre-cascade ids) was NOT shifted in lockstep, so the
post-cascade node csids pointed at the neighbouring call's monomorphization —
the generic Box `iter()` node inherited the following `next()` instantiation.

**Boundary investigation (HIR → MIR → SSA → LLVM):**

- HIR desugaring is correct: distinct `iter`/`next` `HQualifiedMember` calls
  with `origin="for_iter"`/`"for_next"`.
- Central resolution is correct: `resolve_qualified_member_ufcs` resolves the
  Box iter callsite to `Box::…::iter` and records the instantiation under the
  callsite_id it holds at resolution time.
- The defect is upstream of MIR/SSA, in the checker's callsite-id bookkeeping:
  the per-callsite maps (`call_info_by_callsite_id`,
  `instantiations_by_callsite_id`) and the HIR node `callsite_id`s diverged.
  The reviewer's address-taken/alloca hypothesis did NOT hold — the iterator
  local IS materialized; the call simply targeted the wrong generic instance.

**Fix part 1 (root cause):** `_max_callsite_id` now uses the canonical
`node_ids.iter_hir_walk` (the same complete traversal `assign_callsite_ids`
uses to seed parse-time ids), so the high-water mark covers every call node and
`_alloc_callsite_id` always allocates ABOVE all existing ids.  The implicit
`const_share` gets a fresh non-colliding id → no cascade → the LB-8
mistargeting cannot occur.  No for-in-specific materialization rule was added.

**Fix part 2 (treat collisions as invariant failures, not recoverable input —
per review):** the silent collision-recovery machinery in the checker's
callsite-id bookkeeping is removed:

- `_record_call_info` collision identity is now the **owner node**, not
  CallInfo equality.  First claim (owner unset) and same-node re-record
  (owner == node) are the only accepted paths; a *different* node claiming an
  already-owned callsite_id raises an internal invariant failure naming the
  conflicting id and BOTH node ids.  The `existing == info` cross-node
  exception and the fresh-id reassignment branch are gone.  Rationale:
  resolution may already have overwritten the owner's per-callsite side-table
  entries before the collision is detected, so recovery cannot reliably
  reconstruct which instantiation belongs to which node — failing at the first
  broken invariant is the only sound option.
- `check_function`'s start-of-check renumber switched from blanket
  `assign_callsite_ids` (overwrite-all, which silently renumbered duplicates
  away) to the new `node_ids.assign_missing_callsite_ids` (fill ONLY missing
  ids, above the high-water mark; never touch or repair existing ids).  This
  makes `check_function` trust the unique dense ids produced upstream
  (`assign_callsite_ids` + `validate_callsite_ids`) and lets a genuine
  duplicate surface as the invariant failure above instead of being masked.
  For real bodies (already dense+unique) it is a no-op.
- The obsolete `prev_csid != csid` instantiation migration on the HMethodCall
  record path (~`type_checker.py:5918`) is **removed** — `_record_call_info`
  no longer reassigns, so it was dead and carried the same owner-stealing
  hazard.
- `check_function`'s hand-rolled "has any call" scanner (the same incomplete
  `__dict__` walker that skipped nested-block statement children) is **deleted**;
  `assign_missing_callsite_ids(body)` is now called unconditionally when a
  callable registry is present.  The old gate could report "no calls" for a body
  whose calls all live in nested blocks, skip the fill, and let a synthesized
  generic call reach `record_instantiation` with `callsite_id=None` (lost
  monomorphization request).  The full-walk fill is a no-op when nothing is
  missing, so the gate bought nothing.

**Rejected (review):** an unconditional "migrate
`instantiations_by_callsite_id[csid]` on reassignment" was prototyped and
removed — at a genuine collision the entry belongs to the OWNER, so migrating
steals it.  Superseded by the fail-loud design above.

**Sweep telemetry (review item #5):** instrumented BRANCH3 (cross-node
same-CallInfo) and BRANCH4 (reassignment) to log every firing, then swept.
Across the full codegen-e2e source set (1293 cases) compiled check-only, plus
several partial full-compile e2e + driver runs, **neither branch fired even
once** — confirming the recovery path was dead in real compilation.  After
landing the fail-loud + fill-missing changes, a second check-only sweep over
all 1293 e2e sources produced **zero** duplicate-callsite_id invariant failures,
confirming no real body reaches `_record_call_info` with a duplicate id.
Instrumentation removed after the sweeps.

**Behavior-changing compiler fix → `DRIFTC_VERSION` 0.33.30 → 0.33.31; ABI 16
unchanged** (checker bookkeeping correction; no compiler/runtime boundary
signature, layout, or calling-convention change).

**Regressions:**

- e2e `for_in_nc_loop_then_generic_box_loop` (minimal failing combination;
  full compile/run, exit 0, memcheck-clean) — fails before the fix with the
  LLVM type error.
- e2e `for_in_distinct_generic_insts_around_const_share_loop` (normal-path
  monomorphization separation: two DISTINCT generic instantiations
  `Box<Int>`/`Box<Bool>` bracketing a const_share loop each keep their own
  monomorphization — NOT collision coverage; callsite_ids are unique there).
- driver `test_for_in_const_share_callsite_id_alignment.py` — two structural
  pins at the MIR boundary: (1) the generic Box loop preheader lowers to
  exactly one `Iterable::iter` `Call` and the program compiles (fails before
  the fix, `rc != 0`); (2) two distinct generic instantiations yield two
  distinct `iter` monomorphization targets (normal-path separation).
- driver `test_node_ids_and_callinfo.py` — duplicate-callsite_id CONTRACT
  failures (replacing the obsolete `*_are_reassigned_per_call_node` test):
  two distinct nodes sharing an id with DIFFERENT CallInfo, and with IDENTICAL
  CallInfo, both raise the invariant failure (message includes the id + both
  node ids).  Plus `test_assign_missing_callsite_ids_preserves_existing_and_
  fills_nested`: existing (non-dense) ids untouched, missing ids in nested loop/
  block positions filled above the high-water mark, idempotent.

## Next actions

LB-1 .. LB-8 are closed (see the resolution-status block above); LB-6 is closed
as an invalid probe.  Required-trait matching uses canonical full `TraitKey`.
`DRIFTC_VERSION` is bumped to 0.33.31 (ABI 16 unchanged; 0.33.31 is the LB-8
callsite-id-alignment fix).  `history.md` describes the final central-resolver +
`required_trait_key` design (the interim `get_candidates` scan was removed).
Remaining:

1. Keep the broad gates green: full codegen e2e sweep (`just test-shard-2`),
   focused resolver / trait / ownership / for-in / const-share driver suites,
   and all affected e2e under `DRIFT_MEMCHECK=1`.

Done since the last revision: the prepared-TypeTable + fresh-registry hydration
regression (`test_const_share_registry_hydration.py`) and the unrelated-trait
for-in negative (`test_for_in_marker_and_selection.py::
test_unrelated_trait_iter_does_not_satisfy_for_in`) are landed; the LB-7
idempotence split (semantic run-once vs per-registry hydration) is implemented;
the test pipeline reuses the real direct-import + re-export visibility builder
(not all-to-all), and the registry hydrator matches the canonical
`std.core.shareable.ConstShare` trait identity (package + module + name).

## Completion gate

This work is complete only when:

- LB-1 through LB-5 are closed with regressions and root-cause fixes, and the
  LB-6 investigation is either disproved by a valid negative test or fixed;
- no custom for-in candidate scan or semantic workaround remains;
- concrete for-in calls carry DIRECT CallInfo targets;
- all ownership modes and generic requirements behave as specified;
- std.json entry enumeration works with manual `next()` and for-in;
- focused and broad tests pass, including memcheck;
- docs and history describe the final implementation rather than an
  intermediate design.
