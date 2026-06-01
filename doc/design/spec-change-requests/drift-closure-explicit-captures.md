# Explicit Capture Mode for Closures (captures(...))

## Status

Spec change request for MVP: introduce an explicit capture mode for closures and
lock down semantics around capture timing, escape rules, and ownership effects.

## Goals

- Provide a user-visible, unambiguous way to control closure captures.
- Avoid trait-driven implicit capture switching.
- Keep implicit capture mode unchanged for MVP.
- Make ownership and borrow effects explicit and deterministic.

## Non-Goals (MVP)

- No mixing implicit captures with explicit captures in the same closure.
- No projections in the captures list (no `x.field`, no indexing, no deref).
- No trait-driven capture inference (traits only gate `copy` later).

## Summary of Change

Introduce a `captures(...)` clause for closures. This defines an explicit capture
mode where:

- Only listed roots may be used as free variables in the body.
- Capture items are evaluated once at closure creation time.
- Borrowed captures are non-escaping in MVP.

## Syntax

Implicit capture mode (existing):

```drift
val f = |n: Int| => {
	n * scale
}
```

Explicit capture mode (new):

```drift
val f = |n: Int| captures (copy i, &mut x) => {
	*x + i + n
}
```

## Semantics (Explicit Capture Mode)

### 1) Completeness (no implicit captures)

Let `F` be the set of free root identifiers used in the body (excluding params
and locals). Let `C` be the set of root identifiers listed in `captures(...)`.

`F` includes value identifiers that resolve to outer bindings (not params or
locals); it excludes type names.

Requirement: `F` must be a subset of `C`. Otherwise, error:

> `y` is used in closure body but is not listed in `captures(...)`.

### 2) Name binding and collisions

- Names in `captures(...)` resolve in the enclosing value scope and must be root
  identifiers (not projections).
- Captured names are bound in the closure body and shadow the outer binding.
- If a closure parameter or local reuses a captured name, it is a hard error.

### 3) Captured name types

The capture item determines the type bound inside the body:

- `x` or `&x` binds `x: &T`
- `&mut x` binds `x: &mut T`
- `copy x` or `move x` binds `x: T`

There is no implicit auto-deref rewrite of these names.

### 4) Capture timing and order

- Capture initializers run exactly once, at closure creation time.
- Evaluation order is left-to-right as written in `captures(...)`.
- Capture items are type-checked and borrow-checked left-to-right as if they are
  a sequence of statements executed at closure creation time.
- The closure body executes at call time and never re-evaluates capture items.

### 5) Root-only capture items (MVP)

Allowed items:

- `x` (shorthand for `&x`)
- `&x`
- `&mut x`
- `copy x`
- `move x`

Rejected (MVP):

- Any projection: `x.field`, `arr[i]`, `*p`, etc.

Required diagnostic:

> explicit captures support root identifiers only in MVP (no field/index/deref captures).

### 6) Body projections

In explicit mode, the "must be listed" rule applies to root identifiers only.
The body may use projections (`x.field`, `x[i]`, `*x`) as long as the root `x`
is captured, subject to existing place model limits.

### 7) Shorthand rule

A bare identifier `x` in `captures(...)` is equivalent to `&x`.

### 8) Mutation requires `&mut`

In explicit mode, if `x` is captured as `&T` (via `x` or `&x`), any body
operation that requires mutation through `x` is an error; capture `&mut x`
instead.

Diagnostic shape:

> `x` is captured as `&T`; capture `&mut x` to mutate.

### 9) Capture list duplicates

Each root may appear at most once in `captures(...)`. Duplicates are a hard
error (for example, `captures(x, &mut x)` or `captures(x, x)`).

### 10) Type and trait gating (MVP)

There is no trait-backed `Copy` yet. For MVP:

- `copy x` requires the type table to mark `x`'s type as copyable.
- Error message: `cannot copy x: type <T> is not copyable`.

### 11) Borrowed captures are non-escaping (MVP)

Borrowed captures (`&x`, `&mut x`) are non-escaping:

- A closure is escaping if it is returned, stored in a longer-lived location,
  or passed to an unknown call site that may retain it.
- In MVP, treat a call as "unknown" unless the compiler can prove the callee
  does not retain the closure value.
- In MVP, escaping closures must not have borrowed captures.
- Closures with only `copy`/`move` captures may escape.

### 12) Borrow checker model: closure-owned loans

When explicit captures include borrows:

- The closure owns the loan.
- Loan lifetime is from closure creation to closure drop.
- Moving the closure transfers loan ownership.

### 13) Move capture effects

`captures(move x)` moves `x` at closure creation time:

- `x` becomes invalid immediately after the closure literal is evaluated.
- Any later use of `x` is a use-after-move error.

## Lowering Model (Required)

Closure creation:

1. Evaluate each capture item left-to-right.
2. Store values into closure object fields.
3. Produce the closure value.

Closure invocation:

1. Load stored fields.
2. Execute the body.
3. Never re-evaluate capture items.

## Diagnostics (Minimum Bar)

- Missing capture in explicit mode.
- Root-only enforcement for `captures(...)`.
- Name collision with params/locals.
- Use-after-move from `captures(move x)`.
- Borrow conflicts tied to closure-owned loans.

## MVP Limitations to Track

- No projections or renaming in `captures(...)`.
- No implicit + explicit mixing in the same closure.
- No trait-driven capture inference.
- Borrowed-capture closures are non-escaping only.
- No auto-deref rewrite of captured names in the body.
- Body projections are limited by the existing place model.

## Future Extensions (Deferred)

- Allow projections in captures with renaming (e.g., `p = &x.field`).
- Add trait-backed `Copy` gating (`T: Copy`) post-typecheck.
- Introduce lifetime-tracked closure types to permit escaping borrowed captures.
- Optional capture-mode mixing or capture lists as pure overrides.
