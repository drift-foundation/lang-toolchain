# Refactor triggers (latent design improvements)

This file is a registry of compiler design improvements that have been
identified but deferred. Each entry names a specific bug shape that, if
encountered, becomes the natural forcing function for the larger
refactor — at which point the bug fix's deliverable is the refactor,
not a minimal patch.

**Process:** when starting any LANGUAGE_BUG fix, scan this file. If the
bug matches a registered trigger, escalate the deliverable to the
refactor. See `AGENTS.md` § "Refactor triggers (registry of
opportunistic uplifts)" for the full rule.

**Entry format:**

```
## <improvement title>

- **Improvement:** what the refactor does.
- **Why deferred (date):** cost vs current value at filing time.
- **Triggers:** the bug shapes that justify the cost.
- **Scope when triggered:** rough estimate.
```

---

## Consolidate borrow-checker walkers

- **Improvement:** unify the ~5 overlapping HIR walkers in
  `lang/driftc/borrow_checker_pass.py` into a shared
  escape / reference-tracking utility. The current walkers:
  - `_ref_binding_ids_in_expr` — collect all ref-binding IDs.
  - `_collect_binding_ids_for_name_in_expr` — by-name collection.
  - `_expr_references_any_binder` — set-membership check.
  - `_expr_passes_binder_to_call` — call-boundary detection.
  - Lambda capture analysis (`_apply_lambda_capture_moves` chain).

  All five do variations on "find HVar with property P in expression
  E," with slightly different return shapes (set vs bool, full-walk
  vs short-circuit, span vs no-span).

- **Why deferred (2026-04-30):** ~200 lines of duplication, real but
  not painful. No current bug attributable to "the duplication itself
  broke something" — each fix lands cleanly even when both walkers
  need touching. Designing the right API from one user (match-arm
  escape) produces speculative shapes that later refactor anyway.
  The walkers' return-type variation is real — `_build_regions` does
  fixed-point dataflow, `_arm_binder_escapes` is one-shot — and a
  generic visitor that fits both badly is worse than 5 simple ones.

- **Triggers:**

  - Taint propagation for indirect arm-binder escape (the v1
    false-negative documented in 0.31.35
    `doc/match_by_ref_variant.md`). Adding taint flow to current
    walkers means a sixth walker; consolidating becomes natural.
  - Exclusive owner-borrow lifetime extension for `&mut` (deferred
    from 0.31.35; would replace conservative-loan-retention with
    proper liveness tracking). New walker on liveness frontiers
    forces the refactor.
  - Any new escape rule beyond the current direct-store + call
    shapes (e.g., escape via array indexing, escape via closure
    capture into a field). Three users is the minimum for a
    correctly-shaped abstraction.

- **Scope when triggered:** ~3-5 days. Three actual users (existing
  match-arm escape, the new feature, plus one of the remaining
  walkers as a refactor target) gives the API enough surface to
  design without speculation. Add span tracking as part of the same
  pass — current walkers return `bool` / `set[int]`, losing
  diagnostic provenance; a `(found, span, path)` return shape
  enables better notes.

- **Concrete plan when triggered (drafted 2026-04-30, parked
  pending trigger).** Start narrow at HIR — MIR / full-pass refactor
  is out of scope. Single new file
  `lang/driftc/hir_walk.py` with three layers:

  1. **Read-only HIR walker.** `walk_expr(expr, visitor, ctx)` /
     `walk_stmt(stmt, visitor, ctx)`. Visitor exposes `pre_expr`,
     `post_expr`, `pre_stmt`, `post_stmt` hooks; supports early
     stop. `ctx` is a lightweight path/context object that
     accumulates as the walker descends (e.g.
     `[MatchArmResult, CallArg(idx=0)]` for a binder appearing
     as the first arg of a call inside a match arm result).

  2. **HIR rewrite visitor.** Returns possibly-replaced nodes,
     used for transformations like the G3 v2
     `HVar -> HUnary(DEREF, HVar)` rewrite.  **Contract on node
     IDs:** any returned replacement node must have its
     `node_id` assigned and any required side tables
     (binding_for_var, type_by_node_id, etc.) updated.  The
     visitor caller does NOT need to know which side tables —
     the framework handles it.  This is non-trivial and easy
     to forget on a first pass; spec it explicitly.

  3. **Query helpers as thin wrappers.** Each ~5-10 lines, no
     hand-written recursive traversal:
     - `find_vars(expr_or_stmt, predicate) -> list[VarHit]`
     - `references_any_binding(expr_or_stmt, ids) -> bool`
     - `expr_passes_binding_to_call(expr, ids) -> Optional[VarHit]`
     - `collect_captures(lambda)` (later)
     - `contains_move(expr)` (later)

  **`VarHit` shape:**

  ```
  @dataclass
  class VarHit:
      binding_id: int
      source_name: str          # via user_facing_binding_name
      span: Span                # the HVar node's loc
      path: list[PathSegment]   # accumulated context, leaf-last
  ```

  `PathSegment` is a closed enum / union extended ONLY when a
  migrating user demands a new variant.  Start with the four
  contexts the existing walkers need (`CallArg`, `AssignRhs`,
  `MatchArmResult`, `BorrowSubject`); add `LetInit`,
  `ReturnValue`, `TernaryCond`, etc. as the second migration
  site demands.  Don't pre-enumerate — speculative variants
  are where API shape goes wrong.  Path is a list, not a
  single tag — leaf-only loses nesting info that's exactly
  what diagnostic notes want.

  **Migration order (mandatory two-patch split):**

  - **Patch 1 — framework only.** Land
    `lang/driftc/hir_walk.py` with the read-only walker,
    `find_vars`, `references_any_binding`, plus a unit-test
    file pinning the API on synthetic HIR fixtures.  No
    consumer changes; no existing walker touched.  This patch
    is judged purely on framework correctness.
  - **Patch 2 — first migration.** Re-implement the existing
    `_arm_binder_escapes` / `_expr_references_any_binder` /
    `_expr_passes_binder_to_call` in `borrow_checker_pass.py`
    on top of `hir_walk`.  Keep public function names
    unchanged so callers in `_visit_expr` HMatchExpr branch
    don't move.  If the migration reveals an API gap (e.g.
    "VarHit needs a `parent_kind` field"), the gap is fixed
    in patch 1's framework, not entangled with the cert tests
    in this patch.

  Do NOT migrate lambda capture analysis or NLL `_build_regions`
  in either patch — those are different beasts (capture-kind
  state machine, fixed-point dataflow) and the abstraction
  designed for one-shot HVar-finding will fit them badly.
  Wait for those subsystems' own forcing function.

  Total scope when triggered: ~3-5 days, split as patch-1
  framework (~1.5 days, ~150 lines + tests) + patch-2 first
  migration (~1.5 days, drop-in replacement with cert tests
  unchanged) + buffer for the API gap discovered during
  migration.

