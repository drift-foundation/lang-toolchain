# Code Review Remediation Work Progress

Date started: 2026-02-20
Owner: Codex
Reviewer follow-up: Klaudia

## Scope Source
- Findings source: `work/code-review/todo.md`
- Remediation order agreed with reviewer/user.

## Execution Order

### Batch 1 — Diagnostics hardening (current)
- [x] F4: `driftc.py` intrinsic validation should emit structured diagnostics, not `AssertionError`.
- [x] F11: add `span` to `CallContractIssue` and plumb through call-contract diagnostics.
- [x] F14: checker helper diagnostics should attach non-None spans where source exists.

### Batch 2 — MIR/codegen safety guardrails
- [x] F2: DiagnosticValue drop alloca/dominance fix in LLVM codegen.
- [x] F6: MIR validator for `VariantGetField` / `VariantGetFieldAddr` bounds/type invariants.
- [x] F8: MIR validator SSA operand existence hygiene.

### Batch 3 — Borrow/copy semantics consolidation
- [x] F3: extend `Optional<&T>` loan tracking (`HInvoke`, chained access, capture-sensitive paths).
- [x] F7: reject escaping lambda `&mut` captures across `spawn`/escape boundaries.
- [x] F5: centralize stage2 copy predicate via canonical helper and migrate callsites.

### Batch 4 — Dedup/cleanup
- [x] F9: single-source variant layout in codegen.
- [x] F12: deduplicate forward nominal chain resolution helper.
- [x] F15: centralize bool i1<->i8 coercion helper.
- [x] F10: pre-validate match constructors before CFG mutation.
- [x] F13: remove user-facing `internal:` leakage in checker diagnostics.

## Notes
- F1 from `todo.md` is marked as stale/inverted relative to current fixed regression behavior; keep existing passing regressions as source of truth and guard with additional coverage if needed.
- All LANGUAGE_BUG fixes follow regression-first protocol.
- Boundary Contract Guardrails apply to any stage-boundary shape changes.

## Progress Log
- 2026-02-20: Initialized remediation tracker and agreed execution order.
- 2026-02-20: Completed Batch 1.
  - F4:
    - `lang/driftc/driftc.py` intrinsic contract validation now emits structured `Diagnostic` entries (phase=`typecheck`) with stable codes and spans instead of `AssertionError`.
    - call sites updated to aggregate and report intrinsic diagnostics in both `compile_stubbed_funcs` and CLI path.
  - F11:
    - `lang/driftc/call_contract.py` `CallContractIssue` now includes `span`.
    - checker call-contract diagnostics now consume `issue.span`.
  - F14:
    - checker `_TypingContext.report_*` helpers now accept span source and emit non-empty spans.
    - index/array diagnostic call sites now forward source spans.
  - Added regressions:
    - `lang/tests/driver/test_intrinsic_callinfo_diagnostics.py`
    - `lang/tests/driver/test_index_diagnostics_spans.py`
  - Validation run:
    - `lang/tests/driver/test_intrinsic_callinfo_diagnostics.py` (pass)
    - `lang/tests/driver/test_index_diagnostics_spans.py` (pass)
    - `lang/tests/driver/test_callinfo_param_layout_contract.py` (pass)
    - `lang/tests/driver/test_index_diagnostics.py` (pass)
    - `lang/tests/driver/test_boundary_matrix_result_variant_contract.py` (pass)
    - `lang/tests/driver/test_no_blank_span_fallbacks.py` (pass)
- 2026-02-20: Completed Batch 2.
  - F6:
    - Added `validate_mir_variant_field_invariants(...)` in `lang/driftc/mir_validate.py`.
    - Wired into compile pipeline in `lang/driftc/driftc.py`.
    - Added regression tests: `lang/tests/stage2/test_mir_validate_variant_and_hygiene.py` (valid + invalid field index paths).
  - F8:
    - Added `validate_mir_basic_hygiene(...)` in `lang/driftc/mir_validate.py`:
      - checks undefined SSA operands on key instruction/terminator paths,
      - checks unknown locals for local-sensitive instructions.
    - Wired into compile pipeline in `lang/driftc/driftc.py`.
  - F2:
    - Reworked DiagnosticValue drop lowering in LLVM codegen:
      - added `_ensure_dv_drop_helper()` in `lang/codegen/llvm/llvm_codegen.py`,
      - replaced inline DV-drop allocas in both `_emit_drop_value()` and recursive array-drop helper emission.
    - Added regression test: `lang/codegen/llvm/tests/test_llvm_codegen_dv_drop_helper.py`.
  - Validation run:
    - `lang/tests/stage2/test_mir_validate_variant_and_hygiene.py` (pass)
    - `lang/codegen/llvm/tests/test_llvm_codegen_dv_drop_helper.py` (pass)
    - `lang/tests/driver/test_mir_validate_boundary_diagnostics.py` (pass)
    - `lang/tests/driver/test_codegen_boundary_diagnostics.py` (pass)
