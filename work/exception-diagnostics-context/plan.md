# Exception Diagnostics Context — Unified Plan

**Status:** **DESIGN REVISED 2026-04-30 — Phase 1 redirected from
borrowed-view to owned-immutable.**

History:
- Phase 0 GREENLIT 2026-04-30: borrowed `ReadOnlyMap<K, V>` over
  `&HashMap<K, V>`.
- Phase 1 attempted under that design; hard gate failed.  The MVP
  escape rule's intraprocedural shape missed method-call auto-borrow
  of local receiver returning a borrowed aggregate.  LANGUAGE_BUG
  filed and fixed separately at 0.31.41 (Bug R2; see
  `work/borrow-origin-method-call-escape/notes.md`).
- Even with R2 fixed, the user's diagnostics-context design
  direction is shifting AWAY from borrowed views over mutable
  HashMap storage.  Reasoning: borrowed views constrain the
  attrs/captures API to source-lifetime-bounded uses; an owned,
  immutable, cheaply-shared shape (refcounted ConstArc-backed) is
  preferred for Error diagnostics — passes through frame
  boundaries safely, copies cheaply, no lifetime bookkeeping.

**New Phase 1 target (this plan from 2026-04-30 revision):**
**owned-immutable `ReadOnlyMap<K, V>` that implements
`ConstShare`.**  Construction consumes/finalizes a mutable
`HashMap<K, V>` via `map.freeze()` / `ReadOnlyMap::from_map(move
map)`.  Post-freeze the map is immutable; no borrow lifetimes;
passes/returns/clones freely under `ConstShare`'s value-like
duplication semantic.

The user-facing center of the design is **`ConstShare`** — the
language/library-level semantic capability for value-like
duplication of immutable data.  `ConstArc<T>` may be the concrete
internal backing primitive in stdlib, but is NOT the user-facing
center.  Users of `ReadOnlyMap` (or `Error.attrs` / `Error.captures`
in later phases) interact with the `ConstShare` semantic — clone
freely, share value-like, no aliasing-observability concern — not
with `ConstArc` directly.

**This connects to the ConstShare / String normalization track**
(`work/string-ownership/plan.md`).  String, ReadOnlyMap, JsonHandle,
immutable byte blobs, frozen graphs are all candidates for the same
`ConstShare` capability.  Each may use `ConstArc<T>` (or another
backing primitive) as its internal implementation; the user-visible
contract is uniform.  See §Cross-track dependencies below.

Pre-pause status was Phase 0 GREENLIT and Phase 1 IN PROGRESS.
This document supersedes the two earlier plans:

- `work/exception-attrs-readonly-map/plan.md` (Q1) — folded in below.
- `work/exception-capture-unwind-stack/plan.md` (Q3) — folded in below.

Those documents stay in `work/` for reference but are not the
authoritative plan. This file is.

**Working directory:** `work/exception-diagnostics-context/`
(ephemeral). Durable artifacts move to `stdlib/` / `lang/...` /
`docs/` as deliverables of the respective phases.

**Branch (when started):** suggested name
`feature/exception-diagnostics-context`.

**Target version:** 0.31.41 (single coordinated release covering the
full feature).

---

## Framing — one diagnostics channel, not two cleanups

The user's framing is load-bearing for this plan: **robust apps need
one reliable exception context channel**. The goal is to leave
breadcrumbs in the throw/unwind context without forcing every layer
to write `try / catch / rethrow` just to append local context.

Today's exception diagnostic surface is fragmented:

| Surface | Today | After this work |
|---|---|---|
| `e.attrs` | Index-only pseudo-field; rejected on whole-map access | First-class read-only map view, iterable, indexable |
| `e.captures` (per-frame `^var` map) | Index-only, only the throwing frame contributes | First-class read-only map view; **every transit frame contributes** its `^vars` as the exception unwinds |
| Catch-site logging | "Log known keys" — schema-bound; useless at boundary catches | "Log everything attached" — wildcard catches finally work |
| App-side ergonomics | Often forced to wrap `try/catch/rethrow` to append breadcrumbs | `^capture` declarations alone suffice; runtime stacks them on unwind |

**Result:** at any catch site (especially boundary catches like a
route closure), the user reads `e.event`, `e.attrs`, and
`e.captures` to get the **complete diagnostic context**: what
exception, what user-attached attrs, what locals were in scope across
every transited frame. One mental model, one access shape, one
logging integration story.

Server/log diagnostics only — not client-facing payload by default.

---

## TL;DR

- New stdlib type **`core.ReadOnlyMap<K, V>`** — generic, reusable
  by user code, exposes `get`, indexing sugar, `entries`, `len`,
  `contains`, `is_empty`. **No mutation surface.**
- **`Error.attrs`** returns
  `&ReadOnlyMap<String, DiagnosticValue>`.
- **`Error.captures`** returns
  `&ReadOnlyMap<String, ReadOnlyMap<String, DiagnosticValue>>`
  (frame-keyed; outer key is `module::function`, inner is
  `^capture` name).
- `e.attrs["k"]` and `e.captures["fr"]["k"]` indexing remains as
  sugar over `.get(...)`.
- Mutation through any of these views is rejected at type-check.
- **`^capture` propagation** stacks across unwound frames: every
  frame the exception transits through contributes its `^vars` to
  the in-flight Error's `captures` map (Q3 work).
- Missing frame / missing key access returns `Optional<&V>::None()`
  via `.get(...)` — non-throwing. Direct index `e.attrs["k"]`
  preserves today's behavior (returns DV, with the existing
  `DV_MISSING` sentinel for missing keys).
- **`std.log` integrates** — `logger.error(msg, &Error)` and
  `logger.error_attrs(msg, &ReadOnlyMap<String, DV>)` are
  first-class; pass `e.attrs` whole, no schema knowledge required.

---

## Acceptance criteria (user-stated, load-bearing)

The track wraps when **all** of these are true:

1. `logger.error("handler-exception", e.attrs)` (or equivalent
   diagnostic helper) compiles cleanly.
2. `for entry in e.attrs.entries() { ... }` (or the Drift-supported
   iterator equivalent) compiles cleanly and visits every entry.
3. `e.attrs["key"]` continues to work with today's semantics (returns
   `DiagnosticValue`, `DV_MISSING` on absent key).
4. Attempted mutation of `e.attrs` (e.g. `e.attrs["k"] = v`,
   `e.attrs.set(...)`) is rejected cleanly at type-check.
5. `^capture` declared in frame A is visible (in `e.captures`) when
   a callee throws and the exception unwinds through A.
6. Multi-frame captures preserve per-frame identity and key names.
7. Missing frame / missing key access remains non-throwing — returns
   `None` (via `.get()`) or `DV_MISSING` (via direct index), as
   today.
