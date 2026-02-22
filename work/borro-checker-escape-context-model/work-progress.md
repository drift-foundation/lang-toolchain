# Borrow Checker Escape Context Model — Work Progress

Author: Klaudia
Current focus: Fn1 SCOPED borrowed-capture coercion — **Phase F1 complete**

---

## Completed work (reference only)

**A5 (escape context model):** All phases 0–5 complete. `EscapeLevel` enum, `Loan.max_escape`, `_check_lambda_scope_escape`, SCOPED/THREAD/STATIC boundary enforcement, `param_nonretaining` fully removed. 22 tests in `test_escape_level_model.py`. Review checklist satisfied.

**A1 (call contract single seam):** Slices 1–4 complete. `call_contract.py` owns all call-shape decisions (arity, kwargs, ctor fields, intrinsic shape, array method arity). 37 contract/guard tests. Anti-regression guard (`test_call_contract_ownership_guard.py`) prevents drift.

---

## Known limitations (carry-forward)

1. **SCOPED + capturing lambdas — type checker gate lifted (F1), MIR lowering blocked (F2-D1).** The call resolver no longer overrides `allow_capture_invoke = False` for Fn-trait-bounded generic params with borrowed captures. The type checker accepts the lambda. However, MIR lowering crashes: `NotImplementedError: No MIR lowering for expr HLambda` — generic monomorphization instantiates `F` to a function pointer type with no room for capture environments. See section 12 (F2-D1) for full analysis. The escape context model is complete; the remaining gap is in the monomorphization/lambda-lowering subsystem.

2. **`_place_is_defined_before_stmt` is conservative (MVP §3.6).** Only the direct enclosing block is checked for place definition. Borrows defined in predecessor or nested blocks are rejected even if provably safe. Full dataflow-based lifetime reasoning is deferred. `test_scoped_spawn_nested_block_false_positive` is the pinned regression for this behavior.

---

## Fn1 SCOPED Borrowed-Capture Assessment

Date: 2026-02-21
Author: Klaudia
Status: **Phase F1 complete.** Revised approach implemented and validated.

### 1. Problem statement

User scenario that should work but currently fails:

```drift
var x = 42
conc.scope(|s| [&x] => {
    s.spawn(|| [&x] => { print(x) })
})
// x is alive here — scope completed, spawned tasks joined
```

The borrow checker's SCOPED acceptance path (Phase 4) is fully implemented and unit-tested. It validates that captured loans are alive across the scope call and accepts the lambda. However, the type checker **rejects the lambda before the borrow checker runs** because `conc.scope<F>(f: F) require F is Fn1<Scope, Void>` forces the lambda through function-pointer coercion, which unconditionally rejects any lambda with `ref`/`ref_mut` captures.

### 2. Concrete path map (file/function anchors)

The failure chain involves 4 files and 11 touch points:

#### TP1 — stdlib declaration (context only)
- **File:** `stdlib/std/concurrent/concurrent.drift:756`
- **What:** `pub fn scope<F>(f: F) -> core.Result<Void, ConcurrencyError> require F is core.Fn1<Scope, Void>`
- **Classification:** out-of-scope / context
- **Notes:** The `require F is Fn1<...>` bound is what forces function-pointer coercion in the call resolver.

#### TP2 — Initial lambda pre-typing (sets `allow_capture_invoke = True`)
- **File:** `lang/driftc/checker/call_resolver.py:5122-5125`
- **What:** All lambda args initially get `arg.allow_capture_invoke = True`.
- **Classification:** metadata propagation
- **Notes:** Correct initial value. Problem is the override later.

#### TP3 — Pre-resolution require scanning (sets `allow_capture_invoke = True`)
- **File:** `lang/driftc/checker/call_resolver.py:2270-2298`
- **What:** Scans `require F is Fn1<A, R>` bounds, infers expected type from trait args, sets `arg.allow_capture_invoke = True`.
- **Classification:** metadata propagation
- **Notes:** Also correct. Sets up the expected function type for the lambda.

#### TP4 — Post-resolution override (**THE REJECTION ROOT CAUSE**)
- **File:** `lang/driftc/checker/call_resolver.py:5227-5240`
- **What:** After resolution succeeds, iterates lambda args. If `sig_inst.param_types[idx]` has `kind == FUNCTION`, sets `arg.allow_capture_invoke = False` and re-types the lambda.
- **Classification:** **acceptance gate (primary)**
- **Notes:** This is the first of two override sites. The logic assumes that any `FUNCTION`-kinded parameter requires bare function-pointer coercion. There is no escape-level or Fn-trait awareness here.

#### TP5 — Post-resolution require re-scan (**SECOND OVERRIDE**)
- **File:** `lang/driftc/checker/call_resolver.py:5277-5314`
- **What:** Re-scans `require` expressions. For each `Fn1`/`Fn2`/etc. bound, sets `arg.allow_capture_invoke = False` and re-types.
- **Classification:** **acceptance gate (secondary)**
- **Notes:** Second override. Even if TP4 were fixed, this loop would still force `allow_capture_invoke = False` for Fn-bounded params. Both sites must be addressed.

