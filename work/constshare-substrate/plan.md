# `ConstShare` Substrate — Standalone Track

**Status:** standalone, **GREENLIT 2026-04-30**.  Branch:
`constshare-substrate`.

**Scope:** language/runtime/library substrate for the `ConstShare`
capability + a first concrete backing primitive (`ConstArc<T>`).

**Out of scope:** any consumer migration.  This track delivers the
substrate; consumers (diagnostics-context's ReadOnlyMap, String
normalization, JsonHandle) migrate in their own tracks once the
substrate is sound.

**Versioning:** compiler version bump on landing; ABI bump if
`ConstArc<T>` runtime helpers / data layout cross the
compiler/runtime boundary (likely yes).  Decided at implementation
time, not pre-committed here.

---

## Why a standalone track

`ConstShare` is a load-bearing language/library contract that
multiple downstream tracks consume:

- **Diagnostics-context** (`work/exception-diagnostics-context/plan.md`):
  `ReadOnlyMap<K, V>` implements `ConstShare`; `Error.attrs` /
  `Error.captures` are owned immutable `ReadOnlyMap` values.
- **String normalization** (`work/string-ownership/plan.md`):
  `String` becomes a `ConstShare` value type;
  `var b = a;` over `String` is value-like-share, not the current
  primitive-Copy-with-hidden-refcount.
- **JsonHandle / future immutable-shared types**: migrations once
  the substrate is proven.

Putting `ConstShare` under any one consumer's track risks shaping
the contract to that consumer's needs.  Both load-bearing
consumers (diagnostics-context, String) deserve a substrate that
was designed against multiple shapes from day one and validated
against at least one synthetic-test consumer before either
real-consumer track depends on it.

---

## The G3 guardrail — load-bearing constraint

**`ConstShare` must NOT be checker-only.**

If `var b = a;` is accepted for `T: ConstShare`, the lowering
pipeline (HIR / MIR / runtime) must see a concrete operation that
produces the second owner.  Either:

- **(a)** HIR/MIR records an explicit `ConstShare` copy/retain
  operation (a new MIR op or a recognized intrinsic call).
  Lowering reads the op and emits the refcount-inc.
- **(b)** The trait has a lowering-visible hook (e.g., a
  compiler-recognized method like `__const_share_dup__(self) ->
  Self`) that the type-checker rewrites the assignment to.
  Lowering sees the call, not just a value flow.

A marker trait alone is **not enough**.  This is the G3 incident's
lesson restated for `ConstShare`: any checker-side decision that
changes the apparent ownership/value shape of an expression must be
represented in the post-check IR, either by rewriting HIR or by
recording a node-level coercion the lowering pass consumes.

The active rule from AGENTS.md § "Checker / Lowering Contract":

> "Any type-checker decision that changes the apparent type or
> value category of an expression in a way that affects lowering
> must be represented in the post-check IR, either by rewriting
> HIR or by recording a node-level coercion consumed by downstream
> passes.  It must NOT exist only in transient checker locals."

`ConstShare` value-like-share triggers the rule directly: the
checker is making `var b = a;` semantically valid where it
otherwise would not be (or would have different semantics — move,
deep-copy, etc.).  Lowering must see the share-construct, not
infer it.

This guardrail is the load-bearing acceptance criterion for the
substrate.  Phase 1a's exit gate explicitly checks it.

---

## Architectural shape

### `ConstShare` contract (stdlib trait)

Lives in `stdlib/std/core/shareable.drift` next to existing `Share`.