## Promote DMIR `_to_jsonable` discriminators to module-qualified names

- **Improvement:** the provisional DMIR encoder at
  `lang/driftc/packages/provisional_dmir_v0.py:42-67` uses bare
  class names as the `_type` discriminator
  (`type(obj).__name__`).  Reconstruction looks up the bare name
  in a registry built by `build_dataclass_registry`, where
  registration order decides who wins on collisions.  Promote
  the encoding to module-qualified names
  (`lang.driftc.stage0.ast.TypeNameRef` instead of
  `TypeNameRef`), with a back-compat read path that accepts
  legacy bare-name `.dmp` files for one release cycle.

  `parser_ast` and `stage0.ast` both define `TypeNameRef`; only
  stage0's carries `module_id`.  The 0.31.28 fix worked around
  the collision by reordering registration so stage0 wins —
  fragile by design.  The failure mode is silent field drop on
  round-trip, surfacing as multi-layer-deep downstream bugs
  (e.g. the `E_INTERNAL_MISSING_CALLSITE_CALLINFO` chain that
  0.31.28 fixed: dropped `module_id` →
  `trait_key_from_expr` fallback → `trait_index` miss → no
  `CallInfo` → confusing diagnostic on the user's
  `captures(share x)` source).

- **Why deferred (2026-04-30):** the structural fix changes the
  `.dmp` format and needs back-compat for deployed consumers,
  possibly an ABI implication.  Real surgery (~3-5 days).  In
  the absence of a *new* collision, the 0.31.28 registration-
  order fix holds.  See companion entry "Defensive collision
  check for DMIR registry" — the lower-cost mitigation lands
  first; this entry's trigger is the assertion in that one
  firing.

- **Triggers:**

  - The defensive collision-check assertion in
    `build_dataclass_registry` fires (a new dataclass with a
    colliding name is added by any module).  Mechanical
    trigger; the author hitting the assertion gets pointed
    here from the assertion message.
  - A reported package-consumer bug traces to silent field
    drop or type collision on `.dmp` round-trip (i.e., the
    failure mode that 0.31.28 mitigated, recurring in a new
    location the registration-order fix doesn't cover).
  - Another planned `.dmp`-format change that lets us bundle
    the discriminator promotion (e.g., a versioned schema
    bump).  Bundling avoids a second back-compat window.

- **Scope when triggered:** ~3-5 days.
  1. Switch `_to_jsonable` to emit
     `f"{type(obj).__module__}.{type(obj).__qualname__}"` as
     `_type`.  ~10 lines.
  2. Update `from_jsonable` to look up by qualified name via a
     `dataclasses_by_qualified` registry.  Keep the bare-name
     registry as a back-compat fallback.  ~30 lines.
  3. Mark the bare-name fallback `@deprecated` with a
     release-cycle removal target; emit a warning when it
     fires so deployed consumers see the upgrade path.
  4. Update producer-side tests + add a fixture that exercises
     a legacy bare-name `.dmp` to confirm back-compat path
     works.
  5. ABI bump if the `.dmp` schema version is part of the
     boundary contract (verify against `AGENTS.md` § "Boundary
     Contract Guardrails").

## ~~Defensive collision check for DMIR registry~~ (attempted 0.31.36, reverted 0.31.37)

> **Status — not viable as filed.**  Attempted at 0.31.36 (`6a01a723`),
> false-fired on the certify-lane smoke compile, reverted at 0.31.37
> (`<TBD>`).  Kept visible per the registry's "record what was tried"
> discipline.

- **What was tried:** runtime assertion in
  `build_dataclass_registry` that fires when two dataclasses
  with the same `__name__` but differing field sets are
  registered.  Intent: turn the silent-data-loss failure
  mode that motivated 0.31.28 into a hard error with a clear
  upgrade pointer to the qualified-discriminator entry above.

- **Why it didn't work:** `parser.ast` and `stage0.ast` are
  intentionally divergent ASTs that share class names by
  convention.  An audit at 0.31.37 enumerated **16+** classes
  whose field name *sets* (not just order) differ between the
  two modules — `Attr` (parser-only `op`), `BlockStmt`
  (`block` vs `body`), `IfStmt` (`condition` vs `cond`),
  `Param` (parser missing `loc`), `Ternary`, `ThrowStmt`,
  `WhileStmt`, etc.  All are benign at runtime: only stage0
  nodes ever reach `_to_jsonable` (parser_ast is parser-only
  and never serialized), so the registry's last-wins
  behavior is correct.  A generic check at registry-build
  time can't tell benign-divergence-with-only-stage0-encoded
  from real-hazard-where-both-encode without per-class
  serialization-side metadata that doesn't exist today.

- **Lesson for any future attempt:** "do two dataclasses with
  the same name have different fields?" is the wrong
  question; the right question is "could a serialized
  instance of one shape end up reconstructed against the
  other shape?"  Answering that needs a side-channel — e.g.
  a `_serialized_in_dmir = True` class-attribute marker, or
  an explicit allowlist of "intentionally divergent
  parser/stage0 twins."  The structural fix (module-qualified
  discriminators, the entry above) sidesteps this entirely
  by making the discriminator unambiguous; until that lands,
  the registry stays on disciplined registration order.

- **Triggers (re-fire):**

  - Module-qualified discriminator entry above is acted on.
    At that point the bare-name registry goes away and any
    defensive check moves with it.
  - A future per-class `_serialized_in_dmir` marker (or
    similar) lands, giving the check enough signal to
    discriminate hazard from intentional divergence.

## Drop-aware `RawBuffer` / `Ptr` write variants

- **Improvement:** today `mem.write<T>(&mut RawBuffer<T>, Int, T)` and
  `mem.ptr_write<T>(Ptr<T>, T)` are raw stores (`_lower_raw_buffer_write`
  / `_lower_ptr_write` in `lang/codegen/llvm/llvm_codegen.py`) — they
  do not drop the previous slot contents.  Stdlib (`std.containers`)
  already obeys the implicit raw-store contract: every callsite drains
  via `mem.read` / `read_value` first when the slot may be initialized.
  The user-facing intrinsic doc was corrected in 2026-04-30 to match
  reality.  When demand surfaces, introduce drop-aware companion
  intrinsics — e.g. `mem.write_drop<T>` and `mem.ptr_write_drop<T>` —
  that lower to `(read old) + DropValue + (raw store)`, mirroring the
  `_emit_assign_store_ref` shape the 0.31.18 replace-store invariant
  established for `&mut`-place assignment.  Migrate stdlib callsites
  that are conceptually doing "replace the value at this initialized
  slot" (HashMap `replace_value`, TreeMap insert-replace, TreeMapEntryMut
  insert-replace) to the drop-aware variants — this collapses the
  manual drain-then-write pair into a single intrinsic and removes a
  category of subtle leak bugs from any future user `unsafe` block.