#### TP6 — Type checker function-pointer coercion (**THE REJECTION SITE**)
- **File:** `lang/driftc/type_checker.py:5495-5509`
- **What:** When `allow_capture_invoke == False`, any lambda with `ref`/`ref_mut` captures is rejected with "closures with borrowed captures are non-escaping in v0".
- **Classification:** **acceptance gate (enforcement)**
- **Notes:** This is the point of rejection. The error message is emitted and the lambda is recorded as unknown type. The borrow checker never sees it.

#### TP7 — Escape annotation injection (timing issue)
- **File:** `lang/driftc/driftc.py:7292-7315`
- **What:** Stamps `param_escape_level = [SCOPED]` on `scope`'s FnSignature. Runs **after** all type checking (line ~7244 is the last `type_checker.check_function` call).
- **Classification:** **metadata propagation (timing)**
- **Notes:** The escape annotation is not available to the call resolver at TP4/TP5 time. The resolver cannot condition the override on escape level.

#### TP8 — Borrow checker SCOPED acceptance (never reached)
- **File:** `lang/driftc/borrow_checker_pass.py:493-509` (`_check_lambda_escape_level`)
- **File:** `lang/driftc/borrow_checker_pass.py:469-491` (`_check_lambda_scope_escape`)
- **Classification:** **borrow-check enforcement dependency**
- **Notes:** This is the target destination. If the lambda reaches the borrow checker with `SCOPED` required level, the SCOPED promotion path validates captured loans and accepts the lambda. Fully implemented and unit-tested (Phase 4).

#### TP9 — `_nonretaining_param_state` (escape→nonretaining mapping)
- **File:** `lang/driftc/type_checker.py:9337-9345`
- **What:** Maps SCOPED → `True` (non-retaining) for borrowed-aggregate boundary checks.
- **Classification:** out-of-scope (unrelated to `allow_capture_invoke`)
- **Notes:** This is used for a different check (`_check_borrowed_arg_boundary`). It correctly recognizes SCOPED as non-retaining but does not influence the Fn1 coercion path.

#### TP10 — Callback wrapper wrapping path
- **File:** `lang/driftc/checker/call_resolver.py:5315-5354`
- **What:** After re-typing, wraps lambda args in `callback0/1/2` calls for `Callback0/1/2`-typed params.
- **Classification:** metadata propagation / potential alternative approach
- **Notes:** Only triggers for `Callback`-typed params, not `Fn1`-typed. For `scope`'s `Fn1` bound, the resolved param is FUNCTION kind, not Callback schema. This path does not engage.

#### TP11 — Borrow checker transparent wrapper propagation
- **File:** `lang/driftc/borrow_checker_pass.py:1969-1979`
- **What:** When an arg is a `callback0/1/2` wrapper call, the borrow checker unwraps it and propagates the outer call's escape level to the inner lambda.
- **Classification:** borrow-check enforcement dependency / potential enabler
- **Notes:** If the solution involves callback-wrapping the lambda for `Fn1`-bounded params, this path would propagate SCOPED to the inner lambda. Already works for Callback-typed params.

### 3. Touch point classification summary

| ID | Location | Classification |
|----|----------|---------------|
| TP1 | `concurrent.drift:756` | out-of-scope / context |
| TP2 | `call_resolver.py:5122-5125` | metadata propagation |
| TP3 | `call_resolver.py:2270-2298` | metadata propagation |
| TP4 | `call_resolver.py:5227-5240` | **acceptance gate (primary)** |
| TP5 | `call_resolver.py:5277-5314` | **acceptance gate (secondary)** |
| TP6 | `type_checker.py:5495-5509` | **acceptance gate (enforcement)** |
| TP7 | `driftc.py:7292-7315` | metadata propagation (timing) |
| TP8 | `borrow_checker_pass.py:469-509` | borrow-check enforcement dependency |
| TP9 | `type_checker.py:9337-9345` | out-of-scope |
| TP10 | `call_resolver.py:5315-5354` | metadata propagation / alternative |
| TP11 | `borrow_checker_pass.py:1969-1979` | borrow-check enforcement dependency |

### 4. Root cause analysis

There are two independent problems that must both be solved:

**Problem A — `allow_capture_invoke` override:** The call resolver (TP4, TP5) unconditionally sets `allow_capture_invoke = False` for any param whose resolved type is `FUNCTION` kind. It does not distinguish between:
- A generic `F is Fn1<A, R>` param with SCOPED/LOCAL escape annotation (safe for captures), and
- A concrete function-pointer param (captures genuinely not allowed).

**Problem B — Escape annotation timing:** The escape annotation for `scope` is stamped at TP7 (line 7298), which runs after all type checking completes. Even if TP4/TP5 wanted to condition the override on escape level, the escape annotation is not yet available on the FnSignature at resolution time.

