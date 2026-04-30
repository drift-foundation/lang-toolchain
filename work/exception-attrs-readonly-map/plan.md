# Exception Diagnostic Maps — Read-Only Generic Stdlib Views — Plan

**Status:** planning / pre-branch. Implementation request, but
multi-step and design-heavy enough that the plan is the alignment
artifact.

**Working directory:** `work/exception-attrs-readonly-map/`
(ephemeral). Durable artifacts (stdlib type, runtime exports, language
ref entries, regression suite) move to `stdlib/` / `lang/...` /
`docs/` as deliverables of their respective phases.

**Branch (when started):** suggested name
`feature/exception-diag-maps`.

**Target version:** 0.31.41 or later (depends on whether bundled with
Q3 unwind-stack — see §Bundling decision).

---

## Goal

Make exception diagnostic payloads **first-class iterable read-only
maps** so boundary catch sites can log "everything attached to this
exception" without knowing the schema in advance.

```drift
} catch e {
    // Today: rejected.
    // Target: works.
    a.logger.error("handler-exception", e.attrs);

    // Or iterate explicitly.
    for entry in e.attrs.entries() {
        a.logger.error_attr(entry.key, entry.value);
    }

    // Including ^capture values across frames (Q3 unwind stack).
    for frame_entry in e.captures.frames() {
        for entry in frame_entry.value.entries() {
            a.logger.error_capture(frame_entry.key, entry.key, entry.value);
        }
    }

    rethrow;
}
```

`e.attrs["key"]` continues to work, sugar over the same view.

The map type used is **generic stdlib**, reusable by any user code
that wants a read-only `Map<K, V>` view over an underlying map.

---

## TL;DR

- `Error.attrs` becomes a **`&ReadOnlyMap<String, DiagnosticValue>`**
  view rather than an index-only pseudo-field.
- `Error.captures` becomes a **`&ReadOnlyMap<String, ReadOnlyMap<String,
  DiagnosticValue>>`** view (frame-keyed, per-frame map of `^var`
  values).
- A **new generic stdlib type** `core.ReadOnlyMap<K, V>` (or
  `containers.ReadOnlyMap<K, V>` — see §Open questions) exposes:
  - `get(key: &K) -> Optional<&V>` (or equivalent borrow shape)
  - bracket-indexing sugar `m[key]`
  - `entries() -> SinglePassIterator<MapItemRef<K, V>>`
  - `len() -> Int`
  - `contains(key: &K) -> Bool`
  - **No mutation surface.** `set` / `insert` / `remove` not on the
    type.
- Existing `e.attrs["key"]` and `e.captures["frame"]["key"]` indexing
  paths lower into `ReadOnlyMap.get` / `[...]`. Sugar preserved.
- The unwind-stack feature (Q3) is **bundle candidate** — both
  reshape `e.captures`, both want to land in a coordinated way.

---

## Decision summary (2026-04-30)

| Question | Decision |
|---|---|
| New language surface? | **One stdlib type**: `ReadOnlyMap<K, V>`. Reusable by user code; not exception-specific. |
| Whole-map access supported? | **Yes.** `e.attrs` and `e.captures` return borrowed views. |
| Mutation from catch code? | **No.** `ReadOnlyMap` has no mutation methods. |
| Indexing sugar preserved? | **Yes.** `e.attrs["key"]` desugars to `e.attrs.get("key").or_throw()` or equivalent. |
| `^capture` values iterable? | **Yes.** `e.captures` returns a frame-keyed `ReadOnlyMap<String, ReadOnlyMap<String, DiagnosticValue>>`. Iteration walks frames then per-frame entries. |
| Unify `e.attrs` and `e.captures` into one flat map? | **No.** Frame-keying is informationally valuable (lost on flatten). Both are separate views; both iterate through the same `ReadOnlyMap` type. |
| Bundle with Q3 unwind-stack? | **Lean yes** — both reshape `e.captures`. See §Bundling decision. |
| ABI bump? | **Likely yes** — runtime helpers for the iteration surface (`drift_error_attrs_iter`, `drift_error_captures_iter`) are new compiler/runtime boundary additions. Confirm during Phase 2. |

---

## Architectural shape

### `core.ReadOnlyMap<K, V>` — new stdlib type

A read-only **view** over an underlying map. Does not own the data;
holds an internal `&` reference to the source. Cheap to copy (Copy
trait); semantically borrow-like.

