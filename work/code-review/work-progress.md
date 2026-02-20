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

- 2026-02-20: Post-batch regression remediation (checker indexing + Result::Ok move/drop handoff).
	  - LANGUAGE_BUG 1 (checker regression):
	    - Symptom: unexpected checker diagnostics `array index must be an Int` in exception/error-param tests and tcp stress case.
	    - Root cause: malformed indentation in `Error.attrs[...]` typing branch made string-key fast-path unreachable.
	    - Fix:
	      - `lang/driftc/checker/__init__.py`
	      - corrected `HIndex` branch for `expr.subject.name == "attrs"` so `Error.attrs` key typing returns DV and does not fall through to array-index diagnostics.
	    - Validation:
	      - `lang/tests/codegen/e2e/exception_result_error_param_by_ref` (pass)
	      - `lang/tests/codegen/e2e/exception_result_error_param_fully_qualified` (pass)
	      - `lang/tests/codegen/e2e/exception_result_error_param_pass` (pass)
	  - LANGUAGE_BUG 2 (stage2 move/drop handoff):
	    - Symptom set:
	      - `maybe_assume_init_read_moves_out_no_leak`, `std_json_leak_*` leaks
	      - `result_ok_move_conn_source_drop_regression` state corruption (exit 21)
	      - `rpc_connect_state_handoff_pure_inmemory`/`struct_ref_field_result_ok_move_drop_once` segfault/double-drop paths.
	    - Root causes:
	      - by-value non-Copy match binders were read from variant payload without consistent ownership transfer behavior.
	      - copy/not-copy classification relied on non-transitive drop-need detection (struct/variant field containment not considered), misclassifying payloads like `Conn` as copyable.
	    - Fixes:
	      - `lang/driftc/stage2/hir_to_mir.py`
	        - non-Copy binder extraction path now uses move semantics (`StoreLocal` + `MoveOut`) and marks arm payload ownership transfer.
	        - scrutinee drop suppression for arms that moved payload ownership.
	        - `_needs_runtime_drop` hardened to structural/transitive recursion via `_needs_runtime_drop_inner(...)` across arrays/structs/variants.
	    - Added regression coverage:
	      - `lang/tests/stage2/test_hir_to_mir_match_by_value_noncopy_move.py`
	        - pins by-value non-Copy binder path to address extraction + move-out semantics on `Optional<String>::Some`.
	    - Validation:
	      - `lang/tests/stage2/test_hir_to_mir_match_requires_binder_indices.py` (pass)
	      - `lang/tests/stage2/test_hir_to_mir_match_by_value_noncopy_move.py` (pass)
	      - `lang/tests/driver/test_boundary_matrix_result_variant_contract.py` (pass)
	      - `lang/tests/codegen/e2e/result_ok_move_conn_source_drop_regression` (pass)
	      - `lang/tests/codegen/e2e/rpc_connect_state_handoff_pure_inmemory` (pass)
	      - `lang/tests/codegen/e2e/struct_ref_field_result_ok_move_drop_once` (pass)
	      - `lang/tests/codegen/e2e/maybe_assume_init_read_moves_out_no_leak` (pass)
	      - `lang/tests/codegen/e2e/std_json_leak_parse_string_loop` (pass)
	      - `lang/tests/codegen/e2e/std_json_leak_stress_parse_loop` (pass)
	      - `lang/tests/codegen/e2e/std_json_leak_stress_parse_loop_drop_only` (pass)
	  - Note for review:
	    - `lang/tests/codegen/e2e/std_net_tcp_stress_connections` is no longer failing with checker diagnostics; in this environment it exits `77` (runtime/sandbox behavior), which is orthogonal to the checker/lowering bugs above.

- 2026-02-20: Ownership decision refactor pass (prep for Klaudia re-review).
	  - Goal:
	    - remove ad-hoc copy/move branching from stage2 hot paths and route decisions through one handler.
	  - Changes:
	    - `lang/driftc/stage2/hir_to_mir.py`
	      - added `_classify_value_transfer(ty, allow_unknown_typevar=False) -> \"copy\"|\"move\"|\"unknown\"`.
	      - `_should_copy_value(...)` is now a thin wrapper over classifier output.
	      - array-index lowering now uses classifier output instead of inline `copy_status` branching.
	  - Why it matters:
	    - ownership-transfer behavior for aggregate payloads now has a single source of truth in stage2, reducing recurrence of divergent copyability logic.
	  - Validation:
	    - `lang/tests/stage2/test_hir_to_mir_match_requires_binder_indices.py` (pass)
	    - `lang/tests/stage2/test_hir_to_mir_match_by_value_noncopy_move.py` (pass)
	    - `lang/tests/driver/test_boundary_matrix_result_variant_contract.py` (pass)
	    - `lang/tests/codegen/e2e/result_ok_move_conn_source_drop_regression` (pass)
	    - `lang/tests/codegen/e2e/rpc_connect_state_handoff_pure_inmemory` (pass)
	    - `lang/tests/codegen/e2e/struct_ref_field_result_ok_move_drop_once` (pass)
	    - `lang/tests/codegen/e2e/maybe_assume_init_read_moves_out_no_leak` (pass)
	    - `lang/tests/codegen/e2e/std_json_leak_stress_parse_loop` (pass)