8. **No app-side catch/rethrow required solely to attach
   breadcrumbs.** The `^capture` annotation alone, plus the
   propagation runtime, suffices.

Plus the standard gates:

9. Full driver / stage / checker / packages / memcheck suites green.
10. `.dmp` package determinism preserved for any package that
    consumes Error.
11. **ABI version bumped (locked YES)**: new runtime helpers for view
    construction + iteration cross the compiler/runtime boundary per
    AGENTS.md §"Boundary Contract Guardrails."  Bundled with the
    0.31.41 release; `lang/tests/driver/test_abi_version_stamp.py`
    mismatch regression updated alongside.
12. Compiler version bumped 0.31.40 → 0.31.41.
13. `docs/effective-drift.md` (and equivalent) updated with the new
    diagnostic-context patterns and the deprecation of catch/rethrow
    breadcrumb idioms.
14. App team's bookkeeper repro verifies end-to-end:
    `logger.error("handler-exception", e.attrs)` compiles, runs,
    logs the full context.

---

## Architectural shape

### `core.ReadOnlyMap<K, V>` — new stdlib type

A read-only **view** over an underlying map. Does not own the data;
holds an internal handle / reference to the source storage.

```drift
pub struct ReadOnlyMap<K, V> {
    // Opaque handle to the underlying map storage.
    // Lowered as a fat pointer; implementation-defined.
}

implement<K, V> ReadOnlyMap<K, V> require K is hash.Hash, K is cmp.Equatable {
    /// Returns `Some(&value)` if `key` is present, `None` otherwise.
    pub fn get(self: &Self, key: &K) nothrow -> Optional<&V>;

    /// Returns true if `key` is present.
    pub fn contains(self: &Self, key: &K) nothrow -> Bool;

    /// Returns the number of entries.
    pub fn len(self: &Self) nothrow -> Int;

    /// Returns true iff `len() == 0`.
    pub fn is_empty(self: &Self) nothrow -> Bool;

    /// Returns a single-pass iterator over key-value pairs.
    /// Iteration order is unspecified.  Each item is borrowed from
    /// the underlying map (and transitively from the view's source);
    /// borrow-checker bounds the iterator's lifetime to the source.
    pub fn entries(self: &Self) nothrow -> ReadOnlyMapIter<K, V>;
}

pub struct ReadOnlyMapIter<K, V> { ... }

implement<K, V> iter.SinglePassIterator<MapItemRef<K, V>>
    for ReadOnlyMapIter<K, V> { ... }

pub struct MapItemRef<K, V> {
    pub key: &K,
    pub value: &V,
}
```

**Bracket indexing — Error-side compat sugar only in v1.**

Generic `ReadOnlyMap<K, V>` does **NOT** support bracket indexing
in v1.  User code calls `.get(key)` and pattern-matches the
`Optional<&V>` explicitly.  Adding generic `m[key]` semantics
(via auto-`or_throw` / panic-on-missing / etc.) is a broader
language-rule decision that doesn't belong in this feature; deferred.

The existing Error-side bracket access is preserved as **per-type
compatibility sugar**, lowered through paths that already exist
today — not via a generic `ReadOnlyMap[]` lowering:

- `e.attrs["k"]` → returns `DiagnosticValue` (Copy); missing key
  returns `DV_MISSING`.  Today's lowering path preserved exactly.
- `e.captures["frame"]["key"]` → double-index sugar; missing frame
  returns an empty-view sentinel (per S5), missing key returns
  `DV_MISSING`.  Today's lowering path preserved exactly.
- Any other `ReadOnlyMap<K, V>` usage (user-side, or an Error field
  accessed in a non-bracket form) requires `.get(key)` —
  `m[key]` is a static-time error pointing at `.get()`.

This split keeps backward compatibility for all existing
`e.attrs[...]` / `e.captures[...][...]` user code without
introducing a new generic indexing policy.  Phase 2/4 implementations
preserve the existing compat-sugar paths verbatim and add the
view/iterator surface alongside.

### `Error.attrs` — re-typed return

| Today | After |
|---|---|
| `Error.attrs` is an index-only pseudo-field (`type_checker.py:8493-8501` rejects bare access) | `Error.attrs` resolves to `&ReadOnlyMap<String, DiagnosticValue>` |
| `e.attrs["k"]` is special-cased lowering | `e.attrs["k"]` desugars to indexing on the view; same runtime DV-lookup path |
| Whole-map access rejected | Whole-map access resolves to the view; iteration / pass-to-helper works |

### `Error.captures` — re-typed return

| Today | After |
|---|---|
| `Error.captures` is double-index-only (`e.captures["fr"]["k"]`) | `Error.captures` resolves to `&ReadOnlyMap<String, ReadOnlyMap<String, DiagnosticValue>>` (frame → per-frame map) |
| Only the throwing frame's `^vars` recorded | **Every transit frame** contributes its `^vars` (Q3 work) |
| Frame-key shape: `module::function` | Unchanged |

### `^capture` unwind propagation (Q3 work folded in)

`_emit_captured_locals(err_val)` (`hir_to_mir.py:907`) is currently
called only at HThrow (`:7377`). After this work, it's also called
before:

- `_visit_stmt_HRethrow`'s `_propagate_error` (`:7432`).
- Each post-callee-Err `_propagate_error` site that exists when a
  throws-call returns Err and the current frame doesn't catch
  (multiple sites; Phase 3 audit enumerates).
- `_visit_stmt_HTry`'s no-arm-matched fallthrough sites
  (`:6303`, `:6309`, `:7546`, `:7553`).

The HThrow path is unchanged (its existing emit is at the throw
site, before `_propagate_error` is even called). Adding emits at
the (b) sites does not double-emit on the (a) path.

**Per-frame collision rule (S(i) from Q3 plan, preserved):
last-write wins.** For recursive frames, the outermost recursion
level's value is observed. Documented; users wanting per-recursion
attribution use distinct `^var` names.

### Logging integration

**`std.log`** grows two integration surfaces:

```drift
implement<L> Logger<L> {
    /// Logs `msg` with the full diagnostic context of `err` —
    /// event name, attrs, and per-frame captures.  Equivalent to
    /// `logger.error_with_context(msg, err.event, &err.attrs, &err.captures)`.
    pub fn error(self: &mut Self, msg: &String, err: &Error) nothrow -> Void;

    /// Logs `msg` with a free-form attrs map (any
    /// ReadOnlyMap<String, DV>).  Used when the caller has a map
    /// from a non-Error source.
    pub fn error_attrs(
        self: &mut Self,
        msg: &String,
        attrs: &ReadOnlyMap<String, DiagnosticValue>,
    ) nothrow -> Void;
}
```