```drift
/// Marker trait for value-like immutable shared data.
///
/// Types implementing `ConstShare` participate in cheap value-like
/// duplication: `var b = a;` on a `T: ConstShare` value produces
/// a second independent owner of the same underlying immutable
/// data.  Aliasing is unobservable because the data is immutable;
/// thread-safe by construction.
///
/// `ConstShare` is intentionally distinct from `Share`:
///
/// - `Share` ("warning-bearing") signals "I'm choosing aliasing
///   because mutations through aliases will be observable."  Used
///   for `Arc<T>`-style mutable-shared types where the user must
///   acknowledge the aliasing surface (`share x` keyword).
///
/// - `ConstShare` signals "I'm taking another stake of immutable
///   data; aliasing is not observable to users."  No keyword
///   ceremony; `var b = a;` works directly.
///
/// A type implements `ConstShare` OR `Share`, not both for the
/// same semantic shape.
///
/// **Mutation contract.** Types implementing `ConstShare` must NOT
/// expose mutation through `&mut` borrows or in-place state
/// changes.  The compiler enforces this at the trait boundary:
/// `&mut` borrow of a `T: ConstShare` value is rejected.
///
/// **Lowering contract.** `var b = a;` for `T: ConstShare` is
/// lowered to a concrete share/retain operation visible at HIR /
/// MIR level (per the G3 rule against checker-only coercions —
/// see AGENTS.md § "Checker / Lowering Contract").  The exact
/// lowering shape is settled in Phase 1a — either an explicit
/// MIR op or a compiler-recognized backing-primitive method
/// call.
///
/// **First adopter:** `core.ConstArc<T>` (this track's backing
/// primitive).  Subsequent adopters via consumer tracks:
/// `containers.ReadOnlyMap<K, V>` (diagnostics-context),
/// `String` (string-ownership), `JsonHandle` (future).
pub trait ConstShare {
    // No method on the trait surface (marker).  Lowering hooks (if
    // (b) is chosen at Phase 1a kickoff) are compiler-internal.
}
```

### `ConstArc<T>` backing primitive

The first concrete `ConstShare` adopter — a stdlib-provided
refcounted-immutable wrapper.

```drift
// Placement TBD at Phase 1a kickoff: either std.core.const_arc
// (proximity to ConstShare contract + Error / DV consumers) or
// std.concurrent.const_arc (sibling of Arc<T>).

pub struct ConstArc<T> {
    // Internal: refcount header + payload.  Lowered as a fat
    // pointer to a heap allocation.  Layout TBD at kickoff;
    // likely (header_ptr, payload_ptr) where header_ptr points
    // to (refcount, drop_fn, ...).
}

implement<T> ConstShare for ConstArc<T> { }

implement<T> ConstArc<T> {
    /// Constructs a new `ConstArc<T>` from an owned `T`.  T's
    /// ownership transfers to the heap allocation.
    pub fn from_value(var v: T) nothrow -> ConstArc<T>;

    /// Returns a borrowed reference to the contained value.
    /// Lifetime bounded by `&self`.
    pub fn get(self: &ConstArc<T>) nothrow -> &T;

    // No `get_mut` — ConstArc is immutable post-construction.
    // Compiler enforces no `&mut` borrow through ConstArc per
    // the ConstShare mutation contract.
}
```

### Lowering shape (chosen at Phase 1a kickoff)

The concrete lowering shape for `var b = a;` on `T: ConstShare`.
Two candidates:

- **(a) Explicit MIR op.**  New `M.ConstShareDup(dest, src,
  ty)` MIR op.  HIR→MIR emits this op when lowering an
  assignment / copy of a `T: ConstShare` value.  Codegen lowers
  the op to a runtime helper call (refcount inc on the backing
  primitive).
  - Pro: clean; works uniformly for any `T: ConstShare` regardless
    of backing.
  - Con: new MIR op surface; needs handlers in every MIR-walking
    pass.

- **(b) Compiler-recognized method call.**  The trait (or the
  backing primitive) exposes a method like
  `__const_share_dup__(self: &Self) -> Self`.  HIR→MIR rewrites
  the assignment to a call to this method.  Codegen sees an
  ordinary method call.
  - Pro: reuses existing HIR/MIR call infrastructure; no new op.
  - Con: Drift's existing implicit-method-call recognition
    (e.g., for `Destructible.destroy`) is the closest precedent;
    need to confirm the surface composes cleanly.

Phase 1a kickoff decides between (a) and (b).  The rest of this
plan treats both as viable; the acceptance gates apply equally.

---

## Phase plan

### Phase 1a — substrate (~5–7 days)

**Deliverable:** `ConstShare` trait + `ConstArc<T>` backing
primitive + type-checker support + lowering-visible
duplication/drop + mutation rejection.  Validated against synthetic
tests (no real consumer migration in this phase).

#### Components

##### Stdlib

- `stdlib/std/core/shareable.drift` — adds `ConstShare` trait next
  to existing `Share`.  Module header documents the
  `ConstShare` vs `Share` distinction and the mutation contract.
- `stdlib/std/core/const_arc.drift` (or `std/concurrent/const_arc.drift`
  per kickoff decision) — `ConstArc<T>` type, `from_value` /
  `get` methods, `implement<T> ConstShare for ConstArc<T>`.

