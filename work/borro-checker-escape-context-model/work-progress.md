# A5 Execution Notes — COMPLETE (2026-02-21)

All phases (0–5) implemented. Review checklist satisfied. Ready for owner sign-off.

---

## Files changed (all phases)

| File | Change |
|------|--------|
| `lang/driftc/borrow_checker.py` | Added `EscapeLevel(IntEnum)` enum; `max_escape: EscapeLevel = EscapeLevel.LOCAL` on `Loan` |
| `lang/driftc/borrow_checker_pass.py` | `_clone_loans_from_ref` propagates `max_escape`; added `_captured_loan_binding_ids`, `_lambda_escape_level`, `_report_escape_violation` (E_ESCAPE_THREAD/SCOPE/STATIC/STORE), `_check_lambda_escape_level`, `_check_lambda_scope_escape`, `_place_is_defined_before_stmt` (HLet+HAssign), `_current_stmt_index`/`_current_block_stmts` fields, `_free_fn_escape_sig` cache + `_resolve_sig_for_call` fallback, `_is_callback_wrapper_call`, `_unwrap_callback_lambda`, HLet+HReturn v0 blanket escape check; replaced call-site dispatch in HCall/HMethodCall/HInvoke; removed `param_nonretaining` at 3 sites |
| `lang/driftc/checker/__init__.py` | Added `param_escape_level` field and `effective_param_escape_level(i)` to `FnSignature`; removed SCOPED→LOCAL bridge; removed `param_nonretaining` field and fallback |
| `lang/driftc/driftc.py` | Added `_STDLIB_ESCAPE_ANNOTATIONS` injection step (spawn/scope/vt_spawn/registry → THREAD/SCOPED/STATIC); removed callback0/1/2 entries after Phase 3c BLOCKER resolution |
| `lang/driftc/stage1/lambda_validate.py` | Removed item 2 (escape enforcement); item 1 (capture discovery) only |
| `lang/driftc/checker/call_resolver.py` | `_is_implicit_wrap=True` at 4 synthesis sites; borrow-capture guard in callback0 handler (typecheck-phase rejection for user-written `callback0(borrow_lambda)`) |
| `lang/driftc/type_resolver.py` | Removed `param_nonretaining` local variable, param loop append, CALLBACK special case, constructor arg |
| `lang/driftc/type_checker.py` | `_nonretaining_param_state`: reads `param_escape_level`; IMMEDIATE/LOCAL/SCOPED → True, THREAD/STATIC → False |
| `lang/driftc/stage1/non_retaining_analysis.py` | Added `_pel_to_nr` + `_build_pel` helpers; writes `param_escape_level` instead of `param_nonretaining`; IMMEDIATE/LOCAL → True, THREAD/STATIC → False, SCOPED/None → None; `_build_pel` preserves stricter pre-seeded levels, clears stale ones on `v is False` |
| `lang/tests/borrow_checker/test_escape_level_model.py` | New: 22 tests (Phases 0–4); see inventory below |
| `lang/tests/stage1/test_lambda_validation.py` | Deleted 9 item-2 tests + 2 helpers; 7 item-1 tests retained |
| `lang/tests/stage1/test_non_retaining_function_params.py` | Updated 4 assertions to `param_escape_level`; added 2 regressions (`test_pre_seeded_local_downgraded_to_retaining`, `test_immediate_level_treated_as_non_retaining`) |
| `lang/tests/codegen/e2e/implicit_callback_borrowed_capture_rejected/expected.json` | Updated to phase="borrow_check" + E_ESCAPE_THREAD message |
| `lang/tests/codegen/e2e/borrow_escape_spawn_rejected/` | New: borrowed capture to `conc.spawn` → E_ESCAPE_THREAD |
| `lang/tests/codegen/e2e/borrow_escape_scope_accepted/` | New: captureless lambda to `conc.scope` → exit 0 |
| `lang/tests/codegen/e2e/borrow_escape_thread_accepted/` | New: captureless lambda to `conc.spawn` + join → exit 0 |

---

## Test inventory — `test_escape_level_model.py` (22 tests)