- **Why deferred (2026-04-30):** the doc/impl divergence was the
  proximate concern, and that's a doc-only fix.  No stdlib callsite
  is affected (all already drain manually).  Adding drop-aware
  intrinsics is real surgery — new MIR ops, new LLVM lowering, new
  ownership-pass handling parallel to the `M.RawBufferRead` /
  `M.PtrRead` / `M.MoveOut` shape covered at
  `lang/driftc/stage2/string_arc.py`.  No current bug attributable
  to the missing drop-aware variant.  Speculative without a real
  consumer.

- **Triggers:**

  - A user-reported leak / UAF in a downstream `unsafe` block traced
    to "I called `mem.write` (or `mem.ptr_write`) into a slot that
    was already initialized, expecting the old value to be dropped."
    The doc fix is the first line of defense; if a user still trips
    over this, the structural fix (drop-aware variant) is justified.
  - Any new stdlib container that needs the drain-then-write idiom
    in a perf-critical path where the manual `read_value` round-trip
    is observable.  Three users of "replace at initialized slot"
    (HashMap, TreeMap, plus the new container) is the minimum for a
    correctly-shaped intrinsic.
  - A safe-API wrapper effort over `RawBuffer<T>` / `Ptr<T>` (e.g.,
    a future `Slot<T>` type that tracks init bit) that wants to
    delegate "replace" to a single primitive instead of composing
    `read` + `write`.

- **Scope when triggered:** ~2-3 days.

  1. New MIR ops (e.g. `M.RawBufferWriteDrop`, `M.PtrWriteDrop`) plus
     HIR→MIR lowering of `mem.write_drop` / `mem.ptr_write_drop`
     intrinsic calls.
  2. LLVM lowering — `(read old into temp) + DropValue(temp) + raw
     store new`.  Mirror the existing `_emit_assign_store_ref` shape.
  3. `string_arc.py` (and any other ownership pass that walks
     drop-bearing reads) handler parallel to `M.RawBufferRead` /
     `M.PtrRead`.
  4. Stdlib migration: HashMap `replace_value`, TreeMap insert-replace,
     TreeMapEntryMut insert-replace.  ~6 lines per site.
  5. Doc update — point users at the drop-aware variant as the
     "replace at initialized slot" canonical primitive.
  6. ABI bump — new compiler→runtime intrinsic surface (verify
     against `AGENTS.md` § "Boundary Contract Guardrails").

## Carry implicit-move classification structurally from borrow-check to MIR lowering

- **Status (2026-05-27, post-0.33.6 language-contract clarification):
  this refactor's premise is largely retired.**  See `Triggers`
  below for the live conditions that would revive it.  Entry kept
  in the registry per the "record what was tried / considered"
  discipline.

- **Improvement (as originally proposed):**  the borrow
  checker's implicit-move classification for non-Copy
  consuming-position reads lives in flow state only —
  `borrow_checker_pass.py::_force_move_place_use_implicit`
  updates `PlaceState.MOVED` for diagnostics but does NOT
  rewrite the HIR.  MIR lowering then re-derives "is this
  consuming?" at each potential consume site.  Promote the
  classification into the HIR itself: a post-borrow-check pass
  rewrites every consuming-position `HVar` into
  `HMove(subject=HPlaceExpr(HVar, []), is_implicit=True)` so
  every consuming path naturally hits `_visit_expr_HMove`'s
  zero-back-on-env-slot emission discipline.

- **Why the premise retired (2026-05-27).**  The
  language-contract clarification that landed in 0.33.6 makes
  Drift's ownership transfer **explicit at the source**: bare
  `f(x)` for a named non-`Copy` owner is a compile error
  (`cannot copy 'x': type 'T' is not Copy (use move x)`).  Users
  MUST write `f(move x)` to transfer ownership.  See
  `doc/design/drift-lang-spec.md` §1.3 and §4.2 for the rule.
  Under that contract, **there are no implicit moves at named
  call args** — every source-level ownership transfer becomes
  an `HMove` at the AST/HIR level (parser sees `move`, lowers
  to `HMove`), so MIR lowering's `_visit_expr_HMove` already
  handles every transfer site uniformly.  Lowering's role is to
  preserve / zero an explicit `move`, never to authorize one.
  The flow-state-only "implicit move" classification in the
  borrow checker has become INTERNAL diagnostic state — it
  never escapes to lowering because the type checker rejects
  the source surfaces that would have required it.

- **Why deferred (2026-05-27):** under the post-0.33.6 contract,
  the refactor is no longer needed for callback-capture UAFs
  (the reported bookkeeper bug — see
  `drift-vt-drop-atexit-use-after-free.md` — was closed by the
  source-side fix `_worker_body(..., move gw, ...)`; the
  compiler shipped no change in 0.33.6).  Net cost was estimated
  at ~3-5 days; cost is now mostly moot until/unless one of the
  triggers below fires.

