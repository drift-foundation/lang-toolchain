# Fix Plan — Interprocedural Borrow Origin via Method-Call Auto-Borrow

**Status:** plan, **not implementation**.  Per AGENTS.md
regression-first discipline, this document captures the fix shape
before the patch lands so the user can sign off on the approach.

**Scope target:** 0.31.41 hotfix (compiler-only; no stdlib /
runtime / API change).

**Companion notes:** `work/borrow-origin-method-call-escape/notes.md`
(symptom, diagnosis, runtime-UAF confirmation, why this blocks the
diagnostics-context track).

---

## Root-cause target

**File:** `lang/driftc/type_checker.py`.

**Function:** `_borrowed_aggregate_origins` (`:10465-10491`).

**Current behavior:**

```python
def _borrowed_aggregate_origins(expr, expected_ty):
    if not _is_borrowed_aggregate_type(expected_ty):
        return None
    if not isinstance(expr, H.HCall):           # ← bails on HMethodCall
        return None
    # ...HCall arg-by-arg origin walk...
```

When the return-value expression is an **`HMethodCall` returning a
borrowed-aggregate type** (e.g. `c.view()` where
`view(self: &Container<T>) -> View<T>`), the early bail at
`isinstance(expr, H.HCall)` returns `None` — the caller
(`_walk_returns_borrowed`'s loop at `:11060-11071`) reads `None` as
"no constraint" and skips the diagnostic.

The intra-procedural rule fires correctly for the **explicit-borrow**
forms because:

- `return view_of(&c);` — the value is an `H.HCall` (free-fn
  invocation); the HCall walker iterates the args; `&c` is an
  `HBorrow` of a local; `_return_origin(arg_expr)` returns `None`;
  the walker hits `if isinstance(arg_expr, H.HBorrow): return set()`
  → empty origins set → rule fires.
- `return View<type T>(source = &c);` — same shape, HCall ctor.

The **method-call** form skips the entire walker because
`isinstance(expr, H.HCall)` is `False` for `HMethodCall`.

---

## Fix shape

**Extend `_borrowed_aggregate_origins` to handle `HMethodCall`** by
treating the method's receiver as the borrow source.

A method `view(self: &Container<T>) -> View<T>` returning a
borrowed-aggregate is interprocedurally equivalent to a free function
`view_of(c: &Container<T>) -> View<T>` — both have one `&T`
parameter that is the only reference origin reachable in the return
value (modulo multi-`&T`-param methods, see §Edge cases below).

The receiver's auto-borrow at the call site (synthesized for
`c.view()`) IS the implicit `&c`.  So:

- If the receiver expression resolves to a **reference parameter**
  (via the existing `_return_origin` machinery), use that origin.
- If the receiver expression is a **local** (no ref-param origin),
  return `set()` — the same "empty origins" signal that the
  explicit-borrow path uses to trigger the rule's rejection.

**Concrete patch shape** (pseudocode, ~10 lines):

```python
def _borrowed_aggregate_origins(expr, expected_ty):
    if not _is_borrowed_aggregate_type(expected_ty):
        return None

    # NEW: method-call returning a borrowed aggregate.  The borrow
    # source is the receiver; auto-borrow of a local receiver is
    # equivalent to an explicit `&local`, which the rule already
    # rejects via the HBorrow path below.  Treat it the same.
    if isinstance(expr, H.HMethodCall):
        receiver_origin = _return_origin(expr.receiver)
        if receiver_origin is None:
            # Receiver is a local (not a ref-param); auto-borrow
            # would create a borrow-of-local — same shape the rule
            # already rejects for explicit `&local`.  Empty set
            # signals "no valid origin" → caller emits MVP-escape
            # diagnostic.
            return set()
        return {receiver_origin}

    if not isinstance(expr, H.HCall):
        return None
    # ... existing HCall arg-by-arg origin walk ...
```

`_return_origin` already handles the HMethodCall recursion (forward
to receiver) at `:10368-10371`, so the receiver lookup chains
through correctly even for nested method calls
(`a.b().c()` → receiver of c() is `a.b()`'s return; `_return_origin`
recurses).

---

## Edge cases to handle

### (a) Multi-`&T`-param method

```drift
implement Foo {
    pub fn make_view(self: &Foo, other: &Bar) -> View<Foo, Bar> {
        return View(left = self, right = other);
    }
}
```

`View<Foo, Bar>` borrows from both `self` and `other`.  The fix
proposed above only credits the receiver as the origin — would
miss `other`.

