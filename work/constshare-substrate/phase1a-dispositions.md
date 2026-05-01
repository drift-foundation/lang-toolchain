# Phase 1a — Open-Question Dispositions

**Status:** recommendations from codebase survey; awaiting user
sign-off before Phase 1a implementation begins.

**Branch:** `constshare-substrate`.

**Companion:** `work/constshare-substrate/plan.md` (the substrate
plan; this file resolves its open questions).

---

## Codebase survey findings

Drift already has a working pattern for trait-with-synthesized-
call-from-checker that lowers cleanly and satisfies the G3 rule:
the existing **`Share` trait**.

**Share's pattern:**

1. **Trait** in `stdlib/std/core/shareable.drift`:
   ```drift
   pub trait Share {
       fn share(self: &Self) nothrow -> Self;
   }
   ```
2. **Checker / AST→HIR** at `lang/driftc/stage1/ast_to_hir.py:546-585`,
   `:761-792`: synthesizes
   `H.HCall(H.HQualifiedMember(base_type_expr=share_trait_ref,
   member="share"), [&x], origin="share_expr")` when the user
   writes `share x` (expression form) or `captures(share x)`
   (closure capture form).
3. **Type-checker** resolves the synthesized HCall to
   `Share::share__inst__<T>` (call instantiation per the trait
   impl).
4. **HIR→MIR** at `lang/driftc/stage2/hir_to_mir.py:1128-1148`
   lowers the resolved HCall normally — no special MIR op needed;
   the trait method's body (e.g. `Arc<T>::share`) does refcount
   inc internally.
5. **G3-compliant**: the synthesis lives in HIR (an HCall node);
   lowering sees the call and emits the refcount inc.  No
   checker-only state.

**This is exactly the lowering shape (b) from the substrate plan**
— compiler-recognized method call, no new MIR op required.  It's
already the established Drift pattern for value-like-share
operations.

For comparison, the codebase has dedicated MIR ops for refcount-
adjacent operations on **specific types** (`StringRetain`,
`StringRelease`, `ArrayDup`, `CopyValue`) — but those are
type-erased fast paths for primitive types where the trait
machinery would be overkill.  For trait-driven sharing,
`Share::share` is the precedent and it goes through normal call
lowering.

---

## Disposition recommendations

### 1. Lowering shape — **(b) compiler-recognized method call**

> `ConstShare` trait exposes a method
> `fn const_share(self: &Self) nothrow -> Self`.  When the
> type-checker accepts `var b = a;` (or parameter-pass / return)
> for a value of type `T: ConstShare`, AST→HIR (or stage1
> normalization) rewrites the value-flow site to
> `H.HCall(ConstShare::const_share, [&a])`.  Lowering sees the
> synthesized call and instantiates / emits it through the
> existing call infrastructure.  No new MIR op.

**Rationale:** mirrors the established `Share::share` pattern at
`stage1/ast_to_hir.py:546-585`.  Reuses every existing
HIR-rewrite, type-checker call resolution, and HIR→MIR call
lowering path.  G3 guardrail satisfied because the synthesized
HCall is a real HIR node; lowering literally executes the trait
method body.  Zero new MIR surface.

**Counter-considered:** option (a) explicit MIR op
`M.ConstShareDup`.  Cleaner conceptually but adds new op surface
that every MIR-walking pass (string_arc, drop_flags, ownership
ledger, codegen) must recognize.  Drift's existing pattern is
"trait call goes through call lowering" — adding a new op for
ConstShare would diverge from precedent without benefit.

### 2. ConstArc placement — **`std.core` direct, on top of `core.Arc`** (post-Arc-relocation)

**Status (2026-05-01, supersedes earlier disposition):** Arc has
been relocated from `std.concurrent` to `std.core` (ABI 11 — see
the relocation disposition at the bottom of this document).
ConstArc therefore lands on top of `core.Arc<T>` and lives in
`std.core` directly without the `std.core.const_arc → std.concurrent
→ std.core` cycle that the original placement was working around.
The old "`std.core.const_arc` submodule + re-export shareable.*"
arrangement is superseded; the ConstArc milestone resumes against
this cleaner layering.

---

### 2. (historical) ConstArc placement — `std.core.const_arc` (NOT re-exported by `std.core`)