The `error(&Error)` overload is the **canonical boundary-catch
shape**: `logger.error("handler-exception", &e);`. Reads attrs +
captures + event from the borrowed Error and produces a single
log record covering the full context.

The `error_attrs(&ReadOnlyMap<...>)` overload is what enables
`logger.error("...", e.attrs)` — the user passes a borrowed view,
the logger iterates it.

Either form satisfies criterion 1 from the acceptance list. The plan
ships both to give explicit control over what's logged.

---

## Semantic decisions to lock before Phase 1

### S1 — Bracket-index semantics for `Error.attrs` (preservation)

> `e.attrs["k"]` returns `DiagnosticValue` (Copy), `DV_MISSING` on
> absent key. Identical to today. Lowered as
> `e.attrs.get("k").unwrap_or(DV::Missing())` (or equivalent) under
> the hood; user-visible behavior unchanged.

This is the **compat preservation contract**. Existing user code
that does `if e.attrs["k"] != DV::Missing() { ... }` continues to
work without modification.

### S2 — Iteration order

> `ReadOnlyMap.entries()` iteration order is **unspecified**.
> Matches `containers.HashMap.iter()`. Documented explicitly;
> users sort if order matters.

### S3 — Lifetime / borrow shape

> `e.attrs` returns `&ReadOnlyMap<...>` borrowed from the Error.
> The view borrows from the Error's internal storage. The iterator
> borrows from the view (and transitively from the Error). The
> borrow-checker bounds iterator and entry lifetimes to the catch
> arm scope (or wherever the Error is in scope).
>
> Storing a `MapItemRef<K, V>` past the Error's scope is a
> borrow-check error. To preserve a value past Error scope, copy
> `entry.value` (DV is Copy) or extract owned data
> (`String::from(entry.key)` etc.).

### S4 — Per-frame capture collision (last-write wins)

> Same-frame recursive `^capture` writes follow last-write-wins. The
> outermost recursion level's value is the one observed in
> `e.captures[frame_symbol][cap_name]` after unwind. Users wanting
> per-recursion attribution use distinct `^var` names per level.

### S5 — Missing-frame / missing-key access (non-throwing)

> `e.captures["nonexistent_frame"]` returns an empty-view sentinel
> (a `ReadOnlyMap` with `len() == 0`). Subsequent
> `["nonexistent_key"]` on that empty view returns `DV_MISSING`.
> Both paths preserve today's non-throwing behavior — no exception
> raised on absent keys at any depth.
>
> The empty-view sentinel is a runtime singleton (constant
> ReadOnlyMap with no entries); zero allocation per missing access.

This is **acceptance criterion 7**.

### S6 — Mutation rejection rule

> Any of these patterns rejected at type-check with a clean
> diagnostic:
>
> - `e.attrs["k"] = v;`
> - `e.attrs.insert(...)`, `e.attrs.set(...)`, etc. — methods that
>   don't exist on `ReadOnlyMap` produce "no matching method" errors.
> - `&mut e.attrs` — borrowing `&mut` from a method returning `&`
>   already errors today; same path applies.
>
> The diagnostic for assignment specifically should point users at
> the (future, separate) explicit mutation API:
> `error: cannot mutate Error.attrs through the read-only view; use
> Error::with_attr(...) or the explicit attrs builder API`.