### 5. Design approaches (3 candidates)

#### Approach A: Callback-wrap Fn1-bounded captures (recommended)

**Idea:** When the call resolver processes a generic call with `require F is Fn1<...>` and the lambda arg has captures (explicit or discovered), wrap it in `callback1(lambda)` with `_is_implicit_wrap = True` instead of forcing function-pointer coercion.

**How it works:**
1. At TP4/TP5, detect that the param type comes from an `Fn1`/`Fn2` bound (the resolver already knows this — it scans `require` expressions).
2. Instead of `allow_capture_invoke = False`, wrap the lambda in `callback1(lambda)` with `_is_implicit_wrap = True`.
3. The type checker (TP6) sees `callback0/1/2(lambda)`, enters the callback handler (call_resolver.py:4287-4301), and because `_is_implicit_wrap = True`, the borrow-capture guard is skipped. `allow_capture_invoke = True` is set on the inner lambda. Typing succeeds.
4. The borrow checker (TP11) unwraps the callback wrapper, reads the outer call's escape level (SCOPED for `scope`), and validates via `_check_lambda_scope_escape`.

**Advantage:** Reuses existing callback-wrap + transparent-wrapper infrastructure (Phase 3c BLOCKER resolution). Minimal new code.

**Risk:** The generic instantiation of `scope<F>` currently expects `F` to unify with a bare function type. A callback-wrapped lambda produces a `Callback1<Scope, Void>` type, not a function pointer. The generic instantiation may fail or need adjustment. This needs investigation.

**Escape annotation timing:** Not a problem. The borrow checker runs after escape annotations are stamped. Only the callback wrapping decision needs to happen at type-check time, and that decision is based on `require F is Fn1<...>` (available) — not on escape level.

#### Approach B: Non-retaining aware `allow_capture_invoke`

**Idea:** At TP4/TP5, when the resolved function has `param_escape_level` data indicating non-retaining for this param, keep `allow_capture_invoke = True` instead of overriding to False.

**Problem:** Blocked by Problem B (timing). The escape annotation is not on the FnSignature at type-check time. The non-retaining analysis (`analyze_non_retaining_params`) runs at line 7286, escape annotations at 7298 — both after type checking. The call resolver has no access to escape-level data.

**Mitigation:** Move escape annotation injection earlier in the driver (before type checking), or hard-code Fn-trait-bounded params as "potentially non-retaining" at the call resolver level. Hard-coding is fragile. Moving annotations earlier requires careful analysis of ordering dependencies (non-retaining analysis depends on type-checked function bodies).

**Risk:** Higher than Approach A. Touches the driver pipeline ordering, which has strict phase dependencies.

#### Approach C: Split coercion gate from escape enforcement

**Idea:** In the type checker (TP6), instead of unconditionally rejecting captures when `allow_capture_invoke = False`, add a weaker gate: accept captured lambdas if they will later be validated by the borrow checker. Emit a deferred marker instead of an error.

**Problem:** Soundness. The type checker's rejection is a **safety net** — it prevents borrowed captures from reaching codegen in contexts where the borrow checker might not run (due to earlier errors or skip conditions). Removing the rejection without guaranteed borrow-checker coverage is unsound.

**Risk:** Highest. Not recommended without a provable guarantee that the borrow checker always runs for deferred lambdas.

### 6. Recommended phased implementation plan

**Approach A was blocked by INV-1 (section 9).** The revised approach was implemented instead.

#### Phase F1 — Fn-bounded `allow_capture_invoke` relaxation (single slice) — COMPLETE

**Goal:** When a generic call has `require F is Fn1/Fn2/...` bound and the lambda arg has borrowed captures, keep `allow_capture_invoke = True` at TP4/TP5 so the type checker accepts the lambda and the borrow checker validates escape level.

**Scope (files changed):**
1. `lang/driftc/checker/call_resolver.py` — TP4: precompute Fn*-bounded param indices from `require` clause, skip override for those. TP5: skip override inside Fn-trait-bound loop.

**Files NOT changed:** `type_checker.py`, `borrow_checker_pass.py`, `driftc.py`, `call_contract.py`.

**Risk (realized):**
- **Over-broad TP4 relaxation (caught during review):** Initial implementation used a TYPEVAR check that would relax the guard for any generic param, not just Fn*-bounded ones. Narrowed to require explicit Fn*-trait bound evidence. Negative test added.

**Phase F1 done-when criteria (exact artifacts):**

1. **Regression-first gate:**
   - [x] Minimal failing regression test added **before** any compiler fix. Test: `test_fn1_bounded_scope_borrowed_capture_accepted` in `lang/tests/driver/test_fn1_scope_borrowed_capture.py`.
   - [x] Test exercises: synthetic generic `fn apply<F>(f: F) require F is Fn1<Int, Void>` + lambda with `captures(&x)` → failed with "closures with borrowed captures are non-escaping in v0" (2 instances: TP4 + TP5).
   - [x] Test confirmed **failing** on current branch before fix. Error: `AssertionError: Type checker must not reject Fn1-bounded borrowed capture: [Diagnostic(message='closures with borrowed captures are non-escaping in v0; ...')]`.

