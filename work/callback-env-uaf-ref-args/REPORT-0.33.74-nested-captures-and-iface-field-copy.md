# 0.33.74 — interface-typed struct-field copies rejected (CORE_BUG)

> **ADDENDUM (release posture + capture-regression fix).** Per the release thread: 0.33.73 was an
> internal candidate, never released; the public sequence is 0.33.72 → 0.33.74, and 0.33.74 contains BOTH
> the nested boxed-callback capture work and this interface-field-copy rejection (doc/history.md now has
> one consolidated 0.33.74 entry; 0.33.73 is marked internal-only).
>
> The 0.33.73 full-gate failure (`test_for_binder_capturable_by_inner_lambda`) is **fixed on this stack**:
> the wrap-site capture rules were over-broad (position-blind), rejecting the sound synchronous pattern
> (`for item in xs { val cb = callback0(captures(copy item) …); cb.call() }` — `item` is `&Int`, but the
> box never leaves the iteration). Enforcement is now **use-aware**
> (`lambda_validate.py::_check_boxed_capture_escapes`): a boxed callback with a frame-pointer-carrying
> capture (ref-valued MOVE/COPY, or REF/REF_MUT borrow) is accepted iff its value provably stays local —
> invoked in place, or let-bound with every use in method-call receiver position — and rejected in any
> escaping position (returned, ctor/call/method argument, assignment, moved, or captured by another
> lambda), same codes (`E_ESCAPE_REF_CAPTURE` / `E_CALLBACK_BORROWED_CAPTURE`), messages now naming the
> escape and the local-use alternative. The scan descends into lambda bodies (the user-fn validation pass
> is the only pass `--test-build-only` reaches, so nested wraps must be visible there); the hidden-lambda
> worklist re-validation stays as the belt. The interim hidden-body BorrowChecker experiment was removed
> (it enforced nothing at ctor/return positions — escape levels only attach to syntactically-direct
> lambda args; binding-level escape dataflow is real feature work, recorded as follow-up).
>
> Verified matrix after the redesign: the for-binder pattern and all sound shapes compile/run; every
> escaping hazardous shape rejects (nested and top-level; implicit `&T`, explicit `captures(copy …)`,
> `&mut`, `Optional<&T>`, implicit borrows; including RETURN positions, which the old design missed on
> nothrow fns). Batteries below re-run green on the final stack.
>
> **Second full-gate finding (fixed):** two pre-existing codegen e2e fixtures
> (`borrow_escape_spawn_rejected`, `implicit_callback_borrowed_capture_rejected`) pin the borrow
> checker's precise `E_ESCAPE_THREAD` ("cannot be sent to a detached virtual thread") for EXPLICIT
> `captures(&x)` passed to `conc.spawn` — my validator's borrowed-capture arm was firing first at
> typecheck with a different message, shadowing the pinned diagnostic. Explicit ref clauses already have
> owners (wrap-resolver rejection on plain boxed wraps; loan-based E_ESCAPE_* at annotated sites), so the
> validator arm is now gated to IMPLICIT borrows only — the case those owners never see. All six
> capture-diagnostic e2e fixtures pass; the implicit-borrow rejection and all 29 driver regressions
> (nested + match-arm + interface files) re-verified green.

**Follow-up to:** the open finding in `2026-07-06T173854Z-nested-boxed-callback-captures-03373.md`
(`val cb = h.cb` double-free/UAF). **Regression-first; independent of the 0.33.73 suite clone** (all work
in the main worktree; the clone untouched).
**Status:** implemented and verified; patch in the working tree
(`lang/driftc/type_checker.py`, `doc/history.md`, `lang/versions.py` modified;
`lang/tests/driver/test_interface_field_copy_rejected.py` new/untracked — needs explicit `git add`).

## Minimal failing regressions (before the fix, on HEAD; first also confirmed on certified 0.33.69)

1. **Boxed-callback field** — plain fn (no lambda nesting involved):
   `Holder { cb: core.Callback0<String> }`, `captures(move note)`, then `val cb = h.cb; cb.call()` →
   **SIGSEGV** (HEAD) / SIGSEGV (certified 0.33.69).
2. **Plain user interface field** — `Holder { g: Greeter }` (struct impl moved in), `val g = h.g` →
   **`tcache_thread_shutdown(): unaligned tcache chunk` abort**. Proves this is not callback-specific.
3. **Transitive** — `Outer { h: Holder }`, `val h2 = o.h` → **codegen ICE**
   (`NotImplementedError: copy not supported for INTERFACE`). Safe but unfriendly, same root.

## Root cause — precise classification

**A checker contract gap, not a missing runtime retain path.** The system's intended semantics were
already consistent everywhere else: interface values are **not Copy** —
- whole-local copy (`val cb2 = cb`) → rejected ("cannot copy 'cb': … is not Copy"),
- ref-subject field read (`fn f(h: &Holder) { val cb = h.cb; }`) → rejected via the existing
  `_require_copy_value` call at the HField gate,
- `Array<Callback0>` element read → rejected.