```drift
pub struct ReadOnlyMap<K, V> {
    // Internal: opaque handle to the underlying map storage.
    // Lowered as a fat pointer / reference — implementation-defined.
}

implement<K, V> ReadOnlyMap<K, V> require K is hash.Hash, K is cmp.Equatable {
    pub fn get(self: &Self, key: &K) nothrow -> Optional<&V>;
    pub fn contains(self: &Self, key: &K) nothrow -> Bool;
    pub fn len(self: &Self) nothrow -> Int;
    pub fn is_empty(self: &Self) nothrow -> Bool;
    pub fn entries(self: &Self) nothrow -> ReadOnlyMapIter<K, V>;
}

pub struct ReadOnlyMapIter<K, V> { ... }
implement<K, V> iter.SinglePassIterator<MapItemRef<K, V>> for ReadOnlyMapIter<K, V> {
    pub fn next(self: &mut Self) nothrow -> Optional<MapItemRef<K, V>>;
}

pub struct MapItemRef<K, V> {
    pub key: &K,
    pub value: &V,
}
```

**Sugar — bracket indexing:**

`m[key]` desugars to `m.get(key).or_throw()` *inside `throws` /
`try {}` contexts*; outside, it's a static-time error (use `.get()`
explicitly). This matches Drift's existing auto-try contract for
Result-returning calls.

**Why a dedicated type, not just `&containers.HashMap<K, V>`:**

- Borrow ergonomics: returning `&HashMap<K, V>` from `e.attrs` works,
  but every user has to know "this is a borrow with the lifetime of
  the Error." A wrapper type makes that ownership story explicit.
- Generic API: `ReadOnlyMap<K, V>` doesn't expose HashMap's
  internals (probe-sequence, capacity, etc.) — only the read surface
  the user actually needs.
- Future flexibility: the underlying storage could change (HashMap
  today; tree map or sorted-flat for small N tomorrow) without
  changing the user-visible type.

### `Error.attrs` — re-typed return

Today: `Error.attrs` is an index-only pseudo-field, sealed by the
type-checker (`type_checker.py:8493-8501`).