2. **Compiler fix:**
   - [x] Smallest viable change applied. Single file: `lang/driftc/checker/call_resolver.py`, two sites (TP4 + TP5). See section 11 for details.
   - [x] Failing regression from step 1 now **passes** after fix.
   - [x] Fix is scoped to Fn-trait-bounded generic params with capturing lambdas only. Condition: `explicit_captures` has ref/ref_mut AND (TP4: param index in precomputed `_fn_bounded_params` set from `require` clause; TP5: inside Fn-trait-bound loop). Captureless lambdas and non-Fn-bounded params are unchanged. Negative test confirms non-Fn-bounded generics are still rejected.

3. **Diagnostic wording preservation:**
   - [x] Zero change to existing A1 diagnostic wording (18/18 checker message assertions pass).
   - [x] Zero change to existing A5 E_ESCAPE_* diagnostic codes or messages (22/22 escape level tests pass).
   - [x] `borrowed_capture_interface_coercion_rejected` e2e still emits phase="typecheck" (ok).

4. **Mandatory regression matrix (all green):**
   - [x] `lang/tests/borrow_checker/test_escape_level_model.py` — 22 passed.
   - [x] A5 e2e boundary set:
     - [x] `borrow_escape_spawn_rejected` — ok
     - [x] `borrow_escape_scope_accepted` — ok
     - [x] `borrow_escape_thread_accepted` — ok
     - [x] `implicit_callback_borrowed_capture_rejected` — ok
     - [x] `borrowed_capture_interface_coercion_rejected` — ok
   - [x] Boundary guard/contract tests:
     - [x] `test_callinfo_param_layout_contract.py` — passed
     - [x] `test_boundary_matrix_result_variant_contract.py` — passed
     - [x] `test_struct_ref_field_boundary_contract.py` — passed
     - [x] `test_call_contract_ownership_guard.py` — passed
   - [x] A1 contract tests — 24 passed.
   - [x] Borrow checker full — 89 passed.
   - [x] Stage2 full — 86 passed.
   - [x] Checker diagnostics (ctor/function/callback) — 18 passed.
   - [x] `test_throwing_lambda_rejected_for_fn.py` — passed (Fn1 nothrow constraint preserved).

5. **Deliverables in `work-progress.md`:**
   - [x] Updated investigation verdicts (INV-1, INV-2, INV-3) with evidence (section 9).
   - [x] Files/functions changed listed (section 11).
   - [x] Pass/fail matrix with results (this checklist).
   - [x] No newly discovered regressions.
   - [x] Explicit go/no-go recommendation for F2 (section 11).

#### Phase F2 — E2e validation (follow-up)

**Goal:** Exercise the full codegen path for `conc.scope` + borrowed capture and add remaining negative e2e tests.

**Scope:**
1. E2e test: `conc.scope(|s| captures(&x) => { ... })` through MIR lowering + LLVM codegen → runs correctly.
2. E2e negative: `conc.spawn(|| captures(&x) => { ... })` → E_ESCAPE_THREAD at borrow check.
3. Verify captureless Fn1 lambdas are unaffected (bare function pointer path preserved).
4. Planned regression tests 3–7 from section 7.

### 7. Regression tests

#### Implemented (F1):
1. **`test_fn1_bounded_scope_borrowed_capture_accepted`** — positive: synthetic `fn apply<F>(f: F) require F is Fn1<Int, Void>` + `captures(&x)` → no "closures with borrowed captures" error. (`lang/tests/driver/test_fn1_scope_borrowed_capture.py`)
2. **`test_non_fn_bounded_generic_borrowed_capture_still_rejected`** — negative: `require F is Marker` (non-Fn* bound) + `captures(&x)` → still rejected. Guards TP4 narrowing. (same file)

#### Planned (F2):
3. `test_scope_borrowed_capture_accepted_e2e`: `conc.scope(|s| captures(&x) => { ... })` through full codegen → compiles and runs correctly.
4. `test_scope_move_capture_still_accepted`: `conc.scope(|s| captures(x) => { ... })` (move capture, no borrow) → no regression.
5. `test_scope_borrowed_capture_non_outliving_rejected`: borrow of variable defined inside scope body → E_ESCAPE_SCOPE.
6. `test_spawn_borrowed_capture_still_rejected`: `conc.spawn(|| captures(&x) => { ... })` → E_ESCAPE_THREAD (no regression).
7. `test_fn1_bounded_thread_annotated_rejected`: Generic `F is Fn1<...>` with THREAD annotation + borrowed capture → E_ESCAPE_THREAD.

