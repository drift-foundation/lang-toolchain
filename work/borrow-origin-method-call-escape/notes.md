# LANGUAGE_BUG — interprocedural borrowed-aggregate origin escape via method-call auto-borrow

**Status:** filed 2026-04-30, **blocking** the
exception-diagnostics-context track Phase 1
(`work/exception-diagnostics-context/plan.md`).

**Classification:** LANGUAGE_BUG per AGENTS.md compiler bug policy.

**Originator:** found during Phase 1 hard-gate validation of
`containers.ReadOnlyMap<K, V, B>` (see plan).  The hard gate's
central invariant — borrowed view/iterator lifetimes are expressible
cleanly without owned-copy fallback — was supposed to hold across
function boundaries.  It does not for one specific shape.

---

## Symptom

Drift's MVP escape rule (`borrowed aggregate return must derive from
a reference parameter`) correctly rejects explicit-borrow forms:

```drift
pub struct View<T> { source: &Container<T> }

fn make_dangling_via_free_fn() -> View<T> {
    var c = Container<T>(...);
    return view_of(&c);                 // rejected ✓
}

fn make_dangling_via_field_init() -> View<T> {
    var c = Container<T>(...);
    return View<type T>(source = &c);   // rejected ✓
}
```

But the *method-call* form (where the receiver is auto-borrowed)
escapes without diagnostic:

```drift
implement<T> Container<T> {
    pub fn view(self: &Container<T>) -> View<T> {
        return View<type T>(source = self);
    }
}

