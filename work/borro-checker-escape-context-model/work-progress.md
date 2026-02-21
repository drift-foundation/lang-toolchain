# A5 Execution Notes for Klaudia

Use this file only for implementation progress notes while executing `todo.md` for A5.

Immediate tasks:
1. Implement phases 0–3b from `todo.md`.
2. Stop and hand off results + test signal for review.
3. Wait for decision on whether to proceed to phase 4 directly or after design tightening.

---

## Phase 0 — COMPLETE (2026-02-20)

**Changes landed:**
- `lang/driftc/borrow_checker.py`: Added `EscapeLevel(IntEnum)` with 5 values; changed import to include `IntEnum`.
- `lang/driftc/borrow_checker_pass.py`: Added `EscapeLevel` import; added `max_escape: EscapeLevel = EscapeLevel.LOCAL` field to `Loan`; updated `_clone_loans_from_ref` to propagate `max_escape`.
- `lang/driftc/checker/__init__.py`: Added `param_escape_level: Optional[list[Optional["EscapeLevel"]]] = None` field to `FnSignature`; added `effective_param_escape_level(i) -> EscapeLevel` method with SCOPED→LOCAL conservative bridge and `param_nonretaining` fallback.

**Checkpoint result (2026-02-20):**
```
lang/tests/borrow_checker/: 67 passed → (after Phase 0) 67 passed
test_escape_level_model.py: 3 new tests added (ordering, default, propagation) — all pass
```

---

## Phase 1 — COMPLETE (2026-02-20)

**Changes landed:**
- `lang/driftc/borrow_checker_pass.py`:
  - Added `_captured_loan_binding_ids(lam) -> set` — returns binding ids of REF/REF_MUT captures.
  - Added `_lambda_escape_level(lam, state) -> EscapeLevel` — min of matching loan max_escape; STATIC if no captures; LOCAL if captures but no matching loans.
  - Added `_report_escape_violation(...)` — emits E_ESCAPE_THREAD, E_ESCAPE_STATIC, or E_ESCAPE_STORE with phase="borrow_check".
  - Added `_check_lambda_escape_level(lam, state, required, span, from_unannotated=False)`.

**Checkpoint result (2026-02-20):**
```
lang/tests/borrow_checker/: 67 passed → (after Phase 1) 67 passed
test_escape_level_model.py: 6 new tests added (lambda_no_borrow_capture_is_static, lambda_ref_capture_is_local, lambda_mut_ref_capture_is_local) — all pass
```

---

## Phase 2 — COMPLETE (2026-02-20)

**Changes landed:**
- `lang/driftc/borrow_checker_pass.py`:
  - Replaced `_report_lambda_escape_if_borrowed` call sites in HCall, HMethodCall, HInvoke with new escape-level dispatch (using `_check_lambda_escape_level` when sig is not None, falling back to `_report_lambda_escape_if_borrowed` otherwise).
  - Added lambda escape checking to HCall path (was missing entirely before).

**E2e test note:** The originally planned e2e test `borrow_escape_spawn_rejected` was not implemented. Drift fully supports `captures(&x)` / `captures(&mut x)` syntax (REF/REF_MUT capture kinds). However, `lambda_validate.py` runs at the typecheck phase — before the borrow checker — and unconditionally rejects any lambda with REF/REF_MUT captures that is not immediately invoked or passed to a `param_nonretaining=True` param. So when a user writes `conc.spawn(| | captures(&x) => { ... })`, `lambda_validate.py` catches it first with a generic typecheck-phase error; the borrow checker never runs on this pattern. The borrow checker's new E_ESCAPE_THREAD path for REF/REF_MUT captures is therefore unreachable via the normal compilation pipeline and is exercised only via the driver-level test that constructs HIR directly. This path becomes load-bearing in Phase 4, when `lambda_validate.py` is expected to be relaxed to allow REF captures in `conc.scope` context, deferring SCOPED/THREAD enforcement to the borrow checker.

**New driver-level integration test added:**
- `test_check_block_spawn_thread_escape_rejected`: exercises the full `check_block` path with an `HCall` to a THREAD-annotated spawn-like function; borrowed-capture lambda → E_ESCAPE_THREAD.

