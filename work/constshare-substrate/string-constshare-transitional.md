# String + ConstShare — transitional disposition (2026-05-01)

## Architectural intent

`String` is intended to become one of the canonical
`ConstShare` examples, alongside `core.ConstArc<T:Frozen>`.  A
`String` value carries refcounted heap-backed data; its
duplication semantics (refcount-bump on clone, release on drop)
match the `ConstShare` contract directly.  In the long run the
`std.core.shareable.Share` family for ownership-with-refcount
should converge on:

  - `Share`             — produces another aliased owner of a
                         possibly-mutating shared resource
                         (`Arc<T>` is the first/only adopter).
  - `ConstShare`        — produces another owner of an immutable
                         shared value (`ConstArc<T:Frozen>`,
                         eventually `String` and the read-only
                         containers like `ReadOnlyMap`).
  - `Copy`              — bitwise duplicable, no destructor.

## Current state (transitional)

`String` is currently classified `Copy + Frozen`.  The `Copy`
impl on `String` (declared in `stdlib/std/core/core.drift`) does
NOT mean bitwise copy at the runtime level — Drift's
`stage2/string_arc.py` rewrites every `String` `LoadLocal` /
`StoreRef` / etc. into the refcount-stake machinery
(`drift_string_retain` / `drift_string_release`).  So `String`'s
behavior is already refcount-bump-on-Copy under the hood; the
`Copy` impl marks it as duplicable at the type-system level
without invoking the trait machinery for refcount.

This is fine for now and lets the substrate work proceed
without touching `string_arc`.  The cost: `String` does NOT
appear in code that explicitly uses `ConstShare` capability
(`require T is ConstShare`, `core.const_arc<type T>`, etc.).
Code that wants a refcounted-immutable `String` field today
wraps it in `core.ConstArc<String>`.

## Substrate-track interaction with String

  - **First real `ConstShare` impl** (this track, current
    phase):  `core.ConstArc<T:Frozen>` only.  String is NOT
    given a direct `implement ConstShare for String { ... }`
    impl yet.

  - **Structural ConstShare proof** (next post-link synthesis
    milestone, deferred — see
    `structural-synthesis-blocker.md`):  A `String` field in a
    user struct WILL qualify under the v1 composition rule via
    the `Copy + Frozen` clause, NOT via a direct `ConstShare`
    impl.  This means a struct like
    `Holder { name: String, tag: Int }` auto-derives
    `ConstShare` (when synthesis lands), and the synthesized
    body reads `self.name` directly through Drift's existing
    borrowed-Copy auto-copy path — exactly the same `String`
    refcount-bump machinery `string_arc` already implements.

  - **Implicit duplication** (this phase): same story —
    implicit `var b = a` for a `String` `a` continues to work
    via the existing `Copy` path; `string_arc`'s rewrite handles
    the refcount.  No `ConstShare` involvement.

## Why defer String's direct ConstShare impl

Adding `implement ConstShare for String` now would create two
parallel duplication paths:

  - the existing `string_arc` path (Copy → `LoadLocal` rewrite
    → `drift_string_retain`); and
  - a new `ConstShare::const_share` call dispatched through
    trait machinery, which would either need to call back into
    `drift_string_retain` (a wrapper) or duplicate the refcount
    contract.

That's competing trait/lowering paths for the same runtime
operation, and it would bless `string_arc`'s ad-hoc lowering as
part of `ConstShare` before any deliberate normalization.  The
right time to add `String: ConstShare` is when `string_arc` is
itself rewritten to go through the same `ConstShare` /
`ConstArc` machinery used by user types — at that point String
becomes a regular `ConstShare` adopter rather than a special
case.

## Concrete plan

  1. Substrate work continues with `ConstArc<T:Frozen>` as the
     sole direct `ConstShare` implementer.
  2. `String` stays `Copy + Frozen`; structural ConstShare
     synthesis (when it lands) treats `String` fields via the
     `Copy + Frozen` clause.
  3. Document each substrate phase's String posture as
     transitional (see headers in
     `phase1a-dispositions.md` and similar).
  4. **String normalization track** (separate, NOT part of the
     substrate track) is the milestone that:
       - rewrites `string_arc` to dispatch through
         `ConstShare::const_share` for `String`,
       - adds an explicit `implement ConstShare for String`
         backed by that machinery,
       - drops the `Copy` impl on `String` (or keeps it as a
         transitional alias — open question for that track),
       - confirms no other stdlib type was secretly relying on
         `String: Copy` for bitwise-copy semantics.
  5. Until that track lands, this document and the substrate
     phase docs cite "String is Copy + Frozen, transitional" so
     no future reader thinks the current shape is the final
     state.

## What this means for the current milestone (implicit duplication)

  - Implicit duplication runs ONLY for types that have a real
    `ConstShare` impl — currently just `core.ConstArc<T>`.
  - `String` continues through the existing `Copy` path; no
    `const_share()` synthesis on `String` reads.
  - Tests and memcheck do NOT exercise `const_share` on
    `String` directly — only via `ConstArc<String>` payloads.
  - Documentation in the milestone's tests / disposition cites
    this transitional posture explicitly.

If the implicit-duplication implementation ends up needing to
SPECIAL-CASE `String` (e.g. avoid wrapping `String` reads in
`const_share()` because String doesn't have the impl), STOP and
report — special-casing would suggest the implementation is
mis-scoped.  The clean rule is "wrap iff the type proves
`ConstShare` via direct impl"; `String` doesn't qualify, so the
existing Copy path catches it without any String-specific code.
