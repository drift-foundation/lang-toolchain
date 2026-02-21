# Borrow Checker Escape Context Model — Work Progress

Author: Klaudia
Current focus: Fn1 SCOPED borrowed-capture coercion (assessment → implementation)

---

## Completed work (reference only)

**A5 (escape context model):** All phases 0–5 complete. `EscapeLevel` enum, `Loan.max_escape`, `_check_lambda_scope_escape`, SCOPED/THREAD/STATIC boundary enforcement, `param_nonretaining` fully removed. 22 tests in `test_escape_level_model.py`. Review checklist satisfied.

**A1 (call contract single seam):** Slices 1–4 complete. `call_contract.py` owns all call-shape decisions (arity, kwargs, ctor fields, intrinsic shape, array method arity). 37 contract/guard tests. Anti-regression guard (`test_call_contract_ownership_guard.py`) prevents drift.

---

## Known limitations (carry-forward)

1. **SCOPED + capturing lambdas blocked by type checker.** The type checker's function-pointer coercion path rejects any capturing lambda passed to a generic `F is Fn1<A, R>` parameter (`conc.scope`'s shape). The borrow checker's SCOPED acceptance path is fully exercised by unit tests but cannot be exercised e2e until the type system allows capturing lambdas in `Fn1`-bounded generic positions. **This is the target of the Fn1 assessment below.**

2. **`_place_is_defined_before_stmt` is conservative (MVP §3.6).** Only the direct enclosing block is checked for place definition. Borrows defined in predecessor or nested blocks are rejected even if provably safe. Full dataflow-based lifetime reasoning is deferred. `test_scoped_spawn_nested_block_false_positive` is the pinned regression for this behavior.

---

## Fn1 SCOPED Borrowed-Capture Assessment

Date: 2026-02-21
Author: Klaudia
Status: assessment complete; awaiting investigation verdicts and owner go/no-go before implementation.

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

Based on the analysis, **Approach A (callback-wrap Fn1-bounded captures)** is recommended. It has the smallest blast radius, reuses existing infrastructure, and does not require driver pipeline changes.

#### Phase F1 — Fn1-bounded callback wrapping (single slice)

**Goal:** When a generic call has `require F is Fn1/Fn2/...` bound and the lambda arg has captures, wrap it in `callback1(lambda)` with `_is_implicit_wrap = True` so it bypasses the function-pointer coercion rejection.

**Scope (files to change):**
1. `lang/driftc/checker/call_resolver.py` — TP4 (lines 5227-5240) and TP5 (lines 5277-5314): detect Fn-trait-bounded params and wrap capturing lambdas instead of forcing `allow_capture_invoke = False`.
2. Possibly `lang/driftc/checker/call_resolver.py` — generic instantiation logic: ensure `Callback1<A, R>` satisfies `require F is Fn1<A, R>` (may already work via trait implementation).

**Files NOT changed:**
- `type_checker.py` — existing `_is_implicit_wrap` guard in callback handler and `allow_capture_invoke = True` path handle this.
- `borrow_checker_pass.py` — transparent wrapper propagation (TP11) already propagates escape level through callback wrappers.
- `driftc.py` — escape annotations unchanged.

**Go/no-go criteria before starting:**
1. INV-1 resolved as "yes" (Callback1 satisfies Fn1 bound). If not, Approach B must be revisited.
2. All A1 + A5 regression suites green on current branch.
3. Owner confirms approach.

**Risk:**
- **Generic instantiation mismatch (medium):** If `scope<F>(f: F)` requires `F` to unify with a bare function type (not Callback1), the callback-wrapped lambda will fail type instantiation. Investigation needed. If blocked, a `Fn1`-aware coercion path must be added.
- **Callback trait semantic difference (low):** `Callback1<A,R>` vs `Fn1<A,R>` may differ in throw semantics. `Fn1` implies nothrow; `Callback1` may imply throw. Need to check if `callback1` vs `callback_throw1` is correctly selected.
- **Codegen shape (low):** MIR lowering for callback-wrapped args differs from bare function pointers. If `scope`'s generic instantiation expects a bare function pointer calling convention in codegen, the wrapper may cause a runtime failure. Needs e2e validation.

**Phase F1 done-when criteria (exact artifacts):**