- **Brief 0.33.6 fix-iteration history.**  Two non-shipping
  shapes were tried before the language-contract decision:
  (1) blanket zero-back in `_visit_expr_HVar` for every
  destructible MOVE-capture read — rejected by review as
  over-broad (would zero non-consuming reads too,
  `SSA: load before store` on positive non-consuming test);
  (2) capture-aware branch in `_lower_call_arg` plus
  `_lower_call` arg-loop unification — rejected because it
  silently allowed bare HVar at by-value call args, regressing
  the 0.31.70 friendly-diag contract pinned by
  `test_use_move_call_arg_friendly_diag.py`.  The user-level
  decision after iteration (2): `f(x)` must not silently
  consume `x` — language-contract clarification, no compiler
  change.

- **Triggers.**  Under the 0.33.6 language-contract
  clarification (`drift-lang-spec.md` §1.3), all named non-`Copy`
  ownership transfers must spell `move` at the source.  The type
  checker rejects bare HVar at every consuming position (HReturn,
  HLet RHS, HAssign RHS, HCall by-value arg, ctor field, ...)
  with `cannot copy 'X': type 'T' is not Copy (use move X)`
  (`E-AUTO-c38540ff`, `type_checker.py:3161`) plus the MIR
  validator's equivalent friendly diagnostic at the call-arg
  site.  Every accepted transfer source then arrives at MIR
  lowering as `HMove`, which routes through `_visit_expr_HMove`'s
  env-slot zero-back path — already correct for both function-
  frame locals and callback captures.  The pre-existing
  `_lower_call_arg`-emits-`MoveOut`-for-non-Copy-HVar fallback
  path remains for non-statement-form call sites pre-dating
  0.31.70's validator, but those sites also fall under the
  source-level "must write `move`" rule and the friendly diag
  fires at any new statement-form regression.

  The structural refactor fires if ANY of the following becomes
  true:

  - **Type-checker / validator relaxation.**  Either the type
    checker at `type_checker.py:3135-3170` OR the MIR
    validator's friendly-diag gate is loosened to accept bare
    non-`Copy` HVar at a consume position (any of HReturn,
    HLet RHS, HAssign RHS, by-value call arg).  At that point
    the spec contract no longer holds at the source surface
    and the compiler needs the structural rewrite to keep
    callback captures from silently double-dropping under
    the newly-accepted shapes.  The trigger pointer goes here
    from the type-check / validator relaxation slice.
  - **New consuming-position site added.**  Any new
    consume-position lowering in `hir_to_mir.py` (or a new
    syntactic form whose lowering consumes a non-`Copy`
    local via bare HVar — `select` arm-body, pattern-match
    consume, async / generator points, etc.) that does NOT
    require explicit `HMove` from the source.  At that point
    duplicating `_move_from_callback_capture_slot` routing at
    the new site is more expensive than landing the
    structural rewrite.
  - **Another callback-capture over-drop reported or
    review-discovered outside the call-arg path.**  Same
    atexit / cb-drop trace shape as
    `drift-vt-drop-atexit-use-after-free.md` but with a
    consume site in the trace that's NOT
    `_lower_call_arg` (or the unified `_lower_call` arg loop
    that funnels into it).  Whether the finding comes from a
    fresh app-team filing or surfaces in a slice review, it
    confirms the type-checker enforcement has a hole the
    structural rewrite would close at the source rather than
    per-site.

  **Borrow-checker walker consolidation lands first** (see
  "Consolidate borrow-checker walkers" above) — that refactor
  introduces span/provenance return shapes that this pass can
  consume directly, lowering the implementation cost.  Not a
  fire trigger; a sequencing preference.

- **Ruling (2026-07-13, E-population LANGUAGE_BUG slice):
  considered — NOT fired — with one RECORDED language exception
  and one per-site containment.**  Addressing the
  "pattern-match consume" wording of the second trigger bullet
  directly:
  - The match SCRUTINEE is the language's ONE deliberate
    implicit-consume position: bare `match r` on a non-Copy
    place scrutinee consumes without a `move` spelling
    (requiring `match move r` would be an ecosystem-wide
    breaking change out of proportion to the benefit).  The
    second bullet targets consuming positions ADDED without
    source-level `HMove`; the scrutinee consume PRE-DATES both
    the trigger and the 0.33.6 contract — it was never a new
    position, it was an untracked one.  This slice made the
    consumption TRACKED (borrow-check flow state;
    E_USE_AFTER_MOVE on any later scrutinee use, including
    re-match) and PINNED as an exception
    (`test_match_consume_and_arm_call_gate.py::
    test_bare_match_exception_legal_and_consuming`).
  - The trigger's predicted per-site failure — a consume site
    without `_move_from_callback_capture_slot` routing — was
    probe-CONFIRMED at exactly this position (match on a
    MOVE-CAPTURED non-Copy scrutinee read a ZEROED payload;
    reproduced on certified 0.33.82) and fixed PER-SITE by
    routing the arm consume through the capture-slot helper
    (`_ensure_arm_scrut_ptr`), with a pin.  One known position,
    now routed and pinned, does not outweigh the 3-5 day
    structural rewrite.
  - SHARPENED fire condition: this trigger FIRES if (a) any
    SECOND implicit-consume position is added or discovered, or
    (b) another capture-slot mis-route is found at any consume
    site — either confirms per-site duplication is recurring
    and the structural rewrite is the durable close.
  - Same slice, related restoration (not an acceptance): the
    explicit-move call-arg gate was found to never reach
    match-arm BODIES (boundary-walk coverage hole) and was
    restored; 49 stdlib sites then spelled `move` explicitly.

  **Confirmation pass result (2026-05-27).**  Drafted minimal
  regressions for HReturn / HAssign / HLet of a callback-
  captured `Arc<T>` against 0.33.6.  All three were rejected by
  the type checker before reaching MIR lowering, confirming
  that every named non-`Copy` ownership transfer must spell
  `move` at the source.  After that finding, the language-
  contract clarification was adopted as the 0.33.6 release
  shape: no compiler change, source-side `move` keyword
  required.  Cert remains unblocked; trigger parked.

