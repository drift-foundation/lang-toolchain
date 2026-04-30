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

## Defensive collision check for DMIR registry

- **Improvement:** add a runtime assertion in
  `build_dataclass_registry` that fires if two dataclasses
  with the same `__name__` but differing field sets are
  registered.  Turns the silent-data-loss failure mode that
  motivated 0.31.28 into a hard error with a clear upgrade
  pointer to the qualified-discriminator entry above.

- **Why deferred (2026-04-30):** running test gate at the
  time the trigger surfaced; deferred to be its own minimal
  patch (~15 lines + unit test) rather than co-mixed with
  active in-flight work.  Strictly internal tightening, no
  behavior change, ABI-neutral.

- **Triggers:**

  - The 0.31.35 test gate clears (signal: no other in-flight
    structural work conflicts).  This is a known scheduled
    next step, not a hypothetical condition.

- **Scope when triggered:** ~30 minutes — 15 lines of
  detection logic in `build_dataclass_registry`, one unit
  test that constructs a colliding registry and asserts the
  expected error.

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