Target: `Error.attrs` returns
`&ReadOnlyMap<String, DiagnosticValue>` (a borrow scoped to the
Error's lifetime).

Changes:

- Type-checker: replace the "must be indexed" diagnostic with a
  resolution to the new view type.
- Type-checker: `e.attrs["key"]` continues to work, lowered as
  `e.attrs.get("key")` (with auto-try if appropriate, OR direct
  index returning `&V` — design decision in Phase 2).
- HIR→MIR: emit ops to construct the `ReadOnlyMap` view from the
  Error's internal storage.
- Runtime: export `drift_error_attrs_view(error) -> ReadOnlyMap`
  helper.

### `Error.captures` — re-typed return

Today: `Error.captures` is double-index-only:
`e.captures["frame"]["key"]` (`type_checker.py:8502-8510`).

Target: `Error.captures` returns
`&ReadOnlyMap<String, ReadOnlyMap<String, DiagnosticValue>>` (frame-name
→ inner map of `^var` values).

`e.captures["frame"]["key"]` desugars to
`e.captures.get("frame").or_throw().get("key").or_throw()` (or the
direct-index equivalent).

`for frame_entry in e.captures.frames() { ... }` — note the
**`.frames()`** name, not `.entries()`, to make the per-frame
structure obvious at the call site. Or just `.entries()` if we want
naming uniformity. Phase 2 settles.

### Iteration model — borrowing concern

`ReadOnlyMap.entries()` returns a `SinglePassIterator` over
`MapItemRef<K, V>`. Each `MapItemRef` carries `&K` and `&V` borrowed
from the underlying map. Lifetime of the iterator must not outlive
the Error.

Drift's borrow-checker already handles this for the `&` returns
from `e.attrs` itself; the iterator inherits the same lifetime.

**Borrow-checker implication:** the user can't store
`MapItemRef<K, V>` past the catch arm without explicit copies (DV
is Copy; String is value-shareable so `entry.key` is OK to
materialize). Standard Drift borrow rules.

---

## Phase plan

### Phase 0 — design alignment + open-question resolution (~1 day, zero compiler change)

**Deliverable:** this plan, reviewed by user. Resolution of the
open questions in §Open questions below.

**Exit gate:** user signs off on:
- Stdlib type name (`core.ReadOnlyMap` vs `containers.ReadOnlyMap`).
- `e.captures` access shape (`.frames()` vs `.entries()`).
- Bracket-index semantics (auto-try vs direct-borrow).
- Bundling decision with Q3 (independent vs combined).

### Phase 1 — `core.ReadOnlyMap<K, V>` stdlib type (~2 days)

**Deliverable:** new stdlib type, library-side only. No Error
integration yet.

- `stdlib/std/<placement>/read_only_map.drift` — type definition,
  surface API, doc comments.
- Compiler: typedef recognition (the type itself is a normal struct
  with internal storage handle); no special-case lowering needed.
- Runtime: helpers for the iteration surface (e.g.,
  `drift_read_only_map_iter_init`, `_iter_next`, `_iter_drop`) if
  the implementation can't be done purely in user-side Drift.
- Standalone tests: construct a HashMap, wrap as ReadOnlyMap (via
  some test-only `.read_only()` method on HashMap, OR via `&` in
  function arg), iterate, assert.

**Why first:** validates the type's shape against an in-source user
(test fixtures) before any Error integration commits to it. If the
shape is wrong, fixing it here is cheap; fixing it after Error
integration is expensive.

**Exit gate:** type compiles, tests pass, memcheck clean.

### Phase 2 — `Error.attrs` re-typing (~2 days)

**Deliverable:** `e.attrs` returns
`&ReadOnlyMap<String, DiagnosticValue>`.

- Type-checker: remove "attrs must be indexed" diagnostic
  (`type_checker.py:8493-8501`); resolve `e.attrs` to the new view
  type.
- Type-checker: `e.attrs["key"]` indexing continues to work, lowered
  through the type's `get` / index sugar.
- HIR→MIR: lower `e.attrs` access to a runtime call constructing
  the ReadOnlyMap view.
- Runtime: `drift_error_attrs_view(error) -> ReadOnlyMap`.
- Regression coverage:
  - **NEW** `test_logger_takes_whole_attrs_map` — primary regression
    matching the bookkeeper use case (`logger.error("...", e.attrs)`).
  - **NEW** `test_attrs_iteration_visits_all_entries` — iterate via
    `.entries()`, count matches `.len()`.
  - **NEW** `test_attrs_index_still_works` — sugar preservation.
  - **NEW** `test_attrs_mutation_rejected_at_typecheck` —
    `e.attrs["k"] = ...` rejected.
  - **NEW** `test_attrs_view_lifetime_bounded_to_error` — borrow
    expiring with Error caught at borrow-check time.

**Exit gate:** all tests pass; 0.31.x existing index tests still
green.

### Phase 3 — `Error.captures` re-typing (~2 days)

**Deliverable:** `e.captures` returns nested ReadOnlyMap.

- Type-checker: parallel changes to Phase 2's `attrs` work.
- HIR→MIR + runtime: helpers for the nested access shape.
- Regression coverage:
  - **NEW** `test_logger_takes_whole_captures_map` — iterate frames,
    iterate per-frame entries.
  - **NEW** `test_captures_index_still_works` — sugar preservation.
  - **NEW** `test_captures_mutation_rejected_at_typecheck`.
  - **NEW** `test_captures_view_lifetime_bounded_to_error`.

**If bundled with Q3 unwind-stack:** Phase 3 ALSO lands the unwind
emit hooks at the propagation sites (per the Q3 plan). Combined
test fixture verifies both: `^vars` from multiple frames are present
AND iterable through the new view.

**Exit gate:** all tests pass; if bundled with Q3, the unwind-stack
regression suite from `work/exception-capture-unwind-stack/plan.md`
also passes.

### Phase 4 — verification + downstream verification (~1 day)

- Full driver / stage1 / checker / packages / memcheck suites green.
- Build representative downstream packages (web-rest, net.tls if
  available) — confirm no breakage of existing `e.attrs["k"]` /
  `e.captures["fr"]["k"]` indexing.
- Bookkeeper team's repro project verified end-to-end: their
  `logger.error("handler-exception", e.attrs)` pattern compiles and
  produces the expected log output.

**Exit gate:** all downstream consumers green; bookkeeper verifies.

**Total: ~7-8 days across 4 phases (or ~9-10 if bundled with Q3).**

---

## Bundling decision — combine with Q3 (`^capture` unwind stack)?

**Q3 plan:** `work/exception-capture-unwind-stack/plan.md`. ~2 days.
Adds emit-at-propagation hooks so transit frames contribute their
`^vars` to the in-flight Error.

**Q1 (this plan):** ~7-8 days. Reshapes `e.attrs` and `e.captures`
into iterable views.

**Why bundling makes sense:**

1. Both reshape `e.captures`. Q3 changes what's IN the captures map
   (more entries, per-frame); Q1 changes how it's accessed. Landing
   independently means Q3's regressions exercise the index-only access
   shape (`e.captures["fr"]["k"]`) — which then has to be re-pinned
   when Q1 lands.
2. ABI bump: both items likely need an ABI bump (Q3 may not, Q1
   probably does). Bundling amortizes one bump instead of two.
3. Downstream re-emission: any package consuming Error's structure
   gets one disruption instead of two.
4. Combined story for the user: "Error diagnostics are now first-class
   iterable, with full unwind-stack coverage" — single release note,
   one mental model shift.

**Why bundling might NOT make sense:**

1. Q3 is small and the team's bookkeeper case wants it sooner. Q1
   is a more invasive refactor; bundling delays Q3 by ~1 week.
2. Independent landing keeps blast radius per-patch smaller — easier
   to revert if either has unforeseen regressions.

**Recommendation:** **bundle, target 0.31.41 (or later if bundling
slips).** Q3 alone delivers per-frame ^vars but they're still
index-only-accessible; Q1 alone makes `e.attrs` iterable but
captures are still under-covered (only the throwing frame). Together,
the user gets the complete diagnostic story.

If app team has a hard need for Q3 sooner, decouple — Q3 lands first
as 0.31.41, Q1 follows as 0.31.42+.

---

## Open questions (resolve in Phase 0)

1. **Stdlib placement.** `core.ReadOnlyMap<K, V>` or
   `containers.ReadOnlyMap<K, V>`?
   - `core` makes it always-available without import (since `Error`
     itself is in core).
   - `containers` is the natural neighborhood (next to HashMap,
     TreeMap).
   - Lean toward **`core`** because Error usage is the load-bearing
     consumer and core dependency on containers is awkward.

2. **Bracket-index semantics.** `e.attrs["key"]` returns:
   - (i) `DiagnosticValue` (Copy; `or_throw` on missing) — auto-try
     compatible.
   - (ii) `&DiagnosticValue` — direct borrow; `Optional<&DV>` if
     missing-tolerant.
   - (iii) `Optional<DiagnosticValue>` — explicit-handle.
   - Existing behavior: `e.attrs["key"]` returns `DiagnosticValue`
     (option i).
   - Lean toward **(i) preserved** for backward compatibility +
     auto-try ergonomics. The new `.get(...)` method gives users
     option (iii) when they want it.

3. **`e.captures` outer-access method name.** `.frames()` (semantic)
   vs `.entries()` (uniform with attrs).
   - `.frames()` makes the per-frame structure obvious at the call
     site.
   - `.entries()` is uniform with `attrs.entries()` and lets the
     user uniformly iterate any ReadOnlyMap.
   - Lean toward **`.entries()`** for uniformity; the value type
     (`ReadOnlyMap<String, DV>`) self-documents the per-frame
     structure.

4. **Bundling with Q3.** Per §Bundling decision above.
   - Lean **yes**, but defer to user's prioritization.

5. **Lifetime / borrow shape of the view.** `e.attrs` returns
   `&ReadOnlyMap<...>` borrowed from the Error. The iterator
   `entries()` returns items borrowed from the map (and transitively
   from the Error). Borrow-checker correctly bounds iterator
   lifetime to the catch arm.
   - This needs to work cleanly with Drift's existing borrow rules.
   - **Risk:** if the borrow-checker's iterator-handling has gaps for
     this pattern, surfaces as Phase 2 implementation friction.
   - Mitigation: write a minimal iteration test in Phase 1 against
     a synthetic ReadOnlyMap before Error integration.

6. **Underlying storage shape.** Today, Error's internal attrs is
   probably some map-like structure (need to audit). For Q1 to work
   without copying, the storage must be efficiently exposable as a
   ReadOnlyMap view.
   - If the current storage is a flat array of (key, value) pairs,
     a HashMap-backed `.entries()` iterator may need to box / build
     a synthetic map. Phase 1 audit clarifies.
   - **Decision deferred to Phase 0/1 audit.**

7. **`DiagnosticValue` hashability / iteration ordering.** Map
   iteration order (`.entries()`) — defined or unspecified?
   - HashMap iteration order is unspecified in Drift today.
   - Logging use case may expect either insertion order or
     deterministic ordering.
   - **Lean toward unspecified** (matches HashMap), with the option
     of a sort-by-key helper if users care about order. Document
     explicitly.

---

## Stop-and-escalate triggers

Halt and consult before proceeding if any of these fire:

- **Phase 1's standalone ReadOnlyMap tests reveal a borrow-checker
  gap** for the iterator-lifetime pattern. Stop, fix borrow-checker
  side first or rescope the type to avoid the problematic pattern.
- **Phase 2 changes to `e.attrs` break a downstream consumer's
  index-only path.** The sugar contract (`e.attrs["k"]`) must
  preserve identically; if it doesn't, the lowering is wrong. Fix
  before continuing.
- **Phase 3 reveals that `e.captures` runtime storage doesn't
  support efficient view construction.** May need runtime-side
  refactor; coordinate with runtime team.
- **ABI bump scope creeps beyond the planned helpers.** Each
  runtime export that crosses the boundary needs explicit
  audit-and-document; uncontrolled growth indicates the design needs
  rethinking.
- **Memcheck regressions in any phase.** Borrowed iterators are a
  classic source of UAF if the borrow-checker doesn't catch
  iterator-outliving-source. Memcheck must be in the gate from
  Phase 1.

---

## Out of scope (will not do in this track)

- **Mutation API for `e.attrs`** post-construction. Read-only by
  design; if a future use case demands mutation, that's a separate
  track (likely with an explicit `attempt_attach` API gated on
  `^pre_throw` semantics).
- **Sorted iteration order.** Unspecified; users sort if they need
  to.
- **Non-String keys.** `e.attrs` keys remain `String`; same for
  `e.captures`. The generic `ReadOnlyMap<K, V>` is general but the
  exception-side use is fixed.
- **`DiagnosticValue` shape changes.** DV stays as today.
- **`^capture` lifecycle changes.** Q3's unwind-emit hooks (whether
  bundled or separate) cover the lifecycle. This track only changes
  how captures are *accessed*, not when/where they're attached.