## Codegen Aggregate Payload Decision Map (for Klaudia review)

Context:
- We observed recurring regressions from duplicated copyability decisions across stage2 and LLVM lowering.
- Stage2 now has a centralized classifier (`_classify_value_transfer`) used by match/result/index paths.
- LLVM side still had localized policy checks and now needs the same audit lens.

Primary LLVM decision sites reviewed:

1) `lang/codegen/llvm/llvm_codegen.py` — `VariantGetField` lowering path (~`_lower_instr`, around lines 2776-2810)
- Previous behavior:
  - ad-hoc `needs_semantic_copy` gate:
    - only for `STRUCT` when `is_copy(field_ty)` and `not is_bitcopy(field_ty)`.
- Risk:
  - policy was shape-specific and disconnected from broader ownership/drop semantics.
  - easy to miss other aggregate forms if support expands.
- Decision/rationale:
  - keep semantic-copy behavior for extracted payload binders, but move toward one classifier-style helper on LLVM side (mirroring stage2 centralization).
  - rationale: payload extraction + subsequent scrutinee/source drop requires one ownership decision source to avoid alias/drop races.

2) `lang/codegen/llvm/llvm_codegen.py` — `_emit_copy_value(...)` (~lines 7156+)
- Behavior:
  - semantic copy implementation for non-bitcopy Copy types.
  - bitcopy short-circuit for trivially copyable values.
- Decision/rationale:
  - preserve as implementation primitive; do not encode policy here.
  - rationale: this function should execute a requested copy, not decide whether a copy is legal/safe at a boundary.

3) `lang/codegen/llvm/llvm_codegen.py` — `_type_needs_drop(...)` (~lines 7285+)
- Behavior:
  - recursive drop-need analysis across arrays/structs/variants and destructible metadata.
- Decision/rationale:
  - use as part of centralized ownership classification on LLVM side.
  - rationale: drop-need is the critical discriminator for aggregate payload handoff safety.

4) `lang/codegen/llvm/llvm_codegen.py` — CopyValue instruction lowering (~`_lower_instr`, around lines 1332+ and 2144+)
- Behavior:
  - respects `is_bitcopy` fast path, otherwise delegates to semantic copy implementation.
- Decision/rationale:
  - retain as execution path only; boundary legality should be decided before emitting `CopyValue`.

Stage2 alignment completed (reference):
- `lang/driftc/stage2/hir_to_mir.py`
  - centralized transfer classifier:
    - `_classify_value_transfer(ty, allow_unknown_typevar=False)`
  - `_should_copy_value(...)` now delegates to classifier.
  - array index path migrated to classifier output (removed local ad-hoc branching).
- Rationale:

- 2026-02-20: Residual-risk follow-up (R4/R5) completed.
  - R5 (variant multi-field drop-in-branch runtime check):
    - Added e2e regression:
      - `lang/tests/codegen/e2e/variant_multifield_drop_in_branch/main.drift`
      - `lang/tests/codegen/e2e/variant_multifield_drop_in_branch/expected.json`
    - Scenario:
      - multi-field variant arm (`Int`, `String`, `Array<Byte>`) created and matched inside both `if`/`else` branch scopes,
      - repeated in a loop to exercise branch-local drop paths.
    - Validation:
      - `PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py -j 1 --debug variant_multifield_drop_in_branch` (pass)
      - `DRIFT_ASAN=1 PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py -j 1 --debug variant_multifield_drop_in_branch` (pass)
  - R4 (DV drop LLVM verifier check):
    - Emitted IR from DV-heavy program:
      - `PYTHONPATH=. ./.venv/bin/python3 -m lang.driftc --stdlib-root stdlib --entry m::main lang/tests/codegen/e2e/diagnostic_value_object_nested_get/main.drift --emit-ir /tmp/dv_drop_verify.ll -o /tmp/dv_drop_verify.bin`
    - Verified IR with LLVM new-pass-manager verifier:
      - `/usr/lib/llvm-20/bin/opt -passes=verify /tmp/dv_drop_verify.ll -disable-output` (pass)

