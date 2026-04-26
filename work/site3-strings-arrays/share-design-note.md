# Share — language capability + first implementation slice

Status: design note for K's review BEFORE any compiler change.

## 0. Branch state at the time of this note

- Patch 3 (String migration to ledger consultation) **reverted**. String stays under `string_arc.py` alias-walk authority by architectural rule (see `architecture-note-late-rewrite-authority.md`). Compiler version restored to 0.31.13.
- Patch 1 carriers retained: `lang/tests/memcheck/test_site3_return_source_alias_walk.py` is now a permanent gate against any future regression of the alias-walk's String branch. The architectural-rule explanation lives inline in `string_arc.py:1518-1545` (consultation-loop docstring) and in the architecture note.
- This branch (`feature/site3-strings-arrays-tier1`) continues with a fresh design slice for **Share** — a new language capability whose first concrete consumer is closure share-capture, with Arc<T> as the canonical adopter.

## 1. Share — what it is in the language model

**Share is a language-level capability for non-Copy shared-owner types.** It is the answer to the question "produce another OWNER of the same logical resource without copying the resource's value." Its essence:

- A `Share` operation on a value of type `T: Share` produces a **new owner** of the same underlying resource.
- Aliasing is part of the meaning. After `share x`, both `x` and the produced value point at the same logical thing. Mutations through either are observable through the other.
- Synchronization across threads/tasks/callbacks is **the programmer's responsibility**. `Share` is intentionally a *warning-bearing* operation: choosing to use it is choosing to accept aliasing.

**Share vs the four existing operational modes:**

| Mode | Semantic | Result | Synchronization burden |
|---|---|---|---|
| `move` | Ownership transfer | One owner; source is dead | None (unique owner) |
| `copy` (only for `Copy`) | Value-like duplication | Two independent values; no aliasing | None |
| `&` / `&mut` | Borrow | Reference; outlives nothing the source doesn't | Borrow-checker enforced |
| **`share`** (for `Share`) | **Shared-owner duplication** | **Two owners of the same resource; aliasing is real** | **Programmer's responsibility** |

The distinction from `copy` is **load-bearing** and must be preserved. `copy` is the language's "value-like, no aliasing" duplication; `share` is the language's "another owner, aliasing intended" duplication. They are not synonyms and should not be allowed to merge.

### Share is not Clone

A common adjacent design is a `Clone` trait that means "produce a duplicate by any means." Drift should NOT call this `Clone`. The reason: `Clone` historically conflates two semantically distinct ideas — "value-like duplication" (`copy`) and "shared-owner duplication" (`share`). Pre-existing language ecosystems where `Clone` is one trait have repeatedly hit ergonomic and correctness traps where users called `clone()` not knowing whether they were getting an independent copy or another handle to a shared resource.

By keeping `Share` distinct from `Copy` (and never introducing a unified `Clone`), Drift gives the user explicit, honest signaling: when you write `share x`, you are saying "I'm OK with aliasing." When you write `copy x` (only available for `Copy` types), you are saying "give me a value-like duplicate."

### Share trait shape

```drift
// In stdlib/std/core/share.drift (proposed):
pub trait Share {
    /// Produce another owner of the same underlying resource.
    /// Aliasing is intentional; synchronization for shared mutable
    /// state is the caller's responsibility.
    pub fn share(self: &Self) -> Self;
}
```