The one leaky path: the checker's HField gate ran `_require_copy_value` **only when the subject is a
reference** (`type_checker.py`, the `subject_is_ref or _expr_reads_through_ref_projection(...)` guard).
Owned-subject field reads instead rely on lowering's *semantic deep copy* — which is real and memory-safe
for struct/array/String recursion (verified: a non-Copy `Inner { items: Array<Int> }` field read
deep-copies and runs correctly) — but `_emit_copy_value` has **no INTERFACE arm**, and the ownership
classification (`_should_copy_value`/`_copy_if_ref_alias`) doesn't cover INTERFACE either. Result: a bare
interface field lowered to a **raw aliased extract** (two owners, one env, no refcount → double-free),
and a nested interface hit the `NotImplementedError` backstop.

**Why "support" is not on the table:** the interface value carries a vtable drop hook but no clone hook,
and the payload is dynamic — a boxed callback's env is opaque and unclonable by construction. There is
nothing to "retain" (no refcount in the iface repr). Any support design is a representation change
(refcounted iface envs), i.e. ABI-bump territory, for a pattern that has sound alternatives.

## VERDICT: reject by-value interface field copies with a clear diagnostic

Implemented in the checker's owned-subject HField path: reject when the field type **is or transitively
contains** an interface value — `_contains_interface_value()` recurses struct fields, variant arm fields,
and array elements, exactly mirroring `_emit_copy_value`'s copy recursion (refs/pointers to interfaces
remain trivially copyable and are excluded). Diagnostic:

```
error: cannot copy field 'cb' out of an owned struct: type 'std.core.Callback0<String>' contains an
interface value, which is not Copy and cannot be cloned. Borrow the field, call through it directly
(e.g. subject.cb.method(...)), or move the whole struct [E_IFACE_FIELD_COPY]
```

The transitive gate also converts repro 3's ICE into the same clean rejection. `_emit_copy_value`'s
INTERFACE `NotImplementedError` stays as the codegen backstop behind the checker gate.

## Files touched

- `lang/driftc/type_checker.py` — `_contains_interface_value()` helper + the owned-subject branch of the
  HField STRUCT gate (`E_IFACE_FIELD_COPY`).
- `lang/tests/driver/test_interface_field_copy_rejected.py` — new, 8 cases (untracked; needs `git add`).
- `lang/versions.py` — 0.33.73 → **0.33.74**. `doc/history.md` — entry added.

## Targeted test results (serial; suite clone untouched)

- New regression file: **10 cases** (review addition: the variant-arm and array-element arms of
  `_contains_interface_value`'s recursion were implemented but unpinned) — five rejections
  (boxed-callback field, plain-interface field, transitive struct-with-interface field,
  variant-with-interface-payload field, `Array<Callback0>` field), **direct-call-through-field positive
  under ASAN** (clean), borrowed-receiver positive, whole-struct-move positive, **non-interface non-Copy
  deep-copy control** (no over-rejection), and pins on the two neighboring gates that were already
  correct (ref-subject, array element). Incidental find while pinning the variant arm: a variant payload
  naming a QUALIFIED generic (`core.Callback0<String>`) emits an unrelated spurious
  `E-TYPE-UNKNOWN: unknown generic type 'Callback0'` alongside otherwise-correct behavior — minor
  pre-existing diagnostic wart, noted for a future cleanup, worked around in the test with a local
  interface.
- Battery: new file + `test_nested_boxed_callback_captures.py` (11) + `test_implicit_callback_wrap.py` +
  `test_product_shape_consumer_patterns.py` + `test_fat_arc_interface_views.py` —
  **69 passed, 0 failed** (19m38s). The 0.33.73 positives that call through fields (`h.cb.call()`)
  are unaffected, confirming the sound pattern survives.
- Valgrind: not separately run for this slice — the fix is a compile-time rejection (nothing new
  executes); the ASAN row covers the remaining positive path. Happy to add a Valgrind row on the
  direct-call positive if wanted for the gate.

## Version/ABI impact

- `DRIFTC_VERSION` 0.33.73 → **0.33.74** (behavior change: new rejection).
- `DRIFT_RT_ABI_VERSION` stays **20** — checker-only; no layout, convention, or runtime change.
- **Source-compat note for release notes:** code that read interface-typed fields by value compiled
  before and now rejects. Every such program was memory-corrupting (or ICE'd for the nested shape), so
  there is nothing working to break — but the diagnostic's migration guidance (borrow / call through /
  move the struct) should be quoted in the notes, same treatment as 0.33.73's capture rejections.

## Follow-ups (out of scope, recorded)

- The philosophical cousin: owned-subject field reads of non-Copy struct/array fields silently DEEP-COPY
  today (P4 behavior, memory-safe, kept unchanged). Whether implicit deep-copy of non-Copy types should
  exist at all is a language-semantics question for the transfer-policy work (String Scope A thread),
  not a memory-safety bug.
- Full serial suite for 0.33.74 owed once the box frees up (0.33.73's gate is running).