#### Non-regression suites (must stay green):
```
# Borrow checker full:
PYTHONPATH=. ./.venv/bin/python3 -m pytest lang/tests/borrow_checker/ -q

# Checker diagnostics:
PYTHONPATH=. ./.venv/bin/python3 -m pytest \
    lang/tests/driver/test_call_ctor_diagnostics_span.py \
    lang/tests/driver/test_call_function_diagnostics_span.py \
    lang/tests/driver/test_callback_dynamic_dispatch.py \
    -q

# A5 boundary trio:
PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py -j4 \
    borrow_escape_spawn_rejected \
    borrow_escape_scope_accepted \
    borrow_escape_thread_accepted \
    implicit_callback_borrowed_capture_rejected \
    borrowed_capture_interface_coercion_rejected

# High-sensitivity:
PYTHONPATH=. ./.venv/bin/python3 -m pytest \
    lang/tests/driver/test_callinfo_param_layout_contract.py \
    lang/tests/driver/test_boundary_matrix_result_variant_contract.py \
    lang/tests/driver/test_struct_ref_field_boundary_contract.py \
    -q

# A1 contract + guard:
PYTHONPATH=. ./.venv/bin/python3 -m pytest \
    lang/tests/driver/test_ctor_call_contract.py \
    lang/tests/driver/test_intrinsic_call_contract.py \
    lang/tests/driver/test_array_method_contract.py \
    lang/tests/driver/test_call_contract_ownership_guard.py \
    -q
```

### 8. Risk analysis

| Risk | Status | Outcome |
|------|--------|---------|
| Generic instantiation rejects `Callback1` for `Fn1` bound | **Realized** | Approach A blocked. Pivoted to revised approach (section 9). |
| Over-broad TP4 relaxation bypasses safety for non-Fn generics | **Caught in review** | Narrowed guard to require explicit Fn*-bound evidence. Negative test added. |
| `borrowed_capture_interface_coercion_rejected` e2e regresses | **Retired** | Passed — revised approach doesn't touch callback handler path. |
| Captureless Fn1 lambdas accidentally relaxed | **Retired** | Guard checks `explicit_captures` has ref/ref_mut. Captureless lambdas unaffected. |
| Non-retaining analysis ordering breaks | **Retired** | No driver ordering changes in revised approach. |

### 9. Investigation items (verdict checklist)

#### INV-1: Does `Callback1<A, R>` satisfy `require F is Fn1<A, R>`?
- **Question:** Check trait implementations in `std.core`. If Callback1 does not implement Fn1, generic instantiation will fail after callback-wrapping. This is the critical go/no-go gate for Approach A.
- **Resolved:** [x] no — **Approach A is blocked.**
- **Evidence:** `stdlib/std/core/copy.drift:162` defines `trait Fn1<A, R>`. `stdlib/std/core/copy.drift:199` defines `interface Callback1<A, R>`. No `implement Fn1 for Callback1` exists anywhere. `callback1<F,A,R>(f: F) -> Callback1<A,R> require F is Fn1<A,R>` — the input must satisfy Fn1, but the output Callback1 does not implement Fn1.
- **Owner:** Klaudia
- **Date resolved:** 2026-02-21

#### INV-2: Calling convention difference (monomorphization shape)
- **Question:** Does `scope` call `f` via `Fn1.call`?
- **Resolved:** [x] yes — `scope` calls `f.call(move s)` via trait method dispatch.
- **Evidence:** `stdlib/std/concurrent/concurrent.drift:758`: `f.call(move s);`. The generic body uses `Fn1.call`, so `F` must implement `Fn1`.
- **Owner:** Klaudia
- **Date resolved:** 2026-02-21

#### INV-3: Does `_wrap_explicit_capture_callbacks` already handle this?
- **Question:** Does `scope(capturing_lambda)` trigger the `_wrap_explicit_capture_callbacks` fallback?
- **Resolved:** [x] no — the fallback never fires.
- **Evidence:** `call_resolver.py:5189-5210`: the fallback at line 5210 only triggers on `ResolutionError`. For `scope(lambda)`, initial resolution **succeeds** (lambda types as a function matching the Fn1 bound), so the code proceeds to line 5227 (post-resolution override) which sets `allow_capture_invoke = False`.
- **Owner:** Klaudia
- **Date resolved:** 2026-02-21

#### Approach pivot

**Approach A (callback-wrap) is blocked by INV-1.** Callback1 does not implement Fn1; wrapping would fail `scope`'s `require F is Fn1<...>` constraint.

**Revised approach: keep `allow_capture_invoke = True` at TP5 for Fn-trait-bounded capturing lambdas.**

At TP5 (call_resolver.py:5277-5314), when the re-scan detects a `Fn1`/`Fn2`/etc. trait bound and the lambda arg has captures, skip the `allow_capture_invoke = False` override. The lambda keeps `allow_capture_invoke = True` from TP2/TP3. The type checker's `allow_capture_invoke = True` path (type_checker.py:5477-5494) accepts the lambda, recording it with the expected function type. The borrow checker later validates escape level via the existing SCOPED promotion path (TP8).