- **Scope when triggered:** ~3-5 days.
  1. Add `lang/driftc/stage1/implicit_move_materialize.py` —
     new pass that runs AFTER the borrow checker and BEFORE
     MIR lowering.  Walks each function's HIR, re-runs the
     borrow-checker classification (or reads from a borrow-
     checker-emitted side-table if the consolidation refactor
     above has shipped), and rewrites consuming `HVar` nodes
     into `HMove(is_implicit=True)`.
  2. Remove the per-consuming-site re-derivation in
     `hir_to_mir.py`: the capture-aware branch in
     `_lower_call_arg`, and any equivalent branches at HLet /
     HReturn / ctor-arg sites that get added as new bugs land.
     `_visit_expr_HMove` becomes the SOLE site emitting the
     consume-implies-zero-back contract.
  3. Update tests:
     `test_vt_capture_implicit_move_atexit_uaf.py` should
     still pass unchanged (the bug is still caught, just
     through HMove now).  Add a test verifying the pass'
     output HIR shape — every implicit-consume reads HMove —
     so a future regression in the rewrite pass is loud rather
     than silent.
  4. ABI: codegen-internal pass, no boundary change.  No bump.

- **Concrete plan when triggered (drafted 2026-05-27, parked
  pending trigger).**  Single new file
  `lang/driftc/stage1/implicit_move_materialize.py` with three
  layers:

  1. **Visitor.**  Reuses the borrow checker's
     `_FlowState` / `_consume_place_value` /
     `_force_move_place_use_implicit` classification — same
     traversal order, same per-expression consume signal — but
     instead of just updating flow state, records each consuming
     `HVar` node by identity (object id) in a per-function set.
  2. **Rewriter.**  Second pass over the HIR that wraps each
     recorded `HVar` in `HMove(subject=HPlaceExpr(base=HVar,
     projections=[]), is_implicit=True)`.  Preserves
     `binding_id` / `name` / `loc` from the original HVar.
     Span: copy the HVar's source span onto the synthetic
     HMove so diagnostics reading off HMove still point at
     the original consume site.
  3. **Driver hook.**  Run after `borrow_check_cli` in
     `compile_stubbed_funcs`; mark the function's ledger
     dirty (`mark_ledger_dirty(func,
     "implicit_move_materialize")`) so downstream passes
     re-derive ownership state from the rewritten HIR.

  Open question — whether the rewrite should also happen for
  function-frame locals (currently handled correctly by the
  `MoveOut(local=name)` emission at `_lower_call_arg`'s
  non-capture branch and by cleanup-authoring's drop-flag
  pass).  Default to YES for uniformity: rewrite both, let
  `_visit_expr_HMove` handle both, retire the per-site
  re-derivation entirely.

## Add span / provenance to existing borrow-checker walkers

- **Improvement:** existing walkers return `bool` or `set[int]` — by
  the time a diagnostic is emitted, the location of the offending
  reference is gone. Returning `(found, span, path)` from every
  walker would let escape diagnostics produce notes like "escape
  created here → owner reassign here" instead of just "cannot write
  to 'r' while it is borrowed."

- **Why deferred (2026-04-30):** strictly diagnostic quality, not
  soundness. Current diagnostics already point at the conflict site
  with a "borrow created here" note, which covers the common case.

- **Triggers:**

  - Same as "consolidate borrow-checker walkers" above — span
    tracking is the natural foundation a unified walker needs.
  - User report that a specific borrow-conflict diagnostic lacks
    sufficient context (e.g., "I can't tell which arm escaped").

- **Scope when triggered:** ~1 day standalone, or absorbed into the
  consolidation refactor.

## String ownership-authoring conformance matrix

- **Improvement:** the String/Arc ownership-authoring subsystem
  (`lang/driftc/stage2/string_arc.py`, `cleanup_authoring.py`,
  `match_cleanup_authoring.py`, `ownership_ledger.py`, and the
  exception-edge cleanup emitted around `Goto`/landing-pad blocks)
  decides, per local/temp, whether a `String` (and other
  `_type_needs_drop` value) is retained / released / dropped / moved
  at every producer, consumer, and exit. It is ~3.5k lines across
  several passes that each re-derive ownership locally, and it has
  failed one path at a time repeatedly (see the recurring-defect list
  below). Build a **representative conformance matrix** that pins the
  ownership contract across the producer × consumer × exit space, and
  centralize the consumed/tombstoned classification so that every exit
  path (normal scope drop, throwing-edge unwind, move-return) reads the
  SAME droppable-set state rather than re-deriving it. The matrix — not
  a full rewrite — is the deliverable; the centralization is whatever
  the matrix's failing cells force.

- **Why deferred (2026-06-20):** the subsystem is correct on the
  common paths and each historical defect has been a *specific*
  uncovered cell (a producer/consumer/exit combination), fixable in
  isolation. A full ownership-model rewrite is high-risk surgery on the
  one part of codegen that, when wrong, silently double-frees. The
  matrix bounds the work: fix the reported root cause, then assert a
  representative grid leak/double-free-clean under valgrind, and only
  centralize the state the failing cells actually require.

- **Triggers** — fire this entry (escalate from one-path patch to
  root-cause-fix + conformance-matrix) on ANY of:

  - A `String`/Arc **leak, double-free, or use-after-free** whose root
    cause is in `string_arc.py`, cleanup authoring (incl.
    `match_cleanup_authoring.py`), the ownership ledger, exception-edge
    cleanup, **container transfer** (array `push`/`insert`/set,
    struct/variant field store), **move-return teardown**, or
    **field/array-element projection lowering**
    (`hir_to_mir.py::_visit_expr_HField`/`_visit_expr_HIndex` — the
    `_ref_field_temps` aliasing classification of a borrowed non-Copy
    field/element read).
  - A defect where a value **consumed by a container/field store**
    (retain-into-storage + release-original, or MoveOut) is still
    visible to a later cleanup site (normal or exception edge) — i.e. a
    stale entry in the droppable set.
  - Any fix that would otherwise be "patch this one emitted
    release/retain instruction" when an exception-edge or move-return
    cleanup can still see the same stale temp.

  **This bug fires it immediately (2026-06-20):**
  `arr.push(throwing_call_returning_heap_String())` repeated ≥2×
  double-frees at teardown. The push consumes the arg temp (`retain →
  store in array; release original`) but the original temp is left in
  the droppable cleanup set, so cleanup/exception-edge blocks emit a
  second `drift_string_release` on a buffer the array already owns.
  Masked for inline literals only because static-string release is a
  no-op. Reported by DriftQuery (M3 file-read → env build).

  **Fired again (2026-06-25) — COVERED, RESOLVED 0.33.58:**
  `fields[j].value.s + ""` (`Array<Field>` → by-value struct `Field.value`
  → `Value.s: String`) double-freed the live array buffer.  NEW recurrence
  class = **parallel field-projection lowering paths**: the
  `HField(HIndex)` fast path in `_visit_expr_HField` borrowed a NON-Copy
  struct field but, unlike the general `StructGetField` path, did NOT add
  the result to `_ref_field_temps`; the next `.s` projection then treated
  the intermediate as an owned rvalue and emitted a spurious drop of a
  shallow `String` view = double free.  (One-hop `arr[i].s` was safe — a
  Copy `String` field returns earlier via the `_should_copy_value`
  CopyValue branch.)  Fix: flag borrowed non-bitcopy field reads as
  ref-aliases in the fast path (mirror the general path).  Lowering-only,
  ABI 18.  Bounded matrix shipped:
  `lang/tests/driver/test_string_concat_nested_struct_array_field.py`
  (7 shapes: bug + 1/3-hop + plain-local + borrow-penultimate + literal &
  no-concat controls, each multi-pass) + a valgrind-memcheck row on the
  failing shape.  Reported by DriftQuery (M6 engine bridge).  Standing
  lesson: any two lowering paths that read a field/element must apply the
  SAME `_ref_field_temps` aliasing rule.

