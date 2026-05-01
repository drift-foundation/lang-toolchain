# Structural ConstShare synthesis — pipeline blocker (2026-05-01)

## Status

**Deferred.**  Structural ConstShare auto-derive for user
structs/variants requires a post-link / linked-world synthesis
phase, not a parser-local feature.  Phase 1 implementation
attempt was reverted; substrate is at "Path A" (direct
`ConstShare for ConstArc<T>` only, user-impl rejection, no
structural shortcut).  Forward-looking negative pin
`test_user_struct_does_not_prove_const_share_yet` in
`lang/tests/driver/test_const_share_substrate.py` documents the
current ceiling.

Working draft of the parser-local synthesizer, kept here as
design scratch only (NOT production tree):
`work/constshare-substrate/structural-synthesis-draft.py`.

## Blocker

Synthesis must qualify field types via the trait prover (no
syntactic name matching, no hardcoded primitive lists — see
`feedback_module_classification_by_contract.md` and the user
review on the v1 sub-plan).  The prover needs a `TraitWorld`
that contains the relevant `ConstShare` / `Copy` / `Frozen`
impls for the field types being checked.

**Per-module `TraitWorld` only sees impls declared in THAT
module.**  Cross-module impls (including stdlib's
`implement<T> ConstShare for core.ConstArc<T>` in
`std.core.const_arc`) are NOT visible to a consumer module's
local world — they only become visible later, when
`link_trait_worlds` produces a `LinkedWorld` at
`driftc.py:851` / `type_checker.py:2475`.

Concrete instrumentation when compiling a user file that
imports `core.ConstArc<String>` and tries to auto-derive
`Holder { handle: ConstArc<String> }`:

| Module | structs | ConstShare impls in local world |
|---|---|---|
| `std.core.const_arc` | 1 | 1 (the ConstArc impl) |
| `main` (user) | 1 (Holder) | **0** |

So a parser-local synthesizer running for `main` calls
`prove_is(ConstArc<String>, ConstShare)` against `main`'s world,
gets REFUTED, and decides `Holder` does not qualify.  No
synthesis fires; `holder.const_share()` fails to resolve a
method, which is the user-visible symptom.

## Rejected resolutions

- **Proof-only structural shortcut (Frozen-style).**  ConstShare
  has a method `fn const_share(self: &Self) nothrow -> Self`,
  so a proof without a method body is an incomplete trait state
  — generic code calling `t.const_share()` for `T: ConstShare`
  would type-check the bound but fail to resolve a method.
  Forbidden.

- **Pre-warming imported deps' impls into a module-local world
  at parser time.**  Creates a second partial linker that has
  to mirror `LinkedWorld.visible_world`'s rules and will drift.
  Not taken.

- **Two-pass per-module compilation.**  Doubles per-module
  work, breaks the existing single-pass model, still doesn't
  cleanly handle cross-package consumers.  Not taken.

- **Syntactic recognition of `ConstArc` / hardcoded
  primitive lists.**  Duplicates trait knowledge outside the
  prover, brittle to renames and to new stdlib types that
  satisfy Copy + Frozen.  Forbidden by the architectural
  principle in `feedback_module_classification_by_contract.md`.

## Preferred direction (its own milestone)

**Post-link / linked-world synthesis phase.**  This is broad
enough to be a separate milestone with its own sub-plan;
implementation is NOT in scope for the substrate track until
explicitly authorized.

Required deliverables for that future milestone's sub-plan:

1. **Pipeline placement** — synthesis runs AFTER
   `link_trait_worlds` produces the global `LinkedWorld`,
   BEFORE method resolution / type checking needs the
   synthesized impls.  Identify the exact insertion point in
   `driftc.py` and verify nothing earlier in the pipeline has
   already cached a "no impl found" answer that synthesis
   would invalidate.

2. **Semantic qualification using the linked world.**
   Prover queries (`prove_is`) against `LinkedWorld.global_world`
   or a `visible_world` projection — same machinery the
   require-enforcer uses.  Same contract as Phase 1's draft
   (ConstShare OR Copy+Frozen), just running against the right
   world.

3. **Generation of real HIR/MIR-visible functions.**
   Synthesized methods must have full HIR + MIR + LLVM lowering,
   not a checker-only side channel.  AST-injection-then-lower
   is one path; another is generating MIR directly with the
   per-T lowering shape that already serves Arc intrinsics.
   The sub-plan must say which.

4. **Package serialization of synthesized impls.**
   When a module that auto-derives ConstShare is published as
   `.dmp`, the synthesized impl + method body must serialize
   alongside hand-written ones.  No special-case branch in the
   package format — synthesized impls are real `ImplDef`s with
   real `FunctionDef` bodies.  Verify the existing serialization
   already covers this; if not, that's part of the milestone.

5. **Method resolution seeing those impls.**
   Type-check / call-resolution must find synthesized impls
   alongside hand-written ones.  No second visibility model.

6. **No duplicate trait visibility model.**
   The synthesis pass uses `LinkedWorld` directly; it does NOT
   build a parallel impl index, does NOT pre-warm any local
   world, does NOT introduce a second linker.

## Practical next steps for the substrate track

Two viable directions, both narrower than the synthesis
milestone:

- **Implicit duplication only for types with real ConstShare
  impls** — i.e., implicit `var b = a` synthesis where `T`
  proves ConstShare via a hand-written stdlib impl
  (currently just `ConstArc<T:Frozen>`).  Cleaner than
  structural synthesis because the method body already exists
  for those types.  No `LinkedWorld`-time synthesis required.
  Still its own milestone (HIR-level liveness analysis + HCall
  insertion); listed as "next phase" in the substrate plan.

- **Pause substrate implementation** and move to
  diagnostics-context using explicit `ConstArc.const_share()`
  where needed.  Substrate stays at the current Path A
  checkpoint until a concrete user-facing need motivates the
  next phase.

The user's instruction: keep Path A clean for now and pick
between these two when the substrate work is ready to resume.
