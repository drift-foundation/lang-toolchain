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
    `docs/match_by_ref_variant.md`). Adding taint flow to current
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