- **Cross-package serialization of ReadOnlyMap.** Not serialized at
  package boundaries; it's a runtime view, not a value type.

---

## Acceptance criteria — full-track close-out

The track wraps when **all** of these are true:

1. `core.ReadOnlyMap<K, V>` (or final-named) exists in stdlib with
   `get`, indexing sugar, `entries`, `len`, `contains`, `is_empty`.
2. `Error.attrs` returns `&ReadOnlyMap<String, DiagnosticValue>`.
3. `Error.captures` returns
   `&ReadOnlyMap<String, ReadOnlyMap<String, DiagnosticValue>>`.
4. `e.attrs["k"]` and `e.captures["fr"]["k"]` index sugar preserved
   with identical semantics.
5. `logger.error("...", e.attrs)` (or equivalent boundary-catch
   diagnostic logging) compiles cleanly.
6. Mutation through the view rejected at type-check.
7. Borrow-checker correctly bounds iterator / entry lifetimes to
   the source Error.
8. Full driver / stage / checker / packages / memcheck suites green.
9. Downstream package re-emission (if ABI bump) verified for
   web-rest / net.tls / bookkeeper.
10. ABI version bumped (if needed) with mismatch regression updated.
11. Compiler version bumped (Phase 4 close-out).
12. `docs/effective-drift.md` (or equivalent) updated with the new
    Error diagnostic-access patterns.
13. App team's bookkeeper repro verifies end-to-end.

---

## Notes on what this plan is NOT

- **Not** a green light to start Phase 0 — that requires user
  approval of the open-question dispositions and bundling decision.
- **Not** a workaround for any one bug. Q1 is a stated direction
  the user articulated; this plan is the first implementation
  scaffolding.
- **Not** a runtime refactor. The Error's internal storage is
  audited but not redesigned unless Phase 1 reveals it can't support
  view exposure efficiently.
- **Not** a commitment to bundling with Q3. Recommended but
  deferable.
- **Not** under source/stdlib/tests/tooling. `work/` is ephemeral;
  durable artifacts (stdlib type, regression tests, language ref
  entries) move to their proper homes as deliverables of Phases 1-3.
