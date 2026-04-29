# `match &Variant` — shared by-reference variant match

This document specifies the user-facing semantics of *shared* by-reference
variant matching in Drift, certified at 0.31.33.

`match &mut Variant` is a separate certification effort and is not yet
covered here. The two forms are not interchangeable at the user level,
even though they share scrutinee-type and binder-typing surface in the
checker.

## Form

```drift
match &<scrutinee> { <pattern_arms> }
```

where `<scrutinee>` is any expression whose type is `&Variant<T...>`.
The reference can arrive at the scrutinee via any of:

- a literal borrow expression (`match &result { ... }`),
- a binding of `&Variant` type (`val r: &core.Result<...> = &v; match r { ... }`),
- an arm binder from an outer `match &outer` (nested match-by-ref, where
  the outer arm bound a payload field that is itself a variant),
- a function return value of `&Variant` type.

The form is uniform: the scrutinee's *type* is what selects the by-ref
match path, not the syntactic shape of the scrutinee expression.

## Semantics

1. **Non-consuming.** The match does not move out of the scrutinee.
   The original variant value remains usable after the match block —
   you can read fields through it, pass it to functions, match it
   again (shared or otherwise), and return it.

2. **Shared arm binders.** Each pattern binder in an arm's payload
   pattern has type `&FieldType`. Reading fields and calling
   `&self`-receiver methods through a binder works as on any
   `&T`-typed value.

3. **No binder ownership.** Binders do not own or drop their payload
   fields. Drop responsibility stays with the original scrutinee. A
   non-Copy payload (e.g. a `String` field) is released exactly once
   when the original variant goes out of scope, never via a binder.

4. **Move-out is rejected.** Attempting `move binder` (consume the
   binder as if owned) or `move binder.field` (move out a payload
   field) is rejected. The existing borrow / move rules already
   enforce this — by-ref match adds no special leniency.

5. **Field-write through a shared binder is rejected.** Writing
   `binder.field = ...` through a `&FieldType` binder is rejected
   at type-check with a clean diagnostic
   (`cannot assign through a shared reference (&T); the place is
   read-only — use a `&mut` reference to mutate`). Pre-0.31.33 this
   was caught only at MIR lowering, with an internal-form
   "(checker bug)" message.

6. **Borrow escape from arms is bounded by owner-borrow tracking.**
   Assigning a binder into an outer container (e.g. `outer_opt =
   Optional::Some(binder)`) extends the live borrow on the original
   scrutinee for as long as the escaped pointer is reachable.
   Subsequent attempts to mutate, move, or reassign the scrutinee
   through any path while the borrow is live are rejected by the
   borrow checker (`cannot take mutable borrow while borrow active
   on '<scrutinee>'`).

   The shape `match &x { Ok(p) => leaked = Some(p) }; <use leaked>;`
   is therefore safe by construction — it compiles only if `x`
   outlives the use of `leaked`.

## Diagnostic hygiene (A.2 invariant)

Any user diagnostic emitted along the by-ref match path uses the
*source* spelling of arm binders, never the internal
`__match_binder_<n>_<src>` form synthesized by HIR lowering. This
holds across rejection paths (e.g. scrutinee-type rejection, body
errors) and is pinned by
`lang/tests/driver/test_match_binder_diagnostic_hygiene.py`.

## What is not yet supported

- **`match &mut Variant`** — certification deferred. The shared
  form's three load-bearing fixes (F1, F2, F3) do not by themselves
  cover the mutable form's borrow-checker, lowering, or memcheck
  surface. A separate patch will either certify `match &mut` with
  full coverage or reject it cleanly with a documented diagnostic.

- **Type-driven exhaustiveness for `match &x` over open variants** —
  exhaustiveness checking treats by-ref match identically to by-value
  match in this release. No special "borrow into open variant"
  ergonomics are committed.

## Tests pinning the certified surface

- `lang/tests/driver/test_match_by_ref_variant.py` — 12 tests across
  F1 (mutability), F2 (escape / no-UAF), F3 (scrutinee form
  generalization), positive product/app shape, and negative
  move/borrow rejections. A.2 hygiene re-pinned in the by-ref
  territory.
- `lang/tests/memcheck/test_match_by_ref_variant_drop.py` — drop-
  bearing payload (heap-seeded `String` field) under valgrind:
  exactly one release per allocation, no leak, no UAF.

## History

- 0.31.33 (this release) — shared `match &Variant` certified.
  - F1: type-checker rejects field-write through shared `&` binder
    before MIR.
  - F2: arm-binder escape pinned safe via owner-borrow lifetime
    extension (existing behavior; tests added).
  - F3: scrutinee variant check now strips `Ref<Variant>` for any
    `&Variant`-typed value, not only literal `&expr` scrutinees.
    Nested / factored / fn-returning references compile.
- 0.31.32 — A.2 binder-leak hygiene (`__match_binder_<n>_<src>`
  stripped from user diagnostics).
- 0.31.31 — pretty-printer renders nominal type-args via the
  instance map.