##### Compiler

- Type-checker recognition: `T: ConstShare` participates in
  value-like-share at assignment / parameter-pass / return.
  `var b = a;` for `T: ConstShare` is accepted (vs the
  current behavior — error on non-Copy types, or move).
- Type-checker mutation rejection: `&mut` borrow of a value where
  the type is `T: ConstShare`-bearing is rejected with a clear
  diagnostic.  Same for `var x: ConstArc<U>; x.method_taking_&mut_self()`.
- HIR/MIR lowering: the chosen shape from §Lowering shape
  produces a concrete IR-visible operation for the
  duplication.  No checker-only coercion.
- Drop / refcount-zero: `ConstArc<T>`'s backing storage drop runs
  `T`'s destructor + frees the heap allocation; lowering integrated
  with existing ownership-pass infrastructure.

##### Runtime

- `ConstArc<T>` heap layout: refcount header (atomic) + payload.
- `drift_const_arc_inc(handle)`, `drift_const_arc_dec(handle)`
  helpers; dec runs the type's drop function and frees at
  refcount-0.
- Possibly `drift_const_arc_alloc(size, drop_fn) -> handle` for
  `from_value`.

##### Tests

Synthetic-consumer tests (in `lang/tests/driver/`):

1. **Synthetic ConstShare-bearing struct.**  Define a small
   user-side type `MyShared<T>` whose only field is
   `ConstArc<T>`.  Implement `ConstShare`.  Test:
   - `var b = a;` accepted; both `a` and `b` usable; refcount
     reaches 2 then drops to 0 cleanly.
   - Returning `MyShared<T>` from a function preserves
     ownership; no R2-class issues since the value is owned.
   - Cross-frame share: caller does `var b = make()` (where
     `make` constructs and returns); subsequent `var c = b;`
     works value-like-share.

2. **Mutation rejection.**  `MyShared<T>::field_mut(self: &mut
   Self, ...)` rejected at type-check.  `&mut my_shared.x` (where
   `x` is the `ConstArc` field) rejected.  Diagnostic mentions the
   `ConstShare` immutability contract.

3. **Non-ConstShare control.**  An ordinary owned struct without
   `ConstShare` continues to behave as before — `var b = a;`
   moves (or errors if the type is non-Copy and owned-only) per
   the existing rules.  Pinned so the new path doesn't change
   behavior for non-ConstShare types.

4. **Lowering-visible probe (G3 guardrail).**  Either:
   - Compile a fixture and check MIR for the explicit
     `M.ConstShareDup` op (path (a)), OR
   - Compile a fixture and check MIR for the compiler-recognized
     dup method call (path (b)).
   - In either case, the test asserts the LOWERING sees the dup,
     not just the type-checker.  This is the G3 acceptance gate.

5. **Memcheck.**  Synthetic ConstShare-bearing struct
   constructed, shared, dropped — under valgrind, zero leaks.
   Multi-share / scoped-drop patterns covered.

6. **`&mut` on ConstShare via composition.**  A struct contains
   a `ConstShare`-bearing field plus a non-ConstShare field; the
   non-ConstShare field is `&mut`-borrowable, but the
   ConstShare-bearing field is not.  Pinned to ensure the
   immutability contract scopes to the right field.

#### Exit gate

All six acceptance criteria (§Acceptance gates below) pass.

### Phase 1b — diagnostics-context resumes (separate track)

Once Phase 1a lands, `work/exception-diagnostics-context/plan.md`
Phase 1 resumes with `ReadOnlyMap<K, V>` as the first real
consumer.  ReadOnlyMap implements `ConstShare`;
`Error.attrs` / `Error.captures` are owned immutable
`ReadOnlyMap` values; no borrowed-view path.

Not in scope of this track.

### Phase 1c — String normalization (separate track)

`work/string-ownership/plan.md` later consumes the substrate to
make `String` a `ConstShare` value type.  `var b = a;` over
`String` becomes value-like-share via the substrate (rather than
the current primitive-Copy with hidden refcount).

Not in scope of this track; specifically NOT bundled.

---

## Acceptance gates (Phase 1a)

The substrate is "done" when **all six** hold:

1. **Positive — synthetic ConstShare consumer.** A small
   `ConstShare`-bearing type can be assigned `var b = a`, both
   values used after the assignment, memcheck clean across the
   share-and-drop lifecycle.
2. **Positive — diagnostics-context-shaped prototype.** A
   `ReadOnlyMap`-shaped prototype (held in `work/` as a probe,
   not committed to mainline stdlib in this track) can own
   immutable storage and be copied value-like without `.clone()`.
   Pinned as proof the substrate works for the load-bearing
   downstream consumer.
3. **Negative — mutation rejection.** `&mut` borrow / mutation
   through a `ConstShare` value rejects with a clear diagnostic.
   The diagnostic mentions the `ConstShare` immutability contract
   (so users understand WHY, not just THAT).
4. **Negative — non-ConstShare unchanged.** Non-ConstShare owner
   assignment behavior is unchanged.  Existing types' move /
   non-Copy / Copy semantics are not affected by the new path.
   Full driver / stage / checker / packages suite green.
5. **Boundary — lowering-visible (G3 guardrail).**
   Retain/release/drop behavior for `ConstShare` is visible at
   MIR / lowering level, NOT in transient checker state.  A
   regression test inspects MIR (or equivalent post-check IR) and
   asserts the dup operation is present.  Without this, the
   substrate fails the G3 rule and the trait must be revised
   before ship.
6. **Versioning.** Compiler version bump (per AGENTS.md
   compiler-versioning rule).  ABI bump if `ConstArc<T>` /
   runtime helpers / data layout cross the compiler/runtime
   boundary — likely YES given the new runtime helpers
   (`drift_const_arc_*`).  `test_abi_version_stamp.py` regression
   updated.

---

## Stop-and-escalate triggers

Halt and consult before proceeding if any of these fire:

- **Lowering-visible probe (gate 5) fails.**  If the dup
  operation isn't visible at MIR after Phase 1a's first round of
  implementation, the chosen lowering path (either (a) MIR op or
  (b) recognized method call) is not delivering on the G3
  guardrail.  Stop, redesign, do not ship.
- **Type-checker mutation rejection (gate 3) is too narrow or
  too broad.**  Too narrow: some pattern that mutates the
  immutable storage compiles cleanly → soundness hole.  Too
  broad: legitimate non-ConstShare-mutation patterns reject →
  user-visible regression.  Either way, stop and refine before
  ship.
- **Non-ConstShare regression (gate 4).**  ANY existing test
  fails because of the new path.  Stop, narrow the change.
- **Performance regression on existing types.**  Existing types'
  assignment / parameter-pass costs measurably worsen even though
  they don't implement ConstShare.  Suggests the dispatch
  machinery is on the hot path for non-target types.  Stop and
  scope the dispatch more tightly.
- **ConstArc layout / runtime helper surface escapes Phase 1a's
  scope.**  If runtime helpers have to grow significantly during
  implementation (e.g., type-erased payload handling, unforeseen
  threading concerns), pause and decide whether the scope
  expansion is acceptable or whether the design needs a smaller
  initial deliverable.

---

## Out of scope (will not do in this track)

- **Any consumer migration.**  No `ReadOnlyMap`, no `String`
  changes, no `JsonHandle` changes.  Each consumer migrates in
  its own track once the substrate lands.
- **`ConstShare` for primitive types.**  `Int`, `Bool`, etc. are
  already `Copy`; they do not need `ConstShare`.  The trait is
  for value-types that hold refcounted-immutable storage.
- **Generic-collection variants beyond `ConstArc`.**  Inline-
  storage for short values (SSO-style), interned tables, etc.
  are future backing primitives that the trait can support but
  this track only delivers `ConstArc<T>`.
- **Cross-package serialization of `ConstArc<T>`.**  Out of
  scope; runtime-only sharing primitive.
- **Interaction with the existing `Share` trait beyond
  documentation.**  The two traits coexist; users don't
  co-implement them for the same shape.  Documented in
  `shareable.drift`'s module header but not policed by the
  compiler.

---

## Open questions (resolved at Phase 1a kickoff)

1. **`ConstArc<T>` placement.**  `std.core.const_arc` (proximity
   to `ConstShare` and downstream consumers) vs
   `std.concurrent.const_arc` (sibling of `Arc<T>`).  Lean toward
   **`std.core`** because the trait is in `core` and the substrate
   is core-foundational; `std.concurrent` placement would force
   `core` types (Error attrs etc. when they migrate) to import
   `concurrent`, which is the wrong direction.