> `stdlib/std/core/const_arc.drift`, module
> `std.core.const_arc`.  Users import directly:
> `import std.core.const_arc as ca`.

**Rationale (revised post-implementation 2026-04-30):**

- Trait `ConstShare` is at `std.core.shareable` (next to
  `Share`); the backing primitive belongs in the same
  neighborhood.
- Downstream consumers — eventual `Error.attrs` /
  `Error.captures` — live in `std.core`.  Placing
  `ConstArc<T>` in `std.concurrent` would put it on the
  wrong side of the prelude.
- **No re-export through `std.core`.**  The original plan called
  for `std.core` to re-export `ConstArc` so users could write
  `core.ConstArc<T>`.  Implementation showed this would form a
  cycle: the wrapper's inner refcount handle is `conc.Arc<T>`,
  so `std.core.const_arc` imports `std.concurrent`, which
  imports `std.core` — adding `std.core` → `std.core.const_arc`
  closes the cycle.  Users instead write `ca.const_arc<T>(...)`
  with a direct submodule import.
- **Self-sufficient submodule import.**  `const_arc.drift`
  re-exports `std.core.shareable.*`, and the primitive `Frozen`
  impls live in `shareable.drift` (next to the trait
  declaration).  This means `import std.core.const_arc as ca`
  is sufficient — the user does NOT need a separate
  `import std.core` to bring `Frozen` into scope.  Pinned by
  `test_const_arc_works_without_explicit_std_core_import` in
  `lang/tests/driver/test_const_arc_substrate.py`.
- **Conditional `Frozen` for `Optional` / `Result` stays in
  `core.drift`.**  Their impls reference `core.Optional` /
  `core.Result` directly; moving them to `shareable.drift`
  would re-introduce the import cycle.  Users who need
  `ConstArc<Optional<…>>` or `ConstArc<Result<…>>` import
  `std.core` explicitly — natural posture, since they are
  already constructing those types.

**Counter-considered:** `std.concurrent.const_arc` — would put
`ConstArc<T>` next to `Arc<T>` for typological symmetry.  But
core ↔ concurrent direction matters more than typological
neighborhood, and the trait + primitive belong together.

### 3. `T` constraint — **REVISED 2026-04-30: `Frozen` marker trait + sealed `ConstShare` impls**

The original "API-only" disposition was rejected.  User feedback:

> "ConstShare is a semantic promise: aliasing is unobservable.
> That is not guaranteed merely because `ConstArc<T>` has no
> `get_mut` method.  If `T` can contain `Mutex`, Cell-like
> state, `Arc`-backed mutable structures, or any type whose
> `&T` API can mutate or observe shared mutation, then
> `ConstArc<T>` is not value-like immutable sharing.  It is
> just `Arc` with fewer methods."

**Soundness gap that motivates the revision.**  `Mutex<T>`'s
public surface is `lock(self: &Mutex<T>) -> MutexGuard<T>`.
Mutation through `&Mutex<T>` is the type's whole point.  If
`ConstArc<Mutex<T>>` is constructible, then two values
produced by `const_share` both hold `&Mutex<T>` access into the
same backing — and the "aliasing unobservable" contract is
broken.  Same for any type whose `&T` API includes mutation:
atomic primitives, future `Cell<T>` / `RefCell<T>`-shaped
types, etc.

**Revised disposition (3 components):**

#### 3a — `Frozen` marker trait