1. **Regression-first gate:**
   - [ ] Minimal failing regression test added **before** any compiler fix. Test: `test_fn1_bounded_scope_borrowed_capture_accepted` in `lang/tests/borrow_checker/test_escape_level_model.py`.
   - [ ] Test exercises: `conc.scope`-shaped generic with `require F is Fn1<...>` + lambda with `ref`/`ref_mut` capture → currently fails with "closures with borrowed captures are non-escaping in v0".
   - [ ] Test confirmed **failing** on current branch before fix (paste output or screenshot reference).

2. **Compiler fix:**
   - [ ] Smallest viable change applied (list every touched file + function name).
   - [ ] Failing regression from step 1 now **passes** after fix.
   - [ ] Fix is scoped to Fn-trait-bounded generic params with capturing lambdas only. Captureless lambdas and non-Fn-bounded params are unchanged.

3. **Diagnostic wording preservation:**
   - [ ] Zero change to existing A1 diagnostic wording (all 12 checker message assertions unchanged).
   - [ ] Zero change to existing A5 E_ESCAPE_* diagnostic codes or messages.
   - [ ] `borrowed_capture_interface_coercion_rejected` e2e still emits phase="typecheck" (type checker safety path preserved).

4. **Mandatory regression matrix (all green):**
   - [ ] `lang/tests/borrow_checker/test_escape_level_model.py` — all 22+ tests pass.
   - [ ] A5 e2e boundary set:
     - [ ] `borrow_escape_spawn_rejected`
     - [ ] `borrow_escape_scope_accepted`
     - [ ] `borrow_escape_thread_accepted`
     - [ ] `implicit_callback_borrowed_capture_rejected`
     - [ ] `borrowed_capture_interface_coercion_rejected`
   - [ ] Boundary guard/contract tests:
     - [ ] `test_callinfo_param_layout_contract.py`
     - [ ] `test_boundary_matrix_result_variant_contract.py`
     - [ ] `test_struct_ref_field_boundary_contract.py`
     - [ ] `test_call_contract_ownership_guard.py`

5. **Deliverables in `work-progress.md`:**
   - [ ] Updated investigation verdicts (INV-1, INV-2, INV-3) with evidence.
   - [ ] Files/functions changed listed.
   - [ ] Pass/fail matrix with command output references.
   - [ ] Any newly discovered regressions documented with minimal repro + subsystem guess.
   - [ ] Explicit go/no-go recommendation for F2.

#### Phase F2 — Validation and cleanup (follow-up if F1 succeeds)

**Goal:** Stabilize the path with full e2e coverage and clean up any transitional hacks.

**Scope:**
1. Add e2e test for SCOPED reject (currently deferred — see known limitation 1).
2. Verify no regression in existing callback/Fn1 patterns (non-capturing lambdas still work as bare function pointers).
3. Document the callback-wrapping behavior for Fn-bounded generic params.

### 7. Regression tests (planned)

#### Positive (must-accept after fix):
1. `test_scope_borrowed_capture_accepted_e2e`: `conc.scope(|s| [&x] => { s.spawn(|| [&x] => { print(x) }) })` where `x` is defined before the scope call → compiles and runs correctly.
2. `test_fn1_bounded_generic_borrowed_capture_accepted`: Synthetic generic `fn apply<F>(f: F) require F is Fn1<Int, Void>` called with a lambda capturing `&x` where the callee is LOCAL-annotated → accepted by borrow checker.
3. `test_scope_move_capture_still_accepted`: `conc.scope(|s| [x] => { ... })` (move capture, no borrow) → still works (no regression from wrapping changes).