(The "explicit mutation API" is out of scope for this track but
named so the diagnostic doesn't leave users stranded.)

---

## Phase plan

### Phase 0 — alignment + open-question resolution (~1 day, zero compiler change)

**Deliverable:** this plan, signed off. Resolution of the open
questions in §Open questions.

**Exit gate:** sign-off on:

- Stdlib placement of `ReadOnlyMap<K, V>` (`core` vs `containers`).
- `e.captures` access shape (`.entries()` vs `.frames()` for outer
  iteration).
- `std.log` integration shape (`error(msg, &Error)` overload set).
- Iterator-lifetime borrow story (verify the borrow-checker handles
  it with a probe test before committing to the API).

### ~~Phase 1 — borrowed `ReadOnlyMap<K, V>` over `&HashMap`~~ — **DEFERRED 2026-04-30 (path abandoned)**

The borrowed-view design is no longer the diagnostics-context
Phase 1 path.  Two reasons:

1. **R2 LANGUAGE_BUG.**  Phase 1's hard gate exposed an
   interprocedural borrow-origin escape via method-call auto-borrow
   of local receiver.  Filed and fixed at 0.31.41
   (`work/borrow-origin-method-call-escape/notes.md`).  With R2
   fixed, the borrow story is sound — but:
2. **Design direction shift (user, 2026-04-30 post-R2).**  Even
   with sound borrows, owned-immutable shared maps are the
   preferred shape for Error diagnostics.  Borrowed views over
   mutable HashMap storage constrain the API to source-lifetime-
   bounded uses; owned-immutable is cheap to copy/return/share, has
   no lifetime bookkeeping, and aligns with the broader
   ConstShare / String normalization model.

**The borrowed-view design is not deleted from this plan** — it
served as the borrow-checker validation vehicle that surfaced R2.
The compiler-side outcome (sound interprocedural escape rule for
method-call returns) is durable and benefits ANY future borrowed-
aggregate-returning method.  Just not used for diagnostics-context.

The current Phase 1 path is the new owned-immutable design below.

### Phase 1 — owned-immutable `ReadOnlyMap<K, V>` implementing `ConstShare` (target ~5–8 days, gated on `ConstShare` capability landing)

**Status:** design draft 2026-04-30; gated on the `ConstShare`
capability being available (with a concrete backing primitive — see
§Cross-track dependencies).

**Architectural shape:**

```drift
// In std.containers (cycle-free placement, per the prior phase's
// Option C finding — same placement applies here).
//
// ReadOnlyMap is an OWNED IMMUTABLE map value.  Implements
// `ConstShare`: cheap value-like duplication, aliasing
// unobservable because the underlying storage is immutable post-
// construction.  Internally backed by an immutable-sharing
// primitive (likely `ConstArc<MapStorage<K, V>>`); that backing
// is an implementation detail not exposed to user code.
pub struct ReadOnlyMap<K, V> {
    // Internal: immutable shared backing.  Concrete primitive
    // settled at Phase 1a kickoff; ConstArc is the leading
    // candidate.  Field is private; users interact only with the
    // ReadOnlyMap surface and the ConstShare semantic.
    storage: <internal immutable-share primitive>
}

// Internal immutable map storage.  Conceptually a frozen variant
// of HashMapCore: same probe-sequence, same buckets, but no
// mutation surface and no internal generation counter (immutable
// → no invalidation).  Not exposed in the public API.
struct MapStorage<K, V> {
    keys:   mem.RawBuffer<K>,
    values: mem.RawBuffer<V>,
    states: mem.RawBuffer<Int>,
    len:    Int,
    cap:    Int
}

// ReadOnlyMap implements ConstShare — user-visible contract:
//   - cheap to copy via the value-like-share path (clone is O(1)
//     refcount inc under the hood, but users don't need to know
//     that);
//   - aliasing is unobservable (immutable data);
//   - thread-safe by construction (immutable + atomic refcount
//     in the backing primitive).
implement<K, V> shareable.ConstShare for ReadOnlyMap<K, V> { ... }

implement<K, V> ReadOnlyMap<K, V>
    require K is cmp.Equatable, K is hash.Hash<hash.DefaultHasher>
{
    pub fn get(self: &ReadOnlyMap<K, V>, key: &K) nothrow -> Optional<&V>;
    pub fn contains(self: &ReadOnlyMap<K, V>, key: &K) nothrow -> Bool;
    pub fn len(self: &ReadOnlyMap<K, V>) nothrow -> Int;
    pub fn is_empty(self: &ReadOnlyMap<K, V>) nothrow -> Bool;
    pub fn entries(self: &ReadOnlyMap<K, V>) nothrow -> ReadOnlyMapIter<K, V>;
    // ConstShare provides cheap clone / value-like-share for free.
    // No `.read_only()` borrow constructor needed.
}
```

**Construction surface:**

```drift
// `freeze()` consumes the mutable HashMap and produces an immutable
// ReadOnlyMap.  Source HashMap's storage is moved into MapStorage,
// avoiding a full copy when possible (the storage layouts can be
// designed so HashMap's RawBuffers transfer directly to MapStorage).
implement<K, V> HashMapCore<K, V, B> require ... {
    pub fn freeze(var self: HashMapCore<K, V, B>) nothrow -> ReadOnlyMap<K, V>;
}

// Or equivalent free-function form:
pub fn read_only_map_from(var m: HashMap<K, V>) nothrow -> ReadOnlyMap<K, V>;
```

**User UX (interacts with ConstShare, not ConstArc):**

```drift
// Owned map; no lifetime bookkeeping.
val ro = move m.freeze();
val ro2 = ro;                  // value-like share via ConstShare;
                               // O(1) under the hood, no .clone()
                               // ceremony.  Both `ro` and `ro2`
                               // are independent owners of the
                               // same immutable storage.

logger.error("...", &ro);      // borrow for iteration
for entry in ro.entries() { ... }
return ro;                     // safe: owned, ConstShare-backed
```

**Why owned-immutable wins for diagnostics:**

1. **No lifetime bookkeeping.** `Error.attrs: ReadOnlyMap<...>`
   (owned) — Error owns the map; Error's lifetime bounds it.  No
   borrow-from-source rules, no MVP escape rule interaction.
2. **Cheap value-like share.** Under `ConstShare`, duplicating a
   `ReadOnlyMap` is O(1) (refcount inc on the internal backing).
   Users don't write `.clone()`; the `share` keyword and value-
   assignment semantics handle it.  Aligns with the wider Drift
   ergonomic for value-like immutable data.
3. **Cross-frame safe.** Returning `ReadOnlyMap` from a frame is
   a normal owned return; no R2-class bugs to worry about.
4. **Iteration borrows from owned data.** `ro.entries()` returns an
   iterator borrowing from `&ro` — the borrow is bounded by the
   ReadOnlyMap value's scope, which is the user's local scope, not
   a source-map's lifetime.
5. **Aligns with broader ConstShare pattern.** String, JsonHandle
   (post-migration), frozen graphs, frozen byte blobs — all
   candidates for the same `ConstShare` capability.  Each may use
   different backing primitives (`ConstArc<T>` is the leading
   candidate for stdlib's general case, but other primitives may
   apply for specific use cases like inline-string-optimization).
   ReadOnlyMap becomes one of the first concrete users of the
   `ConstShare` semantic.

**Not in this Phase 1:**
- Mutable construction post-freeze (frozen is frozen).
- Multiple-builder hashers (B-parameterization).  Default-builder
  only for the diagnostics use case; if a user wants custom-hasher
  immutable maps, that's a Phase-N+ extension.
- Iteration-while-mutating semantics.  Frozen → no concern.

---

## Cross-track dependencies

The owned-immutable Phase 1 above depends on the **`ConstShare`
capability** being usable from stdlib types.  `ConstShare` is the
language/library-level semantic capability: cheap value-like
duplication of immutable data; aliasing unobservable; thread-safe
by construction; no `.clone()` ceremony.

`ConstShare` is also load-bearing for:

- **`work/string-ownership/plan.md`** — String normalization.  The
  goal there is for `String` to implement `ConstShare` such that
  `var b = a;` over `String` is value-like-share, not the current
  primitive-Copy with hidden refcount.
- **`stdlib/std/json/json.drift`** — `JsonHandle` (currently uses
  `concurrent.Arc<JsonNode>` with explicit `.clone()`).  Future
  migration: `JsonHandle` implements `ConstShare`; users write
  `var h2 = h1;` instead of `var h2 = h1.clone();`.
- **Hypothetical future:** frozen graphs, frozen byte blobs,
  interned-symbol tables — all candidates for `ConstShare`.

### What `ConstShare` requires from the language

| Component | Where | Status |
|---|---|---|
| `ConstShare` trait surface | `stdlib/std/core/shareable.drift` (lives next to `Share`) | TRAIT NOT YET DEFINED |
| Type-checker support: `T: ConstShare` types behave value-like-share without explicit `.clone()` | `lang/driftc/type_checker.py` (Copy-via-share path) | NOT YET DESIGNED |
| Borrow-checker / lowering: `var b = a;` for `T: ConstShare` lowers to a refcount-inc on the backing primitive | `hir_to_mir.py`, ownership-pass integration | NOT YET DESIGNED |
| Diagnostic story: rejecting attempts to `&mut`-borrow / mutate a `ConstShare`-bearing value | `type_checker.py` | NOT YET DESIGNED |

### What backing primitives `ConstShare` needs

`ConstArc<T>` is the leading candidate for the general case
(stdlib-provided refcounted-immutable wrapper):

| Component | Where | Status |
|---|---|---|
| `ConstArc<T>` runtime layout | `lang/runtime/*.c` (atomic refcount, drop, etc.) | NOT YET DESIGNED |
| `ConstArc<T>` stdlib type | `stdlib/std/concurrent/` or `stdlib/std/core/` | NOT YET DESIGNED |
| `Frozen<T>` / `Immutable<T>` marker trait (if needed for `T` constraint) | `stdlib/std/core/` | NOT YET DESIGNED |
| Compiler integration (`copy_status` recognition for `T: ConstShare` structs, etc.) | `type_checker.py`, `core.py` | NOT YET DESIGNED |

`ConstArc<T>` may not be the only backing primitive.  Other
candidates (not in scope for this plan, listed for completeness):
inline-storage variants for small values (e.g., short-string
optimization for String), interned/canonicalized storage for
symbol tables, etc.  The user-facing `ConstShare` contract is
uniform regardless of backing.

### Phasing options

The diagnostics-context Phase 1 cannot land before `ConstShare` is
usable from stdlib types.  Either:

- **(P-1)** Land `ConstShare` (trait + at least one backing
  primitive like `ConstArc`) as its own track first — likely
  under the String normalization track's banner, since String is
  the highest-value adopter — then resume diagnostics-context
  Phase 1.
- **(P-2)** Bundle `ConstShare` + `ReadOnlyMap` as a single
  combined track.  ReadOnlyMap is a small, semantically simple
  first user — good validation shape for the trait + backing
  primitive design.  String migration (much bigger lift per
  `work/string-ownership/plan.md`) follows separately once the
  capability is proven.
- **(P-3)** (least preferred) Ship `ReadOnlyMap` over an
  ad-hoc-internal-only refcounted shape (no public `ConstShare`
  trait), with the contract that the ad-hoc backing is a private
  implementation detail to be replaced when `ConstShare` lands.
  Loses the chance to validate `ConstShare`'s design against a
  real user.  May be acceptable if `Error.attrs` /
  `Error.captures` are the only consumers and the use of
  `ConstShare`-style ergonomics is purely "future-fits-cleanly."

**Lean toward (P-2)**: `ReadOnlyMap` is small, semantically simple,
and a good first user to validate the `ConstShare` design against.
The trait + at least one backing primitive (`ConstArc`) ship as
part of the same effort; the trait shape is settled by being
exercised against this real first user.  String migration follows
separately once the capability is proven.

---

## Required compiler/runtime/library pieces (revised Phase 1)

### Language / capability — `ConstShare`
- Trait `ConstShare` defined in `stdlib/std/core/shareable.drift`
  (next to existing `Share`).  Marker trait + contract docs:
  immutable data, value-like duplication, aliasing unobservable.
- Type-checker recognition: a `T: ConstShare` value participates in
  value-like-share semantics.  `var b = a;` for `T: ConstShare`
  lowers to a refcount-inc on the backing primitive (or
  whichever cheap-share mechanism the backing exposes), not a
  deep copy or a move.
- Mutation rejection: `&mut` borrow of a `T: ConstShare` value
  rejected; in-place mutation rejected.  (Underlying storage is
  immutable; the trait promises that.)
- Trait scope behavior: types implementing `ConstShare` should NOT
  also implement `Share` for the same semantic shape (the warning-
  bearing `Share` keyword is for genuinely-aliasing-matters cases).
  Documented in `shareable.drift`'s module header.

### Backing primitive — `ConstArc<T>` (the leading candidate)
- Runtime: heap layout (refcount header + payload), atomic
  increment/decrement helpers, drop/finalize at refcount-0.
- Stdlib: `ConstArc<T>` type with the surface that `ReadOnlyMap`
  (and later String) needs internally.  Placement: likely
  `std.core` (proximity to Error / DV) or
  `std.concurrent` (sibling of `Arc<T>`); decided at Phase 1a
  kickoff.
- Compiler integration: type-recognition for `ConstArc<T>` so a
  struct field of `ConstArc<U>` participates correctly in the
  containing struct's `ConstShare` impl.

### Specifics for diagnostics-context
- `MapStorage<K, V>` type — internal to `std.containers`,
  potentially a frozen-twin of `HashMapCore`.
- `ReadOnlyMap<K, V>` type at `std.containers.ReadOnlyMap`.
- `ReadOnlyMap<K, V>` `implement ConstShare` — making the type
  share-able under the value-like contract.
- `HashMap.freeze()` method consuming the map and returning
  `ReadOnlyMap<K, V>`.  HashMap → MapStorage layout transfer
  (same RawBuffer types → no copy; ownership transfers via
  `move self`).
- Iterator types (`ReadOnlyMapIter<K, V>`) over the immutable
  storage — simpler than the borrowed-view iterator since no
  invalidation tracking.

---

## Phase plan (revised)

### Phase 1a — `ConstShare` capability + `ConstArc<T>` backing primitive (~5–7 days)

**Deliverable:** `ConstShare` trait in `stdlib/std/core/shareable.drift`
+ `ConstArc<T>` backing primitive (runtime + stdlib type) +
type-checker support so `T: ConstShare` types get value-like-share
semantics.

Out of scope at this phase: any user-facing migration (no String
migration, no JsonHandle migration, no ReadOnlyMap).  Pure
capability + primitive landing, validated against synthetic tests
(value-like-share, mutation rejection, threading).

**Why first:** the user-facing center of the design is
`ConstShare`; users interact with the trait, not with `ConstArc`.
Settling the trait shape with a concrete primitive (`ConstArc`)
in hand validates that the contract is implementable.  No real
user (ReadOnlyMap, String) commits to `ConstShare` until its shape
is settled.

**Possible coordination:** if the String normalization track
(`work/string-ownership/plan.md`) wants to land Phase 1a under its
banner, this phase folds in.  Either banner works; coordinate.

### Phase 1b — `ReadOnlyMap<K, V>` owned-immutable + `ConstShare` impl (~3 days)

**Deliverable:** `ReadOnlyMap<K, V>` + `HashMap.freeze()` in
stdlib; `implement ConstShare for ReadOnlyMap<K, V>`.  Standalone
tests (not Error-integrated): construction via `freeze()`,
iteration via `entries()`, `ConstShare`-driven value-like-share
via `var b = a;` (no `.clone()`).  Memcheck for
freeze / share-and-drop / iteration.

Built on Phase 1a's `ConstShare` + `ConstArc`.

### Phase 2 — `Error.attrs` re-typed to owned `ReadOnlyMap<String, DV>`

(Same as the prior Phase 2; uses owned ReadOnlyMap instead of
borrowed `&ReadOnlyMap`.  `e.attrs` is now an owned value of type
`ReadOnlyMap<String, DV>` carried by the Error.)

### Phase 3+ — captures unwind-stack, log integration, etc.

(Largely same as prior phases; downstream of Phase 2.)

---

## Decisions to revisit at Phase 1a kickoff

1. **`ConstShare` trait shape.**  Marker trait, or one with a
   default-impl method?  Whether to expose the implicit-share
   semantic via the existing Drift Copy mechanism or a new
   share-via-trait path.
2. **`ConstArc<T>` placement.** `std.core.const_arc` vs
   `std.concurrent.const_arc`.  Interaction with existing
   `concurrent.Arc<T>` (which is `Share`-bearing, not
   `ConstShare`-bearing).
3. **`freeze()` cost.**  In-place layout transfer (cheap) vs
   copy-then-finalize (safe, avoids HashMap-internal padding /
   alignment surprises).
4. **B-parameterization.**  Drop default-only restriction, or
   keep for Phase 1 simplicity?  Lean: default-only.
5. **Coordination with String track.**  Run Phase 1a under this
   plan's banner, or migrate it to
   `work/string-ownership/plan.md` as the load-bearing
   prerequisite there?  Either works; coordinate with whoever
   takes the work.
6. **Mutation semantics for `T: ConstShare` `&mut` borrow.**
   The trait's contract says immutable; the type-checker should
   reject `&mut self` on a `ConstShare`-bearing value.  Decide
   whether this is a hard error or whether interior-mutability
   patterns (e.g., refcount field on the backing) are
   special-cased.  Lean: hard error at user level; backing-side
   atomic refcount is internal (not user-observable).

---

## Pre-revision Phase 1 description (preserved for cross-reference)

The previously-attempted borrowed-view shape, including the failure
mode that motivated R2's investigation, is preserved below for
historical context.  This shape is NOT the current path.

<details>
<summary>(click to expand) prior borrowed-view Phase 1 description</summary>

**Deliverable:** new stdlib type at `std.core` (per Phase 0 lock),
no Error integration yet.

- `stdlib/std/core/read_only_map.drift` — type definition, surface
  API, doc comments per §Architectural shape.
- Compiler: typedef recognition only (no special-case lowering;
  implemented as an ordinary stdlib type with internal handle).
- Runtime exports: helpers for iterator-init/next/drop where the
  Drift-side implementation can't construct them purely.
- Standalone tests:
  - Construct a `containers.HashMap`, get a `ReadOnlyMap` view of
    it (construction-surface settled in this phase per open
    question 6 above).
  - Iterate via `.entries()`, count matches `.len()`.
  - **Borrow-check probe (HARD GATE)**: storing a `MapItemRef<K, V>`
    past the source's scope must be rejected by the borrow-checker.
    See §Stop-and-escalate triggers for the rule on what to do if
    this fails — owned-copy workarounds are forbidden without
    explicit API-contract revision and user signoff.
  - Memcheck: iteration over a HashMap of `(String, DV)` — no leak.

**Why first:** validates the type's shape (and its borrow story)
against in-source users (test fixtures) before Error integration
commits to it.  The hard-gate borrow probe is the single most
important Phase 1 deliverable — if iterators can't be borrow-bounded
to the source, every later phase's design assumption breaks.

**Exit gate:**

- Type compiles, all standalone tests pass, memcheck clean.
- Borrow-check probe (HARD GATE) confirms iterator and `MapItemRef`
  lifetimes are correctly bounded to the source map / source Error
  reference.  If the probe fails: stop, escalate per §Stop-and-
  escalate triggers — do NOT proceed to Phase 2.

</details>

### Phase 2 — `Error.attrs` re-typing (~2 days)

**Deliverable:** `e.attrs` returns
`&ReadOnlyMap<String, DiagnosticValue>`.

- Type-checker (`type_checker.py:8493-8501`): replace "attrs must
  be indexed" with resolution to the new view type.
- Type-checker: `e.attrs["k"]` continues lowered through the same
  DV-lookup path; sugar preserved per S1.
- HIR→MIR: emit ops to construct the `ReadOnlyMap` view from the
  Error's internal attrs storage. May reuse existing
  `M.ErrorAttrIndexDV` for the bracket path; new MIR op (or runtime
  helper) for the view-construction path.
- Runtime: `drift_error_attrs_view(error) -> ReadOnlyMap` helper.
- Mutation rejection per S6.

**Regression coverage:**

| Test | Asserts |
|---|---|
| `test_logger_takes_whole_attrs_map` | Acceptance criterion 1 — primary regression matching the bookkeeper case. |
| `test_attrs_iteration_visits_all_entries` | Acceptance criterion 2 — `.entries()` covers every entry; count matches `.len()`. |
| `test_attrs_index_still_works` | Acceptance criterion 3 — sugar preservation. |
| `test_attrs_index_missing_returns_dv_missing` | Acceptance criterion 7 (attrs side) — missing key non-throwing. |
| `test_attrs_mutation_rejected_at_typecheck` | Acceptance criterion 4 — mutation rejected. |
| `test_attrs_view_lifetime_bounded_to_error` | S3 — borrow-checker enforcement. |
| `test_attrs_iteration_memcheck_clean` | Memcheck under `lang/tests/memcheck/` — DV/String iteration drops cleanly. |

**Exit gate:** all tests pass; existing 0.31.x indexing tests
still green.

### Phase 3 — cross-frame `^capture` propagation (~1 day)

**Deliverable:** `_emit_captured_locals` called at all (b)-class
propagation sites in `hir_to_mir.py` so transit frames contribute
their `^vars` to the in-flight Error.

- Audit: enumerate every `_propagate_error` call site, classify
  (a)-already-covered vs (b)-needs-emit.
- For each (b) site, insert `_emit_captured_locals(err_val)`
  immediately before `_propagate_error(err_val)`.
- HRethrow path also gets the emit before its propagate.

**Regression coverage** (single-source; multi-frame chain):

| Test | Asserts |
|---|---|
| `test_caught_error_after_unwind_has_full_capture_stack` | Acceptance criterion 5 + 6 — `inner ^step / middle ^task_name / outer ^wo_id` chain; caught Error's `e.captures` contains all three frames with correct keys. |
| `test_rethrow_attaches_rethrowing_frame_captures` | Rethrow inside a catch arm adds the rethrowing frame's `^vars`. |
| `test_no_capture_decls_no_emit` | Frame with NO `^var` declarations transits an error: no extra entry, no crash. |
| `test_partial_chain_some_frames_have_captures` | Mixed: some frames have `^var`, others don't. Caught Error has only the contributing frames. |
| `test_recursion_last_write_wins` | S4 — recursive frame, last-write-wins. |

**Exit gate:** all tests pass; HThrow path unchanged (no double-emit
verified by the throw-only-innermost-frame baseline test).

### Phase 4 — `Error.captures` re-typing (~2 days)

**Deliverable:** `e.captures` returns nested `ReadOnlyMap`.

- Parallel changes to Phase 2's `attrs` work, but for the nested
  shape.
- Phase 3's expanded captures-map content is now iterable through
  the view.
- Runtime: `drift_error_captures_view(error) -> ReadOnlyMap<...>`
  helper; nested view construction.

**Regression coverage:**

| Test | Asserts |
|---|---|
| `test_logger_takes_whole_captures_map` | Acceptance criterion 1+2 (captures side) — iterate frames, iterate per-frame entries. |
| `test_captures_index_still_works` | Acceptance criterion 3 — sugar preservation, double-index. |
| `test_captures_missing_frame_returns_empty_view` | Acceptance criterion 7 — non-throwing. |
| `test_captures_mutation_rejected_at_typecheck` | Acceptance criterion 4 — captures side. |
| `test_captures_view_lifetime_bounded_to_error` | S3 — captures side. |
| `test_full_diagnostic_context_round_trip` | Combined: Error with attrs + multi-frame captures, log via `logger.error(&e)`, parse log output, assert all data present. |
| `test_captures_iteration_memcheck_clean` | Memcheck — multi-frame DV/String iteration drops cleanly. |

**Exit gate:** all tests pass; existing index-only captures tests
still green.

### Phase 5 — `std.log` integration (~1 day)

**Deliverable:** the two new logger overloads
(`error(msg, &Error)` and `error_attrs(msg, &ReadOnlyMap<...>)`).

- `stdlib/std/log/log.drift` — new methods on the `Logger` type (or
  whatever the existing logger surface is).
- Internal: log records get a structured representation of the
  diagnostic context (event, attrs map serialized, captures map
  serialized) per the existing log-record schema.

**Regression coverage:**

| Test | Asserts |
|---|---|
| `test_logger_error_with_full_error_context` | `logger.error("...", &e)` produces a log record containing event + all attrs + all transit-frame captures. |
| `test_logger_error_attrs_with_readonly_map` | `logger.error_attrs("...", &m)` works for any ReadOnlyMap, not just `e.attrs`. |
| `test_logger_error_no_capture_or_rethrow_needed` | Acceptance criterion 8 — a function that just declares `^var` and lets the exception propagate produces a logged record with that capture. **No catch/rethrow in user code.** |

**Exit gate:** logger surface compiles, log records contain the
expected structure, downstream packages using `std.log` rebuild
cleanly.

### Phase 6 — verification + downstream (~1 day)

- Full driver / stage1 / checker / packages / memcheck suites green.
- Build representative downstream packages (web-rest, net.tls if
  available) — confirm no breakage of existing
  `e.attrs["k"]` / `e.captures["fr"]["k"]` indexing.
- Bookkeeper team's repro project verified end-to-end:
  - `logger.error("handler-exception", e.attrs)` compiles.
  - `^wo_id` declared in `handle_submit` is visible when
    `_extract_callback` throws, even without `handle_submit`
    catching/rethrowing (acceptance criterion 8).
  - Full diagnostic context appears in the produced log record.
- ABI bump landed (locked YES per Phase 0); `test_abi_version_stamp.py`
  mismatch regression updated to assert the new stamp.

**Exit gate:** all downstream consumers green; bookkeeper verifies;
all acceptance criteria 1-14 met.

**Total: ~9-10 days across 6 phases.** Single coordinated release as
0.31.41.

---

## What blocks unification today

These mirror the open-question audit but are listed here so the
phase plan's risks are visible up front:

### (a) Borrow-checker iterator lifetime

`ReadOnlyMap.entries()` returns an iterator; iterator items borrow
from the underlying map (transitively from the Error). Drift's
borrow-checker must correctly bound the iterator's lifetime so it
can't outlive the Error.

**Risk:** if the borrow-checker has a gap for this pattern, the
fix may be in borrow-checker territory rather than this track.

**Mitigation:** Phase 1's standalone-ReadOnlyMap probe test exercises
this lifetime pattern *before* Error integration commits to it.

### (b) Per-frame storage shape

`Error.captures` today is a flat hash-of-hash storage. Iteration
over the outer (frame) layer requires exposing it as a
`ReadOnlyMap<String, ReadOnlyMap<String, DV>>`. The inner views
must each be views into the same Error's storage (cheap, no copy).

**Risk:** if the runtime's per-frame storage isn't easily
view-exposable, may require a runtime-side refactor.

**Mitigation:** Phase 4 audits before commit; if surgery needed,
that becomes its own sub-phase.

### (c) Q3's `_emit_captured_locals` idempotency

Adding emits at multiple `_propagate_error` sites must not cause an
Error to be double-recorded for a single frame. Currently each
function's `_active_captured_locals` is keyed by binding_id; the
emit writes per-binding. If a path hits two emit sites for the
same Error in the same function (which shouldn't happen but the
audit must verify), last-write-wins per S4.

**Risk:** an unanticipated control-flow path that triggers double
emit.

**Mitigation:** Phase 3 audits. If found, the right answer is to
fix the control-flow oversight, not to make the emit
idempotent.

### (d) ABI bump

New runtime helpers (`drift_error_attrs_view`,
`drift_error_captures_view`, `drift_read_only_map_iter_*`) cross
the compiler/runtime boundary. Per AGENTS.md §"Boundary Contract
Guardrails", an ABI bump is required.

**Risk:** none specific to this track; just the standard
downstream re-emission cost.

**Mitigation:** ABI bump bundled with the 0.31.41 release; standard
mismatch regression updated.

### (e) Diagnostic preservation under HIR/MIR changes

Phase 2 and 4 reshape how `e.attrs` / `e.captures` lower. Existing
tests grep for specific diagnostic strings; some may shift. Per
S(3) of the prior unified-lambda plan, preserve `(code, severity,
span_class)` not byte-for-byte text.

**Mitigation:** standard test-suite green-gate at every phase.

---

## Out of scope (will not do in this track)

- **Mutation API for `e.attrs` / `e.captures`** post-construction.
  Read-only by design. A separate `Error::with_attr(...)` builder
  API may be designed later if user demand surfaces.
- **Sorted iteration order.** Unspecified per S2.
- **Non-String keys.** `e.attrs` and `e.captures` keys remain
  `String`. The generic `ReadOnlyMap<K, V>` is general but the
  exception-side use is fixed.
- **`DiagnosticValue` shape changes.** DV stays as today.
- **`^capture` semantic changes** beyond unwind-time stacking. The
  `^var` declaration syntax, the per-frame keying, the
  function-symbol form — all unchanged.
- **Cross-package serialization of ReadOnlyMap.** Not serialized at
  package boundaries; it's a runtime view, not a value type.
- **Client-facing exception payload.** Diagnostic context stays
  server/log only by default.
- **Per-recursion-level captures.** Last-write-wins per S4; users
  use distinct `^var` names if they need recursion-level
  attribution.
- **Filtering / sampling** of high-fan-out captures. If perf becomes
  a concern, that's a follow-on.

---

## Open questions — Phase 0 dispositions

The five user-locked decisions below close the major design surface
for Phases 1-6.  Two implementation-detail items remain open and are
deferred to their respective phases.

### Locked — user dispositions (2026-04-30)

1. **Stdlib placement of `ReadOnlyMap<K, V>`.**
   **DECIDED: `std.core`.**
   Load-bearing for `Error`, `DiagnosticValue`, and logging.
   `containers.HashMap.read_only()` (Phase 1) returns the
   `core.ReadOnlyMap<K, V>` type — `containers` does not get its
   own ReadOnlyMap.

2. **`e.captures` outer iteration method name.**
   **DECIDED: `.entries()`** — uniform map API across attrs and
   captures.  The value type (`ReadOnlyMap<String, DV>`)
   self-documents the per-frame structure.

3. **`std.log` overload set.**
   **DECIDED: ship both.**
   - `logger.error(msg, &Error)` — canonical boundary-catch form.
   - `logger.error_attrs(msg, &ReadOnlyMap<String, DV>)` — useful
     for attrs-only cases (passing the attrs view directly).

4. **Generic `ReadOnlyMap[]` indexing semantics.**
   **DECIDED: no generic bracket-indexing in v1.**
   Generic `ReadOnlyMap<K, V>` exposes `.get`, `.entries`, `.len`,
   `.contains`.  Bracket indexing (`m[key]`) is a per-type
   compatibility sugar for `Error.attrs` and `Error.captures` only,
   lowered through the existing paths.  Generic indexing as an
   auto-`or_throw` lowering is a broader language rule that doesn't
   belong in this feature; deferred.

5. **ABI bump.**
   **DECIDED: locked YES.**
   Bundled with 0.31.41; `test_abi_version_stamp.py` mismatch
   regression updated alongside.

### Deferred — implementation-detail items (resolved in their phase)

6. **`ReadOnlyMap` construction surface.** Whether
   `core.ReadOnlyMap<K, V>` has a public constructor taking
   `&HashMap<K, V>` directly, or only via
   `HashMap.read_only(self: &Self) -> ReadOnlyMap<K, V>` instance
   method, settled in **Phase 1**.  Lean toward the instance method
   approach (matches "anyone can iterate user-side maps" from the
   user's framing); Phase 1's standalone-iteration tests pin
   whichever shape ships.

7. **Empty-view sentinel for missing frame.** Static-allocated
   `ReadOnlyMap` constant vs runtime-constructed on demand —
   settled in **Phase 4**'s captures-view implementation.  Lean
   toward static-allocated singleton (zero per-access alloc).

8. **HIR rewrite vs runtime resolution for `e.attrs["k"]` sugar
   path.** Whether the existing lowering stays a special-case HIR
   shape or rewrites at HIR time to
   `e.attrs.get("k").unwrap_or_missing()` — settled in **Phase 2**.
   Per Edit 3 (locked indexing semantics), the existing lowering
   path **must be preserved verbatim** for the compat-sugar; the
   choice is purely about whether to architecturally clean it up
   inside that constraint.  Lean toward (ii) keep-special-case
   for minimum diff and exact behavior preservation.

---

## Stop-and-escalate triggers

Halt and consult before proceeding if any of these fire:

- **Phase 1's borrow-check probe fails — HARD GATE.**  If borrowed
  iterator / view lifetimes don't work cleanly under the
  borrow-checker, **stop**.  Do NOT paper over with owned-copy
  workarounds (e.g., reshaping `MapItemRef<K, V>` to carry owned
  `K` / `V` clones, or making `entries()` return owned iterators).
  Such workarounds would silently change the API contract from
  "view borrows from source, lifetime bounded" to "view materializes
  copies" — invisible to user code at first but with real
  perf/refcount consequences (DV is Copy but String values would be
  cloned per iteration).  The acceptable outcomes when the probe
  fails are:
  1. Fix the borrow-checker gap in a separate pre-req patch, then
     resume this track.
  2. Rescope the API contract explicitly (e.g., document
     "iteration produces owned values; cheap for DV, refcount for
     String") via a plan revision and user signoff.
  Quietly defaulting to (2) without an explicit contract change
  is forbidden.
- **Phase 2 breaks `e.attrs["k"]` semantics for any existing
  test**: sugar preservation per S1 is mandatory; if it slips,
  fix before proceeding.
- **Phase 3 audit reveals a control-flow path that double-emits
  captures**: fix the path, not the emit. The emit is intentionally
  not idempotent (last-write-wins for legitimate recursive cases
  per S4); silently making it idempotent would mask real bugs.
- **Phase 4 reveals runtime storage can't support efficient
  per-frame view exposure**: coordinate with runtime team; may
  require a runtime-side refactor as its own pre-req sub-phase.
- **Memcheck regression in any phase**: borrowed iterators are a
  classic UAF source. The memcheck gate must catch this; if a
  regression slips, fix before continuing.
- **`.dmp` package determinism breaks** for any existing package:
  the runtime helpers exposed here cross the compiler/runtime
  boundary, but the package serialization path doesn't depend on
  them. If determinism breaks, the lowering is doing something
  unintended; investigate.

---

## Bundling rationale (why one feature, not two)

The user's framing is the load-bearing reason: **one diagnostics
context channel, one mental model**. Separate landings would mean:

- Q3 alone delivers per-frame `^vars` — but they're still
  index-only-accessible. Users can't iterate or pass them whole.
- Q1 alone makes `e.attrs` iterable — but `e.captures` is still
  under-covered (only the throwing frame). Users iterating
  `e.captures` would see a confusing single-frame view.

Together, the user gets the complete diagnostic story: every
attached value, from every transited frame, accessible through one
uniform read-only map shape. Acceptance criterion 8 ("no app-side
catch/rethrow needed solely to attach breadcrumbs") only clears
when both pieces land — without unwind-stack, breadcrumbs from
deep callees miss outer frames; without iterable views, "log
everything" is impossible.

ABI bump amortizes once. Downstream re-emission disrupts once.
Single release note, single mental model shift, single set of
docs updates.

---

## Notes on what this plan is NOT

- **Not** a green light to start Phases 1-6.  Phase 0 is greenlit
  (resolve open-question dispositions, update plan with locked
  decisions, then send for signoff).  Phases 1-6 begin only after
  Phase 0's revised plan is signed off.
- **Not** a workaround for any one bug. Combines the user's two
  stated direction items into one coordinated track.
- **Not** mutation-API work. Read-only views; mutation deferred to
  a separate (potential future) track.
- **Not** lambda-architecture work. Distinct from
  `work/unified-lambda-as-fn/plan.md`.
- **Not** under source/stdlib/tests/tooling. `work/` is ephemeral;
  durable artifacts (stdlib type, runtime exports, regression
  tests, language ref entries) move to their proper homes as
  deliverables of Phases 1-5.

---

## Supersedes

- `work/exception-attrs-readonly-map/plan.md` — Q1 standalone plan.
  Folded into this unified plan.
- `work/exception-capture-unwind-stack/plan.md` — Q3 standalone
  plan. Folded into this unified plan.

Both prior plans should remain in `work/` for cross-reference but
are not the authoritative implementation plan; this file is.