**Phase 0 (3):** `test_escape_level_ordering`, `test_loan_default_max_escape`, `test_loan_max_escape_propagation`

**Phase 1 (3):** `test_lambda_no_borrow_capture_is_static`, `test_lambda_ref_capture_is_local`, `test_lambda_mut_ref_capture_is_local`

**Phase 2 (4):** `test_borrowed_capture_to_thread_param_rejected`, `test_borrowed_capture_to_local_param_accepted`, `test_no_borrow_capture_to_thread_accepted`, `test_check_block_spawn_thread_escape_rejected`

**Phase 3a (3):** `test_scope_outer_closure_annotated_scoped_returns_scoped`, `test_sort_in_place_comparator_local_accepted`, `test_static_level_dry_run`

**Phase 3b (4):** `test_trait_object_callback_unannotated_thread_default`, `test_hashmap_iter_callback_local_accepted`, `test_spawn_thread_annotation_rejected`, `test_registry_set_dropper_static_annotation_rejected`

**Phase 3c (1):** `test_spawn_cb_ref_capture_caught_by_borrow_checker_directly`

**Phase 4 (4):** `test_scoped_spawn_with_outlying_borrow_accepted`, `test_scoped_spawn_with_non_outlying_borrow_rejected`, `test_scoped_spawn_nested_block_false_positive` (pinned conservative false positive — must not be converted to accept), `test_scoped_spawn_assigned_before_scope_accepted`

---

## Final checkpoint (2026-02-21)

```
lang/tests/stage1/test_non_retaining_function_params.py: 7/7
lang/tests/stage1/test_lambda_validation.py: 7/7
lang/tests/borrow_checker/: 89 (22 in test_escape_level_model.py)
lang/tests/driver/test_callinfo_param_layout_contract.py: 11/11
lang/tests/driver/test_explicit_capture_diagnostics.py: 10/10
lang/tests/driver/test_boundary_matrix_result_variant_contract.py: 4/4
lang/tests/driver/test_struct_ref_field_boundary_contract.py: 8/8
e2e:
  borrow_escape_thread_accepted: ok
  borrow_escape_spawn_rejected: ok
  borrow_escape_scope_accepted: ok
  result_ok_move_conn_source_drop_regression: ok
  struct_ref_field_result_ok_move_drop_once: ok
```

---

## Post-Phase-5 gap closure (2026-02-21)

Three review checklist items were ambiguous after Phase 5. Decisions agreed with owner:

1. **`borrow_escape_static_rejected` e2e — resolved as driver-level.**
   `test_registry_set_dropper_static_annotation_rejected` in `test_escape_level_model.py` covers E_ESCAPE_STATIC via a real STATIC-annotated stdlib sig. STATIC annotations target intrinsic-only paths not callable from user Drift source; an e2e shape would be brittle. No synthetic annotations added. Checklist updated.

2. **THREAD accept e2e — added.**
   `borrow_escape_thread_accepted/` — captureless lambda to `conc.spawn` + join, exit 0. Closes the THREAD boundary pair.

3. **SCOPE reject e2e — deferred.**
   Blocked by the Fn1 coercion limitation (known limitation 1 below). Reject path covered by unit tests. Checklist updated to reflect accept e2e + unit reject coverage.

---

## Known limitations / hand-off items

1. **SCOPED + capturing lambdas blocked by type checker.** The type checker's function-pointer coercion path rejects any capturing lambda passed to a generic `F is Fn1<A, R>` parameter (`conc.scope`'s shape). The borrow checker's SCOPED acceptance path is fully exercised by unit tests but cannot be exercised e2e until the type system allows capturing lambdas in `Fn1`-bounded generic positions. Type system extension required; out of scope for A5.

2. **`_place_is_defined_before_stmt` is conservative (MVP §3.6).** Only the direct enclosing block is checked for place definition. Borrows defined in predecessor or nested blocks are rejected even if provably safe. Full dataflow-based lifetime reasoning is deferred. `test_scoped_spawn_nested_block_false_positive` is the pinned regression for this behavior.