- **Scope when triggered:** root-cause fix in the owning pass +
  a **bounded** conformance matrix (NOT an open-ended rewrite, NOT a
  full Cartesian product):
  - Producers: heap concat, `string_from_utf8_bytes`, static literal
    (mask-control only).
  - Consumers: array `push`, struct/variant field, local
    assignment/reassignment.
  - Exits: normal scope drop, throwing-edge unwind, move-return
    teardown.
  - Include nested `Array<Struct{String}>` if cheap (matches the
    reported env shape).
  - Each accepted row asserted leak/double-free-clean under valgrind or
    alloc-track; static-literal rows are CONTROLS, not ownership proofs.
  - If the matrix exposes further defects, fix root causes in the same
    subsystem rather than narrowing the tests around them — that is the
    point of firing the trigger.

- **Resolved in 0.33.70 (`fix/projected-capture-lowering`).** Found during
  research (2026-07-05) as a THIRD parallel lowering path missing the same
  `_ref_field_temps` aliasing mark, and fixed as part of the
  projected-capture-lowering feature landing (was tracked here as a
  prerequisite while that feature was still pending; the feature has now
  landed so this note is historical, not open work).
  `hir_to_mir.py::_load_capture_from_env`'s REF/REF_MUT branch did a
  `LoadRef` into a fresh temp — structurally identical to the general deref
  path (`_visit_expr_HUnary` DEREF) and the array-index field-projection
  fast path — but did NOT add the result to `_ref_field_temps` for a
  non-bitcopy inner type, unlike both of those; now fixed. Separately (a
  distinct gap, same root confusion): the COPY-kind lambda-capture
  env-construction site, in both `_lower_lambda_immediate_call` and
  `_lower_lambda_callback`, never called `_copy_if_ref_alias()` before
  storing the captured field's value into the heap-allocated closure env,
  unlike every other ownership-transfer boundary (struct/variant construct,
  return, assignment, call args); now fixed at both sites. Confirms the
  standing lesson yet again: "any two lowering paths that read a
  field/element must apply the SAME `_ref_field_temps` aliasing rule" — was
  three paths, now consistent.
  **Caveat added post-landing (mutation-testing finding, 0.33.70 review):**
  for `String` specifically, `stage2/string_arc.py`'s later, independent
  ledger-based ARC-insertion pass was found to already provide equivalent
  protection at all three sites — reverting any of the three fixes and
  re-running under ASAN and Valgrind produced no observable difference.
  The fixes are kept (correct, and consistent with the rest of the file's
  ownership-transfer-boundary contract), but are not proven load-bearing
  for String today. Whether they're load-bearing for a STRUCT/VARIANT-typed
  projected capture (outside what was mutation-tested) is untested — see
  `work/callback-env-uaf-ref-args/REPORT-0.33.70-projected-capture-lowering.md`.

- **Found during 0.33.70 review, NOT fixed (out of this branch's scope,
  pre-existing, whole-root capture — not the projected `p.field` path this
  branch targets):** a FOURTH parallel lowering path with the same shape.
  `hir_to_mir.py`'s `HVar` visitor has its own inline REF/REF_MUT capture
  read (the exact-`HVar` capture-slot branch that does its own `LoadRef`
  when the captured key is a bare root, not a projection) that bypasses
  `_load_capture_from_env()` entirely and does not apply the
  `_ref_field_temps` alias mark. This is whole-root capture behavior
  (`captures(&p)`/implicit REF of a bare local), independent of the
  projected-capture (`p.field`) work in this branch. Fold into a future
  pass ONLY if the `_ref_field_temps`/`_copy_if_ref_alias` alias-helper
  claim ("must be called at every ownership-transfer boundary") is being
  made complete/audited end-to-end — not a standalone priority given the
  0.33.70 mutation-testing finding above (a later independent pass already
  covers String; the practical risk surface, if any, is STRUCT/VARIANT).

---

## Unify String/Arc ownership under one central transfer-policy classification

- **Improvement:** define one central transfer-policy classification
  (bitcopy / retain-copy / structural-copy / move-only) and make
  `String` always classify as **retain-copy + needs-drop**, as a fixed
  property of the type, independent of whether the stdlib's Copy-trait
  query has been installed (`TypeTable._copy_query`). Route `String`,
  `Arc<T>`, and future refcounted handles through the same
  `CopyValue`/`DropValue` ownership path where possible, instead of
  `String` being a `TypeKind.SCALAR` with scattered special cases across
  Copy-status, drop-policy, and lowering. This is broader than — and a
  superset trigger of — "Consolidate ownership-authoring" above: that
  entry is about the DROP/CLEANUP passes re-deriving ownership per exit
  path; this one is about the TYPE-CLASSIFICATION layer itself having
  two disagreeing authorities for the same question (see below), which
  no amount of cleanup-pass consolidation fixes on its own.