TP4 also needs the same treatment but with a narrower guard: before the TP4 loop, precompute which param indices have Fn*-trait bounds by scanning the `require` clause. Only skip the override when the param index is in that set AND the lambda has borrowed captures. Non-Fn-bounded generic params (e.g., `require F is Marker`) still get `allow_capture_invoke = False`.

**Why this is sound:**
- The lambda is typed correctly (expected function type from the Fn1 bound).
- Generic instantiation succeeds (F = the function type, which satisfies Fn1).
- The borrow checker runs after escape annotations are stamped and enforces SCOPED/THREAD/STATIC.
- The type checker's safety net for non-Fn-bounded positions is unchanged (direct Callback-typed params, user-written `callback0(borrow_lambda)` still rejected).
- `borrowed_capture_interface_coercion_rejected` is unaffected (its path is through the callback0 handler, not Fn-trait-bounded generic resolution).

### 10. Recommendation

**Phase F1 is complete.** The revised approach (keep `allow_capture_invoke = True` for Fn*-bounded capturing lambdas) was implemented, narrowed to require explicit Fn*-bound evidence at TP4, and validated with full regression matrix + positive/negative tests. See section 11 for implementation details and go/no-go for F2.

---

### 11. Phase F1 implementation results

**Date:** 2026-02-21
**Author:** Klaudia
**Approach used:** Revised approach (section 9 "Approach pivot"). Not Approach A/B/C from section 5.

#### Files changed

Single file: `lang/driftc/checker/call_resolver.py`

**TP4 fix (lines 5227-5268):** Precomputes a set of param indices that have explicit Fn*-trait bounds (`Fn0`/`Fn1`/`Fn2`/`FnThrow0`/`FnThrow1`/`FnThrow2`) by scanning the function's `require` clause. Before overriding `allow_capture_invoke = False`, the code now checks:
1. The lambda has borrowed explicit captures (ref/ref_mut), AND
2. The param index is in the precomputed `_fn_bounded_params` set (subject matched via TypeParamId or name-based lookup, same logic as TP5).
Only then is the override skipped. Non-Fn-bounded generic params (e.g., `require F is Marker`) still get `allow_capture_invoke = False`.

**TP5 fix (lines 5318-5324):** Inside the Fn-trait-bound scanning loop, before setting `allow_capture_invoke = False`:
1. Checks if the lambda has borrowed explicit captures (ref/ref_mut).
2. If so, skips the override. The lambda keeps `allow_capture_invoke = True`.

**Files NOT changed:** `type_checker.py`, `borrow_checker_pass.py`, `driftc.py`, `call_contract.py`. No other files touched.

#### New tests

`lang/tests/driver/test_fn1_scope_borrowed_capture.py` — 2 driver-level tests:
1. `test_fn1_bounded_scope_borrowed_capture_accepted` — positive: generic `F is Fn1<Int, Void>` + `captures(&x)` → no "closures with borrowed captures" error.
2. `test_non_fn_bounded_generic_borrowed_capture_still_rejected` — negative: generic `F is Marker` (non-Fn* bound) + `captures(&x)` → still rejected. Guards against over-broad TP4 relaxation.

#### Newly discovered regressions

None.

#### Go/no-go for Phase F2

**GO** — with caveats:
- F1 lifts the type checker gate for Fn-bounded borrowed captures. The lambda now reaches the borrow checker.
- However, no e2e test yet exercises `conc.scope` with an actual `captures(&x)` lambda through full codegen. F2 should add this.
- The borrow checker's SCOPED path is proven by 22 unit tests. The remaining gap is the end-to-end path through MIR lowering and LLVM codegen, which may surface issues with the lambda's capture environment struct layout or calling convention.
- F2 scope should include: (1) e2e `conc.scope` + borrowed capture test, (2) verification that captureless Fn1 lambdas are unaffected, (3) negative e2e tests (spawn + borrowed capture → reject).

---

### 12. Phase F2 implementation

**Date:** 2026-02-21
**Author:** Klaudia
**Status:** Complete — **STOP for review** (blocker found: F2-D1)

#### F2 checklist (copied from todo.md template)

- [x] Added/updated F2 e2e test for SCOPED borrowed-capture accept (full codegen/runtime).
  - **BLOCKED by F2-D1.** Test created (`scope_fn1_borrowed_capture_accepted`) but marked `skip: true`. Lambda passes type checker (F1 fix works) but crashes at MIR lowering: `NotImplementedError: No MIR lowering for expr HLambda`. Root cause: monomorphization gap (see F2-D1 below).
- [x] Added/updated F2 e2e test for SCOPED borrowed-capture non-outliving reject (`E_ESCAPE_SCOPE`).
  - **N/A at e2e level.** E_ESCAPE_SCOPE requires synthetic MIR block layout not constructible from Drift source. Covered by 3 unit tests in `test_escape_level_model.py`.