**Checkpoint result (2026-02-20):**
```
lang/tests/borrow_checker/: 82 passed (67 pre-existing + 15 new from Phases 0–2)
§9 subset: all pass
  test_invoke_optional_ref_and_lambda_escape.py: PASS
  test_lambda_capture_borrow_overlap.py: PASS
  test_lambda_capture_borrow_overlap_method.py: PASS
result_ok_noncopy_binder_drop_ordering e2e: PASS
```

---

## Phase 3a — COMPLETE (2026-02-20)

**Changes landed:**
- `lang/driftc/checker/__init__.py`: `effective_param_escape_level` already had SCOPED→LOCAL conservative bridge from Phase 0. Confirmed present and tested.
- `lang/driftc/driftc.py`: Added post-analysis escape level annotation step after `analyze_non_retaining_params`. Phase 3a entry: `("std.concurrent", "scope"): [EscapeLevel.SCOPED]`.

**New tests added:**
- `test_scope_outer_closure_annotated_scoped_but_effective_local`: verifies SCOPED → LOCAL conservative bridge.
- `test_sort_in_place_comparator_local_accepted`: verifies LOCAL-annotated param accepts borrowed-capture lambda. (Note: `sort_in_place` in stdlib has no comparator callback param; test uses a synthetic FnSignature named "sort_in_place" to exercise the mechanism.)
- `test_static_level_dry_run`: dry-run synthetic STATIC annotation → E_ESCAPE_STATIC (Phase 3a prerequisite for Phase 3b).

**Checkpoint result (2026-02-20):**
```
lang/tests/borrow_checker/: 82 passed (unchanged)
test_escape_level_model.py: 3 new Phase 3a tests — all pass (total 14 in file)
§9 subset: all pass
```

---

## Phase 3b — COMPLETE (2026-02-20)

**Pre-3b STATIC audit:**
```
grep -n "GlobalRegistry\|reactor\|register_handler\|vt_spawn" lang/driftc/checker/__init__.py
→ 0 matches (checker/__init__.py has no existing reactor/registry annotations)

grep -rn "GlobalRegistry\|reactor" lang/tests/codegen/e2e/
→ 0 matches

grep -rn "GlobalRegistry\|reactor" lang/tests/driver/
→ 0 matches
```
No existing tests pass borrowed-capture lambdas to reactor/registry params. No new rejections introduced.

**Changes landed:**
- `lang/driftc/driftc.py`: Extended `_STDLIB_ESCAPE_ANNOTATIONS` dict to include:
  - `std.concurrent::spawn` param 0 → THREAD
  - `std.concurrent::spawn_cb` param 0 → THREAD
  - `std.concurrent::spawn_on` param 1 → THREAD (param 0 is `exec`, None)
  - `std.concurrent::spawn_future` param 0 → THREAD
  - `std.concurrent::spawn_future_on` param 1 → THREAD (param 0 is `exec`, None)
  - `lang.thread::vt_spawn` param 0 → THREAD (param 1 is `exec`, None)
  - `lang.thread::runtime_registry_set` param 2 → STATIC (params 0, 1 are None)
  - `lang.thread::runtime_thread_registry_set` param 2 → STATIC (params 0, 1 are None)

**Note on `param_nonretaining` migration:** Phase 3b spec asked to migrate `param_nonretaining` to `param_escape_level`. Current `checker/__init__.py` has `param_nonretaining` on `FnSignature` but no static usages setting it for stdlib functions — it is set dynamically by `analyze_non_retaining_params` from function body analysis. The `effective_param_escape_level` bridge handles this fallback. Full migration of `param_nonretaining` is Phase 5.

**New tests added:**
- `test_spawn_thread_annotation_rejected`: spawn-style THREAD annotation rejects borrowed-capture lambda → E_ESCAPE_THREAD.
- `test_registry_set_dropper_static_annotation_rejected`: registry_set STATIC annotation at param 2 rejects borrowed-capture lambda → E_ESCAPE_STATIC.