- **Why deferred (2026-07-05):** identified as a research question, not
  yet as a scoped patch — see the paired research doc requirement
  below. Do not fold into the 0.33.69 UAF fix (checker-only diagnostic,
  already scoped and landing) or the projected-capture follow-up's
  narrow alias-bookkeeping fixes (§4e in
  `research-copy-projected-captures.md`, which mirror an EXISTING
  lowering path rather than redesign the classification). Escalating
  either of those in-flight patches to this refactor would be exactly
  the kind of scope creep the "no partial moves" / minimal-fix
  discipline exists to prevent. This trigger is registered so the NEXT
  bug in this shape (see Triggers) escalates instead of yet another
  isolated patch — three is the pattern-recognition threshold this repo
  uses elsewhere (see "Consolidate borrow-checker walkers": "Three
  users is the minimum for a correctly-shaped abstraction").

- **Mandatory first step when triggered: a standalone research/design-impact
  doc, NOT implementation.** `String` is used everywhere; the current
  special-casing may be load-bearing for ergonomics (implicit copies at
  call sites, literals, concat, field projection, diagnostics/JSON
  payloads) that a strict Arc-like model would make more ceremonious
  (explicit `.clone()`/`move`/`&` at sites that currently just work).
  The research doc must cover, before any code changes are proposed:
  - **Current String semantics map:** every place String is treated as
    scalar/primitive/special-cased (checker, borrow-checker, lowering,
    codegen, runtime ABI helpers); where it's treated as Copy; where it
    needs drop/retain/`CopyValue`; where behavior differs between
    isolated-`TypeTable`/unit-test mode and full-stdlib/post-link mode
    (the `_copy_query` disagreement found in this research pass is one
    concrete instance — there may be others).
  - **UX dependency audit:** what ergonomic wins the current special
    treatment buys (implicit copies, argument passing, field
    projection, literals, concat, formatting, diagnostics, JSON/error
    payloads, package boundaries) and which of those a user would
    directly feel the loss of (more explicit `clone`/`move`/`copy`/`&`)
    if String became "just Arc-like."
  - **Compiler/runtime impact map:** every touched code path (type
    checker, borrow checker, MIR lowering, ownership ledger,
    `string_arc.py`, codegen, runtime ABI helpers); what ABI/runtime
    boundary changes are actually required to do this right — **ABI
    preservation is explicitly NOT a goal here** (2026-07-05 direction:
    "zero effort to preserve it"); map the impact so the ABI bump is
    sized correctly, don't contort the design to avoid one; what tests
    are likely to churn.
  - **Risk matrix:** what gets simpler, what gets stricter/less
    ergonomic, what might break downstream packages/consumers, and
    what should REMAIN intentionally special-cased even after the
    refactor (not everything special about String is necessarily a
    defect).
  Only after that doc exists and is reviewed does this trigger's "Scope
  when triggered" (implementation) become actionable. Research doc:
  the string-semantics audit commissioned 2026-07-05 (research-only,
  no implementation; produced in the string-ownership-refactor phase —
  its adopted conclusions are recorded across the 0.33.7x String
  Scope A entries in doc/history.md).

- **Triggers:**

  - Any bug where a NEW lowering/checker path independently
    mis-classifies `String` (or a future refcounted-Copy handle) as
    bitcopy, or forgets it needs drop/retain — i.e., a THIRD distinct
    instance of the "String ownership-authoring conformance matrix"
    trigger's recurring-defect class (that trigger already has two
    fired instances plus the projected-capture research's finding
    above; a further NEW one — not just another cell in the SAME
    subsystem's matrix, but a genuinely new code path — is this
    trigger's threshold, not that one's).
  - Discovery of a SECOND place (beyond `copy_status()`'s
    query-vs-structural-fallback split, found 2026-07-05) where
    isolated/unit-test type-table state and full-stdlib-loaded state
    disagree about a core type's ownership properties.
  - A future refcounted handle type (beyond `String`/`Arc<T>`) needing
    its own bespoke Copy/drop special-casing, rather than fitting the
    existing `Arc<T>`-style model — evidence that "String is special"
    isn't actually about String, but about a missing general category.

- **Scope when triggered — DECIDED (2026-07-05), research doc landed:**
  the audit (`research-string-semantics-audit.md`) split "unify
  String/Arc" into two genuinely different projects, Scope A
  (classification + centralization, ABI-neutral) and Scope B
  (runtime-representation reshape toward `ArcBox`, ABI bump). **Target
  Scope A only for the next compiler refactor:**
  1. Make `String`'s retain-copy + needs-drop classification structural
     and mode-independent (closes the `copy_status()`
     query-vs-structural-fallback split and its downstream propagation
     into `DropPolicy.is_cheap_copy`/`_should_copy_value`).
  2. Centralize the MIR helper paths that mark borrowed String/
     non-bitcopy aliases (`_ref_field_temps`) and apply `CopyValue` at
     ownership-transfer boundaries (`_copy_if_ref_alias`), so new
     lowering paths are forced through the shared helpers instead of
     each re-implementing the marking. **Both steps are required** —
     per the audit's corrected risk-matrix framing, classification alone
     fixes the policy split but does NOT by itself close the recurring
     missed-retain/missed-alias bug class; that requires this
     centralization step too.
  3. **Keep `String` Copy.** The improvement is unifying the mechanism,
     not removing Copy-ness — Copy-removal is a separately-justified,
     much larger decision this trigger does not mandate (see the
     audit's UX dependency audit: ~80 Copy-dependent read sites,
     300+ by-value String params).
  4. **Do NOT reshape the runtime representation (Scope B) as part of
     this work.**

  Scope B (`DriftString` toward an `ArcBox`-style representation) is an
  explicitly SEPARATE, later project, not bundled into Scope A or into
  the projected-capture follow-up. ABI preservation is not a constraint
  for Scope B when/if it's taken up (2026-07-05 direction: "zero effort
  to preserve it") — if the correct representation requires an ABI bump
  (likely — the audit enumerates the ABI-visible surface: the
  `DriftString` by-value calling convention, all `drift_string_*`
  exports, static-literal layout, `DRIFT_RT_ABI_VERSION`), take it. But
  Scope B's UX/source blast radius (every by-value String param, every
  Copy-dependent call site) is still a real scope constraint on THAT
  project, separate from ABI — keep the three tracks (0.33.69 UAF fix,
  projected-capture follow-up, Scope A) separate from Scope B even
  though they share root cause.

## FFI-handle lifecycle lint (`RawPtr` field without `Destructible`)