```drift
// stdlib/std/core/shareable.drift, next to Share + ConstShare.

/// Marker trait: `&T` access never mutates and never observes
/// mutation through aliases.  A type implementing `Frozen`
/// promises that any reachable state through its public `&Self`
/// API is purely read; no `Mutex`-style locking, no atomic
/// store, no interior-mutability shape.
///
/// `Frozen` is the soundness boundary for `ConstArc<T>` and
/// `ConstShare`-implementing types: shared aliasing of a
/// `Frozen` value is unobservable because there is no
/// observation surface for mutation.
///
/// **Auto-derived.**  A user struct is Frozen iff all its fields
/// are Frozen.  Compiler enforces; users do not write
/// `implement Frozen` blocks directly in v1.  See
/// "v1 implementability rules" below.
///
/// **Default impls (stdlib, baked):** Int, Uint, Uint64, Bool,
/// Byte, Float, String, Void, DiagnosticValue, Optional<T>
/// where T: Frozen, Result<T, E> where T: Frozen + E: Frozen.
///
/// **Default NON-impls (explicitly NOT Frozen):**
/// - `&T` / `&mut T` for any T — a reference value is itself
///   immutable, but its REFERENT may not be.  Allowing
///   `ConstArc<&T>` would risk extending / sharing a view of
///   external mutable / lifetime-bound state under a
///   "deep-immutable" label.  Borrowed Frozen payloads
///   require lifetime/origin proof at ConstArc-construction
///   time, which v1 does not provide.  References stay
///   non-Frozen until that proof exists.
/// - `Array<T>` (mutable element storage)
/// - `HashMap<K, V>` (mutable surface)
/// - `Mutex<T>`, `RwLock<T>`, atomic primitives
/// - `Arc<T>` (its inner `&T` may itself expose mutation —
///   `Arc<Mutex<U>>` is the canonical aliasing-observable
///   shape `Share` is for, not `ConstShare`)
/// - All builder / aggregator types.
pub trait Frozen { }
```

**v1 implementability rules:**

- **Stdlib-baked impls** for primitives + immutable composites
  listed above.  Hand-rolled in `shareable.drift`.
- **Auto-derive** for user structs / variants where every field
  type is `Frozen`.  Compiler-checked; no user-side
  `implement Frozen` syntax in v1.
- **No user-side hand-rolled `Frozen` impls** in v1.  This
  prevents a user from slapping `implement Frozen for
  MyMutexBox` to claim a contract the type doesn't honor.
  Rationale: contract verification for an arbitrary type's
  `&T` surface is non-trivial; auto-derive over Frozen fields
  is the only verifiable shape v1 can ship.
- **Future relaxation:** `unsafe implement Frozen for X { }`
  could permit user-side hand-rolled impls with explicit
  unsafe-block ceremony — deferred, not v1.

#### 3b — `ConstArc<T>` requires `T: Frozen`

```drift
implement<T> ConstArc<T> require T: Frozen {
    pub fn from_value(var v: T) nothrow -> ConstArc<T>;
    pub fn get(self: &ConstArc<T>) nothrow -> &T;
}

implement<T> ConstShare for ConstArc<T> require T: Frozen { ... }
```

`ConstArc<Mutex<T>>` rejects at instantiation (Mutex is not
Frozen).  `ConstArc<MyConfig>` where `MyConfig` is a struct of
String + Int compiles (auto-derived Frozen).
`ConstArc<MyHandle>` where `MyHandle` holds an `Arc<Mutex<T>>`
field rejects (Mutex not Frozen → MyHandle not Frozen).

#### 3c — `ConstShare` impls sealed-via-composition in v1 (tightened)

**No user-side `implement ConstShare for MyType { ... }` block
is accepted in v1.**  User types acquire `ConstShare` only via
a **strict structural-composition rule**:

> A struct or variant is automatically `ConstShare` iff
> **every owned field's type is one of:**
>
>   (i)   `ConstArc<U>` for some `U: Frozen`, OR
>   (ii)  another type that is itself `ConstShare` (recursive
>         per this rule), OR
>   (iii) a type that is **`Frozen` AND `Copy`** (e.g. `Int`,
>         `Bool`, `Byte`, `Float` — trivially-copyable
>         immutable scalar metadata).
>
> The auto-derived `const_share` body invokes each field's
> own `const_share` (for cases (i) / (ii)) or bitwise-copies
> the field (for case (iii)) and reconstructs the struct.
>
> If ANY owned field has a type that is NOT one of (i)/(ii)/
> (iii), the struct/variant is **NOT** `ConstShare`.  No
> partial-share, no skipping, no auto-clone of mutable fields.

This is intentionally narrow.  It admits, in v1, exactly the
shape needed for the realistic immutable-shared types:

- A wrapper/newtype over `ConstArc<T: Frozen>`:
  ```drift
  pub struct JsonHandle { data: core.ConstArc<JsonNode> }
  // JsonHandle is auto-ConstShare (only field is ConstArc<U: Frozen>).
  ```