- [x] Added/updated F2 e2e test for THREAD borrowed-capture reject (`E_ESCAPE_THREAD`).
  - **Already covered** by existing `borrow_escape_spawn_rejected` e2e test (5/5 passes). No additional test needed — `conc.spawn` uses `Callback0<T>`, not Fn-bounded generic. No stdlib function combines `F is Fn1<...>` with THREAD escape annotation.
- [x] Confirmed `lang/tests/driver/test_fn1_scope_borrowed_capture.py` passes. (2/2)
- [x] Confirmed `test_escape_level_model.py` passes. (22/22)
- [x] Confirmed A5 e2e boundary set passes:
  - `borrow_escape_spawn_rejected` — ok
  - `borrow_escape_scope_accepted` — ok
  - `borrow_escape_thread_accepted` — ok
  - `implicit_callback_borrowed_capture_rejected` — ok
  - `borrowed_capture_interface_coercion_rejected` — ok
- [x] Confirmed boundary guard/contract tests pass:
  - `test_callinfo_param_layout_contract.py` — 11 passed
  - `test_boundary_matrix_result_variant_contract.py` — 4 passed
  - `test_struct_ref_field_boundary_contract.py` — 8 passed
  - `test_call_contract_ownership_guard.py` — 3 passed
- [x] Confirmed full borrow checker suite: 89 passed
- [x] Confirmed full stage2 suite: 86 passed
- [x] Documented newly discovered compiler defect (F2-D1) with minimal repro + subsystem analysis.
- [x] Added F2 go/no-go recommendation (see below).

#### F2 scope analysis

**Planned tests 3–7 assessment:**

| # | Test | E2e feasibility | Status |
|---|------|-----------------|--------|
| 3 | `scope_fn1_borrowed_capture_accepted` — `conc.scope` + `captures(&x)` through full codegen | **Created, skip=true** — blocked by F2-D1 (MIR lowering crash) | **BLOCKED** |
| 4 | `scope_fn1_move_capture_accepted` — `conc.scope` + `captures(copy x)` no regression | **Created, skip=true** — blocked by pre-existing limitation (capturing lambdas cannot be coerced to function pointers) | **BLOCKED** |
| 5 | `scope_fn1_borrowed_capture_non_outliving_rejected` — E_ESCAPE_SCOPE | **Not constructible** at e2e level — requires synthetic MIR block layout; covered by `test_scoped_spawn_with_non_outlying_borrow_rejected` + `test_scoped_spawn_nested_block_false_positive` in unit tests | N/A (unit-level only) |
| 6 | `spawn_borrowed_capture_still_rejected` — E_ESCAPE_THREAD | **Already covered** by existing `borrow_escape_spawn_rejected` e2e test (passes) | Confirmed |
| 7 | `fn1_bounded_thread_annotated_rejected` — Fn1 + THREAD + borrowed capture | **Not constructible** — no stdlib function combines `F is Fn1<...>` bound with THREAD escape annotation; `conc.spawn` uses `Callback0<T>` (not Fn-bounded generic) | N/A (no stdlib surface) |

**Why tests 5 and 7 are not constructible at e2e level:**

- **Test 5:** E_ESCAPE_SCOPE requires a captured loan whose place is not defined before the scope call in the *direct enclosing MIR basic block*. At the Drift source level, lexical scoping ensures any referenceable variable is defined in an accessible scope. The conservative rejection only surfaces when MIR basic block boundaries split variable definitions from their use — a synthetic scenario already covered by 3 borrow checker unit tests.

- **Test 7:** `conc.spawn` takes `core.Callback0<T>`, not a generic `F is Fn*`. `conc.scope` is the only stdlib function with `F is Fn1<...>` bound, and it has SCOPED (not THREAD) escape annotation. Creating a user-defined function with THREAD escape level would require the `analyze_non_retaining_params` driver phase to classify it as THREAD, which depends on internal thread-spawning behavior not expressible in user code.

**Risk analysis for F2 (realized):**

| Risk | Status | Outcome |
|------|--------|---------|
| Borrowed capture lambda fails at MIR lowering | **Realized** | F2-D1: `NotImplementedError: No MIR lowering for expr HLambda` |
| Copy capture lambda regresses from F1 changes | **Confirmed pre-existing** | "capturing lambdas cannot be coerced to function pointers" — same root cause as F2-D1 |
| Existing A5 boundary tests regress | **Retired** | All 5/5 pass, all guard/contract tests pass |

#### F2-D1: Monomorphization gap for capturing lambdas in Fn-trait-bounded generics

**Severity:** Blocker for `conc.scope` + captured lambda user scenario.

**Minimal repro:**
```drift
module m
import std.core as core;
fn apply<F>(f: F) nothrow -> Void require F is core.Fn1<Int, Void> { f.call(42); }
fn main() nothrow -> Int {
	var x: Int = 10;
	apply(|_a| captures(&x) => {});
	return 0;
}
```

