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