- **Improvement:** diagnostics for the "acquired via unsafe FFI, no
  matching `Destructible`" leak shape (drift-query `LmdbStorage`
  report, 2026-07-11: an `MDB_env*` stored as a plain `Uint` field on
  a Copy struct — every handle leaked on the success path).  Ladder:
  - **v1 lint:** struct declares a `RawPtr<T>` field and the struct
    has no `Destructible` impl → warn.  Cheap, no dataflow.  Only
    effective paired with the authoring convention "store acquired
    FFI handles as `RawPtr`, never as word ints" (effective-drift
    candidate) — the reported bug's `env: Uint` erased the last
    statically visible pointer trace, so no lint catches it without
    the convention.
  - **v2 annotation:** an acquires-resource marker on `extern "C"`
    declarations with checked flow-to-owner (value must reach a
    `Destructible`-implementing container or an explicit release).
    Catches the REAL reported dataflow — out-param write, buffer
    read-back, cast to `Uint` — which v1 and the reporter's own
    "field assigned from an FFI call result" phrasing both miss.
    Language-surface design slice, not a lint.

- **Why deferred (2026-07-11):** one report, already fixed correctly
  downstream by opting into `Destructible`; v1 without the convention
  has near-zero catch rate on real code (handles get stored as ints);
  v2 is new annotation surface + taint analysis through unsafe
  out-param writes and casts, with a real false-positive budget to
  design (many FFI-adjacent fields legitimately need no cleanup).
  Post-0.33.79 roadmap (C3 modeling, Arrays elision, B-repr(B5))
  outranks it.

- **Triggers:**
  - A second FFI-handle leak report from a downstream team → build v1
    + land the effective-drift convention note.
  - Any slice that adds extern-decl annotations for other reasons
    (ownership conventions A/B already documented in the FFI notes)
    → fold the v2 acquires-resource marker into its design.

- **Scope when triggered:** v1 ~1 day (checker walk over struct
  schemas + one diagnostic + pins); v2 is its own design doc first.

## Creation-site lifetime registration for all site-3-only owned locals

- **Improvement:** give every local that today relies on the
  Return-boundary destructible sweep (string_arc "site-3", relocated to
  the coordinated late-cleanup phase by B2+C) a first-class creation-site
  cleanup authority via `_register_drop_local` / `_materialize_owned_temp`,
  so it flows through the mainstream scope-exit `_emit_scope_cleanup_hook`
  → `cleanup_authoring` path instead of a Return sweep. Only when **ALL**
  current site-3-only lifetime classes are covered can the relocated
  site-3 sweep be **deleted outright**. Those classes (measured over the
  924-fixture corpus, 1,088 site-3 drops) are:
  1. **named error binders** (`catch e`) — deliberately unregistered
     (`# materialize-audit: allow consumed`), inline drops on only some
     exit edges + single-candidate rethrow hook;
  2. **anonymous `catch _` binders** — lower via `_canonical_local(None,
     "_")` → `__discard*` slots, also unregistered (these `__discard*`
     slots are catch binders — there is NO separate discard-temp
     owned-local population);
  3. **immediate-lambda MOVE/SHARE capture locals** whose environment
     field has no separate immediate-env drop authority
     (`hir_to_mir.py:5768-5777` loads the env field into a body local but
     deliberately skips `_register_drop_local` for MOVE/SHARE; the stack
     immediate env has no drop thunk, so site-3's Return drop IS the live
     release authority — measured instance:
     `closures_share_capture_arc_generic::__lambda_main_0_0::app` (`Arc`),
     the one STRUCT member of the 1,088).
  Catch-binder registration **alone is insufficient** — class 3 is a
  live second authority class that a binder-only change would leave
  uncovered, so site-3 could not be deleted by registering binders only.

- **Why deferred (2026-07-20):** the string-arc endgame adopted Option
  A (relocate the structural site-3 sweep behind the coordinated
  cleanup phase; B2+C R6 architectural review, 0.33.86-0.33.87 series —
  see the string_arc-endgame entries in doc/history.md).
  This creation-site model is the architecturally cleaner end-state
  (single cleanup authority) but **expands HIR→MIR catch/unwind
  ownership semantics** — historically this compiler's densest bug
  cluster (throw-unwind Destructible drop; typed-catch binder into
  ctor field double-free; match-arm `move` binder zeroed variant;
  VT-capture atexit UAF; cb_drop phantom destroy of moved-out
  captures). Today both known classes are *deliberately* left out of
  scope registration — error binders via `# materialize-audit: allow
  consumed` (inline drops on only some exit edges + single-candidate
  rethrow hook), immediate-lambda MOVE/SHARE capture locals via the
  explicit skip at `hir_to_mir.py:5776` — and site-3 is their
  Return-boundary safety net. Rewiring that during the endgame buys no
  endgame-required benefit and reopens the whole cluster, so it is a
  separate design-first, memcheck-gated project.

- **Triggers:** (note — the "second category of unregistered owned
  local" is NO LONGER a future trigger: immediate-lambda MOVE/SHARE
  capture locals (class 3 above) are already a live site-3-only
  authority class, folded into the improvement's scope. The remaining
  forcing shapes are:)
  - A **semantic leak / double-drop / ordering** bug in EITHER known
    class (catch binders, or immediate-lambda MOVE/SHARE capture locals)
    — i.e. site-3's relocated sweep mis-handles one of them.
  - A **third / new** category of unregistered owned local that requires
    the Return sweep (the safety-net pattern recurs for a local outside
    the two known classes).
  - A language feature that requires these lifetimes (catch binders and
    immediate-lambda capture locals) to participate in normal
    scope-order semantics (e.g. deterministic interleaving with other
    scope locals).

- **Scope when triggered:** design doc first. Must cover: retirement of
  the inline binder fall-through/rethrow cleanup and the single-
  candidate throw hook; a creation-site drop authority for immediate-
  lambda MOVE/SHARE capture locals (and the immediate/callback
  distinction — callback captures are already owned by the heap env +
  cb drop thunk, immediate captures are not); `MoveOut`-before-transfer
  proofs on every propagation edge so `verdict_at` returns
  skip/`MUST_NOT_DROP` on moved-out paths; destruction-order equivalence
  vs today's `sorted(destructible_locals)`; unwind + capture memcheck
  coverage across all catch/throw/`_`-binder and immediate-lambda
  MOVE/SHARE shapes; and removal of the relocated site-3 safety net only
  after ALL of the above are green.