2. **Lowering shape: (a) MIR op vs (b) recognized method call.**
   Both deliverable; (b) reuses more existing infrastructure but
   may be harder to reason about; (a) is cleaner conceptually but
   adds MIR surface.  Decided after a quick implementation
   probe at kickoff.

3. **`Frozen<T>` / `Immutable<T>` constraint on `T`.**  Does
   `ConstArc<T>` require `T: Immutable` to ensure the contract
   holds end-to-end?  Or is the immutability invariant carried
   purely by `ConstArc`'s API (no `get_mut`)?  Lean: API-only,
   matches the simplest design.  If interior-mutability concerns
   surface, revisit.

4. **Atomic refcount on single-thread programs.**  Does the
   refcount inc/dec need to be atomic in all builds, or can
   single-threaded programs use non-atomic ops?  Lean: atomic
   always, matches `concurrent.Arc<T>`'s precedent.

5. **`ConstArc<T>` `T`-destructor behavior at refcount-0.**  Does
   the refcount-zero path call `T`'s destroy method (if any) and
   then free the heap allocation?  Yes — standard refcounted-Box
   shape.  Implementation detail to confirm at runtime helper
   design.

6. **Trait-vs-marker shape at the Drift surface level.**  Is
   `ConstShare` a marker trait (no methods) with all behavior
   driven by compiler recognition, or does it expose at least one
   compiler-recognized method (e.g., `__const_share_dup__`)?
   Tied to the lowering-shape decision (open question 2).

---

## Risk register

- **R1 — checker/lowering split.**  Greatest risk.  G3 lesson:
  if the type-checker accepts `var b = a;` but lowering doesn't
  see the dup, we ship a soundness hole.  Mitigation: gate 5
  (lowering-visible probe) is part of the exit gates; without
  passing it, the substrate doesn't ship.
- **R2 — interaction with existing Copy machinery.**  Drift's
  existing Copy / non-Copy / move dispatch is intricate; adding
  a `ConstShare` path that runs alongside risks edge-case
  interactions.  Mitigation: gate 4 (non-ConstShare unchanged)
  catches regressions; full suite gate before ship.
- **R3 — backing primitive scope creep.**  `ConstArc<T>` could
  grow features (alignment-aware allocation, weak refs, in-place
  mutation hooks) during Phase 1a.  Mitigation: scope creep
  triggers a stop (per stop-and-escalate triggers above).  Phase
  1a delivers the minimum needed for downstream consumers (alloc,
  inc, dec, get).
- **R4 — atomic refcount overhead on cold paths.**  Using atomic
  ops universally may have measurable cost on single-threaded
  programs.  Mitigation: gate 4 measurement; if cost is real,
  the existing `Arc<T>` pattern is the precedent — no
  separate-handling design unless it materializes.

---

## Notes on what this plan is NOT

- **Not** a green light to start implementation.  Phase 1a
  kickoff requires sign-off on the open-question dispositions,
  particularly the lowering-shape choice (open question 2 / 6)
  which is load-bearing for the G3 guardrail.
- **Not** under any consumer's banner.  Both
  diagnostics-context and String normalization tracks consume
  this substrate but neither owns it.
- **Not** bundled with String migration.  The String track
  consumes the substrate as one of its phases later;
  bundling the migration into this track expands scope
  unacceptably.
- **Not** a workaround for any specific bug.  The substrate is
  forward-looking infrastructure for value-like immutable
  sharing; R2 (the interprocedural method-call escape bug) is a
  separate compiler bug fixed independently.

---

## Cross-track references

- **`work/exception-diagnostics-context/plan.md`** — primary
  downstream consumer; resumes after Phase 1a lands with
  `ReadOnlyMap<K, V>` as the first real `ConstShare`-implementing
  type.
- **`work/string-ownership/plan.md`** — secondary downstream
  consumer; consumes the substrate later for String migration.
- **`work/borrow-origin-method-call-escape/notes.md`** — the R2
  LANGUAGE_BUG; fixed in 0.31.41 independently of this track.
- **`stdlib/std/core/shareable.drift`** — existing home of the
  `Share` trait; `ConstShare` lands here.
- **AGENTS.md § "Checker / Lowering Contract"** — the G3 rule
  this substrate's gate 5 directly encodes.
