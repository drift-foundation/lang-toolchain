# By-reference variant match (`match &Variant`, `match &mut Variant`)

This document specifies the user-facing semantics of by-reference variant
matching in Drift.  Shared (`match &Variant`) was certified at 0.31.33;
mutable (`match &mut Variant`) was certified at 0.31.35.

The two forms share scrutinee-type and binder-typing surface in the
checker but differ in arm-binder type, escape rules, and the
diagnostics they produce.  See the *`match &mut Variant`* section
below for the mutable form's contract.

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

   **Match ergonomics for `Copy` payload fields.** When the
   payload field type is `Copy` (`Int`, `Uint`, `Uint64`, `Float`,
   `Bool`, `Byte`, `Char`, etc.) AND the scrutinee is a *shared*
   by-ref match (`match &v`, not `match &mut v`), the binder is
   marked as a *Copy arm binder*. At three syntactic positions
   inside the arm body, a bare `HVar` reference to a Copy arm
   binder is rewritten to `HUnary(DEREF, HVar)` during
   type-check, so the lowered IR contains a `LoadRef` and the
   loaded value participates in the operation:

   - Binary-operator operands — `n + 1`, `n > 0`.
   - Ternary condition — `b ? a : c` (when `b: &Bool`).
   - Match arm result expression — `match &s { Active(n) => n }`
     producing a value-typed result (e.g. `val k: Int = match …`).

   For all other contexts (passing the binder to `fn(&T)`, taking
   its address, nested `match` over `&Variant`, etc.) the binder's
   type remains `&FieldType` per the spec. The rewrite is keyed
   by binding-id (set membership), not by type pattern, so an
   ordinary `&Int` value from any other source continues to
   behave as a borrow — no broad `Ref<Copy> → Copy` coercion.

   Stdlib's existing pattern of explicitly dereferencing a Copy
   binder with `*x` continues to work — the binder type is
   unchanged, so `*x` derefs the borrow as before.

   Non-Copy payload fields (struct, `String`, `Array<T>`, etc.)
   preserve the strict `&T` binder typing — moving / copying /
   cloning semantics aren't elided.

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

## `match &mut Variant` — mutable by-reference match (0.31.35)

The mutable form is certified alongside the shared form. The
contract is **"no unsafe escape,"** not "no escape" — direct
escapes are rejected at the escape site, while call-mediated
escapes are handled by retained owner-borrow tracking. Details
below.

1. **Non-consuming.** `match &mut r { ... }` does not move out of
   `r`. After the match expression scope ends, when no arm binder
   has escaped (see 4 below), `r` remains usable for any
   operation: subsequent `match &r`, `match &mut r`, `move r`,
   reassignment `r = ...`, return.

2. **Mutable arm binders.** Arm payload binders are `&mut FieldType`.
   Writes through the binder land in the original variant payload.
   Calling methods that take `&mut Self` works.

3. **No payload move-out.** `move binder` / `move binder.field` is
   rejected — `&mut` gives mutation, not ownership extraction.

4. **Escape rules — the actual v1 contract.**

   The certified invariant is **no *unsafe* escape**: direct
   escapes are rejected at the escape site, and call-mediated
   escapes retain the scrutinee borrow conservatively (later
   owner mutation / move / reborrow may reject by the standard
   loan-conflict check).  The retention is conservative —
   triggered by *any* call passing the binder, regardless of
   whether the callee actually stored it — so well-formed
   programs that pass an arm binder to a non-storing helper
   may still reject a later scrutinee operation as a
   false-positive.  This is the v1 trade-off; the alternative
   (call-site rejection) would break the iterator pattern.

   - **Direct escape is rejected at the escape site** with the
     diagnostic:

         &mut match arm binder must not escape the match arm;
         this would extend exclusive access to the scrutinee

     Direct shapes covered:

     - `outer = x`
     - `val outer: &mut T = match &mut r { Ok(x) => x, ... }`
     - direct container wrapping in the arm:
       `leaked = Optional::Some(x)`

   - **Call-mediated use is allowed.** Passing the arm binder
     to a function or method does not error at the call site:

     - `x.next()`
     - `helper(x)`
     - `store(&mut leaked, x)`

     The iterator pattern `match self { Ctor(it) => it.next()
     }` and similar load-bearing stdlib shapes work
     unchanged.

   - **When a call passes the arm binder, the scrutinee loan
     is retained conservatively.** The borrow-checker keeps
     the `&mut r` loan live across the match expression
     regardless of whether the callee actually stored the
     reference; any subsequent owner mutation, move, or
     reborrow may reject by the standard loan-conflict check.
     This is what closes the UAF path for call-mediated
     escape — the unsafe step is rejected at the conflict
     site, not at the call site, and the diagnostic notes
     point back to the still-live borrow.  The retention is
     conservative-and-coarse: passing a binder to a
     non-storing helper followed by a scrutinee operation
     may reject as a false-positive; the workaround is to
     scope the helper call inside a block whose arm result
     does not need a follow-up scrutinee borrow, or to
     restructure to avoid the helper call entirely.
     No call-site diagnostic is required.