- 2026-02-20: Started Batch 3 (F5 partial).
  - Added canonical stage2 helper `_should_copy_value(ty)` in `lang/driftc/stage2/hir_to_mir.py`.
  - Migrated key match-lowering copy predicates (scrutinee move/copy and binder copy decisions) to helper.
  - Initial validation run:
    - `lang/tests/codegen/e2e/result_ok_move_conn_source_drop_regression` (pass)
    - `lang/tests/codegen/e2e/struct_ref_field_result_ok_move_drop_once` (pass)
    - `lang/tests/codegen/e2e/result_ok_array_match_move_no_double_free` (pass)
    - `lang/tests/stage2/test_hir_to_mir_match_requires_binder_indices.py` (pass)
    - `lang/tests/driver/test_boundary_matrix_result_variant_contract.py` (pass)
- 2026-02-20: Completed Batch 3 (F3/F7/F5).
  - F3:
    - `lang/driftc/borrow_checker_pass.py` now tracks Optional-ref originating loans for `HInvoke`.
    - `_borrow_from_optional_ref_call(...)` now peels nested `HField` / `HIndex` / `HPlaceExpr` chains to find the underlying call origin.
    - Added `HInvoke` traversal in ref-use collectors (`_ref_binding_ids_in_expr`, `_collect_ref_uses_in_expr`) to keep region/liveness accounting aligned.
  - F7:
    - `lang/driftc/borrow_checker_pass.py` now reports escaping borrowed-capture lambdas in call/invoke/method arg positions unless param is proven non-retaining.
    - Added helpers `_lambda_has_borrow_capture(...)` and `_report_lambda_escape_if_borrowed(...)`.
  - F5:
    - Migrated remaining stage2 copy/not-copy boolean decisions in `lang/driftc/stage2/hir_to_mir.py` to `_should_copy_value(...)` where semantics are equivalent.
  - Added regressions:
    - `lang/tests/borrow_checker/test_invoke_optional_ref_and_lambda_escape.py`
  - Validation run:
    - `lang/tests/borrow_checker/test_invoke_optional_ref_and_lambda_escape.py` (pass)
    - `lang/tests/borrow_checker/test_lambda_capture_borrow_overlap.py` (pass)
    - `lang/tests/borrow_checker/test_lambda_capture_borrow_overlap_method.py` (pass)
    - `lang/tests/stage2/test_hir_to_mir_match_requires_binder_indices.py` (pass)

- 2026-02-20: Batch 4 progress (F10/F12/F13 done, F15 partial).
  - F10:
    - `lang/driftc/stage2/hir_to_mir.py` now pre-validates all non-default match constructors before dispatch chain emission, preventing partial CFG mutation on bad ctor names.
  - F12:
    - `lang/codegen/llvm/llvm_codegen.py` now centralizes forward nominal resolution and canonicalization via:
      - `_resolve_forward_nominal_typeid(...)`
      - `_canonical_codegen_typeid(...)`
    - Both `_variant_layout(...)` and `_llvm_type_for_typeid(...)` now consume the same canonicalization path.
  - F13:
    - `lang/driftc/checker/__init__.py` user diagnostics no longer expose `internal:` prefixes in checker-facing contract failures.
  - F15 (partial):
    - Started migrating ad-hoc bool coercion callsites to helper usage (`_emit_variant_value(...)` now uses `_bool_to_storage(...)`).
  - Validation run:
    - `lang/codegen/llvm/tests/test_llvm_codegen_dv_drop_helper.py` (pass)
    - `lang/tests/driver/test_callinfo_param_layout_contract.py` (pass)
    - `lang/tests/driver/test_intrinsic_callinfo_diagnostics.py` (pass)
    - `lang/tests/driver/test_index_diagnostics_spans.py` (pass)

- 2026-02-20: Completed remaining Batch 4 (`F15`) and closed `F9`.
  - F15:
    - `lang/codegen/llvm/llvm_codegen.py` now uses centralized bool-storage predicate helper:
      - added `_is_bool_storage_pair(value_llty=..., storage_llty=...)`
      - replaced scattered ad-hoc checks (`store_llty == "i8" and ... == "i1"`) across:
        - dbg keepalive/entry-param stores,
        - struct/variant construction paths,
        - variant field extraction/address paths,
        - load/store-ref paths,
        - copy/tombstone/drop helper variant/struct field paths.
  - F9:
    - Verified variant layout arithmetic is now single-source through `_variant_layout(...)`; drop/copy helper paths consume layout metadata instead of recomputing payload offsets/alignment.
    - Marking this finding resolved as deduped by current implementation shape.
  - Validation run:
    - `py_compile` on edited modules (pass)
    - `lang/codegen/llvm/tests/test_llvm_codegen_dv_drop_helper.py` (pass)
    - `lang/tests/borrow_checker/test_invoke_optional_ref_and_lambda_escape.py` (pass)
    - `lang/tests/stage2/test_hir_to_mir_match_requires_binder_indices.py` (pass)
    - `lang/tests/driver/test_callinfo_param_layout_contract.py` (pass)
    - `lang/tests/driver/test_intrinsic_callinfo_diagnostics.py` (pass)
    - `lang/tests/driver/test_index_diagnostics_spans.py` (pass)
    - `lang/tests/driver/test_boundary_matrix_result_variant_contract.py` (pass)
    - `lang/tests/driver/test_mir_validate_boundary_diagnostics.py` (pass)
    - `lang/tests/driver/test_codegen_boundary_diagnostics.py` (pass)