A method (not a compiler primitive). `share` takes a `&Self` (so the original isn't consumed) and returns a new `Self` (the additional owner). Implementers do whatever is appropriate for their type:

- `Arc<T>` implements `Share` by atomically incrementing the strong count and returning a new `Arc<T>` pointing to the same `ArcBox<T>` (this is what `_arc_clone_impl` already does).
- `Array<T>` (future, IF we want to migrate Array to a shared-backing model — see §5) implements `Share` by incrementing the array's refcount header and returning a new `Array<T>` pointing to the same backing store.
- User types implement it however makes sense for their resource (file handle, opaque database connection, etc.).

### Share is checker-enforced, not implicit

`share x` requires the type of `x` to implement `Share`. Compile-time error otherwise. There is NO implicit fall-through to `clone()` or `copy()`. The checker emits a focused diagnostic naming the type and pointing the user at:
- `copy x` (if the type is `Copy`).
- `move x` (if ownership transfer is what was meant).
- "implement `Share` for this type" (if the user really does want shared-owner semantics for a custom type).

### Share is `nothrow`

The trait method MUST be `nothrow`. Aliasing-duplication should not introduce error-paths into the program. Implementations that need fallible setup should expose a separate factory function, not abuse `share`.

## 2. First adopters

| Type | Adopt Share? | Reason |
|---|---|---|
| **`Arc<T>`** | **Yes (canonical first adopter)** | `Arc<T>` IS the motivating shared-owner type. `_arc_clone_impl` already exists and provides the exact semantic. The implementation is one trait impl + an alias from the existing `clone()` intrinsic to the trait method. |
| `String` | **No, intentionally** | String is `Copy`. `captures(copy s)` is the right user-facing story today. Forcing String into Share would conflate Copy and Share, which §1 explicitly warns against. (The fact that String is implemented with shared backing under the hood is irrelevant — the user-facing semantic IS value-like duplication.) |
| `Array<T>` | **Deferred** (see §5 for the architectural question) | The current `Array<T>` semantic is closer to Rust's `Vec<T>` — owned, non-shared. Adopting Share for Array would mean changing Array's semantic from "owned vector" to "shared-backing buffer," which is a substantial language-level decision separate from the Share trait introduction itself. |
| `MutexGuard<T>`, `VirtualThread<T>` | No | Resource-RAII types; not shared-owner in the Share sense. |
| User types | Opt-in | Authors add `implement Share for MyType` if their type genuinely models a shared resource. |

The first PR introducing Share would land it as a trait + the `Arc<T>` impl. No change to Array, String, or any other stdlib type in the same patch.

## 3. First syntax consumer — `captures(share x)`

### Surface syntax

```drift
|req, ctx| captures(share app) => { app.handle(req, ctx) }
```

**Semantics:**
1. At closure-construction time, evaluate `share app` once. This calls `app.share()` and produces a new owner of the same underlying resource.
2. Move-capture the shared value into the closure's environment. The closure's `app` field owns its own +1.
3. The outer `app` binding remains usable after the closure is constructed (because `share` only borrows `&self`).
4. When the closure is dropped, its captured `app` is dropped (releases the closure's +1). The outer `app` is dropped at its own scope boundary.

**Equivalence (mental model):**

```drift
// captures(share app) is operationally equivalent to:
var __share_tmp = app.share();
|req, ctx| captures(move __share_tmp) => { ... use __share_tmp ... }
```

— except it removes the user-visible temporary. The compiler does the lowering.

### Touch points (concrete file-level)

| Layer | File | Change |
|---|---|---|
| Lexer/grammar | `lang/driftc/parser/grammar.lark:516-519` | Add `SHARE NAME` alternative to `lambda_capture_item`; declare `SHARE.2: /share\b/` token. |
| Parser AST | `lang/driftc/parser/ast.py` (`LambdaCapture` shape) | Add `SHARE` to the capture-kind enum. |
| Parser builder | `lang/driftc/parser/parser.py` (`_build_lambda_capture`) | Recognize `SHARE NAME` alternative. |
| HIR | `lang/driftc/stage1/closures.py:HCaptureKind` | Add `SHARE = auto()` (5th variant alongside REF/REF_MUT/COPY/MOVE). |
| Capture discovery | `lang/driftc/stage1/capture_discovery.py` | Map `SHARE` syntax to `HCaptureKind.SHARE`. |
| Type checker | `lang/driftc/checker/...` | New diagnostic: a `SHARE` capture's local type must implement the `Share` trait. Point at `copy` / `move` / "implement Share" alternatives. |
| HIR→MIR lowering | `lang/driftc/stage2/hir_to_mir.py` | At closure construction site: for `SHARE` captures, emit a synthesised method call `Share::share(&local)` whose result is then `move`-captured into the closure env. |
| Stdlib | `stdlib/std/core/share.drift` (new) + `stdlib/std/concurrent/concurrent.drift` (Arc<T> impl) | Define the `Share` trait; impl for `Arc<T>` calling through `_arc_clone_impl`. |

The implementation is well-bounded. None of it changes the existing capture surface — `move` / `copy` / `&` / `&mut` continue to mean what they mean today.

## 4. Diagnostics

| User mistake | Diagnostic | Suggested fix |
|---|---|---|
| `captures(share x)` where `T` is `Copy` (e.g., `Int`, `String`, POD struct) | `E-CAPTURE-SHARE-NOT-SHARE: type 'Int' is Copy, not Share. For value-like capture, use 'captures(copy x)'.` | `captures(copy x)` |
| `captures(share x)` where `T` is non-Copy and non-Share (e.g., `Token`, `MutexGuard<T>`) | `E-CAPTURE-SHARE-NOT-SHARE: type 'Token' does not implement Share. To transfer ownership, use 'captures(move x)'. To enable share-capture, implement 'Share' for 'Token'.` | `captures(move x)` or implement Share |
| `share x` outside a `captures(...)` context (e.g., bare `val y = share x;`) | Permitted — `share x` is a normal expression once Share lands; calls `Share::share(&x)`. Same diagnostic family on type errors. | — |
| `captures(copy x)` where `T` is `Share` but NOT `Copy` (e.g., `Arc<T>`) | `E-CAPTURE-COPY-NOT-COPY: type 'Arc<T>' is not Copy. For shared-owner capture, use 'captures(share x)'.` | `captures(share x)` |

The diagnostics ALWAYS distinguish Copy and Share in the user-facing message, reinforcing the semantic split.

## 5. Array semantics under Share — the open question

The user originally asked about Array semantics:

> val b = a moves
> val b = share a aliases same backing store
> mutation through either owner observes same storage
> synchronization is user responsibility
> no generic deep-copy API initially

**This describes a substantial change to `Array<T>`'s semantic model.** Today `Array<T>` is owned-vector-like (move on assignment, deep-copy via explicit clone). Adopting Share would make Array a shared-backing buffer (assignment moves; explicit `share` aliases backing). That's a real language-level shift, not just a trait impl.

The question for K: **is Array's semantic shift in scope for the Share patch, or a separate later track?**

My recommendation: **separate later track.** Reasons:

1. **Bounded first slice.** Introducing the Share trait + Arc adopter + `captures(share x)` syntax is already a substantial cross-layer change (parser → lexer → AST → HIR → checker → HIR→MIR → stdlib). Adding Array's semantic migration on top doubles the surface and the regression risk.

2. **Array's semantic shift is its own design discussion.** Switching Array from owned-vector to shared-backing has knock-on effects: every Array construction site, every mutation API, every iteration pattern. It needs its own design note covering: backing-store representation, refcount header layout, mutation-through-aliased-share semantics (does `a.push(x)` after `b = share a` reallocate or grow shared?), interaction with existing `Array.clone_deep()` etc.

3. **Compiler ownership authority for Array doesn't NEED Share to migrate.** The site-3 strings/arrays migration (this branch's original goal) is independent of whether Array adopts Share. Array's `array_locals` alias-walk branch in `string_arc.py` stays put either way; the late-rewrite authority architectural rule applies to it identically (see §3 of the architecture note). If we want to MIR-first the array drop-effects (option (b) from the architecture note), that's also independent of Share.

4. **Arc-first proves the trait surface.** Landing Share with Arc as the sole adopter validates the trait shape, the `captures(share x)` syntax, the diagnostics, and the lowering. Array adoption (if and when) is then a focused per-type migration with the trait surface already proven.

**Recommendation for this branch's scope:** introduce Share (trait + Arc impl + closure capture syntax). Defer Array's semantic shift to a follow-up branch (`feature/array-shared-backing` or similar) with its own design discussion.

## 6. Compiler ownership authority for Share operations

Per the architectural rule established in `architecture-note-late-rewrite-authority.md`:

> Ledger authority is valid only for ownership effects visible in the MIR snapshot used to build the ledger. Any late pass that creates/releases refcount stakes remains its own authority unless we rebuild/extend the ledger after that pass or move those effects earlier.

**Share operations must be MIR-visible at ledger-build time.** Since `share` is a normal trait method call, this is automatic — `Share::share(&x)` lowers to a `Call` MIR node, which the lattice sees. The result of the call is a normal owned value; the closure's `MoveOut` of the captured value is a normal MIR `MoveOut`.

No new MIR op needed. No late-rewrite pass needed. Share is structurally clean from the lattice's perspective.

For Arc<T> specifically: `Share::share(&arc)` lowers to `Call(t, _arc_clone_impl, [&arc])`. This is the SAME shape Arc already uses today for `arc.clone()`. The lattice already handles it correctly.

If a future shared-owner type wants to implement Share but its `share()` requires late-pass synthesised refcount mutations (analogous to `string_arc`'s StringRetain), that type's `share()` falls back into the late-rewrite trap and needs to follow the same containment pattern as String (own its own authority for its own retain/release).

## 7. Programmer guarantee summary

When a user writes `share x`:

- **Guaranteed**: a new owner of the same underlying resource is produced. Both `x` and the new value will need to be released/dropped. Reference equality (`ptr==`) holds for the underlying resource.
- **Warning carried**: aliasing is real. Subsequent mutations to the resource through either owner are visible to the other. If the resource is shared across threads / tasks / callbacks, synchronization is the programmer's responsibility.
- **NOT guaranteed**: thread-safety of mutations through `share`d aliases. Atomicity of `share`'s refcount mutation is implementation-detail of the type's `Share` impl (Arc's is atomic; some user type's might not be — that's the user type's contract).

This warning carries weight because it is captured in the spec and effective-drift docs (see §10) and signaled in code review by the explicit `share` keyword. Reviewers see `share x` and know "this code accepts aliasing."

## 8. Why Arc<T> is the canonical first adopter

- `Arc<T>` is the dominant shared-owner pattern in Drift today.
- The runtime mechanism (`_arc_clone_impl` atomic refcount increment, `_arc_destroy_impl` atomic decrement) is already in place and battle-tested.
- The user-facing ergonomic gap is real and recurring: `var app2 = app.clone(); ... captures(move app2)` is the daily workaround.
- Adopting `Share` for `Arc<T>` is a one-line trait impl that delegates to existing intrinsics. No new runtime work.
- Arc validates every layer of the trait surface without requiring the broader Array semantic discussion.

## 9. Why String stays on `copy`, not `share`

- String IS `Copy`. The user-facing semantic for "give me another usable String" is value-like duplication, not shared-owner aliasing.
- The implementation detail that String is refcount-backed is invisible to the user. Treating `copy s` as "value-like" is correct at the semantic layer; the runtime's refcount mechanism is just an optimization for cheap "copies."
- Forcing String into `Share` would either:
  - (a) make String NOT Copy (breaking source compatibility everywhere), or
  - (b) make String BOTH Copy AND Share (defeating the point of the distinction; code review can no longer rely on `share` as a warning).
- Neither is acceptable. String stays on `copy`.

If at some future point a user wants to make the String-aliasing observable (e.g., a shared-mutable string-buffer type), that's a different type (e.g., `Buffer`, `StringBuilder`, `Arc<String>`), not a re-classification of `String`.

## 10. Spec / effective-drift documentation — part of the feature, not cleanup

When Share lands, the same patch must include:

### 10a. Language-spec update (`docs/design/drift-lang-spec.md`)

A new section under "Capabilities" (or the equivalent existing structure):

- The `Share` trait, its method signature, its semantic contract.
- The `share x` expression form.
- The `captures(share x)` capture form.
- The relationship between Copy, Share, move, and ref/ref_mut.
- The aliasing-and-synchronization warning carried by `share`.
- The diagnostic categories for misused `share` / `copy`.

### 10b. Effective-drift entry (`docs/effective-drift.md`)

A new idiom entry titled something like **"share vs copy: when the alias matters"**. Includes:

- The motivating closure example: `|req, ctx| captures(share app) => { ... }`.
- A short example showing why `share` is a semantic warning label, not just convenience syntax — e.g., a counter-example where `share`d state is mutated from two callbacks without synchronization, with the explicit warning that this is the programmer's responsibility.
- A "when to use what" table mirroring §1's mode comparison.
- A note that `String` stays on `copy` and why.

### 10c. Regression coverage that pins documentation accuracy

- A test that `captures(share x)` for `Arc<T>` compiles, runs, and the outer binding remains usable.
- A test that the captured Arc is dropped exactly once (refcount accounting balanced).
- A test that `captures(share x)` for a `Copy` type emits the `E-CAPTURE-SHARE-NOT-SHARE` diagnostic suggesting `copy`.
- A test that `captures(share x)` for a non-Share non-Copy type emits the `E-CAPTURE-SHARE-NOT-SHARE` diagnostic suggesting `move` or "implement Share".
- A test that `captures(copy x)` for `Arc<T>` (which is non-Copy) emits the `E-CAPTURE-COPY-NOT-COPY` diagnostic suggesting `share`.

## 11. Recommended first implementation slice

**Slice 1 — Share trait + Arc adopter + `captures(share x)` syntax.**

Order of work:

1. **Stdlib first.** Add `stdlib/std/core/share.drift` with the trait. Add `implement<T> Share for Arc<T>` in `stdlib/std/concurrent/concurrent.drift`. No compiler change yet — at this point `share` is not a keyword, but the trait is callable as `Share::share(&arc)`.

2. **Add SHARE token + grammar + AST + parser builder.** Smallest possible change to recognise the `share` keyword in `captures(...)`. Reject everywhere else for now (don't allow bare `share x` expressions in slice 1; defer to slice 2 if requested).

3. **HIR + checker.** Add `HCaptureKind.SHARE`. Capture discovery maps the syntax to the kind. Checker enforces "type must implement Share" with the focused diagnostic.

4. **HIR→MIR lowering.** At closure construction, for SHARE captures, emit `Call(Share::share, [&local])` then capture the result via `MoveOut`. Pre-existing MOVE-capture machinery handles the rest.

5. **Spec + effective-drift docs (per §10).** Same patch.

6. **Tests (per §10c).** Same patch.

7. **Memory entry** for the future-work guidance: future shared-owner types should adopt Share (not Clone), and the architectural-rule late-rewrite trap applies to any type that synthesises refcount mutations after ledger build.

Out of slice 1 (deferred to follow-ups):

- Bare `share x` expression in non-capture contexts (slice 2).
- Array semantic migration to shared-backing (separate branch entirely).
- Other stdlib types adopting Share.
- The site-3 strings/arrays MIR-first refactor (option (b) from the architecture note).

**Estimated scope of slice 1:** parser (~30 lines), HIR (~10 lines), checker (~50 lines for the diagnostic + Share-trait check), HIR→MIR (~20 lines), stdlib (~30 lines for trait + Arc impl), spec docs (~80 lines), effective-drift entry (~50 lines), tests (~5 focused pins). Single coherent feature; one commit (or one tight series).

ABI no bump (no runtime ABI shape change). Compiler version: minor bump when the slice lands.

## 12. Open questions for K

1. **Slice 1 scope confirmation.** Trait + Arc impl + `captures(share x)` syntax + spec docs + tests. Bare `share x` expression deferred. Array deferred entirely. Agree?
2. **Trait location.** `stdlib/std/core/share.drift` (new file) vs adding to `stdlib/std/core/copy.drift` (existing trait file with Copy). My recommendation: separate file, separate concern.
3. **Method name.** `share(self: &Self) -> Self`. Confirm the method name; alternatives could be `share_owner`, `acquire`, `dup_owner`. My recommendation: `share` matches the keyword and the user mental model; no qualifier needed.
4. **`Send`/`Sync`-style markers later?** Not in slice 1. Worth flagging that the Share trait alone says nothing about thread-safety; if Drift wants compile-time thread-safety guarantees later, that's a separate trait family.
5. **Once Share lands and Arc adopts it, deprecate `Arc.clone()`?** My recommendation: leave `clone()` as a non-deprecated method that delegates to `Share::share`. Code-search migration from `.clone()` to `share` can happen organically; no need to break source compatibility.