- A wrapper with one ConstArc-bearing field plus
  trivially-copyable scalar metadata:
  ```drift
  pub struct VersionedConfig {
      data: core.ConstArc<ConfigBody>,
      schema_version: Int    // Frozen + Copy → allowed
  }
  // VersionedConfig is auto-ConstShare.
  ```
- A composition of two ConstShare-bearing fields (recursive):
  ```drift
  pub struct ScopedAttrs {
      attrs:    core.ConstArc<MapStorage<String, DV>>,
      captures: core.ConstArc<MapStorage<String, MapStorage<String, DV>>>
  }
  // Both fields ConstShare → ScopedAttrs is auto-ConstShare.
  ```

It does NOT admit (rejected):

- `Mutex<T>` field (mutable).
- `Array<T>` field (mutable element storage).
- `HashMap<K, V>` field (mutable).
- `Arc<T>` field (potentially-mutable referent).
- `&T` / `&mut T` field (reference; not Frozen — see §3a).
- Any user type with a field of any of the above.

**The composition rule is NOT a backdoor for "mostly immutable
but partly mutable" values.**  All-or-nothing: every owned
field must satisfy (i)/(ii)/(iii) or the type is not
ConstShare.

**Why sealed-via-composition vs full-sealed:**

- Full-sealed (only stdlib types) wouldn't let user-side
  immutable-data types like `JsonHandle` / `FrozenGraph`
  participate without each one being a stdlib type.
- Sealed-via-composition with the strict rule gives users a
  controlled UX: define your type with the right
  shape-of-fields, get `ConstShare` for free.  Try to wrap
  anything mutable, get rejected.  No user can claim
  `ConstShare` for a heterogeneously-immutable type.

**v1 diagnostic for direct-impl attempt:**

```
error: cannot directly implement ConstShare for 'MyType'
note: in v1, ConstShare is auto-derived from struct/variant
      composition; user-defined direct impls are not accepted.
note: a struct is auto-ConstShare iff every owned field's type
      is ConstArc<U: Frozen>, another ConstShare type, or a
      Frozen + Copy scalar.
note: see std.core.shareable for the contract details.
```

**v1 diagnostic for auto-derive failure** (e.g., user has a
struct with a `Mutex` field and tries to use it as
ConstShare):

```
error: 'MyType' cannot be auto-derived as ConstShare
note: field 'mtx: std.concurrent.Mutex<Int>' is not ConstShare
      and is not (Frozen + Copy); the strict composition rule
      requires every owned field to satisfy one of:
        (i)   ConstArc<U: Frozen>, or
        (ii)  another ConstShare type, or
        (iii) a Frozen + Copy scalar.
note: replace the Mutex field with an immutable design, OR
      use Arc<Mutex<T>> + the Share trait if observable
      mutation is the intent.
```

#### Future relaxation (NOT v1)

A future patch could open `ConstShare` impls (and `Frozen`
impls) under explicit `unsafe` ceremony — `unsafe implement
ConstShare for X { fn const_share ... }`.  Out of scope for
this track.

### 4. (corollary) `ConstShare` trait method signature

> `pub fn const_share(self: &Self) nothrow -> Self;`

Same shape as `Share::share`.  Returns `Self` (a new owner of
the same backing); takes `&Self` (does not consume the
original).  `nothrow` because refcount inc cannot fail.

### 5. (corollary) Atomic refcount in single-threaded programs — **always atomic**

Matches `concurrent.Arc<T>`'s precedent; avoids a separate
non-atomic backing path for single-threaded programs.  Cost is
real but small; matches user expectations from `Arc<T>`.  If
profiling later shows atomic ops on cold paths matter, that's a
separate optimization track, not a v1 design concern.

### 6. (corollary) Implicit-share trigger sites — narrow v1 + precise copy-vs-move spec

**Spec — implicit `const_share` synthesis fires iff ALL of:**

1. The expression's value type is `T: ConstShare`.
2. The source binding is **not being moved out** (i.e., neither
   `move x` syntax nor an explicit move-into-call).
3. The source binding **remains usable** at the syntactic
   position right after the value-flow site — i.e., the
   language is preserving the source's usability and would
   otherwise reject a non-Copy source as already moved.