fn make_dangling_via_method() -> View<T> {
    var c = Container<T>(...);
    return c.view();                    // ACCEPTED — BUG
}
```

**Confirmed runtime UAF.**  Compiling `make_dangling_via_method` and
running the result under valgrind:

```
==N== HEAP SUMMARY:
==N==     in use at exit: 0 bytes in 0 blocks
==N==   total heap usage: ... allocs, ... frees, ... bytes allocated
==N== ERROR SUMMARY: 1 errors from 1 contexts (suppressed: 0 from 0)
```

The returned view dereferences the dropped local's storage.

---

## Diagnosis

The MVP escape rule fires within the *current* function based on
locally-visible borrow shape.  When the borrow source crosses a call
boundary, the rule sees:

- **Inner function (`view`)**: `self: &Container<T>` — a parameter,
  rule passes.  `View(source = self)` derives from a reference
  parameter.  Legitimately allowed.
- **Outer function (`make_dangling_via_method`)**: gets `View<T>` back
  from `c.view()`.  Doesn't trace the inner function's `self` back to
  the caller-side `&c` auto-borrow.  Treats the return value as a
  black-box value.

The rule is intraprocedural.  The explicit-borrow forms work because
the `&c` operator is *literally present* in the offending function's
body — local-origin trivially visible at the rule's evaluation site.
The method-call form launders the local's identity through:

1. Caller-side auto-borrow `&c` (synthesized at the
   method-call site, possibly not surfaced in the borrow-tracking
   pass the same way an explicit `&c` is).
2. Inner method's `self: &Container<T>` parameter (looks fine to the
   rule when checking the inner function in isolation).
3. Returned `View<T>` aggregate (the rule, evaluated in the outer
   function, sees an opaque return value, not a borrow-of-local).

This is interprocedural lifetime tracking, which Drift's MVP escape
rule explicitly does not do.  Closing this gap requires either:

- **(a) Origin propagation across method-return-of-borrowed-aggregate**
  — track that a method returning a borrowed-aggregate built from
  `self` propagates the receiver's origin to the caller; the caller's
  escape rule must then check that origin against the surrounding
  scope.  Genuine interprocedural lifetime tracking, scoped to the
  borrowed-aggregate-return shape.

- **(b) Method-receiver auto-borrow recognition**
  — flag method calls on locals where the method returns a
  borrowed-aggregate as a direct local-origin borrow at the call site.
  Lighter-weight; requires the type-checker / lowering to identify
  this shape and synthesize the equivalent of an explicit `&local` for
  the rule's eyes.

Option (b) is closer to the current rule's intraprocedural shape and
likely the lower-cost fix.  Either way, the gap is in the
borrow-checker / escape-tracking pass, not in the
exception-diagnostics-context feature.

---

## Regression test

`lang/tests/driver/test_borrow_origin_method_call_escape.py` (3 tests):

- **`test_method_call_returns_view_of_local_currently_compiles`** —
  pre-fix BUG carrier.  Currently asserts `rc == 0` (the bug allows
  the program to compile cleanly).  **At fix time, INVERT the
  assertion** to `rc != 0` + assert the
  `borrowed aggregate return` diagnostic fires.  The docstring
  instructs the implementer.

- **`test_explicit_borrow_return_of_local_correctly_rejected`** —
  control: free-function `view_of(&c)` form.  Must keep rejecting
  pre-fix and post-fix.

- **`test_explicit_field_init_borrow_of_local_correctly_rejected`** —
  control: `View(source = &c)` direct-field-init form.  Must keep
  rejecting pre-fix and post-fix.

3/3 pass on current main.

---

## Why this blocks Phase 1 of exception-diagnostics-context

The Phase 1 hard gate stated that the borrow / lifetime invariants
must be expressible cleanly without owned-copy fallback.  The
existing rule covers explicit-borrow forms but leaves the
method-call shape open.  Until that gap is closed, **no public
construction API for borrowed-view types is sound** — any future
ergonomic method like `m.read_only()` re-opens the hole, and the
Phase 1 design specifically wanted that ergonomic form to be
available alongside the free-function constructor.

Per the user's stop instruction
(`work/exception-diagnostics-context/plan.md` updated to reflect
the stop):

> "Do not expose `ReadOnlyMap` construction API publicly until
> this is fixed or until we intentionally choose an
> owned/opaque/runtime-backed representation that does not borrow
> from a local HashMap."

The Phase 1 staged work (type definitions in `array.drift`, public
exports of `ReadOnlyMap` / `ReadOnlyMapIter` / `MapItemRef` /
`read_only_map_view` / `HashMap.read_only()`) was reverted at
2026-04-30 stop.  The bug-pinning regression at
`test_borrow_origin_method_call_escape.py` is preserved as a
mainline test (3 passing) so any future weakening of the explicit
forms is caught and so the bug-fix work has the test ready to flip.

---

## Resolution paths

### Path A — fix the compiler gap, then resume Phase 1

Pursue option (a) or (b) from §Diagnosis.  Estimated scope unknown
without deeper investigation of the existing escape-rule pass.
Likely 2–5 days for option (b) (surface-level recognition of
method-call-returning-borrowed-aggregate at the caller site); option
(a) is genuine interprocedural work, scope substantially larger.

When the fix lands, flip
`test_method_call_returns_view_of_local_currently_compiles`'s
assertion from `rc == 0` to `rc != 0 + diagnostic`, then resume
exception-diagnostics-context Phase 1 from where it stopped.

### Path B — redesign `ReadOnlyMap` as owned/opaque/runtime-backed

Drop the `&HashMap` field.  Store an opaque pointer + length, with
methods backed by runtime helpers that the HashMap-side construction
populates.  No borrow story to enforce; lifetime is owner-style
(refcount or move).

Larger architectural commitment — same scope as Phase 0's rejected
"Option B."  May still be the right long-term shape but is its own
design discussion, not a Phase 1 deliverable.

### Path C — accept reduced API in Phase 1

Ship `ReadOnlyMap` with **only** the explicit-borrow free-function
constructor (`read_only_map_view(&m)`); do **not** provide
`HashMap.read_only()` method.  The free-function path correctly
enforces the borrow rule.

User explicitly rejected this on 2026-04-30: "Do not expose
`ReadOnlyMap` construction API publicly until this is fixed or until
we intentionally choose an owned/opaque/runtime-backed representation."
Reasoning: even if the free-function path is sound today, exposing
*any* construction API leaves the door open for future ergonomic
methods (or compiler refactors) to re-open the hole.

So **Path C is not on the table**.

---

## Triggers for revisiting

The exception-diagnostics-context track resumes when **either**:

1. Path A's compiler fix lands.
   `test_borrow_origin_method_call_escape.py::test_method_call_returns_view_of_local_currently_compiles`
   inverts to a passing post-fix assertion.

2. Path B's owned/opaque redesign is signed off as a separate
   architectural decision.  The diagnostics-context track's
   ReadOnlyMap reshape happens then.

Until one of these, Phase 1 stays paused.  The bug-pinning regression
is the canary.

---

## Related

- `work/exception-diagnostics-context/plan.md` — the parent track
  paused by this bug.  Phase 1 status updated to "stopped pending
  this LANGUAGE_BUG."
- `work/catch-arm-scope-misresolution/notes.md` — separate
  LANGUAGE_BUG, also blocking nothing in this track but filed at
  the same time.
- AGENTS.md § "Compiler/Runtime Bug Policy" — regression-first
  discipline applied here.