- 2026-02-20: Residual-risk follow-up (R1) completed.
  - R1 (F1 mixed copy/non-copy multi-arm coverage gap):
    - Added e2e regression:
      - `lang/tests/codegen/e2e/result_ok_mixed_payload_arms_drop_ordering/main.drift`
      - `lang/tests/codegen/e2e/result_ok_mixed_payload_arms_drop_ordering/expected.json`
    - Scenario:
      - `Result::Ok(Conn)` where `Conn` contains `Resp` variant with mixed payload arms:
        - `Copy(id: Int)` and `NonCopy(msg: String)`.
      - Executes both arm paths (`check(true)` and `check(false)`) and asserts:
        - source object stays alive (`alive=true`, `drops=0`) throughout arm body,
        - arm discrimination is correct for both payload classes.
    - Validation:
      - `PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py -j 1 --debug result_ok_mixed_payload_arms_drop_ordering` (pass)
      - `DRIFT_ASAN=1 PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py -j 1 --debug result_ok_mixed_payload_arms_drop_ordering` (pass)
      - `DRIFT_MEMCHECK=1 PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py -j 1 --debug result_ok_mixed_payload_arms_drop_ordering` (pass)

- 2026-02-20: Status sync
  - Remaining A1–A5 items are intentionally deferred as separate architectural work.
  - Residual-risk execution items from Klaudia review are now closed (R1/R4/R5).
  - one source of truth for copy/move/unknown decisions at MIR boundaries.

Planned follow-up for LLVM parity:
- Add LLVM-local transfer classifier helper (analogous to stage2) and route `VariantGetField` semantic-copy gate through it.
- Keep `_emit_copy_value` and `_type_needs_drop` as primitives consumed by classifier logic.
- Add one LLVM-focused regression that fails if non-bitcopy aggregate extraction bypasses semantic-copy when source is dropped in same scope.

Status update (2026-02-20):
- Completed LLVM parity centralization for payload extraction policy.
  - `lang/codegen/llvm/llvm_codegen.py`
    - added `_classify_payload_extract_transfer(ty_id)` as single-source ownership decision for by-value payload extracts.
    - `VariantGetField` lowering now routes through classifier:
      - `copy-semantic` -> load + `_emit_copy_value(...)`
      - `copy-bitcopy` -> plain load
      - `move`/`unknown` -> hard internal contract assertion (stage2/checker boundary bug)
- Rationale:
  - removes shape-specific inline heuristics (`struct && is_copy && !is_bitcopy`) from lowering hot path.
  - aligns LLVM behavior with stage2’s centralized transfer-class model.
- Validation:
  - `lang/tests/driver/test_result_ok_copy_struct_string_retain.py` (pass)
  - `lang/tests/driver/test_boundary_matrix_result_variant_contract.py` (pass)
  - `lang/tests/stage2/test_hir_to_mir_match_requires_binder_indices.py` (pass)
  - `lang/tests/stage2/test_hir_to_mir_match_by_value_noncopy_move.py` (pass)
  - `lang/tests/driver/test_mir_validate_boundary_diagnostics.py` (pass)
  - `lang/tests/driver/test_codegen_boundary_diagnostics.py` (pass)
  - e2e:
    - `result_ok_move_conn_source_drop_regression` (pass)
    - `rpc_connect_state_handoff_pure_inmemory` (pass)
    - `struct_ref_field_result_ok_move_drop_once` (pass)
    - `maybe_assume_init_read_moves_out_no_leak` (pass)
    - `std_json_leak_stress_parse_loop` (pass)

Follow-up (2026-02-20): direct-MIR Optional<String> payload regression
- Trigger:
  - `lang/codegen/llvm/tests/test_llvm_codegen_optional_ops.py::test_optional_ops_round_trip_string_payload`
  - failure: `VariantGetField reached LLVM with non-copy payload transfer class 'move'`
- Root cause:
  - LLVM classifier hard-asserted on `move` transfer class for `VariantGetField`.
  - direct-MIR tests can still emit `VariantGetField` for non-Copy payloads (bypassing stage2 contract shaping).
- Fix:
  - `lang/codegen/llvm/llvm_codegen.py` `VariantGetField` now handles `move` class safely:
    - load extracted value
    - zero/tombstone source payload field in-place (`_emit_zero_value` + `store`)
  - This preserves ownership safety and avoids double-drop when source variant is later dropped.
- Validation:
  - `lang/codegen/llvm/tests/test_llvm_codegen_optional_ops.py::test_optional_ops_round_trip_string_payload` (pass)
  - `lang/tests/driver/test_result_ok_copy_struct_string_retain.py` (pass)
  - `lang/tests/driver/test_boundary_matrix_result_variant_contract.py` (pass)
  - e2e:
    - `result_ok_move_conn_source_drop_regression` (pass)
    - `rpc_connect_state_handoff_pure_inmemory` (pass)
    - `struct_ref_field_result_ok_move_drop_once` (pass)
    - `maybe_assume_init_read_moves_out_no_leak` (pass)
    - `std_json_leak_stress_parse_loop` (pass)