**Resolution:** the existing rule explicitly rejects multi-origin
borrowed-aggregate returns (`:11072-11079`:
`borrowed aggregate return must derive from a single reference
parameter`).  So the inner method body is already rejected.  The
caller of `make_view` doesn't need to track multi-origin — the
inner function fails first.

The fix's "use receiver's origin" heuristic is sound *for methods
that pass the existing rule on their own bodies* — which is the only
case that reaches the caller-side check anyway.  No additional work
needed for multi-param case.

### (b) `&mut` receiver

```drift
implement Foo {
    pub fn view_mut(self: &mut Foo) -> ViewMut<Foo> {
        return ViewMut(source = self);
    }
}

fn dangling() -> ViewMut<Foo> {
    var c = Foo(...);
    return c.view_mut();  // must reject
}
```

Same shape, `&mut` instead of `&`.  Auto-borrow of `c` for the
`&mut` receiver still lands as a local borrow.  The fix above
treats `HMethodCall` uniformly — it doesn't distinguish `&` vs
`&mut` receivers, just calls `_return_origin(receiver)`.

The existing `requires_mut_origin` check at `:11082` then enforces
that the origin parameter must be `&mut` — separately.

### (c) Method on a reference parameter (positive control)

```drift
fn ok(c: &Container<T>) -> View<T> {
    return c.view();  // must accept — receiver is ref-param
}
```

`_return_origin(c)` where `c: &Container<T>` is a parameter →
returns `c`'s binding_id (positive).  Fix returns `{origin}` →
caller's rule sees one valid origin → accepts.

This is the existing rule's intended shape.  Test must pin it.

### (d) Chained method calls

```drift
fn dangling() -> View<T> {
    var a = ...;
    return a.b().view();  // a.b() returns &T derived from local a
}
```

Receiver of `view()` is `a.b()` — an HMethodCall.  Recurses through
`_return_origin(a.b())` → forwards to `_return_origin(a)` → `a` is
a local → `None`.  Fix's `if receiver_origin is None: return set()`
fires → reject.

### (e) Builder / non-method call returning borrowed aggregate

```drift
fn dangling() -> View<T> {
    var c = ...;
    return make_view_of(&c);  // free function
}
```

Already handled by the existing HCall path; `&c` is an HBorrow of
local; rule rejects.

---

## Test plan

Tests added to
`lang/tests/driver/test_borrow_origin_method_call_escape.py`
(currently 3 tests, expanding to 5).

### Test 1 — flip the BUG-pin (existing test)

`test_method_call_returns_view_of_local_currently_compiles` →
rename to
`test_method_call_returns_view_of_local_rejected`.  Flip
`assert rc == 0` to `assert rc != 0` + assert the
`borrowed aggregate return must derive from a reference parameter`
diagnostic fires.

### Test 2 — positive control: method on `&` ref-param accepts

```drift
fn ok_through_ref_param(c: &Container<Int>) nothrow -> View<Int> {
    return c.view();
}
```

Must compile.  Pinned to ensure the fix doesn't accidentally
reject the legitimate case (the inner method returning a borrowed
aggregate from `self` IS valid when the caller's receiver is a
reference parameter).

### Test 3 — control: explicit free-fn form stays rejected (existing)

`test_explicit_borrow_return_of_local_correctly_rejected` —
unchanged.  Pinned to ensure the rule still rejects the
explicit-borrow path for the case it currently catches.

### Test 4 — control: explicit field-init form stays rejected (existing)

`test_explicit_field_init_borrow_of_local_correctly_rejected` —
unchanged.  Same control as test 3 with a different surface.

### Test 5 — NEW: `&mut` method receiver

```drift
implement<T> Container<T> {
    pub fn view_mut(self: &mut Container<T>) nothrow -> ViewMut<T> {
        return ViewMut<type T>(source = self);
    }
}

fn dangling_mut() nothrow -> ViewMut<Int> {
    var c = Container<type Int>(x = 99);
    return c.view_mut();  // must reject
}
```

Asserts: rc != 0 + diagnostic mentions `mutable references must
derive from an &mut parameter` (the existing
`requires_mut_origin` arm at `:11082`) OR the bare
`borrowed aggregate return must derive from a reference parameter`.
Either is acceptable — the receiver is a local in both cases, so
both arms are valid rejections.

### Test 6 (optional) — chained method call

```drift
fn dangling_chain() nothrow -> View<Int> {
    var a = Holder(...);
    return a.inner().view();  // a.inner() borrows from local a
}
```

Asserts rejection.  Confirms the recursion through `_return_origin`
works across multiple method calls.  Lower-priority — the
existing `_return_origin` recursion already handles this; test
exists purely as belt-and-suspenders.