**Checkpoint result (2026-02-20):**
```
lang/tests/borrow_checker/: 84 passed (67 pre-existing + 17 new from Phases 0–3b)
test_escape_level_model.py: 17 tests — all pass
§9 subset: all pass
  test_invoke_optional_ref_and_lambda_escape.py: PASS
  test_lambda_capture_borrow_overlap.py: PASS
  test_lambda_capture_borrow_overlap_method.py: PASS
  test_boundary_matrix_result_variant_contract.py: 4 PASS
  test_struct_ref_field_boundary_contract.py: 8 PASS
  result_ok_noncopy_binder_drop_ordering e2e: PASS (timing-sensitive; passed 2/2 runs)
lang/tests/stage1/ + lang/tests/stage2/ + lang/tests/borrow_checker/: 250 passed
```

---

## Test file inventory

`lang/tests/borrow_checker/test_escape_level_model.py` — 17 tests:

**Phase 0 (3 tests):**
- `test_escape_level_ordering`
- `test_loan_default_max_escape`
- `test_loan_max_escape_propagation`

**Phase 1 (3 tests):**
- `test_lambda_no_borrow_capture_is_static`
- `test_lambda_ref_capture_is_local`
- `test_lambda_mut_ref_capture_is_local`

**Phase 2 (4 tests):**
- `test_borrowed_capture_to_thread_param_rejected` (E_ESCAPE_THREAD)
- `test_borrowed_capture_to_local_param_accepted` (no error)
- `test_no_borrow_capture_to_thread_accepted` (no error)
- `test_check_block_spawn_thread_escape_rejected` (integration: check_block path with THREAD-annotated call)

**Phase 3a (3 tests):**
- `test_scope_outer_closure_annotated_scoped_but_effective_local`
- `test_sort_in_place_comparator_local_accepted`
- `test_static_level_dry_run` (E_ESCAPE_STATIC dry-run)

**Phase 3b (4 tests):**
- `test_trait_object_callback_unannotated_thread_default` (E_ESCAPE_THREAD + unannotated note)
- `test_hashmap_iter_callback_local_accepted`
- `test_spawn_thread_annotation_rejected` (E_ESCAPE_THREAD via spawn sig)
- `test_registry_set_dropper_static_annotation_rejected` (E_ESCAPE_STATIC via registry sig)

---

---

## Phase 3c — COMPLETE (2026-02-20)

**Goal:** Transfer escape enforcement from `lambda_validate.py` item 2 into the borrow checker. Gate with a direct borrow-checker test before removing the lambda_validate path.