**Trigger sites covered in v1:**

- **Value assignment:** `var b = a;` where `a: T: ConstShare`
  and `a` is read-only-used (or used) after the assignment.
- **Owned-parameter pass:** `f(a)` where `f`'s parameter is
  `T: ConstShare`-typed (owned, not `&` / `&mut`) AND `a` is
  used after the call.  If `a` is NOT used after, this is a
  move (no `const_share` synthesized — the original `a` flows
  directly to the parameter).
- **Owned return:** `return a;` where the function's return
  type is `T: ConstShare`-typed.  Treated as a move (the
  function's frame is collapsing); no `const_share` needed.

**Explicit forms — NEVER synthesize `const_share`:**

- **`move x` syntax**: drains `x`; the value is consumed.
  Compiler MUST NOT auto-synthesize a const_share to keep `x`
  usable when the user explicitly opted into move semantics.
- **`copy x`** (the `core.Copy` keyword): for `T: ConstShare`
  types that are NOT `Copy`, this is a type-checker error
  (per existing Copy semantics).  For types that are
  simultaneously `Copy` and `ConstShare` (rare, but theoretically
  possible — e.g., a struct of all-`Copy` + `ConstArc<U>` fields
  if we allowed it; v1 doesn't), `copy x` performs the bitwise
  copy path, not the const_share path.  Phase 1a verifies no
  type is simultaneously Copy + ConstShare.

**Trigger-site decision table:**

| Source pattern | Source used after? | Synthesizes const_share? | Notes |
|---|---|---|---|
| `var b = a;` | YES | **YES** | The implicit-share case. |
| `var b = a;` | NO  | **NO** (move) | Source is dead; no need to share. |
| `f(a);` | YES | **YES** | Same. |
| `f(a);` | NO  | **NO** (move) | |
| `return a;` | (always end-of-frame) | **NO** (move) | Frame collapses; move. |
| `move a;` | (irrelevant) | **NO** (move) | Explicit user intent. |
| `&a` / `&mut a` | (irrelevant) | **NO** (borrow) | Not a value-flow site. |
| `copy a` | YES | **NO** (copy) | Bitwise copy if T: Copy. |

**"Used after" determination:** existing Drift liveness analysis
at the type-checker / borrow-checker layer.  The synthesis path
queries the same liveness info that decides "is this a move or
not."

**Out of scope for v1 (deferred):**

- Field-init copy (`Foo { x: a }` where `a: T: ConstShare`) —
  may or may not fall out of the assignment path; revisit when
  diagnostics-context Phase 1b consumes the substrate.
- Deep struct field share (a containing struct's `var b = c;`
  where one of `c`'s fields is `T: ConstShare`) — handled by
  the auto-derive structural rule (§3c), not by trigger-site
  synthesis.

Narrow start lets v1 ship with focused semantics.  Each
deferred case is a follow-on patch only when a real consumer
surfaces the need.

---

## Open questions deferred (not blocking Phase 1a)

These are listed in the substrate plan but I'm marking them as
"settled at implementation time, not blocking sign-off":

- **`freeze()` cost** (HashMap → MapStorage) — irrelevant for
  Phase 1a (which doesn't ship ReadOnlyMap).  Decided when
  diagnostics-context Phase 1b kicks off.
- **B-parameterization of ReadOnlyMap** — same: not Phase 1a's
  concern.
- **Trait scope behavior** (whether `ConstShare` and `Share`
  could be co-implemented) — documented in
  `shareable.drift`'s module header, not policed by the
  compiler.  Already noted in the substrate plan as out of
  scope.

---

## Summary table for sign-off (revised 2026-04-30 post-user-feedback)

| # | Question | Disposition | Status |
|---|---|---|---|
| 1 | Lowering shape | **(b) compiler-recognized method call**, mirroring `Share::share` synthesis pattern at `ast_to_hir.py:546-585` | ✅ APPROVED |
| 2 | `ConstArc<T>` placement | **`std.core.const_arc`**, NOT re-exported by `std.core` (would cycle through `std.concurrent`); submodule re-exports `std.core.shareable.*` so a direct import is self-sufficient | ✅ APPROVED (revised post-implementation 2026-04-30) |
| 3 | `T` constraint | **`Frozen` marker trait + sealed-via-composition `ConstShare` impls (strict — every owned field must satisfy the rule, no partial-immutability backdoor; references NOT Frozen in v1)** | ✅ APPROVED with two further tightening edits 2026-04-30: (a) `&T` removed from Frozen defaults; (b) composition rule narrowed to all-fields-must-satisfy |
| 4 | Trait method signature | `fn const_share(self: &Self) nothrow -> Self` | ✅ APPROVED |
| 5 | Atomic refcount | always atomic | ✅ APPROVED |
| 6 | Implicit-share trigger sites | narrow v1 with precise copy-vs-move spec; `move x` does NOT synthesize | ✅ APPROVED |

---

## Acceptance gates — must include negative cases (per user 2026-04-30)

In addition to the substrate plan's six acceptance gates, Phase 1a
ships with these **negative regressions** specifically required
by the user feedback:

### N1 — `ConstArc<Mutex<T>>` rejected at instantiation

```drift
fn fail_mutex_payload() {
    var m = ...; // Mutex<Int>
    val arc = core.ConstArc::from_value<type conc.Mutex<Int>>(move m);
    //                  ^^^^^^^^^^^^^^^^^^^^^
    // error: ConstArc<T> requires `T: Frozen`;
    //        `std.concurrent.Mutex<Int>` is not Frozen
    //        (Mutex exposes mutation through `&Mutex<T>::lock()`).
}
```

Test asserts rc != 0 + diagnostic mentions `Frozen` constraint.
Also covers `Atomic*`, `Arc<T>` (NOT Frozen since its `&T` may
expose mutation), `Array<T>`, `HashMap<K, V>` payloads.

### N2 — User-defined direct `implement ConstShare for X` rejected

```drift
struct MyStruct { x: Int }

implement core.ConstShare for MyStruct {
    pub fn const_share(self: &MyStruct) nothrow -> MyStruct {
        return MyStruct(x = self.x);
    }
}
//      ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
// error: cannot directly implement ConstShare for 'MyStruct'
// note: in v1, ConstShare is acquired via composition: hold a
//       `core.ConstArc<T>` field where T: Frozen.
// note: an explicit ConstShare impl would require a way to
//       verify the immutability contract of MyStruct's `&Self`
//       surface, which v1 does not provide.
```

Test asserts rc != 0 + diagnostic mentions the
"composition not direct" rule.

### N3 — `move x` on `T: ConstShare` is a move (NOT auto-share)

```drift
struct Wrap { data: core.ConstArc<Int> }
// Wrap auto-derives ConstShare via §3c structural rule.

fn check_move_semantics() {
    var a: Wrap = ...;
    val b = move a;
    val c = a;          // error: 'a' is moved
    //      ^
    // error: use of moved value 'a'
}
```

Test asserts: **first** the move + reuse-after produces a
"moved value" error, confirming `move a` did NOT synthesize
const_share to keep `a` usable.  This is the load-bearing
distinction the user named as required.

### N4 — Two live values after `var b = a` both drop cleanly (memcheck)

Memcheck regression at `lang/tests/memcheck/`:

```drift
fn share_and_drop_both_owners() {
    var a: Wrap = make_wrap_with_string("hello");
    val b = a;                  // implicit const_share; refcount = 2
    val c = a;                  // again; refcount = 3
    use_then_drop(b);           // refcount = 2
    use_then_drop(c);           // refcount = 1
    use_then_drop(a);           // refcount = 0; backing freed
}
```

Asserts under valgrind:
- Zero "definitely lost" bytes.
- Zero invalid reads/writes/frees.
- Final refcount-zero path runs the inner String's destructor
  exactly once (no double-drop, no leak).

### N5 — Non-trusted user type cannot circumvent via composition trick

```drift
// User attempts to claim ConstShare via fake composition.
struct FakeFrozen {
    // Nothing inside is actually Frozen; user is trying to
    // wrap a Mutex by hand.
    inner: conc.Mutex<Int>
}
// FakeFrozen is NOT auto-derived as Frozen (Mutex isn't Frozen).
// FakeFrozen is NOT auto-derived as ConstShare (no ConstArc field).

struct UserType {
    data: core.ConstArc<FakeFrozen>
    //                  ^^^^^^^^^^
    // error: ConstArc<T> requires `T: Frozen`;
    //        `FakeFrozen` is not Frozen
    //        (field `inner: std.concurrent.Mutex<Int>` is not Frozen).
}
```

Test asserts rc != 0 + the diagnostic chains the Frozen
violation to the Mutex field — the user can see WHY their
type isn't Frozen, not just THAT.

### N6 — Explicit `move` does not synthesize `const_share` (lowering visibility)

Companion to N3 at the lowering level.  After type-check
accepts a fixture with `move x` on a `T: ConstShare`,
**MIR inspection asserts no `ConstShare::const_share` HCall is
present** in the lowered output.  Pin so a future refactor
that accidentally synthesizes const_share for `move` paths is
caught immediately.

This is a sub-gate of the substrate plan's gate 5
(lowering-visible).  Negative form: not just "lowering sees
the dup when synthesis fires" but also "lowering sees no dup
when synthesis must NOT fire."

### N7 — `ConstArc<&T>` and reference-payload wrappers rejected

```drift
fn fail_reference_payload() {
    var x: Int = 42;
    val r: &Int = &x;
    val arc = core.ConstArc::from_value<type &Int>(r);
    //                  ^^^^^^^^^^^^
    // error: ConstArc<T> requires `T: Frozen`;
    //        `&Int` is not Frozen
    //        (a reference's referent may be mutable; v1 does
    //        not carry lifetime/origin proof into ConstArc
    //        construction — see std.core.shareable for the
    //        contract).
}
```

Also rejects user-wrapper composition:

```drift
struct RefHolder<T> { r: &T }
// RefHolder is NOT auto-Frozen (field `r: &T` is not Frozen).
// RefHolder is NOT ConstShare (no ConstArc field; even if it
// had one, the &T field would fail the strict-composition rule).

struct Bad {
    data: core.ConstArc<Int>,
    leaked_ref: &Int            // not Frozen, not Copy+Frozen for &T
}
// Bad is NOT auto-derived as ConstShare:
// - field `leaked_ref: &Int` violates the strict-composition rule
//   (&T is neither ConstShare nor Frozen+Copy in v1).
```

Test asserts rc != 0 + diagnostic mentions:
- The `Frozen` requirement (from ConstArc constraint), AND
- The "ConstShare payloads must be owned Frozen data" framing
  (so users understand v1 doesn't ship borrowed-payload
  ConstShare).

This is the load-bearing gate for the user-stipulated
"ConstShare must not silently accept reference payloads."  A
future patch with explicit lifetime/origin proof could lift
this restriction; until then, references stay non-Frozen and
this gate stays green.

---

## Acceptance gates — full list (substrate-plan + this revision's negatives)

For sign-off / Phase 1a closure, all of the following must hold:

**Original substrate-plan gates (unchanged):**

1. Positive — synthetic ConstShare consumer: `var b = a` works + memcheck clean.
2. Positive — diagnostics-context-shaped prototype works.
3. (subsumed by N1) Negative — mutation rejection with `Frozen` reason.
4. Negative — non-ConstShare unchanged.
5. Boundary — lowering-visible (G3 guardrail).
6. Versioning — compiler bump + ABI bump.

**Negative gates from user feedback (mandatory):**

- **N1** — `ConstArc<Mutex<T>>` and equivalents rejected at instantiation.
- **N2** — User-defined direct `implement ConstShare for X` rejected.
- **N3** — `move x` on `T: ConstShare` is a move; reuse-after is a "moved value" error.
- **N4** — Two live values after `var b = a` both drop cleanly under valgrind; refcount-zero runs inner destructor exactly once.
- **N5** — Composition-trick to wrap `Mutex` etc. rejected with chained Frozen-violation diagnostic.
- **N6** — `move x` on `T: ConstShare` produces no `const_share` HCall in lowered MIR (companion to gate 5; negative form).
- **N7** — `ConstArc<&T>` and any user wrapper containing a reference-typed field rejected with diagnostic explaining "ConstShare payloads must be owned Frozen data" (v1 does not ship borrowed-payload ConstShare; references stay non-Frozen until lifetime/origin proof exists).

---

## What changes after sign-off — revised implementation order

Phase 1a implementation on `constshare-substrate` branch.  Order
matters: foundation traits before ConstArc, ConstArc before
synthesis, synthesis last (it's the riskiest piece).

**Foundation (steps 1-3): traits and Frozen**

1. Add `Frozen` marker trait to
   `stdlib/std/core/shareable.drift`.  Stdlib-baked impls for
   primitives (Int/Bool/Float/Byte/String/Void/DiagnosticValue/
   Optional<T:Frozen>/Result<T:Frozen,E:Frozen>/&T).  Explicit
   non-impls for Array/HashMap/Mutex/Atomic/Arc.
2. Add `Frozen` auto-derive in the type-checker: a user struct
   or variant is Frozen iff all field types are Frozen.  No
   user-side `implement Frozen` syntax (rejected with
   diagnostic).
3. Add `ConstShare` trait to `stdlib/std/core/shareable.drift`.
   Method `fn const_share(self: &Self) nothrow -> Self`.

**ConstArc (steps 4-6):**

4. Add `ConstArc<T>` to new `stdlib/std/core/const_arc.drift`,
   `T: Frozen` constraint.  NOT re-exported by `std.core` (cycle
   via `std.concurrent`).  The submodule re-exports
   `std.core.shareable.*` so `import std.core.const_arc as ca`
   alone is sufficient — primitive `Frozen` impls live in
   `shareable.drift`.  Pinned by
   `test_const_arc_works_without_explicit_std_core_import`.
5. Runtime helpers: `drift_const_arc_alloc`,
   `drift_const_arc_inc`, `drift_const_arc_dec` in
   `lang/runtime/`.  Atomic refcount.
6. Implement `ConstShare for ConstArc<T> require T: Frozen`
   with the `const_share` method that does refcount inc via
   the runtime helper.

**Sealed-via-composition (step 7):**

7. Type-checker: auto-derive `ConstShare` for user structs/
   variants per §3c rule (at least one ConstArc-bearing field;
   all other fields ConstShare or Copy).  Reject direct
   user-side `implement ConstShare for X` with the
   "composition not direct" diagnostic.

**Mutation rejection (step 8):**

8. Type-checker: `&mut` borrow of a `T: ConstShare` value
   rejected with diagnostic citing the immutability contract.
   Same for in-place mutation patterns.

**Synthesis (steps 9-10):**

9. AST→HIR / stage1: synthesize `ConstShare::const_share`
   HCall at the trigger sites per §6 spec.  **Critical**:
   synthesis MUST NOT fire on `move x` paths or other
   move-classified value-flow sites.  Mirror `Share::share`
   synthesis pattern from `ast_to_hir.py:546-585 / 761-792`
   for the call shape; gate the *trigger* on liveness
   (source-used-after).
10. Type-checker: recognize the synthesized HCall and resolve
    to `ConstShare::const_share__inst__<T>`.

**Regression tests (steps 11-13):**

11. Acceptance gates 1-6 (substrate-plan original).
12. Negative gates N1-N6 (this revision's mandatory cases).
13. Memcheck regression for the share-and-drop-both-owners
    pattern (N4) and any other ConstShare-bearing test
    fixtures.

**Verification (steps 14-17):**

14. Full driver+stage+checker+packages suite green.
15. Full memcheck suite green.
16. Lowering-visible probe (gate 5 + N6): MIR inspection
    confirms `const_share` HCall present where synthesis
    fires, absent where it must not (after `move`, etc.).
17. ABI bump regression (`test_abi_version_stamp.py`).

**Versioning + history (steps 18-19):**

18. ABI bump (`DRIFT_RT_ABI_VERSION`) + compiler version bump
    (`DRIFTC_VERSION`).
19. `docs/history.md` entry naming the substrate, the soundness
    boundary (Frozen + sealed-via-composition), and the
    G3-compliance posture.

Estimated: ~7–9 days for the revised scope (vs ~5–7 in the
original plan; the Frozen marker + sealed impls add real work,
but they're the right work).

The implementation does not commit changes incrementally — each
step lands in `constshare-substrate`'s working tree, then the
full set commits as one substrate patch (or a small number of
review-friendly logical splits) once gates 1-6 + N1-N6 all
pass.
