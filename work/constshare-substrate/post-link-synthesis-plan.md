# Post-link / linked-world ConstShare structural synthesis — plan

**Status:** design plan; implementation NOT started.  Approval
required before coding.

**Premises (per user direction 2026-05-01):**
  - Path A is the committed substrate checkpoint — explicit
    `ConstShare for ConstArc<T:Frozen>`, user-impl rejection,
    forward-looking negative pin for user structs.
  - ConstArc-only implicit duplication is NOT the next phase
    (would normalize a half-feature; user-defined ConstShare
    composition is the actual language need).
  - **Next phase: post-link structural ConstShare synthesis.**
  - **Phase after that: implicit duplication**, applied
    uniformly to ConstArc AND derived user structs/variants.

---

## 1. Pipeline placement

Synthesis runs as a **post-link phase**, between
`link_trait_worlds` (which produces the global `LinkedWorld`)
and the type-check pass that resolves method calls against
synthesized impls.

Existing pipeline (relevant subset, from `lang/driftc/driftc.py`):
```
per-module:  parse → lower_program → trait_world (per-module)
                                  └── HIR built
global:      link_trait_worlds(trait_worlds) → LinkedWorld
             type_check (uses LinkedWorld for proofs)
             HIR → MIR (uses resolved method calls)
             MIR → LLVM
```

Synthesis insertion point:
```
global:      link_trait_worlds → LinkedWorld
        ===> NEW: synthesize_const_share_impls(LinkedWorld, all_modules)
             type_check (sees synthesized impls)
             HIR → MIR (sees synthesized method bodies)
```

**Why this works:**
  - LinkedWorld has every module's stdlib + user impls — full
    visibility for prover queries.
  - Synthesis adds new `ImplDef`s to `LinkedWorld.global_world`
    AND adds new `FunctionDef` HIR to per-module `prog.implements`.
  - Subsequent type-check sees both pieces (impl registry +
    method bodies) and dispatches normally.
  - HIR→MIR walks the per-module `prog.implements` (now extended)
    and lowers synthesized methods like any other.

**Why this avoids the parser-local blocker:**
  - No parser-local prewarming.  No second visibility model.
  - Single source of truth: LinkedWorld.

---

## 2. Discovery — semantic qualification using the linked world

For each user struct/variant declaration across all modules,
ask the prover (`prove_is`) against the linked world:

  - `prove_is(field_ty, ConstShare).status is PROVED` — field
    qualifies via direct/recursive ConstShare proof.
  - OR `prove_is(field_ty, Copy).status is PROVED` AND
    `prove_is(field_ty, Frozen).status is PROVED` — field
    qualifies via Copy+Frozen.

If every owned field qualifies, the struct/variant auto-derives
ConstShare.

**Fixed-point iteration:** an auto-derived struct's synthesized
impl makes IT qualify as a field of OTHER structs.  Iterate
discovery+synthesis until no new types qualify.

