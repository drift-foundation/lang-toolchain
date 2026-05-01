# Catch-arm Scope Misresolution — LANGUAGE_BUG (filed, deferred)

**Status:** filed, **NOT blocking** the diagnostics-context track
(`work/exception-diagnostics-context/plan.md`).

**Classification:** LANGUAGE_BUG per AGENTS.md compiler bug policy.

**Filed:** 2026-04-30, post-0.31.40.

**Originator:** bookkeeper / web-rest 0.4.1 report on driftc 0.31.40,
follow-up on the post-Bug-A diagnostic gates.  Confirmed reproduced
locally against staged 0.31.40 toolchain.

---

## Symptom

Inside a `throws` method's catch arm, calling a method on
`self.<Mutex_field>` (or on a let-bound `&Mutex<T>` aliased outside
the try) misresolves the receiver to
`Result<String, RestError>` — exactly the return type of an earlier
`rest.path_param(...)` call in the same function body.

Diagnostic:

    error: argument 0 to std.concurrent::MutexGuard<T>::get_mut__inst_…
      has type std.core.Result<String, web.rest.errors.RestError>,
      expected RefMut<std.concurrent.MutexGuard<…Inner>>

Variation matrix from app team:

| Earlier `path_param` calls | Result |
|---|---|
| 0 | PASS |
| 1 | **FAIL** (minimum trigger) |
| 3 | FAIL (same diagnostic) |

---

## Critical load-bearing finding (mine, 2026-04-30)

**The bug fires only on full-lowering compile (driver default), NOT
on `--test-build-only`.**

- `driftc --test-build-only repro/main.drift` → **exit 0, clean**.
- `driftc repro/main.drift -o /tmp/r` (full lowering, codegen) →
  **fails with the diagnostic**.

The `__inst__` instantiation suffix in the diagnostic
(`get_mut__inst__702d8820bcd2b9af`) confirms this is happening
during generic instantiation / lowering, not during initial
type-checking.

The diagnostic emits from
`lang/driftc/checker/__init__.py:2756-2766` —
`check_call_signature` invoked from the legacy stub-checker's
`_validate_calls` HIR walker (`:2324`), which calls
`_infer_hir_expr_type(receiver_expr, ...)` to obtain the receiver's
type at validation time (`:2425`).

For the failing case, `_infer_hir_expr_type` returns
`Result<String, RestError>` for `self.store` (an `HField` /
`HPlaceExpr` rooted in `self`) — wrong; the field's actual type is
`&Mutex<Inner>`.

---

## Hypothesis (unverified — needs trace)

The auto-try contract synthesizes an `or_throw()` HMethodCall HIR
node when an unannotated `Result<T, E>`-returning call is bound in
a `throws` outer fn (`val a = path_param(...)` inside a throws
method).  The synthesis path:

`type_checker.py:_wrap_auto_try` (`:9349`) constructs
`H.HMethodCall(receiver=expr, method_name="or_throw", args=[],
kwargs=[])`, assigns a `callsite_id` and a fresh `node_id`, and
returns it as the new HIR for the binding's value.

Suspect: when the legacy stub-checker (`checker/__init__.py`)
walks the HIR after type-checking has rewritten it, its
`_infer_hir_expr_type` consults a node-id-keyed type cache (or
the `recorded_types` dict).  If the synthesized HMethodCall's
`node_id` collides with a later catch-arm HVar / HField node-id,
the type cache returns the synthesized call's recorded type
(`Result<String, RestError>`) for the wrong expression.

Alternative hypothesis (less likely given the variation-matrix
pattern): scope-binding pollution where the synthesized auto-try
binding overwrites `self`'s scope entry inside the catch arm.

Either way: the lookup path that returns the wrong type is
**inside the legacy stub-checker, not the modern type_checker**.

---

## Repro

**Project:** `/home/sl/src/pushcoin/work/repro-catcharm-scope/`
(app team, 56-line reduction).

**Manifest:** depends on `web-rest@0.4` and `web-jwt@0.3` (cross-package
`RestError` is what the diagnostic mentions; not yet verified whether
local Throw-implementing types reproduce).