5. **Borrow lifetime ends at the match expression when no arm
   binder escapes.** The same G1 fix that landed for shared
   applies to `&mut`. The function-scoped overshoot that
   previously rejected `match &mut r; match &r` is gone for both
   forms in the no-escape case.

6. **Stdlib iterator pattern stays green.** `fn next(self: &mut V)
   { match self { ... } }` works because the arm binder is used
   only within the call expression in the arm body; the function
   typically returns from the match, so the conservative
   loan-retention has no later operation to conflict with.

7. **Primitive payloads.** Explicit `*n` deref / write
   (`*n = *n + 1`) is the documented surface. Bare-binder
   ergonomics like `n + 1` / `n = ...` for `&mut` payloads are
   intentionally NOT extended in this release — the shared-form
   G3 ergonomics are scoped to shared binders only. A separate
   ergonomics decision applies if needed.

## What is not yet supported

- **Call-site `&mut` escape diagnostics.** Call-mediated escape
  keeps the loan live and rejects the unsafe downstream
  operation; it does not emit a diagnostic at the call itself.
  Adding call-site rejection would break the iterator pattern
  (which is also a call passing the arm binder) and is
  explicitly out of scope.

- **Bare-binder ergonomics for `&mut` primitive payloads.**
  Use explicit `*n` / `*n = ...` for `&mut` payloads. The
  shared-form G3 autoderef is scoped to shared binders.

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

- 0.31.35 — certify `match &mut Variant`.
  - G1: scrutinee borrow lifetime now ends at the match
    expression (for both `&` and `&mut` forms) when no arm
    binder escapes.  Fixes the function-scoped overshoot that
    previously blocked `match &mut r; match &r`, repeated `&mut`
    matches, use/move/return after `&mut` match, and primitive
    write+readback.
  - G2: arm-binder escape detection.  If a shared (`&`) arm
    binder escapes via assignment / let-init to a target outside
    the arm scope, the existing F2 owner-extends-lifetime
    contract kicks in (loan stays live; subsequent owner
    mutation rejected).  If a `&mut` arm binder escapes, it's
    rejected directly with a clear diagnostic.
  - Implementation: `lang/driftc/borrow_checker_pass.py`
    `_visit_expr` HMatchExpr branch — pre/post snapshot of
    scrutinee loans, per-arm binder-id collection, escape
    detection via `_arm_binder_escapes`, then conditional loan
    drop / direct rejection based on (mut/shared, escape/no-escape).

- 0.31.34 — primitive-payload binder under shared by-ref match.
  - G3: HIR rewrite that inserts `HUnary(DEREF, HVar)` at HBinary
    operands, ternary conditions, and match arm-result expressions
    when the operand is a bare `HVar` referring to a Copy
    match-arm binder (`Ref<Copy>`).  HIR→MIR's existing DEREF
    lowering emits a `LoadRef`, so the load is materialized in
    the lowered IR.  Closes the gap that 0.31.33's cert
    overlooked — primitive-payload variants like
    `variant V { Active(n: Int) }` now allow `match &v { Active(n)
    => n + 1 }`, `n > 0`, `val k: Int = match &v { Active(n) => n }`
    end-to-end (compile + execute), not just type-check.
  - The rewrite is keyed by binding-id set membership
    (`copy_arm_binder_ids`), populated only for shared-by-ref
    arm binders whose payload field type is `Copy`.  Ordinary
    `&Int` values from any other source remain strict references
    — no broad `Ref<Copy> → Copy` coercion.
  - G4: routed copy/arithmetic diagnostics through
    `user_facing_binding_name` so the internal
    `__match_binder_<n>_<src>` form does not leak in those paths.

- 0.31.33 — shared `match &Variant` certified.
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