**Conservative:** `UNKNOWN` does not promote to PROVED.  Cycle
handling uses the prover's existing `_in_progress` set
(returns UNKNOWN on recursion → blocks the structural
promotion, matching Frozen's structural shortcut precedent).

**Generic structs (typevar fields):** handled iff the struct's
declared `require` clause already provides enough — i.e.,
`pub struct Box<T> require T is ConstShare { value: T }`
auto-derives with `require T is ConstShare` propagated to the
synthesized impl.  No implicit constraint-strengthening; user
opts in explicitly via the existing require clause.

**Cross-module composition:** a field of type `m.Inner` where
`Inner` lives in module `m` qualifies iff `Inner` proves
`ConstShare` via the LinkedWorld — including when `Inner`'s
proof comes from `m`'s OWN synthesized impl.  Discovery's
fixed-point handles this naturally because all modules'
candidates are processed against the same world.

---

## 3. Generation — real lowering-visible method bodies

Synthesized output per qualifying type:

  - A `parser_ast.ImplementDef` registered with
    `LinkedWorld.global_world.impls` (and the three
    `impls_by_*` indices, mirroring
    `world.py::build_trait_world` lines 904-919).
  - A `parser_ast.FunctionDef` for `const_share` appended to
    the type's owning module's `prog.implements`.
  - The FunctionDef's body is parser AST that goes through the
    normal HIR/MIR lowering pipeline:
      - **struct**: `return Self(f1=self.f1.const_share(),
        f2=self.f2, ...)` — `.const_share()` for ConstShare-path
        fields, direct read for Copy+Frozen-path fields.
      - **variant**: `match self { Arm0(...) => return
        Arm0(...), Arm1 => return Arm1, ... }` per-arm
        reconstruction.

**Per-field path tracking:** discovery records which
qualification path each field took.  Synthesis uses that record
to choose `.const_share()` (path 1) vs direct read (path 2) per
field.

**Phase-1 narrowing (if needed):**
  - Structs only — no variants.  If variant match-arm AST
    construction proves invasive, defer to Phase 2 with a clear
    negative test for variants.
  - Concrete fields only — generic struct/variant support is
    Phase 3.

These phase boundaries must be EXPLICIT in the milestone scope
and matched by negative tests.  No silent partial coverage.

---

## 4. Method resolution sees synthesized impls

Type-check / call-resolution walks `LinkedWorld.global_world`'s
impl indices.  Synthesized impls are registered there during
the post-link synthesis pass, BEFORE type-check runs.  No
visibility surgery needed.

`make_call_ctx` already receives `global_trait_world` and
`visible_trait_world` — both come from `LinkedWorld`.
Synthesized impls flow through naturally.

**Verification:** after synthesis, dump every key in
`global_world.impls_by_trait_target[(ConstShare-key, _)]` and
confirm both hand-written (ConstArc) and synthesized (user
structs) entries are present.  Add a test that calls
`holder.const_share()` and verifies the resolved method's
`fn_id` is the synthesized one.

---

## 5. Package serialization of synthesized impls

This is the open question that requires the most careful
inspection.  Two possible answers:

**(a) Synthesized impls serialize like hand-written ones.**
  - Pro: package consumers see the impl directly; no
    consumer-side work needed.
  - Pro: consumers built against an old compiler that doesn't
    know about synthesis still get the impl (it's in the .dmp).
  - Con: the synthesized FunctionDef body is real HIR/MIR that
    goes into the package — increases package size by some
    amount per auto-derived struct.

**(b) Synthesized impls are regenerated per-build.**
  - The .dmp records the type's auto-derive eligibility flag,
    not the body.
  - Each consumer's build re-runs the synthesis pass on the
    consumed type.
  - Pro: no extra package payload.
  - Con: requires the consumer's compiler to know about
    synthesis (forces ABI bump or version-gate).
  - Con: consumer must have the type's full structural info
    (field types + their ConstShare-ness) to regenerate the
    body.  Structural info is already in .dmp; ConstShare-ness
    is computable via prover queries against the consumer's
    LinkedWorld.

**Recommended**: **(a)**.  Reasons:
  - It's the same code path as hand-written impls; less new
    serialization surface.
  - Package size cost is bounded (auto-derived bodies are
    small, structurally regular).
  - Consumer-side regeneration would re-introduce the kind of
    parallel mechanism the user has been ruling out across this
    track.

**Verification needed (sub-plan deliverable):** trace the
existing `.dmp` serialization (`lang/driftc/packages/`) to
confirm it doesn't filter out synthesized FunctionDef bodies.
If it does, the milestone includes the patch to either
(i) un-filter or (ii) tag synthesized impls so they survive.

---

## 6. No duplicate trait visibility model

The pass uses `LinkedWorld.global_world` directly.  It does NOT:
  - build a parallel impl index,
  - pre-warm any per-module local world,
  - introduce a second linker.

If a sub-plan iteration ever proposes one of those, that's a
signal the design has drifted; reject and re-converge on the
single LinkedWorld.

---

## 7. Sub-plan deliverables (when authorized)

Before any code:

  1. **Pipeline insertion point — exact line** in `driftc.py`
     where synthesis runs.  Confirm: link → synthesize →
     type_check ordering, no caches invalidated, no
     duplicate-load on re-runs.
  2. **`LinkedWorld` mutability rule** — adding to
     `global_world.impls` after `link_trait_worlds` finishes.
     Verify this is sound (no immutable-marker on LinkedWorld;
     no other consumer caches the impls between link and
     synthesis).
  3. **Discovery algorithm** — fixed-point iteration with
     concrete termination guarantee.  Cycle handling delegated
     to the prover's existing `_in_progress` set.
  4. **Generic require-clause propagation rule** — exact spec
     for which generic requires propagate from struct to
     synthesized impl.  Document edge cases (multiple typevar
     constraints, conditional impls).
  5. **Method-body construction** — parser AST shapes for
     struct and variant cases.  Concrete examples + edge cases
     (empty struct, single-field, nested user types,
     cyclic struct types).
  6. **Package serialization audit** — trace existing .dmp
     serialization through `lang/driftc/packages/`; identify
     whether synthesized impls flow naturally; if not, list
     concrete patch sites.
  7. **Cross-module / package-mode test plan** —  enumerate
     the test scenarios that must be in scope from day 1:
     - same-module composition;
     - cross-module same-build composition;
     - cross-package consumer (struct in published .dmp,
       consumer auto-derives based on its own ConstShare
       requirements);
     - generic struct with explicit require;
     - negative cases (Mutex / Arc / Array / HashMap / refs
       block);
     - direct user impl still rejected;
     - forward pin for variants / generics if those are
       Phase 2 / Phase 3.
  8. **Phasing** — explicit landing order.  Recommend:
       - Phase 1: structs, concrete fields, same-module.
       - Phase 2: structs, cross-module + cross-package.
       - Phase 3: generics with explicit require.
       - Phase 4: variants.
     Each phase commit-able and revertable; each has its own
     tests + memcheck before landing.
  9. **Stop conditions** for each phase, mirroring the
     stop-and-report discipline used so far.
  10. **Verification gates** for each phase:
        - substrate driver tests + memcheck (existing 49 +
          new milestone tests),
        - cross-module/package tests,
        - full driver+stage+checker+packages suite,
        - full memcheck suite.

---

## 8. Future phase (after structural synthesis lands)

**Implicit duplication, uniformly across ConstArc and derived
user types.**

Once user structs/variants can prove ConstShare AND have
synthesized method bodies, the implicit-duplication trigger
described in the prior milestone — `val/var b = a` and
owned-arg pass auto-wrapping with `ConstShare::const_share()`
— becomes the next phase.  At that point:

  - The trigger is uniform: any non-Copy type that proves
    ConstShare.
  - Both ConstArc and user-derived types are supported.
  - No risk of normalizing a half-feature.

Implementation hooks (from the discarded ConstArc-only attempt,
preserved for reuse):
  - `_try_const_share_rescue(expr, ty_id)` predicate using
    `prove_is` + `copy_status` guard.
  - `_wrap_const_share(expr)` synthesizing the HCall on
    `HQualifiedMember(ConstShare-trait, "const_share")` with
    `HBorrow(<local>)` — mirror of `share x` synthesis.
  - HVar-resolution rescue at the two `_require_copy_value`
    call sites.
  - HLet wrap site after `inferred_ty = type_expr(...)`.
  - Owned-arg pass wrap — requires plumbing through
    `make_call_ctx` and `_coerce_args_for_params`.  This is
    the "broad surgery" piece flagged in the previous attempt;
    sub-plan it carefully when that phase lands.

---

## 9. Decision points awaiting user

  - **Approval to proceed with sub-plan deliverables** (items
    1-10 in §7).  Sub-plan is design-only, no implementation.
  - **Phasing preference** (4 phases as proposed, or
    consolidated)?
  - **Package serialization preference** (a vs b in §5)?

Holding at Path A until called.