**Build (reproduces):**

    cd /home/sl/src/pushcoin/work/repro-catcharm-scope
    driftc \
      --package-root /home/sl/opt/drift/certified/current/libs \
      --dep web-rest@0.4.1 --dep web-jwt@0.3.1 \
      --trust-store drift/trust.json \
      src/main.drift -o /tmp/repro.bin

Exit 1 with the diagnostic above.  Add `--test-build-only` and exit
becomes 0 (type-check passes, full-lowering check is the failure).

Confirmed against staged 0.31.40 toolchain
(`/home/sl/opt/drift/staged/toolchain/drift-0.31.40+abi10/bin/driftc`).
Also reproduces against certified 0.31.38.  **The R1 fix in 0.31.40
did NOT close this bug** — they are different code paths.

---

## Workaround in bookkeeper

Lift the catch-arm body into a free function:

    fn _mark_failed(store: &Mutex<TaskStore>, key: &TaskKey) -> Void {
        var guard = store.lock();
        val s = guard.get_mut();
        // ...
    }

    } catch e {
        _mark_failed(&self.store, key);
        rethrow;
    }

Inside the catch arm, only the call site
(`_mark_failed(&self.store, key)`) involves `self.<Mutex_field>`
— and that path doesn't trigger the misresolution.

---

## Why deferred (not blocking diagnostics-context track)

1. **Independent code paths.** The diagnostics-context work
   (`work/exception-diagnostics-context/plan.md`) reshapes
   `e.attrs` / `e.captures` views and adds `^capture` propagation.
   Neither touches the `_validate_calls` / `_infer_hir_expr_type`
   path in the legacy stub-checker.
2. **Bookkeeper has a clean workaround.** `_mark_failed` free
   function pattern is in production; not blocking app-team
   delivery.
3. **Investigation requires a deep trace.** The legacy stub-checker
   has its own type-cache discipline; tracing why it returns the
   wrong type for `self.<field>` after auto-try synthesis is a
   non-trivial investigation.  Better to do it as its own focused
   patch with a clean regression test.

---

## Next-step plan (when triaged)

Per AGENTS.md regression-first sequence:

1. **Add minimal failing regression test.** Use the team's repro
   shape OR (if reproducible without web-rest) a single-source
   variant.  Cross-package may be required — file a
   `lang/tests/driver/test_catch_arm_scope_after_auto_try.py`
   that uses the existing cross-package fixture infrastructure
   (mwlib-style mini-package with a `path_param`-shaped fn
   returning `Result<String, LocalError>`).
2. **Confirm fail on current main.**
3. **Trace `_infer_hir_expr_type` for `HField` / `HPlaceExpr`
   rooted in `self`** inside a catch arm, with auto-try-synthesized
   nodes earlier in the same fn body.  Identify the exact lookup
   path that returns the wrong type.
4. **Fix root cause.**  Likely candidates:
   - Node-id collision in the legacy stub-checker's type cache
     (need to dedup or invalidate on synthesized-node insertion).
   - Scope/binding misresolution where synthesized `or_throw()`
     binding pollutes scope.
   - The legacy stub-checker missing a re-typing pass that the
     modern type_checker performs.
5. **Confirm regression passes.**
6. **Full driver/stage/checker/packages/memcheck suite green.**
7. **Verify bookkeeper's repro project compiles cleanly without
   the `_mark_failed` workaround** (workaround retired in
   bookkeeper as the cert-lane verifier).

**Estimated scope:** 1-3 days, single patch.  Target
0.31.41-or-later (depending on diagnostics-context track timing).

---

## Related

- Bug A (0.31.38) — auto-try contract leaking across lambda body
  boundary.  Different code path; pre-existing diagnostic-cascade
  pattern but distinct symptom.
- Bug R1 (0.31.40) — generic Callback nothrow inference through
  typevars.  Different code path; closed by switching to permissive
  kind detector in `call_resolver.py`.  **Did NOT close this bug.**
- Bug Q2 (0.31.39) — catch-arm binder treated as outer capture in
  explicit-capture lambda.  Different code path; closed by
  capture-discovery seeding.  Confirmed unrelated.

This bug shares the "auto-try synthesis under throws context
interacts badly with later type queries" theme but the failure
point is the legacy stub-checker, not the auto-try emit itself.