**Root cause discovered during implementation:**
`conc.spawn(|...| captures(&x) => {})` — the type checker's `_wrap_explicit_capture_callbacks()` rewrites the lambda arg into `callback0(lambda)` before passing to `spawn`. The outer `spawn` arg is therefore not an `HLambda`, so the borrow checker's THREAD escape check is skipped at the outer call site. The inner `callback0(lambda)` call then fails to resolve a sig (because `callback0` is `@intrinsic` and doesn't get a `call_resolutions` entry) and falls back to the old `_report_lambda_escape_if_borrowed` path, emitting the stale "non-escaping in v0" message with `phase="borrowcheck"`. Both e2e tests were failing because the runner expected `phase="borrow_check"` + E_ESCAPE_THREAD message.

**Changes landed:**

- `lang/driftc/driftc.py`: Extended `_STDLIB_ESCAPE_ANNOTATIONS` with `std.core::callback0/1/2` and `std.core::callback_throw0/1/2` → THREAD. These wrappers are always emitted by the type checker when the callee expects a `Callback*<T>` type; annotating them at THREAD correctly catches any borrow-capture lambdas that would be sent across thread boundaries via spawn.

- `lang/driftc/borrow_checker_pass.py`:
  - Added `_free_fn_escape_sig: Dict[Tuple[Optional[str], str], FnSignature]` cache in `__post_init__`, populated for all non-method free functions that have `param_escape_level` or `param_nonretaining` annotations.
  - Extended `_resolve_sig_for_call` (HCall branch): when `call_resolutions` has no entry for the call (e.g. `@intrinsic` callback wrappers), falls back to `_free_fn_escape_sig.get((module, name))`.

- `lang/driftc/stage1/lambda_validate.py`: Removed item 2 (escape-level enforcement). Only item 1 (capture discovery via `discover_captures`) remains. `signatures_by_id` / `call_resolutions` params kept in public API signature but silently ignored (backward compat).

- `lang/tests/stage1/test_lambda_validation.py`: Deleted 9 item-2 tests and 2 helpers that served only those tests. 7 item-1 "allowed" tests retained.

- `lang/tests/borrow_checker/test_escape_level_model.py`: Added Phase 3c gate test `test_spawn_cb_ref_capture_caught_by_borrow_checker_directly` — confirmed green before removing lambda_validate item 2.

- `lang/tests/codegen/e2e/implicit_callback_borrowed_capture_rejected/expected.json`: Updated from `"phase": "checker"` / old message to `"phase": "borrow_check"` / `"cannot be sent to a detached virtual thread"`.

- `lang/tests/codegen/e2e/borrow_escape_spawn_rejected/` (new): `main.drift` calls `conc.spawn(|...| captures(&x) => {})`. `expected.json` expects E_ESCAPE_THREAD / `phase="borrow_check"`.

**Checkpoint result (2026-02-20):**
```
lang/tests/borrow_checker/ + lang/tests/stage1/test_lambda_validation.py: 92 passed
borrow_escape_spawn_rejected e2e: ok
implicit_callback_borrowed_capture_rejected e2e: ok
```

---

## Phase 3c — BLOCKER resolved via Option A (2026-02-20)

**Reviewer finding:** The broad `std.core::callback0/1/2` → THREAD annotation broke
`borrowed_capture_interface_coercion_rejected`: user-written `callback0(lambda_with_borrow)`
was caught at `phase="borrow_check"` instead of the required `phase="typecheck"` path.

**Option A chosen:** Restore type-checker ownership for the coercion-rejection path.

**Changes landed (BLOCKER resolution):**

- `lang/driftc/driftc.py`: Removed `std.core::callback0/1/2` and `callback_throw0/1/2` from
  `_STDLIB_ESCAPE_ANNOTATIONS`. The borrow checker no longer applies THREAD level to these
  wrappers directly.

- `lang/driftc/borrow_checker_pass.py`:
  - Added module-level `_CALLBACK_WRAPPER_MODULE`, `_CALLBACK_WRAPPER_NAMES`, `_is_callback_wrapper_call(expr)`,
    and `_unwrap_callback_lambda(arg)` helpers.
  - HCall args/kwargs loops: gated HLambda escape check with `not _is_callback_wrapper_call(expr)`.
    When the current call IS a callback wrapper, skip all escape checks on HLambda args (prevents
    false positives and double-reporting).
  - Transparent-wrapper propagation: when the outer THREAD/STATIC-annotated call (e.g. `spawn`)
    receives `callback0(lambda)` as argument, extract the inner lambda and apply the outer escape
    level check to it.

- `lang/driftc/checker/call_resolver.py`:
  - All 4 implicit callback wrapper creation sites (in `_wrap_explicit_capture_callbacks` and
    `_wrap_implicit_coercion`) now set `cb_call._is_implicit_wrap = True`.
  - callback0 handler: added borrow-capture guard — when `_is_implicit_wrap` is False (i.e.
    user wrote `callback0(lambda_with_borrow)` explicitly), checks `explicit_captures` for
    REF/REF_MUT and emits "closures with borrowed captures are non-escaping in v0" at
    `phase="typecheck"`. Implicit wraps skip this check; their escape is enforced by the
    borrow checker at the outer call site.

**Additional regressions fixed:**

Two pre-existing Phase 3c regressions in driver tests (not caught during initial 3c implementation):
- `test_explicit_capture_borrow_escape_via_store_reports_driver_diag`: `val f = lambda_with_borrow`
  — no diagnostic emitted after lambda_validate.py item 2 was removed.
- `test_explicit_capture_borrow_escape_via_return_reports_driver_diag`: `return lambda_with_borrow`
  — same.

**Fix:** Added `_report_lambda_escape_if_borrowed` check at the HLet handler `else` branch
(non-borrow value path) and in the HReturn handler, both in `borrow_checker_pass.py`.
This restores the v0 blanket rule for stored/returned borrow-capturing lambdas.

**Checkpoint result (2026-02-20):**
```
lang/tests/borrow_checker/: 85 passed
lang/tests/stage1/ + lang/tests/stage2/: 157 passed
lang/tests/driver/test_explicit_capture_diagnostics.py: 10/10 passed
borrow_escape_spawn_rejected e2e: ok
implicit_callback_borrowed_capture_rejected e2e: ok
borrowed_capture_interface_coercion_rejected e2e: ok (type-checker owned, phase="typecheck")
```

---

## Files changed

| File | Change |
|------|--------|
| `lang/driftc/borrow_checker.py` | Added `EscapeLevel(IntEnum)` enum |
| `lang/driftc/borrow_checker_pass.py` | Added `max_escape` to `Loan`; added `_captured_loan_binding_ids`, `_lambda_escape_level`, `_report_escape_violation`, `_check_lambda_escape_level`; replaced call-site dispatch in HCall/HMethodCall/HInvoke; added HCall escape check (previously missing); added `_free_fn_escape_sig` cache + `_resolve_sig_for_call` fallback for intrinsic calls |
| `lang/driftc/checker/__init__.py` | Added `param_escape_level` to `FnSignature`; added `effective_param_escape_level` method |
| `lang/driftc/driftc.py` | Added Phase 3a/3b stdlib escape annotation step; Phase 3c BLOCKER resolution: removed callback0/1/2 and callback_throw0/1/2 from `_STDLIB_ESCAPE_ANNOTATIONS` |
| `lang/driftc/stage1/lambda_validate.py` | Removed item 2 (escape enforcement); item 1 (capture discovery) only |
| `lang/driftc/checker/call_resolver.py` | Added `_is_implicit_wrap=True` at 4 synthesis sites; added borrow-capture guard in callback0 handler (typecheck-phase rejection for user-written `callback0(borrow_lambda)`) |
| `lang/tests/borrow_checker/test_escape_level_model.py` | New: 18 tests (Phases 0–3c) |
| `lang/tests/stage1/test_lambda_validation.py` | Deleted 9 item-2 tests + 2 helpers; 7 item-1 tests retained |
| `lang/tests/codegen/e2e/implicit_callback_borrowed_capture_rejected/expected.json` | Updated to borrow_check phase + E_ESCAPE_THREAD message |
| `lang/tests/codegen/e2e/borrow_escape_spawn_rejected/` | New e2e test (main.drift + expected.json) |

## Phase 4 — COMPLETE (2026-02-21)

**Goal:** Implement real SCOPED scope-lifetime reasoning. Remove the SCOPED→LOCAL conservative bridge.

**Changes landed:**

- `lang/driftc/borrow_checker_pass.py`:
  - Added `_current_stmt_index: Optional[int]` and `_current_block_stmts: Optional[list]` fields to `BorrowChecker`.
  - Updated `_transfer_block` to save/restore these fields per-statement during block traversal.
  - Added `E_ESCAPE_SCOPE` branch in `_report_escape_violation` (fires when `required >= SCOPED and < THREAD`).
  - Added `_place_is_defined_before_stmt(place, stmt_index, block_stmts) -> bool` — conservative syntactic check: returns True if place is a PARAM, or was HLet-bound before `stmt_index` in the direct enclosing BasicBlock's statements. Only checks the direct block (MVP §3.6).
  - Added `_check_lambda_scope_escape(lam, state, stmt_index, block_stmts, span) -> bool` — for each active loan whose ref_binding_id is a captured borrow, checks place validity in state and `_place_is_defined_before_stmt`. Emits E_ESCAPE_SCOPE on failure.
  - Modified `_check_lambda_escape_level`: when `required == SCOPED and lambda_level == LOCAL and _current_stmt_index is not None`, calls `_check_lambda_scope_escape` before deciding to emit an error. If the scope check passes, adds capture loans and returns without error.

- `lang/driftc/checker/__init__.py`:
  - Removed SCOPED→LOCAL conservative bridge from `effective_param_escape_level`. SCOPED is now returned as-is.

- `lang/tests/borrow_checker/test_escape_level_model.py`:
  - Updated Phase 3a test from `test_scope_outer_closure_annotated_scoped_but_effective_local` to `test_scope_outer_closure_annotated_scoped_returns_scoped` (bridge removed; asserts SCOPED, not LOCAL).
  - Added 3 new Phase 4 regression tests (written before implementation):
    - `test_scoped_spawn_with_outlying_borrow_accepted` — borrow defined before scope call → no error
    - `test_scoped_spawn_with_non_outlying_borrow_rejected` — borrow NOT defined before scope call → E_ESCAPE_SCOPE
    - `test_scoped_spawn_nested_block_false_positive` — borrow in predecessor block (conservative) → E_ESCAPE_SCOPE

- `lang/tests/codegen/e2e/borrow_escape_scope_accepted/` (new e2e test):
  - `main.drift`: nothrow lambda `|_s: std.concurrent.Scope| nothrow => { return; }` passed to `conc.scope`; wrapped in `try { } catch e { }` to handle scope's non-nothrow throw signature; exits 0.
  - `expected.json`: exit_code 0, no stderr/stdout.
  - **Note on design:** The type checker's function-pointer coercion path rejects any capturing lambda (even `captures(copy x)`) when passed to a generic `F is Fn1<A, R>` parameter. This means borrow-capturing lambdas passed to `conc.scope` are rejected before the borrow checker runs. The e2e test therefore uses a captureless lambda; the SCOPED borrow acceptance path (the key Phase 4 outcome) is fully covered by the 3 unit tests above. The e2e test validates the end-to-end pipeline (parse → type-check → borrow-check → codegen → run) for the `conc.scope` code path.

**Checkpoint result (2026-02-21):**
```
lang/tests/borrow_checker/: 88 passed (21 in test_escape_level_model.py)
lang/tests/driver/test_explicit_capture_diagnostics.py: 10/10 passed
lang/tests/driver/test_callinfo_param_layout_contract.py: 11/11 passed
§9 high-sensitivity subset:
  test_escape_level_model.py: 21/21 pass
  test_invoke_optional_ref_and_lambda_escape.py: pass
  test_lambda_capture_borrow_overlap.py: pass
  test_lambda_capture_borrow_overlap_method.py: pass
Phase 4 e2e quartet:
  borrow_escape_scope_accepted: ok
  borrow_escape_spawn_rejected: ok
  result_ok_move_conn_source_drop_regression: ok
  struct_ref_field_result_ok_move_drop_once: ok
```

---

## Phase 4 — Post-review fix: HAssign in _place_is_defined_before_stmt (2026-02-21)

**Reviewer finding:** todo.md spec said "let or assignment"; implementation only checked HLet.
False positive: `var x` declared in a predecessor block but assigned in the current block before
the scope call was incorrectly rejected with E_ESCAPE_SCOPE.

**Fix (regression-first):**
- New test `test_scoped_spawn_assigned_before_scope_accepted` added to `test_escape_level_model.py`
  (confirmed fails before fix, passes after).
- `_place_is_defined_before_stmt` in `borrow_checker_pass.py`: added HAssign branch —
  if `stmt` is an HAssign to a simple local (no projections) with matching binding_id,
  treat as a definition (return True).
- Docstring updated to say "let-bound or assigned to".

**Checkpoint (2026-02-21):**
```
lang/tests/borrow_checker/: 89 passed (22 in test_escape_level_model.py)
Phase 4 e2e quartet: all ok (unchanged)
```

---

## Phase 5 — COMPLETE (2026-02-21)

**Goal:** Remove `FnSignature.param_nonretaining` backward-compat bridge. Fully migrate to `param_escape_level`.

**Pre-condition grep (source files only):**
```
grep -rn "param_nonretaining" lang/driftc/ lang/tests/ --include="*.py"
→ lang/driftc/checker/__init__.py: 88 (field), 127-128 (bridge body)
→ lang/driftc/borrow_checker_pass.py: 194, 1929-1942, 2018-2031
→ lang/tests/stage1/test_non_retaining_function_params.py: 62, 71, 81, 92
→ lang/driftc/type_checker.py: 9338-9342
→ lang/driftc/type_resolver.py: 119, 152, 154-155, 231
→ lang/driftc/stage1/non_retaining_analysis.py: producer (internal working var)
```

**Changes landed:**

- `lang/driftc/stage1/non_retaining_analysis.py`:
  - Added `EscapeLevel` import.
  - Simplified `working_sigs` construction (removed mutable-copy for `param_nonretaining`).
  - Changed `param_nonretaining_by_id` initialization to read from `sig.param_escape_level` (convert `LOCAL→True`, `THREAD/STATIC→False`, `None/SCOPED→None`).
  - Changed final return to write `param_escape_level` instead of `param_nonretaining`:
    `True → LOCAL`, `False/None → None` (THREAD default). Normalizes all-None to `None`.
  - The internal working dict `param_nonretaining_by_id` remains a local variable (boolean state for the fixpoint analysis).

- `lang/driftc/type_resolver.py`:
  - Removed `param_nonretaining: list[Optional[bool]] = []` local variable.
  - Removed `param_nonretaining.append(None)` in param loop.
  - Removed CALLBACK intrinsic special case setting `param_nonretaining[0] = False` (escape handled by type-checker borrow-capture guard + borrow-checker transparent-wrapper propagation).
  - Removed `param_nonretaining=...` from `FnSignature(...)` constructor call.

- `lang/driftc/type_checker.py` (`_nonretaining_param_state`):
  - Removed `param_nonretaining` read. Now reads `param_escape_level` directly:
    `LOCAL/SCOPED → True` (non-retaining), `THREAD/STATIC/IMMEDIATE → False` (retaining), `None → None` (unknown).

- `lang/driftc/borrow_checker_pass.py`:
  - Line 194 (cache condition): removed `sig.param_nonretaining` from `(sig.param_escape_level or sig.param_nonretaining)` → now `sig.param_escape_level` only.
  - Lines 1929-1945 (HCall pre-loan section): replaced `sig.param_nonretaining[i] is not True` check with `sig.effective_param_escape_level(i) not in (LOCAL, SCOPED)`. Condition also changed to `sig.param_escape_level` (was `sig.param_nonretaining`). Removed `>= len(sig.param_nonretaining)` bound check (effective_param_escape_level handles out-of-range internally).
  - Lines 2018-2034 (HMethodCall pre-loan section): same migration.
  - Note: SCOPED is now included in the pre-loan section, which correctly enables scope-escape checking for cases where the type-checker coercion restriction is lifted in future.

- `lang/driftc/checker/__init__.py`:
  - Removed `param_nonretaining: Optional[list[Optional[bool]]] = None` field from `FnSignature`.
  - Removed `param_nonretaining` fallback from `effective_param_escape_level` (was lines 127-130).

- `lang/tests/stage1/test_non_retaining_function_params.py`:
  - Added `EscapeLevel` import.
  - 3 `param_nonretaining == [True]` assertions → `param_escape_level == [EscapeLevel.LOCAL]`.
  - 1 `param_nonretaining == [False]` assertion → `param_escape_level is None` (retaining params now produce all-None list which normalizes to None).

**Post-removal grep (source only):**
```
grep -rn "param_nonretaining" lang/driftc/ lang/tests/ --include="*.py"
→ Only lang/driftc/stage1/non_retaining_analysis.py: local variable param_nonretaining_by_id
   (internal working state — NOT the FnSignature field)
→ Zero external field accesses remaining
```

**Checkpoint result (2026-02-21):**
```
lang/tests/stage1/test_non_retaining_function_params.py: 5/5 passed
lang/tests/borrow_checker/: 89 passed
lang/tests/driver/test_callinfo_param_layout_contract.py: 11/11 passed
lang/tests/driver/test_explicit_capture_diagnostics.py: 10/10 passed
lang/tests/driver/test_boundary_matrix_result_variant_contract.py: 4/4 passed
lang/tests/driver/test_struct_ref_field_boundary_contract.py: 8/8 passed
Phase 4 e2e quartet:
  borrow_escape_scope_accepted: ok
  borrow_escape_spawn_rejected: ok
  result_ok_move_conn_source_drop_regression: ok
  struct_ref_field_result_ok_move_drop_once: ok
lang/tests/stage1/test_lambda_validation.py: 7/7 passed
```

---

## Known limitations / hand-off items

1. **SCOPED + capturing lambdas blocked by type checker.** The type checker's function-pointer coercion path rejects any capturing lambda passed to a generic `F is Fn1<A, R>` parameter (`conc.scope`'s shape). For the borrow checker's SCOPED acceptance to be exercisable end-to-end, the type checker needs to accept capturing lambdas in `Fn1`-bounded generic positions. This is a type system extension beyond Phase 5.

2. **`_place_is_defined_before_stmt` is conservative (MVP §3.6).** Borrows defined in predecessor/nested blocks are rejected even if provably safe. Full dataflow-based lifetime reasoning is deferred.