#### Negative (must-reject after fix):
4. `test_scope_borrowed_capture_non_outliving_rejected`: `conc.scope(|s| => { var y = 42; s.spawn(|| [&y] => { ... }) })` → E_ESCAPE_SCOPE (y defined inside scope, does not outlive).
5. `test_spawn_borrowed_capture_still_rejected`: `conc.spawn(|| [&x] => { ... })` → E_ESCAPE_THREAD (THREAD boundary, not SCOPED — no regression).
6. `test_fn1_bounded_thread_annotated_rejected`: Generic `F is Fn1<...>` with THREAD annotation + borrowed capture → E_ESCAPE_THREAD.

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

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Generic instantiation rejects `Callback1` for `Fn1` bound | Medium | Blocks Approach A | Pre-investigation: check trait impl. If blocked, evaluate Approach B |
| Callback wrapping changes codegen calling convention | Low | Runtime failure in e2e | Test with `borrow_escape_scope_accepted` extended to use captures |
| Captureless Fn1 lambdas accidentally wrapped | Low | Performance regression (extra indirection) | Only wrap when captures are present; bare lambdas keep current path |
| `borrowed_capture_interface_coercion_rejected` e2e regresses | Medium | Type checker's safety net bypassed | Carefully scope wrapping to Fn-trait-bounded generic params only — not direct Callback-typed params |
| Non-retaining analysis ordering breaks | Low | Wrong escape levels | No driver ordering changes in Approach A |

### 9. Investigation items (verdict checklist)

#### INV-1: Does `Callback1<A, R>` satisfy `require F is Fn1<A, R>`?
- **Question:** Check trait implementations in `std.core`. If Callback1 does not implement Fn1, generic instantiation will fail after callback-wrapping. This is the critical go/no-go gate for Approach A.
- **Resolved:** [ ] yes / [ ] no
- **Evidence:** _(link to grep/command output or file:line)_
- **Owner:** Klaudia
- **Date resolved:** —
- **Verdict commands:**
  ```
  grep -n "implement.*Fn1.*for.*Callback1\|implement.*Callback1.*Fn1" stdlib/std/core/core.drift
  grep -rn "Callback1" stdlib/std/core/ | head -20
  ```

#### INV-2: Calling convention difference (monomorphization shape)
- **Question:** When `scope` is monomorphized with `F = Callback1<Scope, Void>` (after wrapping), the codegen may generate different calling code than for `F = fn(Scope) -> Void`. Does the generic body call `f.call(s)` via `Fn1.call`? If so, Callback1 must implement `Fn1.call`.
- **Resolved:** [ ] yes / [ ] no
- **Evidence:** _(link to grep/command output or file:line)_
- **Owner:** Klaudia
- **Date resolved:** —
- **Verdict commands:**
  ```
  grep -n "\.call(" stdlib/std/concurrent/concurrent.drift | head -10
  grep -n "Fn1" lang/driftc/checker/__init__.py | head -20
  ```

#### INV-3: Does `_wrap_explicit_capture_callbacks` already handle this?
- **Question:** The fallback at line 5210 wraps lambdas when initial resolution fails. Does `scope(capturing_lambda)` trigger the fallback? If so, the wrapping may already occur for some shapes but the re-typing at TP4/TP5 undoes it.
- **Resolved:** [ ] yes / [ ] no
- **Evidence:** _(link to grep/command output or file:line)_
- **Owner:** Klaudia
- **Date resolved:** —
- **Verdict commands:**
  ```
  # Static: trace control flow from line 5189 (resolution attempt) through 5210
  # (_wrap_explicit_capture_callbacks call on ResolutionError) to 5227 (post-resolution
  # override).  If resolution succeeds on the first try, the fallback never fires.
  grep -n "_wrap_explicit_capture_callbacks\|ResolutionError" lang/driftc/checker/call_resolver.py | head -10

  # Dynamic: run the existing borrow_escape_scope_accepted e2e with DRIFT_DEBUG_RESOLVER=1
  # (if available) or add a one-line `print("WRAP_FALLBACK", expr.fn.name)` inside
  # _wrap_explicit_capture_callbacks, run the e2e, then revert.  Capture output as evidence.
  # Preferred: static trace above is sufficient if resolution path is unambiguous.
  ```

### 10. Recommendation

**Approach A (callback-wrap Fn1-bounded captures)** is the recommended path, contingent on INV-1 being resolved as "yes". If Callback1 does not implement Fn1, Approach B (move escape annotation timing earlier) should be evaluated as the fallback.

Implementation prerequisites:
1. INV-1, INV-2, INV-3 resolved with verdicts filled in (section 9).
2. Owner reviews this assessment and confirms the approach.
3. All A1 + A5 regression suites are green.
4. Phase F1 done-when criteria (section 6) used as the acceptance checklist.