---

## Verification gates

1. **All 5+ tests pass** (with test 1 flipped; tests 2-5 added).
2. **Full driver suite** (`lang/tests/driver/`) green.  No collateral
   regressions — the existing `_return_origin` machinery handles the
   recursion; the only change is `_borrowed_aggregate_origins`'s
   handling of HMethodCall.
3. **Stage1 / checker / packages suites** green — no impact
   expected, but the fix touches type-checker code so they're in
   the gate.
4. **Memcheck suite** green — no expected impact (this is a
   type-check-time rule; runtime behavior of programs that already
   compile is unchanged).
5. **Manual valgrind verify** of the BUG repro: the
   `make_dangling_via_method` program from the bug notes now
   **fails to compile** (rc != 0).  No need to run it under
   valgrind post-fix — it doesn't reach codegen.

---

## Risks

- **Over-rejection.**  If a legitimate method-return-of-borrowed-
  aggregate pattern in stdlib or downstream packages relies on
  the current accept-all behavior, the fix would surface it as a
  new rejection.  Mitigation: full driver / stage / checker /
  packages suite gate before commit.  If a stdlib usage breaks,
  that usage was unsound and needs reshaping.
- **Multi-`&T` receiver edge case (a).**  Resolved upstream: such
  methods are already rejected by the existing single-origin rule,
  so the caller-side check never sees them.  No additional risk.
- **`_return_origin` recursion through complex receiver shapes.**
  The existing recursion handles HMethodCall and ptr_at_ref/mut.
  If a receiver shape isn't covered, `_return_origin` returns
  `None`, the fix's `if receiver_origin is None: return set()`
  treats it conservatively as "no valid origin" → rejects.  This
  is the safe default — it might over-reject edge shapes (e.g.,
  returning a `&T` from a non-receiver-derived call inside the
  method) but that's a separate `_return_origin` enhancement, not
  this fix's scope.

---

## What this fix does NOT do

- **No new diagnostic message.**  Reuses the existing
  `borrowed aggregate return must derive from a reference parameter
  (MVP escape rule)` text and the
  `mutable references must derive from an &mut parameter`
  variant.  Users see consistent error text for the existing rule's
  full domain.
- **No interprocedural lifetime tracking in the general sense.**
  The fix is the narrow shape: "method-call returning a
  borrowed-aggregate uses receiver origin."  General
  cross-function lifetime analysis (Path A's heavier interpretation
  in `notes.md`) remains out of scope.
- **No stdlib changes.**  Per user's instruction: "No stdlib or API
  workaround. This is LANGUAGE_BUG, not a ReadOnlyMap design
  decision."  The fix is in `type_checker.py` only.
- **No version bump for the diagnostics-context Phase 1 resume.**
  Phase 1 resumes as a separate patch after the LANGUAGE_BUG fix
  lands.  This patch's sole deliverable is the rule fix + the
  expanded regression suite.

---

## Patch deliverables

1. **`lang/driftc/type_checker.py`** — extend
   `_borrowed_aggregate_origins` (`:10465`) with the HMethodCall
   case (~10 lines + comment).
2. **`lang/tests/driver/test_borrow_origin_method_call_escape.py`**
   — flip BUG-pin assertion to post-fix-correct shape; add tests
   2 (positive control on ref-param receiver) and 5 (`&mut`
   receiver).  Optionally add test 6 (chained method calls).
3. **`lang/versions.py`** — bump `DRIFTC_VERSION` per AGENTS.md
   "Compiler versioning rule" (behavior-changing fix; no boundary
   shape change → no ABI bump).  Likely `0.31.40 → 0.31.41`.
4. **`docs/history.md`** — entry naming: bug shape, diagnosis
   (intraprocedural rule + missing HMethodCall path), fix
   location, regression coverage, link to
   `work/borrow-origin-method-call-escape/notes.md` for full
   context.

---

## After the fix lands

`work/exception-diagnostics-context/plan.md` Phase 1 status updated
from "PAUSED — Phase 1 hard gate failed (LANGUAGE_BUG)" to
"resuming."  Phase 1 implementation re-stages the
`containers.ReadOnlyMap<K, V, B>` work under Option C (per the
package-cycle finding from the prior attempt) with the public
construction API including both
`containers.read_only_map_view(&m)` AND
`HashMap.read_only(self: &Self)` — both now safe under the fixed
rule.

The bug-pinning regression in
`test_borrow_origin_method_call_escape.py` stays as a permanent
mainline test guarding against future regressions.