**What happens:**
1. **Type checker:** Accepts the lambda (F1 fix works — `allow_capture_invoke` stays True for Fn*-bounded params with borrowed captures).
2. **Borrow checker:** Would accept (SCOPED promotion path, proven by 22 unit tests).
3. **MIR lowering:** Crashes with `NotImplementedError: No MIR lowering for expr HLambda` at `hir_to_mir.py:1039`.

**Root cause chain:**

1. **Surface:** `_lower_expr_raw` dispatches by HIR expression type. There is no `_visit_expr_HLambda` handler. HLambda is handled only in specific contexts: intrinsic args (line 2348), immediate calls (line 2407-2408), and `_lower_lambda_callback` (for callback0/1/2 wrapping). A bare HLambda as a non-intrinsic call arg falls through to `NotImplementedError`.

2. **Structural:** The call resolver (TP4/TP5/TP10) transforms lambdas before MIR lowering in two ways:
   - **Captureless:** Coerced to function pointer (`allow_capture_invoke = False` → type checker treats as fn ptr). No HLambda remains.
   - **Callback-typed:** Wrapped in `callback0/1/2(lambda)` (TP10). The intrinsic handler extracts the lambda to a hidden function + env struct. HLambda is consumed.
   - **Fn-bounded + captures (F1 case):** Neither path applies. The lambda keeps `allow_capture_invoke = True` (no fn ptr coercion) and the param is Fn1-typed, not Callback-typed (no TP10 wrapping). A bare HLambda with captures reaches MIR lowering.

3. **Architectural (root cause):** Generic monomorphization instantiates `F` to a function pointer type (the lambda's inferred type). Function pointers are a single value — no room for capture environments. The monomorphized body of `apply` calls `f.call(42)` as a direct function call, expecting a single fn ptr argument. But a capturing lambda needs both a fn ptr AND an env ptr.

   This is a variant of the problem Rust solves with unique closure types + Fn trait impls. Drift's monomorphization model doesn't generate unique types for capturing lambdas, so there's no way to carry capture environments through generic `F is Fn1<A,R>` parameters.

**Why copy captures have the same limitation:**

Copy-capturing lambdas (e.g., `captures(copy x)`) hit the same wall earlier — at the type checker. With `allow_capture_invoke = False` (F1 fix doesn't apply to copy captures), the type checker tries to coerce the lambda to a function pointer, which fails: "capturing lambdas cannot be coerced to function pointers". The F1 fix only relaxes the borrowed-capture-specific rejection message.

**Relationship to INV-1 (Approach A):**

This confirms that INV-1 (Callback1 doesn't implement Fn1) is the deeper blocker. Even if MIR lowering had an HLambda handler, the monomorphized call site would not pass the env pointer correctly. A correct fix requires one of:
1. **Closure type generation:** Generate a unique type for each capturing lambda that carries the env struct. Add `implement Fn1 for <ClosureType>`. Monomorphize `apply` with the closure type.
2. **Fn1 impl for Callback:** `implement Fn1 for Callback1`. Then callback-wrapping (Approach A) would work.
3. **Env-passing calling convention:** Monomorphize Fn-bounded generics to pass an implicit env pointer alongside the function pointer.

All three are significant architectural changes beyond F2 scope.

**E2e tests added:**
- `scope_fn1_borrowed_capture_accepted/` — skip=true, documents desired behavior + F2-D1 blocker.
- `scope_fn1_move_capture_accepted/` — skip=true, documents desired behavior + pre-existing limitation.

#### F2 go/no-go recommendation

**NO-GO for closure.** F2 validation reveals F2-D1: the `conc.scope` + captured lambda user scenario is blocked at MIR lowering by a monomorphization architecture gap. The F1 type checker fix is correct and valuable (lifts the borrowed-capture rejection for Fn-bounded generics), but the end-to-end path requires additional compiler work beyond the escape context model.

**What F1 accomplished (still valid):**
- Type checker no longer rejects borrowed-capture lambdas for Fn*-bounded generic params. This is the correct semantic decision.
- Borrow checker SCOPED/THREAD/STATIC enforcement is complete and proven by 22 unit tests.
- All existing safety boundaries are preserved (spawn, callback coercion, interface boxing).

**What remains:**
- MIR lowering path for capturing lambdas as generic call args (F2-D1).
- This is a monomorphization/calling convention issue, not an escape context model issue. Recommend filing separately under a compiler architecture item.

**Recommended next action:**
1. Keep F1 fix in place (correct semantic, no regressions).
2. Keep skipped e2e tests as documentation of the desired behavior.
3. File F2-D1 as a separate monomorphization item (outside borrow-checker-escape-context-model scope).
4. Close the borrow-checker-escape-context-model work item — the escape context model is complete. The remaining gap is in the monomorphization/lambda-lowering subsystem.
