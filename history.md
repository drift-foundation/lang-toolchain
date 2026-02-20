## 2026-02-20 – Code review remediation batch completed (Klaudia findings F2–F15)
- Completed tracked remediation from `work/code-review/todo.md` with execution/status recorded in `work/code-review/work-progress.md`.

- Batch 3 completed (borrow/copy semantics consolidation):
  - `lang/driftc/borrow_checker_pass.py`
    - added `HInvoke` coverage for Optional-ref loan origin tracking and ref-use traversal,
    - extended `_borrow_from_optional_ref_call(...)` to peel nested chains (`HField`/`HIndex`/`HPlaceExpr`),
    - added escape diagnostics for borrowed-capture lambdas in escaping call/invoke/method arg positions unless proven non-retaining.
  - `lang/driftc/stage2/hir_to_mir.py`
    - finished migration of stage2 boolean copy decisions to canonical `_should_copy_value(...)` helper where semantics match.
  - Added regression:
    - `lang/tests/borrow_checker/test_invoke_optional_ref_and_lambda_escape.py`.

- Batch 4 completed (dedup/cleanup):
  - F10:
    - `lang/driftc/stage2/hir_to_mir.py` now pre-validates all non-default match constructors before dispatch-chain CFG emission.
  - F12:
    - `lang/codegen/llvm/llvm_codegen.py` now centralizes forward nominal resolution/canonicalization:
      - `_resolve_forward_nominal_typeid(...)`
      - `_canonical_codegen_typeid(...)`
    - both `_variant_layout(...)` and `_llvm_type_for_typeid(...)` consume the same canonicalization path.
  - F13:
    - `lang/driftc/checker/__init__.py` checker-facing contract diagnostics no longer leak `internal:` prefixes.
  - F15:
    - `lang/codegen/llvm/llvm_codegen.py` now uses centralized bool-storage predicate helper:
      - `_is_bool_storage_pair(...)`
    - replaced ad-hoc `i1`↔`i8` checks across struct/variant construction, field extraction, ref load/store, copy, tombstone, and helper paths.
  - F9 closure:
    - variant layout arithmetic is now consumed from `_variant_layout(...)` metadata in drop/copy paths; no remaining duplicate payload-offset arithmetic path retained.

- Validation highlights:
  - borrow/stage2/codegen/driver focused suites all pass after changes:
    - `lang/tests/borrow_checker/test_invoke_optional_ref_and_lambda_escape.py`
    - `lang/tests/borrow_checker/test_lambda_capture_borrow_overlap.py`
    - `lang/tests/borrow_checker/test_lambda_capture_borrow_overlap_method.py`
    - `lang/tests/stage2/test_hir_to_mir_match_requires_binder_indices.py`
    - `lang/codegen/llvm/tests/test_llvm_codegen_dv_drop_helper.py`
    - `lang/tests/driver/test_callinfo_param_layout_contract.py`
    - `lang/tests/driver/test_intrinsic_callinfo_diagnostics.py`
    - `lang/tests/driver/test_index_diagnostics_spans.py`
    - `lang/tests/driver/test_boundary_matrix_result_variant_contract.py`
    - `lang/tests/driver/test_mir_validate_boundary_diagnostics.py`
    - `lang/tests/driver/test_codegen_boundary_diagnostics.py`

## 2026-02-19 – CallInfo repair fix for generic direct-call signatures (`std.core::cell`)
- Fixed checker regression where named direct-call CallInfo repair could overwrite instantiated generic call signatures with template `TypeVar` shapes.
  - symptom: valid calls like `core.cell(true)` failed with `argument 0 to std.core::cell has type Bool, expected TypeVar<std.core::cell#0>`.
- Root-cause fix in `lang/driftc/checker/__init__.py`:
  - `_repair_named_call_callinfo(...)` now preserves the existing instantiated `CallSig` when the repaired target is generic.
  - full signature rewrite is limited to non-generic targets.
- Regression added:
  - `lang/tests/driver/test_callinfo_param_layout_contract.py::test_named_call_repair_preserves_instantiated_generic_sig`.
- Validation:
  - `/tmp/repro_cell_infer_bool.drift` compiles cleanly again (`exit_code: 0`).
- Boundary Contract Guardrails check:
  - positive regression added for the repaired generic call path:
    - `lang/tests/driver/test_callinfo_param_layout_contract.py::test_named_call_repair_preserves_instantiated_generic_sig`.
  - negative contract coverage remains pinned in the same suite (`E_CALLINFO_PARAM_LAYOUT` and related target-shape checks).
  - no stage-boundary type-shape expansion in this change (checker CallInfo repair only), so no new stage2/MIR/LLVM boundary-shape updates were required.

## 2026-02-19 – Structural `core.Copy` check false-negative for repeated scalar fields (LANGUAGE_BUG)
- Fixed checker bug where structurally-Copy structs with repeated scalar fields (for example, two `Uint` fields) were rejected as non-Copy.
  - symptom: `core.Copy impl target must be structurally Copy in MVP` for:
    - `struct S { a: Uint, b: Uint }`.
- Root-cause fix in `lang/driftc/type_checker.py` (`validate_trait_impls`):
  - `_is_structurally_copy(...)` now performs scalar/primitive fast-path checks before recursion tracking.
  - recursion tracking is now path-scoped (`seen.add(...)` with `finally: seen.discard(...)`) to avoid sibling-field false cycle hits.
- Regression added:
  - `lang/tests/driver/test_trait_impl_signature_validation.py::test_copy_impl_allows_struct_with_repeated_uint_fields`.
- Validation:
  - `lang/tests/driver/test_trait_impl_signature_validation.py` passes (3 tests).
  - `/tmp/repro_copy_uint_should_compile.drift` now compiles (`exit_code: 0`).
- Boundary Contract Guardrails check:
  - positive regression added:
    - `lang/tests/driver/test_trait_impl_signature_validation.py::test_copy_impl_allows_struct_with_repeated_uint_fields`.
  - negative regression retained in the same suite:
    - `lang/tests/driver/test_trait_impl_signature_validation.py::test_copy_impl_on_noncopy_field_struct_is_rejected`.
  - change is checker-only policy validation (`validate_trait_impls`) and does not alter checker→MIR→LLVM payload/type boundary support.

## 2026-02-19 – `core.Copy` non-Copy target rejection (Defect #6)
- Closed defect where `implement core.Copy for <struct>` could be accepted even when the target struct was not structurally Copy (for example, had `String` fields).
- Checker fix:
  - `lang/driftc/type_checker.py` now enforces structural-Copy validation for `core.Copy` impl targets and emits a normal user diagnostic when invalid.
  - behavior now rejects invalid impls with: `core.Copy impl target must be structurally Copy in MVP`.
- Regression pinned:
  - `lang/tests/driver/test_trait_impl_signature_validation.py::test_copy_impl_on_noncopy_field_struct_is_rejected`.
- Repro confirmation:
  - `/tmp/repro_copy_string_forbidden.drift` now fails at the impl site (exit 1) instead of compiling/linking.

## 2026-02-19 – Boundary hardening sweep (Result/Variant + trait impl contracts + main-thread IO pacing)
- Consolidated multiple staged/uncommitted fixes and regressions across checker/stage2/LLVM/runtime-facing stdlib behavior.

- LLVM/codegen boundary hardening:
  - `lang/codegen/llvm/llvm_codegen.py`
    - fixed forward-nominal recursive sizing in `_size_align_typeid(...)` so variant payload sizing is stable for aliased/forward nominal nested fields.
    - canonicalized variant arm field type sizing in `_variant_layout(...)`.
    - replaced many `insertvalue ... undef` aggregate seeds with `zeroinitializer` to remove undefined aggregate seed paths in emitted IR.
  - Added regressions:
    - `lang/tests/driver/test_variant_payload_forward_nominal_size.py`
    - `lang/tests/driver/test_llvm_no_insertvalue_undef_seeds.py`

- Match/binder lowering and Result payload regression coverage:
  - `lang/driftc/stage2/hir_to_mir.py`
    - hardened by-value binder extraction and scrutinee move/copy handling for non-Copy/runtime-drop payloads.
    - stabilized binder addr-path extraction behavior and payload-moved tracking.
  - Added/expanded regressions:
    - `lang/tests/codegen/e2e/result_ok_move_conn_source_drop_regression`
    - `lang/tests/codegen/e2e/rpc_connect_state_handoff_nonnetwork_shape`
    - `lang/tests/codegen/e2e/rpc_connect_state_handoff_pure_inmemory`
    - `lang/tests/codegen/e2e/copyvalue_string_loop_phi_regression`
    - `lang/tests/codegen/e2e/match_ref_scrutinee_noncopy_copy_rejected`
    - `lang/tests/codegen/e2e/std_text_utf8_from_bytes_range_match_move_no_leak`
    - `lang/tests/stage2/test_hir_to_mir_match_requires_binder_indices.py`

- Boundary matrix expansion (prevent regression recurrence):
  - Added driver matrix:
    - `lang/tests/driver/test_boundary_matrix_result_variant_contract.py`
    - covers positive/negative Result payload and borrowed-aggregate boundary cases with non-internal diagnostic assertions.
  - Added e2e matrix:
    - `lang/tests/codegen/e2e/result_variant_payload_matrix`
    - runtime payload bind/move/drop integrity across scalar/string/array/struct shapes.

- Trait impl contract validation + inherited nothrow fix:
  - `lang/driftc/driftc.py`
    - validates trait impl signatures from `module_exports` in stubbed compile path.
  - `lang/driftc/type_checker.py`
    - added trait impl signature validator (param/return/throw behavior checks).
    - fixed inherited-nothrow behavior for impl methods with omitted throw markers:
      - omitted marker now inherits trait non-throwing contract while preserving explicit `throws` mismatch diagnostics.
  - `lang/driftc/parser/__init__.py`
    - trait-method nothrow lookup now uses resolved trait identity fallback (`trait_key_from_expr`) for impls where direct trait key is absent.
  - Added regression:
    - `lang/tests/driver/test_trait_impl_signature_validation.py`
  - Verified fixes against:
    - `lang/tests/driver/test_cmp_operator_resolution.py::test_eq_uses_std_core_cmp_without_std_algo`
    - `lang/tests/driver/test_trait_impl_nothrow_inherits_interface.py::test_trait_impl_method_inherits_interface_nothrow_when_omitted`

- stdlib runtime pacing under main-thread (non-VT) IO:
  - `stdlib/std/io/io.drift`
  - `stdlib/std/net/net.drift`
  - introduced `MAIN_THREAD_IO_POLL_QUANTUM_MS` and `_park_main_thread_io(...)` to cap long parks in non-virtual-thread IO waits.
  - all main-thread IO wait paths now use bounded slice parking instead of parking full remaining timeout in one step.

## 2026-02-19 – Result::Ok aggregate payload corruption fix (LANGUAGE_BUG)
- Fixed deterministic runtime state corruption on `Result::Ok` bind handoff (`EXIT:135` probe) caused by incorrect variant payload sizing in LLVM codegen.
  - symptom:
    - live probe (`connect_state_handoff_probe_regression_test`) returned wrong post-bind booleans despite pre-return checks passing in callee.
    - ASAN/memcheck stayed clean, indicating semantic/lowering corruption rather than memory-safety trap.
  - root cause:
    - variant layout size model under-counted nested forward/alias nominal field sizes in some payload shapes.
    - `_size_align_typeid(...)` could fall back to generic size for `FORWARD_NOMINAL` during recursive struct sizing, producing undersized payload words.
  - fix:
    - `lang/codegen/llvm/llvm_codegen.py`
      - canonicalize/resolve `FORWARD_NOMINAL` in `_size_align_typeid(...)` before size/alignment calculation.
      - keep arm field canonicalization in `_variant_layout(...)` so both direct and recursive sizing paths agree.
- Regression-first coverage added:
  - `lang/tests/driver/test_variant_payload_forward_nominal_size.py`
    - asserts emitted variant payload words are sufficiently sized for a large forward-nominal alias payload in `Result<AliasStruct, Int>`.
- Validation:
  - local targeted driver/e2e regressions pass.
  - host repro now passes:
    - prior failure: `EXIT:135`
    - after fix: `EXIT:0`
    - memcheck clean (`0 errors`, `0 leaks`).

## 2026-02-19 – Checker call-signature UNKNOWN param handling fix (Array.push cross-module regression)
- Fixed LANGUAGE_BUG where checker call-signature validation rejected valid intrinsic method calls when a CallInfo param slot remained `UNKNOWN`:
  - symptom:
    - `argument 1 to lang.__intrinsic::__method::push has type __local__::mariadb.rpc.RpcArg, expected UNKNOWN`
  - root cause:
    - `check_call_signature(...)` treated `UNKNOWN` param/arg types as hard mismatches during shallow validation.
  - fix:
    - in `lang/driftc/checker/__init__.py`, skip strict mismatch checks when either side is `UNKNOWN`.
- Added regression test:
  - `lang/tests/driver/test_array_push_unknown_param_regression.py`
  - pins cross-module variant element + `Array.push(...)` path (`Array<rpc.RpcArg>` with `rpc.arg_int(...)`) as compile-success.
- Validation:
  - new regression passes,
  - nearby call-contract suite remains passing:
    - `lang/tests/driver/test_callinfo_param_layout_contract.py`,
  - reproduced `tmp/mariadb-rpc/tests/e2e/live_rpc_smoke_test.drift` compile path now passes with no diagnostics.

## 2026-02-19 – Alias-forward boundary canonicalization + match-binder deref checker fix
- Fixed alias/forward-nominal leakage across checker→stage2→MIR boundary:
  - added centralized canonicalization in `lang/driftc/driftc.py`:
    - `_canonicalize_forward_nominal_type_id(...)`
    - `_canonicalize_signature_type_ids(...)`
    - `_canonicalize_mir_type_ids(...)`
  - canonicalization is applied before MIR validation so unresolved alias-forward types do not reach layout-sensitive MIR/LLVM paths.
- Fixed checker LANGUAGE_BUG for `match` binders on `&Variant` scrutinees:
  - in `lang/driftc/checker/__init__.py`, `_walk_hir(...)` now seeds arm binder locals as `&T` / `&mut T` when scrutinee is `&Variant` / `&mut Variant`.
  - in checker typing context, unary deref inference now resolves `&T -> T` correctly for shallow checker validations.
- Added regression-first coverage for both fixes:
  - alias boundary:
    - `lang/tests/driver/test_alias_return_struct_field_assignment.py`
      - positive: alias-return assigned into struct field reaches codegen.
      - negative: unresolved alias target stays user-facing and does not leak boundary `internal:` failures.
    - existing companion remains green:
      - `lang/tests/driver/test_module_alias_exported_type_alias_ctor.py`.
  - match/deref binder typing:
    - `lang/tests/driver/test_match_ref_variant_binder_deref.py`
      - positive: `match a: &Arg` binder deref (`*v`) infers payload primitive type end-to-end.
      - negative: value-scrutinee binder deref rejects with user diagnostic (`deref requires a reference value`) and no `internal:` diagnostics.
- Validation:
  - targeted driver subset passes for new/related boundary tests and match binder index/lowering checks.

## 2026-02-19 – Struct ref-field restricted MVP landed (single-origin borrowed aggregates)
- Enabled struct ref fields in parser/type declarations (removed hard parser reject for `&T` / `&mut T` struct fields).
- Landed checker-side borrowed-aggregate boundary enforcement for restricted MVP:
  - return provenance enforcement for borrowed aggregates:
    - allowed only when tied to reference-parameter origin,
    - single-origin only,
    - mutable ref fields require `&mut` param provenance,
    - wrapper constructor returns supported for:
      - `Result::Ok(borrowed_aggregate)`
      - `Optional::Some(borrowed_aggregate)`.
  - retaining-boundary enforcement:
    - by-value borrowed-aggregate passing rejected by default on retaining generic/call boundaries,
    - explicit non-retaining/by-ref paths allowed.
  - container/global/escape guards in checker coverage:
    - owning `Array<borrowed_aggregate>` rejected,
    - escaping callback/lambda captures with borrowed aggregates rejected,
    - registry/global retaining stores rejected through same retaining-boundary rule.
- Landed positive+negative regression coverage for struct-ref-field contract:
  - driver:
    - `lang/tests/driver/test_struct_ref_field_return_rules.py`
    - `lang/tests/driver/test_struct_ref_field_boundary_contract.py`
    - `lang/tests/driver/test_loop_all_paths_return_no_internal.py`
  - e2e:
    - `lang/tests/codegen/e2e/struct_ref_field_result_return_ok`
    - `lang/tests/codegen/e2e/struct_ref_field_array_store_rejected`
    - `lang/tests/codegen/e2e/struct_ref_field_callback_capture_rejected`
    - `lang/tests/codegen/e2e/struct_ref_field_registry_store_rejected`
    - updated `lang/tests/codegen/e2e/struct_ref_field_rejected` to accepted behavior.
- Boundary contract hardening for this feature:
  - positive path pinned to reach IR/codegen without internal contract failures,
  - negative paths pinned to fail in `typecheck` with non-internal diagnostics.

## 2026-02-19 – Struct ref-field hardening follow-up (provenance flow + container + alias stress)
- Strengthened borrowed-aggregate return provenance through local variable flow in checker:
  - supports valid `return local_var` / `return move local_var` for wrapper-carried borrowed aggregates tied to single ref-param origin,
  - rejects local-origin borrowed aggregate returns through local bindings.
- Added return-flow regressions:
  - driver:
    - `test_borrowed_aggregate_return_single_origin_via_local_wrapper_allowed`
    - `test_borrowed_aggregate_return_from_local_binding_rejected`
    - `test_struct_ref_field_result_return_via_local_wrapper_reaches_codegen_boundary`
    - `test_struct_ref_field_local_return_rejected_at_checker_boundary`
  - e2e:
    - `struct_ref_field_result_return_local_wrapper_ok`.
- Closed explicit container coverage gap beyond Array:
  - added e2e negatives:
    - `struct_ref_field_hashmap_store_rejected`
    - `struct_ref_field_treemap_store_rejected`
  - added driver boundary tests:
    - `test_struct_ref_field_hashmap_store_rejected_at_checker_boundary`
    - `test_struct_ref_field_treemap_store_rejected_at_checker_boundary`.
- Added borrow-checker alias stress coverage for structs with ref fields and `&mut self` receiver methods:
  - driver:
    - `lang/tests/driver/test_struct_ref_field_borrow_alias_conflicts.py`
      - direct conflict
      - `if` conflict
      - `match` conflict
      - `loop` conflict
  - e2e negatives:
    - `struct_ref_field_mut_self_alias_if_rejected`
    - `struct_ref_field_mut_self_alias_match_rejected`
    - `struct_ref_field_mut_self_alias_loop_rejected`.
- Validation matrix for new hardening coverage:
  - targeted driver suites: pass
  - targeted e2e subsets: pass
  - targeted e2e with `DRIFT_ASAN=1`: pass
  - targeted e2e with `DRIFT_MEMCHECK=1`: pass.

## 2026-02-18 – driftc wrapper regression pin for relative `-o` output paths
- Added driver regression to lock relative output behavior when invoking wrapper from a non-repo working directory:
  - `lang/tests/driver/test_driftc_wrapper_env_modes.py::test_driftc_wrapper_relative_output_from_non_repo_cwd`.
- Extended wrapper test helper to support custom `cwd` so this path is validated directly.
- Validation run:
  - `PYTHONPATH=. ./.venv/bin/python3 -m pytest -q lang/tests/driver/test_driftc_wrapper_env_modes.py -k "relative_output_from_non_repo_cwd or runtime_archive_mode_links_static_runtime"` (passed).

## 2026-02-17 – std.codec landed (hex/base64/base32 + strict/permissive decode paths)
- Added new stdlib module `stdlib/std/codec/codec.drift` with shared codec error surface:
  - `CodecError { tag: String, offset: Int }` (+ `core.Diagnostic` impl).
- Implemented codec APIs (both directions):
  - Hex:
    - `hex_encode(bytes)`
    - `hex_decode(s)` (strict default)
    - `hex_decoder()` builder with:
      - `allow_whitespace(flag: Bool)`
      - `allow_prefix_0x(flag: Bool)`
      - `decode(s)`
  - Base64:
    - `base64_encode(bytes)`
    - `base64_decode(s)` (strict default)
    - `base64_decoder()` builder with:
      - `allow_whitespace(flag: Bool)`
      - `allow_url_safe(flag: Bool)`
      - `decode(s)`
  - Base32:
    - `base32_encode(bytes)`
    - `base32_decode(s)` (strict default)
    - `base32_decoder()` builder with:
      - `allow_whitespace(flag: Bool)`
      - `allow_lowercase(flag: Bool)`
      - `decode(s)`
- Strict decode contracts + deterministic error taxonomy covered:
  - Hex:
    - `hex-odd-length`
    - `hex-invalid-char`
  - Base64:
    - `base64-invalid-length`
    - `base64-invalid-char`
    - `base64-invalid-padding`
    - `base64-trailing-data`
  - Base32:
    - `base32-invalid-length`
    - `base32-invalid-char`
    - `base32-invalid-padding`
    - `base32-trailing-data`
- Added new e2e coverage:
  - `lang/tests/codegen/e2e/std_codec_hex_base64_strict`
  - `lang/tests/codegen/e2e/std_codec_decoder_builder_permissive`
  - `lang/tests/codegen/e2e/std_codec_hex_fixture_source_style`
- Pinned practical fixture pattern for binary-heavy tests:
  - readable source fixture strings like `"0xDE AD BE EF\n01 02"` decoded via:
    - `hex_decoder().allow_whitespace(true).allow_prefix_0x(true).decode(...)`.
- Validation matrix for codec e2e subset:
  - normal mode: pass
  - `DRIFT_ASAN=1`: pass
  - `DRIFT_ALLOC_TRACK=1`: pass
  - `DRIFT_MEMCHECK=1`: pass

## 2026-02-17 – Compiler bug batch hardening (diagnostics + typevar/index regressions)
- Closed and pinned compiler bug-batch items #1–#4 from `issues/compiler-core-bugs-2026-02-17/description.md`.
- Fixed checker call-signature diagnostic quality regression in `lang/driftc/checker/__init__.py`:
  - removed raw internal `TypeId` leakage (`type 170, expected 1`-style messages),
  - now renders symbolic type labels via `TypeTable.type_key_string(...)`,
  - propagated call-site source span into `check_call_signature(...)` (replaced `loc=None` path).
- Added/expanded regression coverage for diagnostic shape and span:
  - `lang/tests/driver/test_callinfo_param_layout_contract.py`
    - new test `test_call_signature_type_mismatch_uses_symbolic_types_and_span`.
- Verified tombstone contract fix sweep (issue #2) across core/package/LLVM/e2e paths:
  - `lang/tests/core/test_variant_tombstone_requirement.py`
  - `lang/tests/packages/test_link_variant_internal_tombstone.py`
  - `lang/tests/driver/test_variant_tombstone_driver.py`
  - `lang/codegen/llvm/tests/test_llvm_codegen_array_string.py -k tombstone`
  - e2e: `variant_droppable_without_tombstone_generic`, `variant_droppable_without_tombstone_non_generic`, `variant_internal_tombstone_array_pop`.
- Added boundary guard regressions for non-Copy array index read behavior (issue #3):
  - new driver file `lang/tests/driver/test_array_index_noncopy_diagnostics.py`,
  - asserts user-facing `typecheck` diagnostics with populated span and no leaked `internal:` diagnostics for non-Copy `arr[i]` value-read rejection.
- Added regression coverage for checker type-param scan stability (issue #4):
  - new driver file `lang/tests/driver/test_checker_typevar_scan_regression.py`,
  - pins no internal exception in generic `HIndex` scan paths,
  - pins nested generic non-Copy case as clean user-facing rejection (span + phase + no `internal:` leakage).
- Validation outcomes:
  - new driver regressions pass,
  - nearby driver/type-checker/e2e subsets pass (`array_index_non_copy_read_rejected`, callinfo boundary suite, array index copy suite),
  - issue tracker updated to mark #2/#3/#4 resolved/pinned for current pipeline.

## 2026-02-16 – Compiler hardening: phase-contract enforcement, shared call contracts, and boundary diagnostic hygiene
- Completed compiler hardening phases focused on checker→MIR→LLVM contract reliability and deterministic failure reporting.
- Added/expanded boundary regression coverage:
  - `lang/tests/driver/test_mir_validate_boundary_diagnostics.py`
  - `lang/tests/driver/test_codegen_boundary_diagnostics.py`
  - `lang/tests/driver/test_codegen_preemit_boundary_diagnostics.py`
  - `lang/tests/driver/test_callinfo_param_layout_contract.py`
  - `lang/tests/stage2/test_callinfo_cutover.py` (new malformed CallInfo boundary cases)
  - `lang/tests/driver/test_no_blank_span_fallbacks.py` (extended for driftc boundary guards).
- Enforced explicit pre-emission LLVM contract in `lang/driftc/driftc.py` via `_validate_codegen_contract(...)`:
  - type table required,
  - SSA map required and complete for emitted MIR functions,
  - `FnInfo/signature` coverage required,
  - direct-call target resolvability required.
- Added checker-side call metadata contract enforcement in `lang/driftc/checker/__init__.py`:
  - target-kind shape checks (e.g. invoke must be indirect; method must not be constructor-target),
  - param-layout checks against effective call argument shape,
  - deterministic checker diagnostics for malformed CallInfo contract shapes.
- Added stage2 call metadata contract assertions in `lang/driftc/stage2/hir_to_mir.py`:
  - invoke requires indirect target and disallows `includes_callee`,
  - method-call rejects constructor targets.
- Fixed compile-path LANGUAGE_BUG where stage2 assertion failures leaked raw exceptions:
  - `compile_stubbed_funcs(...)` now converts stage2 lowering assertion failures into deterministic diagnostics:
    - `internal: MIR lowering contract failure (...)`
    - phase=`mir_validate`.
- Added codegen-helper regression to pin same behavior through `compile_to_llvm_ir_for_tests(...)` when stage2 contract failures occur.
- Completed boundary diagnostic span hygiene:
  - introduced best-effort boundary span selection in `lang/driftc/driftc.py`,
  - removed anonymous `span=Span()` from MIR/LLVM boundary contract diagnostics where source location is available,
  - extended driver tests to assert `line/column` presence for boundary failures.
- Structural decomposition completed:
  - added shared call metadata contract module `lang/driftc/call_contract.py` with reusable call-shape primitives:
    - `call_arg_exprs_for_param_layout(...)`
    - `call_expected_param_count(...)`
    - `explicit_arg_param_types(...)`
    - `call_contract_issues(...)`.
  - integrated across:
    - `lang/driftc/checker/__init__.py`
    - `lang/driftc/stage2/hir_to_mir.py`
    - `lang/driftc/borrow_checker_pass.py`.
- Centralized boundary diagnostic construction in `lang/driftc/driftc.py`:
  - `_append_boundary_contract_diag(...)` now emits MIR/LLVM contract diagnostics with shared message/phase/span policy.
  - Added anti-regression guard test to enforce boundary failures route through the helper.
- Call/entrypoint span hardening also landed in this cycle:
  - constructor/call diagnostics now consistently carry source spans,
  - entrypoint and fixed-width reserved-type diagnostics now carry deterministic phase+location expectations.
- Validation outcomes:
  - hardening regression subsets and stage2 callinfo suites pass clean,
  - boundary diagnostics suites pass with span assertions,
  - no-blank-span guard and central-helper guard pass,
  - follow-up targeted ownership/borrow and callback seam checks remain green.

## 2026-02-14 – Logger sink strictness + runtime-state ownership leak fix
- Tightened logger sink path to be capability-only:
  - removed `std.log` direct console fallback writes from emit path.
  - when runtime-state handle or stderr capability is unavailable, emit returns `false` (still nothrow/best-effort).
- Added e2e coverage for preamble + logger bootstrap:
  - `std_log_preamble_registry_stderr_default`
  - validates global-registry stderr capability presence at process start and successful `create_logger(...).info(...)` without manual stdio install.
- Fixed logger runtime-state lifetime leak:
  - root cause: heap-allocated `LoggerRuntimeState` from `_alloc_runtime_state` was not released.
  - `Logger` now implements `Destructible` and frees owned runtime-state allocation.
  - `with_min_level(...)` and `derive(...).build()` now allocate independent runtime-state instances (no shared-handle alias ownership).
- Validation:
  - logger subset passes in normal mode.
  - same subset passes under `DRIFT_ALLOC_TRACK=1`.
  - logger+concurrency subset passes under `DRIFT_ASAN=1 DRIFT_ALLOC_TRACK=1`.
  - driver logger/macro smoke subset passes (`6 passed`).

## 2026-02-14 – Runtime preamble stdio capability install + e2e coverage
- Added `std.io.install_process_preamble() -> Bool`:
  - no-arg helper that resolves `std.runtime.global_registry()` and calls `install_process_stdio(reg)`.
- Wired compiler-generated OS entry wrappers to run preamble before user entry:
  - `emit_entry_wrapper` (`main()`) now calls `std.io::install_process_preamble__impl` first.
  - `emit_argv_entry_wrapper` (`main(argc, argv)`) does the same before argv materialization/call.
- Added e2e regressions:
  - `std_io_preamble_installs_stdio`
  - `std_io_preamble_installs_stdio_argv`
  - both assert `ProcessStdinCapability`/`ProcessStdoutCapability`/`ProcessStderrCapability` are present in global registry at program start.
- Validation:
  - new e2e tests pass.
  - logger smoke subset remains passing.
  - subset passes under `DRIFT_ASAN=1 DRIFT_ALLOC_TRACK=1`.

## 2026-02-14 – Thread registry (VT-local) support for scoped logging context
- Added execution-local registry API in stdlib runtime:
  - `std.runtime::ThreadRegistry`
  - `std.runtime.thread_registry()`
  - overloaded helpers on thread registry: `contains/get/get_mut/expect/expect_mut`.
- Added intrinsic surface in `lang.thread`:
  - `runtime_thread_registry_ptr`
  - `runtime_thread_registry_set`
  - `runtime_thread_registry_contains`
  - `runtime_thread_registry_get`
- Implemented runtime + LLVM codegen wiring for thread-registry intrinsics.
- Runtime behavior:
  - when inside a virtual thread, registry storage is VT-local (isolated by VT instance),
  - outside VT context, uses a thread-local fallback registry.
- Lifetime/cleanup:
  - VT-local thread-registry entries are destroyed on VT teardown and process-exit VT cleanup,
  - fallback thread-registry entries are included in registry cleanup path.
- Added e2e regression:
  - `std_runtime_thread_registry_isolation`
  - validates same type-tag isolation across concurrent spawned tasks with preserved main-thread value.
- Updated app logging wrapper e2e to consume thread registry:
  - `macro_log_app_logging_context/app/logging.drift` now uses `rt.thread_registry()` for logger/context state.
- Validation:
  - `std_runtime_thread_registry_isolation`, `macro_log_app_logging_context`, `std_log_context_scoped` pass.
  - same subset passes under `DRIFT_ASAN=1 DRIFT_ALLOC_TRACK=1`.

## 2026-02-14 – Logger nested context scope regression coverage
- Added e2e `std_log_context_nested_scopes` to pin nested context semantics:
  - outer context emission before inner scope,
  - inner context emission with event-level key override,
  - outer context restoration after inner guard drop,
  - no context bleed after all guards drop.
- Validation:
  - `std_log_context_nested_scopes`, `std_log_context_scoped`, `macro_log_app_logging_context` pass.
  - `std_log_context_nested_scopes` passes with `DRIFT_ASAN=1 DRIFT_ALLOC_TRACK=1`.

## 2026-02-14 – Logger scoped-context API landed (explicit, non-magic)
- Added explicit context surface in `std.log`:
  - `LogContext` type + `log.log_context()` constructor.
  - `LogContext.put(key, value)` (`value` via `Debuggable`), `LogContext.get(key)`, `LogContext.clear()`.
- Added explicit context-aware logger calls (no implicit TLS/global auto-consume):
  - `logger.debug_ctx/info_ctx/error_ctx(ev, &ctx)`
  - `logger.debug_ctx_attrs/info_ctx_attrs/error_ctx_attrs(ev, &ctx, attrs)`
  - free-function equivalents: `log.debug_ctx/...` and `log.debug_ctx_attrs/...`.
- Implemented merge semantics:
  - effective attrs = context attrs + event attrs,
  - event attrs override context on key collision.
- Kept existing attr-only API unchanged (`log.<level>(ev, attrs)`).
- Added e2e regression:
  - `lang/tests/codegen/e2e/std_log_context_scoped`
  - validates scoped push/pop usage (`std.runtime::ScopedStack<LogContext>`), context-only emit, override behavior, and no post-scope context bleed.
- Validation:
  - targeted logger e2e subset passes.
  - targeted logger subset passes under `DRIFT_ASAN=1 DRIFT_ALLOC_TRACK=1`.

## 2026-02-14 – Macro logger call path expanded + app logging wrapper e2e
- Expanded built-in macro call rewriting for `info!/debug!/error!`:
  - now accepts `2..4` positional args before caller injection:
    - `(logger, ev)`,
    - `(logger, ev, arg3)` (ctx or attrs by overload),
    - `(logger, ev, ctx, attrs)`.
- Added matching `std.log` macro overloads:
  - no-context form,
  - explicit context form,
  - explicit context + attrs form,
  - existing attrs-only form kept.
- Added end-to-end app-wrapper scenario:
  - new e2e `macro_log_app_logging_context` with `app.logging` module pattern over registry:
    - logger category fetch helper,
    - scoped request context push/pop via `ScopedStack<LogContext>`,
    - macro usage with 2/3/4 argument forms,
    - verified no context bleed after scope.
- Validation:
  - `macro_log_registry_stub_smoke`, `std_log_context_scoped`, `macro_log_app_logging_context` pass.
  - `DRIFT_ASAN=1 DRIFT_ALLOC_TRACK=1` pass for new context/macro coverage.

## 2026-02-13 – Registry singleton ABI fix (leak closure) + stale skipped test cleanup
- Fixed a LANGUAGE_BUG in runtime registry codegen ABI for dropper callbacks:
  - `drift_runtime_registry_set` was emitted as taking `%DriftIface` by-value in LLVM IR.
  - Runtime C ABI expects byval-pointer semantics for this struct parameter.
  - Result before fix: registry cleanup saw null/invalid dropper vtable and skipped payload-drop callback invocation, leaving registry-owned payload allocations live at process exit.
- Fixes landed:
  - aligned LLVM `%DriftIface` definition to runtime ABI layout with explicit tail padding (`{ i8*, i8*, [4 x i64], i8, [7 x i8] }`);
  - changed `drift_runtime_registry_set` LLVM declaration to byval-pointer form;
  - changed lowering of `lang.thread::runtime_registry_set` calls to spill iface to stack and pass byval pointer.
- Validation:
  - `DRIFT_ALLOC_TRACK=1`: registry leak regressions now pass:
    - `std_runtime_global_registry_arc_payload`
    - `std_runtime_global_registry_get_concurrent_stress`
    - `std_runtime_global_registry_nontrivial_payload`
  - `DRIFT_ASAN=1 DRIFT_ALLOC_TRACK=1`: same subset passes.
  - broader `std_runtime_global_registry_*` subset passes under alloc tracking.
- Removed stale skipped codegen e2e placeholder by deleting empty directory:
  - `lang/tests/codegen/e2e/catch_typed_binder_field_projection`.

## 2026-02-12 – JSON API refactor finalization (legacy helper removal)
- Finalized wrapper-only JSON mutation API:
  - Legacy `JsonNode` mutation helper surface is now treated as removed/deprecated path (use `json.new_array/new_object` + `JsonArray.push`/`JsonObject.set`).
- Added regression coverage to pin this contract:
  - `lang/tests/driver/test_std_json_regressions.py::test_std_json_legacy_node_mutation_helpers_are_rejected`
  - Confirms rejection of legacy calls:
    - `JsonNode::new_array`
    - `JsonNode::new_object`
    - `array_push`
    - `object_set`
- Updated docs:
  - `docs/effective-drift.md` JSON section now explicitly states shape mutation is wrapper-only.
- Validation:
  - JSON regression driver subset passes with `DRIFT_ASAN=1` and `DRIFT_ALLOC_TRACK=1`.
  - JSON examples compile and run clean under `DRIFT_ASAN=1` + `DRIFT_ALLOC_TRACK=1` (`live_blocks=0`, `live_bytes=0`).

## 2026-02-12 – Lock-free foundations wrap-up (docs/spec + naming cleanup)
- Closed remaining lock-free branch wrap-up items before branch closure:
  - Completed spec/doc sync for current `std.sync` API:
    - observed-CAS signatures (`compare_exchange_observed`) across scalar atomics,
    - fence APIs (`thread_fence`, `signal_fence`),
    - handle/token surfaces (`Handle<T>`, `AtomicHandle<T>`, `RefToken<T>`, `AtomicRef<T>`),
    - `MpscQueue<T>` and epoch reclamation API coverage.
  - Updated effective-drift atomic example to current `compare_exchange(expected, desired, ...)` call shape.
- Renamed stale e2e case directories from `lockfree_mpsc_handle_queue_*` to `lockfree_mpsc_queue_*` to align with public API naming.
- Refreshed stale expected descriptions mentioning “handle queue”.
- Validation:
  - targeted lock-free MPSC e2e subset after rename: 10/10 passing.
- Lock-free foundations delivered on this track:
  - Added observed-CAS support end-to-end for `Bool`/`Int`/`Uint`/`Uint64` (`lang.atomic`, `std.sync`, runtime intrinsics, LLVM codegen wiring).
  - Added `Handle<T>`/`AtomicHandle<T>` and restricted tokenized reference surface (`RefToken<T>`/`AtomicRef<T>`) in `std.sync`.
  - Added explicit fence APIs end-to-end:
    - `lang.atomic.thread_fence` / `lang.atomic.signal_fence`
    - `std.sync.thread_fence` / `std.sync.signal_fence`
    - runtime + codegen integration.
  - Added lock-free viability probes/regressions for handle CAS and tokenized atomic refs.
  - Added fence semantic regressions (release/acquire message-passing + stress) and fixed a hot-loop lowering bug caused by per-iteration zero-payload variant stack allocation.
  - Implemented baseline epoch reclamation API (`EpochDomain`/`EpochParticipant`) plus deterministic and multithread stress regressions.
  - Implemented `std.sync::MpscQueue<T>` (`mpsc_queue`, `push`, `pop`) and expanded coverage:
    - basic behavior
    - contention
    - capacity normalization
    - full/empty determinism
    - per-producer ordering/integrity
    - drop-with-pending
    - Arc clone/drop ordering
    - wraparound churn
    - full-drain/refill cycles
    - tiny-capacity pressure.
- LANGUAGE_BUG fixes landed during lock-free track:
  - Fixed LLVM type canonicalization gaps in intrinsic-heavy paths (`StoreRef` and `CastScalar` checks against mixed canonical vs alias scalar forms).
  - Fixed LLVM `_emit_zero_value` scalar materialization for `Uint`/`Uint64`/`i8`.
  - Fixed default-executor shutdown UAF by clearing `drift_default_executor` before global executor teardown.
  - Stabilized package/link schema matching for `std.sync:EpochDomain` by pinning field types to `lang.atomic.AtomicUint`, resolving cross-package/signing/instantiation failures.
- Validation matrix outcomes captured:
  - lock-free subsets pass in normal mode,
  - `DRIFT_ASAN=1` pass,
  - `DRIFT_ALLOC_TRACK=1` pass,
  - bounded flaky-hunter sweeps passed (`3/3` for ASAN and alloc-track over lock-free subset).

## 2026-02-11 – Concurrency namespace consolidation (`std.concurrent` only) + Arc/Mutex migration
- Consolidated concurrency surface to a single stdlib package:
  - `std.concurrent` is now the sole concurrency namespace.
  - Removed `std.concurrency` shim/module after migrating all in-tree usage.
- Migrated shared-state primitives into `std.concurrent`:
  - Added `Arc<T>`, `Mutex<T>`, `MutexGuard<T>` and helpers (`arc`, `mutex`, `lock`, `mutex_guard_get_mut`) to `stdlib/std/concurrent/concurrent.drift`.
  - Updated exports and trait impls (`Borrow`, `BorrowMut`, `Destructible`) for these types.
- Pinned and fixed a LANGUAGE_BUG uncovered by the migration:
  - LLVM integer binop lowering now normalizes mixed abstract/concrete integer type tags (`drift.int`/`drift.uint` vs concrete LLVM widths) before op selection.
  - This resolved codegen crashes on new atomic/intrinsic paths used by Arc/Mutex internals.
- Added/updated regression coverage:
  - New canonical e2e `std_concurrent_arc_mutex_full_mutation`.
  - Existing Arc/Mutex callback/effective-drift e2e cases migrated to `std.concurrent`.
  - Removed compatibility-only e2e case after shim deletion (`std_concurrency_compat_arc_mutex`).
  - Driver callback fixture modules/imports updated from `std.concurrency` to `std.concurrent`.
- Validation highlights:
  - Targeted e2e Arc/Mutex and effective-drift cases pass in normal and ASan modes.
  - Targeted driver callback/arc subsets pass after migration.
  - Refcount memory-order policy aligned to pinned spec/perf target:
    - `Arc` increment uses Relaxed (`fetch_add`),
    - `Arc` decrement uses Release (`fetch_sub`),
    - zero-destroy path performs Acquire barrier (`atomic_load_int` Acquire) before dropping payload.
  - Removed stale empty compatibility e2e directory that was showing as skipped (`std_concurrency_compat_arc_mutex`).

## 2026-02-11 – JSON branch closure: sanitizer mode, runtime lifetime fixes, and final plan sync
- Added ASan mode to codegen e2e runner via `DRIFT_ASAN=1`:
  - compile/run sanitizer wiring (`-fsanitize=address -g`)
  - env defaults for actionable crash reports
  - incompatibility guard with valgrind-backed modes (`DRIFT_MEMCHECK`/`DRIFT_MASSIF`)
  - normalization of known non-fatal ASan `swapcontext` warning noise to avoid false stderr mismatches.
- Fixed intermittent concurrency/runtime memory corruption found during stress/sanitizer runs:
  - hardened VT/reactor teardown so reactor no longer retains stale VT references after destroy
  - tightened worker completion ordering to avoid stale VT state reads after completion publish
  - adjusted executor teardown sequencing to remove race windows in queued/prestart cancellation paths.
- Fixed post-join cancel use-after-free at stdlib boundary:
  - `VirtualThread.join`/`join_timeout` now clear native handle after successful join state transition
  - `VirtualThread.cancel` now no-ops when already joined/handle-cleared.
- Fixed logger shutdown nondeterminism causing stderr snapshot mismatches:
  - log worker now drains queued records before exit on shutdown path.
- Completed branch sync/docs updates:
  - updated `work/stdlib-json/work-progress.md` to reflect completed JSON MVP scope and what is explicitly deferred out-of-scope
  - documented diagnostics env toggles in toolchain/e2e docs to support repeatable alloc/sanitizer sweeps.

## 2026-02-11 – std.json MVP completion, leak/crash hardening, and iterable ergonomics pinning
- Completed `std.json` MVP with first-class Drift-side JSON model and APIs:
  - `JsonNode` variant (`Null`, `Bool`, `Number`, `String`, `Array`, `Object`)
  - parse surface: `parse(&String) -> Result<JsonNode, JsonErrorData>`
  - encode surface: `encode`, `encode_compact`, and config-based variants.
- Landed deterministic encoding behavior and policy controls:
  - duplicate object keys on parse are keep-last
  - key ordering policy implemented (`Unordered` default, `OrderedLexUtf8` for canonical signing use-cases)
  - added broader deterministic snapshots including deep mixed nested object/array structures.
- Finalized JSON parse/error semantics:
  - machine-tagged `JsonErrorData` with structured fields (`tag`, `offset`, `line`, `col`, `path`, `key`)
  - parse error position reporting implemented and covered
  - non-finite number rejection in parser (JSON-compliant)
  - control-character escaping fixed for valid JSON string emission.
- Completed navigation/extractor APIs and behavior:
  - `get`, `get_path`, `entries`, `as_*`, `expect_*`
  - `entries()` iterator semantics are empty for non-object nodes
  - strict extractor failures use machine-friendly `std.json:JsonError` tags.
- Regression-first compiler/runtime fixes discovered through JSON work:
  - MIR ownership join fix (`LoadLocal` -> `MoveOut`) for array ownership correctness on JSON parse paths
  - LLVM lowering fix for variant `DropValue` CFG/PHI corruption (no inline injected labels; helper-call drop path)
  - match-lowering cleanup fix so non-Copy binders are scope-dropped (prevents early-return leaks)
  - lambda move-capture double-drop fix (capture prologue no longer duplicates drop ownership)
  - interface-owned callback lifetime fix (stage2 runtime-drop participation + iface-init MIR validation alignment).
- Runtime hardening and leak-signal infrastructure completed:
  - assert/abort paths now still emit alloc stats for alloc-tracked runs
  - deterministic runtime teardown at exit for logger worker/queue, default reactor, and virtual-thread registry
  - cancel/join prestart race fixes and timeout-path leak fixes
  - `VirtualThread<T>` destructor semantics added for dropped-but-unjoined cleanup.
- Alloc/leak validation outcomes:
  - `std_json_encode_determinism_deep_mixed_snapshot` validated leak-free under valgrind (`in use at exit: 0`, `ERROR SUMMARY: 0`)
  - sampled alloc-tracked JSON/concurrency/logging-adjacent sweeps green after fixes
  - full-suite alloc-tracking run remains environment/user-run gate (`DRIFT_ALLOC_TRACK=1 just`).
- LANGUAGE_BUG and ergonomics regressions pinned/fixed for iterable usage from JSON:
  - fixed `for` iteration over already-borrowed iterables (`&Array<T>`) used by `expect_array(...)`
  - added e2e regressions:
    - `for_iter_json_expect_array`
    - `for_iter_ref_array_local`
  - fixed UFCS `for_iter` nested-ref receiver handling and callsite instantiation recording.
- Added broader `&Array<JsonNode>` usage-matrix regression and validation:
  - `ref_array_jsonnode_usage_matrix` covers direct calls, nested-ref arg coercion (`&& -> &`), direct expression arguments, pass-through refs, and `for` iteration
  - valgrind memcheck for matrix case is clean.
- Added dedicated dot-call iterator regression on `&Array<JsonNode>`:
  - `ref_array_dot_iter_next` (`users.iter()` + `it.next()`)
  - pinned required trait-scope rule for manual trait-method calls:
    - `use trait iter.Iterable;`
    - `use trait iter.SinglePassIterator;`
  - valgrind memcheck for this case is clean.
- Documentation updates:
  - `docs/effective-drift.md` updated for final `std.json` API/error-tag contract
  - added explicit guidance that preferred JSON array iteration is `for val item : users`, while manual `iter()/next()` form requires trait imports.

## 2026-02-08 – Logger interface baseline, JSON emission, and deterministic masking
- Completed `std.log` MVP user-facing interface coverage with e2e/driver tests while keeping mechanics runtime-backed for now.
- Added/validated map-literal attrs usage for logger calls (`log.<level>(ev, {"k": v, ...})`) and type-gated attrs (`V is Debuggable`).
- Wired runtime-backed logger enqueue/worker emission to output structured JSON lines with fields:
  - `tm` (ISO-8601 UTC with millis),
  - `level`,
  - `ev`,
  - `logger`,
  - `attrs`,
  - `tid`.
- Added intrinsic plumbing for logger runtime helpers (`init`, `min_level`, `enqueue`, `flush`, JSON escape, and `DiagnosticValue` JSON conversion) across stdlib/thread/codegen/runtime.
- Fixed a critical ABI mismatch for `DiagnosticValue` logger conversion by switching `log_runtime_dv_to_json` to by-ref (`&DiagnosticValue` -> pointer at runtime boundary), restoring correct attr values.
- Fixed C header interop issue for shared `DriftString` definitions (`diagnostic_runtime.h` guarded against `string_runtime.h` redefinition).
- Extended codegen e2e runner to support nondeterministic JSON-field masking:
  - new `stderr_jsonl` expected shape,
  - `__ANY__` wildcard matching for fields like `tm` and `tid`.
- Updated logger e2e expectations to JSONL masked assertions and validated logger suite stability.
- Verified green runs:
  - codegen e2e logger suite (`std_log_*`): 8/8 pass,
  - driver logger API smoke: 3/3 pass.
- Pinned follow-on direction: split next work into atomics/memory-ordering capability and migrate logger internals from runtime scaffolding to pure Drift incrementally.

## 2026-02-07 – Exception captures API read path + e2e value coverage
- Implemented public capture lookup path for exceptions via `.captures[frame][key]`, lowered as a single non-throwing runtime lookup returning `DiagnosticValue` (`Missing` on unknown frame/key).
- Added runtime accessor `__exc_captures_get_dv(...)` and compiler/codegen support (`ErrorCapturesGetDV`) for typed capture reads.
- Fixed captured-local value loss in runtime ABI:
  - `drift_error_add_local_dv` now takes `const DriftDiagnosticValue*` (pointer ABI), matching codegen emission and avoiding struct-by-value misclassification to `Missing`.
- Added/updated e2e coverage:
  - `exception_capture_locals_values` now validates real captured values (`Int`, `String`) and missing-key behavior.
  - New `exception_capture_missing_frame` validates missing-frame lookup returns `DiagnosticValue::Missing`.
  - Existing smoke and non-primitive rejection cases remain green.

## 2026-02-07 – Namespace migration + concurrency park/deadline regression fix
- Repository namespace cleanup:
  - Moved active compiler/runtime tree from `lang2/` to `lang/`.
  - Moved legacy pre-refactor tree to `lang-obsolete/`.
  - Rewired repository references, tooling, tests, and runners to `lang.*` paths/modules (including `justfile`, e2e runners, and docs links), and removed temporary compatibility symlink after validation.
- TODO source-of-truth cleanup:
  - Removed stale `docs/TODO.md` and updated references to root `TODO.md`.
- Concurrency timeout/parking hardening:
  - Added e2e regression `concurrent_sleep_task_join_timeout_regression` to capture timeout behavior when a spawned task sleeps and caller uses `join_timeout`.
  - Fixed `std.concurrent.sleep` VT path to park until an absolute deadline (`now_ms + duration`) after timer registration.
  - Fixed `FutureGroup.join_any` parking loop to be context-aware:
    - VT context uses absolute park deadline (`now_ms + 1`).
    - non-VT context uses relative sleep (`1ms`), avoiding long unintended sleeps/timeouts.
  - Validated focused concurrency suites including cancel/join timeout, reactor wakeup, and IO timeout paths.

## 2026-02-07 – Console/IO API completion, hardening, and docs alignment
- Completed the `std.io`/`std.console` MVP migration from legacy file-open APIs to configured builder-based streams:
  - Added/standardized `stdin/stdout/stderr` handles and builders, configured stream/file types, fluent file builder (`read/write/create/truncate/append/mode/timeout/build`), and configured operations (`read/write/close/read_line`).
  - Moved `std.console` internals onto `std.io` nonblocking/reactor-backed write loops with bounded timeout (no special compiler intrinsic path).
- Finalized IO error surface to flat errno-style model:
  - `IoError::Errno(code)` only, sentinel codes (`IO_ERR_WOULD_BLOCK`, `IO_ERR_EOF`, `IO_ERR_LINE_TOO_LONG`) and helper predicates (`io_is_*`, `is_*_error`, `io_error_code`).
- Completed line I/O semantics and coverage:
  - `read_line()` semantics pinned and implemented (newline consumed, EOF/line-too-long in error space, empty-line behavior).
  - Added deterministic stdin-line edge matrix e2e (`std_io_stdin_line_edge_matrix`) covering consecutive newlines, empty-input EOF, over-cap line, and mixed newline/EOF boundaries.
- Executed legacy API removal gate:
  - Removed public legacy `OpenOptions`/`io.open(...)` and timeout-arg `File` methods from `std.io`.
  - Migrated remaining tests/examples to configured-builder path.
  - Gate results green: targeted std.io e2e + targeted driver + package regression.
- Added true pipe-style e2e and runner stdin support:
  - e2e runner now accepts optional `stdin` from `expected.json`.
  - New case `std_io_pipe_reverse_stdout` validates stdin->process->stdout flow (`"ABCD\\n"` -> `"DCBA"`).
- Regression-first fix for resolver deadlock (not workaround-only):
  - Added timeout-guarded compile regression for fluent `FileBuilder` chains (`append/mode` path).
  - Fixed call-resolution recursion by threading known receiver type into mutability checks (`_receiver_can_mut_borrow(..., recv_ty_hint)`), avoiding recursive re-typechecking loops.
  - Reverted temporary API workaround and verified by-ref fluent builder chains remain stable.
  - Added additional timeout anti-regression for rvalue mut-receiver chain termination (`test_autoborrow_mut_rvalue_chain_terminates_without_resolver_recursion`).
- Updated docs/spec for current surface:
  - `docs/design/drift-lang-spec.md` IO/console sections aligned to builder/configured APIs, flat error model, `read_line` semantics, and console wrapper behavior.
  - `docs/effective-drift.md` file IO examples updated to current `file_builder` API; matching examples added under `lang/examples/file_io/read_file.drift` and `lang/examples/file_io/write_file.drift`.

## 2025-12-29 – Core trust enforcement (reserved namespaces)
- Made the core trust store mandatory for reserved namespaces; removed fallback to project/user trust for `lang.*`, `std.*`, and `drift.*`.
- Added dev-only override via `--dev --dev-core-trust-store` (non-normative), and documented the exception in the spec.
- Core-key revocations now consult only the core trust store; user/project revocations cannot disable toolchain keys.
- Added a toolchain core trust file with the required format header and updated tests accordingly.
- Prevented instantiation signatures from re-serializing template type exprs (clears `param_types`/`return_type`), fixing cross-package instantiation dedup.
- Cleaned match statement grammar (removed duplicate `match_stmt_arm_body`) and added a negative test to reject value-style arms in statement-form match.
- Updated trait-bound test harness to pass full `trait_worlds` into `enforce_fn_requires`.
- Made `enforce_fn_requires` merge use-site visible modules deterministically and preserved module-less builtins in trait requirement normalization; added driver coverage for use-site require visibility.

## 2025-12-28 – Function pointers: thunks + captureless lambdas
- Added NOTHROW→CAN_THROW Ok-wrap thunking for function values with a dedicated FunctionRefKind and a thunk cache; typed-context assignment can insert thunks while `cast<T>` stays strict.
- Added captureless lambda coercion to `fn(...)` pointers with capture rejection and can-throw validation.
- Materialized thunk and lambda synthetic functions in the driver pipeline pre-LLVM (MIR emission is now explicit and stable).
- Added tests for thunk selection, captureless/capturing lambda coercion, and synthetic MIR emission (including unique lambda ids per enclosing function).
- Moved CLI stub-checker enforcement after typecheck with CallInfo so nothrow method-boundary violations are enforced deterministically (no name-based inference); normalized HIR is used for CallInfo alignment.

## 2025-12-26 – Borrow checker statement-level liveness + ref-copy loans
- Refined NLL-lite borrow tracking with per-statement ref liveness inside blocks, while preserving conservative “unused borrow stays live” behavior via lexical-scope caps.
- Propagated loans across ref-to-ref `let`/assignment by cloning loans onto the destination ref with its own region cap.
- Added regression tests for same-block last use, ref-copy liveness, and unused-borrow conservatism (including inner-scope release).
- Borrow checker suite and targeted borrow codegen e2e cases passed.
- Replaced `nonescaping` annotations with internal tri-state `param_nonretaining` metadata, added a conservative non-retaining analysis pass, and wired lambda validation + borrow checking to use it.
- Added strict fallback resolution for direct free-function calls in non-retaining analysis and allowed immediate `.call(...)` invocation on lambda receivers.

## 2025-12-21 – Modules + packages + trust, plus core language additions
- Landed multi-module workspace builds with explicit module roots (`-M/--module-path`) and deterministic module-id inference from directory paths, with strict module header validation (duplicate headers / not-first / mismatch / invalid ids / reserved prefixes).
- Implemented explicit exports (`export { ... }`) and module-only imports (`import m [as x]`) with private-by-default visibility, deterministic star export expansion, and strict conflict rules (import/import + import/local are hard errors; repeated imports idempotent).
- Added re-export authority (values/types/consts): `export { foo }` can re-export imported bindings; re-exported values materialize as trampolines; re-exported consts are materialized into the exporting module’s const table; packages validate that interfaces match payload exports.
- Introduced deterministic package artifacts (DMIR-PKG v0) as an offline container for compiler IR with strong hash verification, plus trust enforcement with sidecar signatures (`pkg.dmp.sig`) and a project-local trust store (revocation supported; driftc is the offline gatekeeper).
- Added `drift` tooling (offline, no compiler internals): `keygen`, `sign`, `trust add-key/list/revoke`, plus local workflow commands `publish`, `fetch`, `vendor` and an authoritative `drift.lock.json` (single version per package id per build pinned).
- Hardened cross-module ABI boundaries: exported functions always use the boundary `FnResult<Ok, Error*>` convention; cross-module calls must target the public wrapper (never `__impl`), with safe unwrap-or-trap in nothrow contexts; strict package interface validation blocks malformed exports/signatures/method exports.
- Expanded core language coverage with passing end-to-end tests:
  - Variants + `match` as an expression with `default` arms, block bodies, and robust binder handling (alpha-renaming + checker-normalized binder field indices; stage2 remains assert-only).
  - Qualified type member access for constructors (`TypeRef::Ctor(...)`) including bounded generic disambiguation (`Optional<Array<String>>::None()`), plus improved constructor diagnostics and a pinned parser diagnostic for duplicate type-arg lists.
  - `const` declarations with compile-time literal evaluation (unary +/-), export/import, module alias access, and package encoding/validation of exported const tables.
  - Float (`double`) end-to-end (literals + formatting via Ryu) and f-strings with typed interpolation.
  - Borrow/move/method/field infrastructure continued to mature (canonical places, materialized rvalue borrows, swap/replace, module-scoped nominal types and methods).

## 2025-12-15 – Exceptions: constructor-only throw syntax + schema-validated args
- Switched exception throwing to constructor-call form only: `throw E(...)` (parens required even for zero-field events via `throw E()`); removed brace-based and shorthand throw forms across parser/AST/HIR/checker/lowering and tests.
- Added shared exception ctor argument resolver (`lang/driftc/core/exception_ctor_args.py`) to map positional/keyword args to declared exception fields (schema order), with diagnostics for missing/unknown/duplicate fields.
- Extended parser/stage0/HIR kwarg nodes to carry name spans for precise diagnostics; `HExceptionInit` now carries `pos_args` and `kw_args` with spans; try-result rewrite preserves the new shape.
- Updated checker (stub + type checker) and HIR→MIR lowering to validate/resolve ctor args against `TypeTable.exception_schemas` and attach attrs deterministically; e2e + unit tests updated accordingly; full suite passes (`just`).

## 2025-12-09 – Borrow checker Phase 2 (coarse loans) + borrow HIR
- Added HBorrow HIR node and parser lowering for `&` / `&mut`; exported via stage1 API.
- Extended borrow checker to track active loans (shared vs mut) in CFG/dataflow state, enforcing lvalue-only borrows, moved/uninit rejects, conflict rules (whole-place overlap), and moves-blocked-while-borrowed. Assignments drop overlapping loans; temporary borrows in expr/conds are dropped after use; Loan carries region_id for upcoming NLL work. Optional shared auto-borrow flag scaffolded with call-scoped temporary loans.
- Added borrow-specific tests (rvalue/moved borrow errors, shared allowed, shared+mut and mut+mut conflicts, move under loan, temp-borrow NLL approx) alongside existing move/CFG tests.
- Updated progress tracking for Phase 2 and documented the new scaffolding; borrow checker docstrings now cover loans. Tests: `PYTHONPATH=. .venv/bin/pytest lang/borrow_checker/tests`.

## 2025-12-09 – Borrow checker scaffolding (places + CFG/dataflow)
- Implemented hashable place identity (`PlaceBase` with kinds/ids) and projection-aware places; added `PlaceState` + `merge_place_state` lattice for dataflow joins.
- Added Phase-1 borrow_checker_pass: builds a CFG from HIR, runs forward dataflow to track UNINIT/VALID/MOVED, walks all HIR expressions to record moves, and emits use-after-move diagnostics with stable names.
- Improved tests and tooling: branch/loop CFG move tests, expanded move-tracking and place-builder coverage, Justfile target `lang-borrow-test` included in `lang-test`; diagnostics reset per run.
- All borrow checker suites passing: `PYTHONPATH=. .venv/bin/pytest lang/borrow_checker/tests`.

## 2025-12-08 – String params & array helper decls
- Fixed LLVM backend to type arguments using function signatures (Int → i64, String → %DriftString) and emit typed call sites; function headers now preload param types into value_types.
- Moved array runtime helper declarations to module scope (emit once per module), preventing invalid IR from function-local declares.
- Added LLVM IR tests for typed params: Int+Int headers/calls and mixed Int/String param plus String return; added String literal pass-through call test.
- Updated docs/comments: compile_to_llvm_ir_for_tests now mentions Int/String/FnResult returns; string work-progress reflects param support; TODO trimmed.
- All tests green (PYTHONPATH=.. ../.venv/bin/pytest).
## 2025-12-08 – String ops in LLVM
- Added String-aware binary op lowering: `==` calls `drift_string_eq`, `+` calls `drift_string_concat`, and String `len` reuses ArrayLen lowering to extract the length field.
- Module builder now emits `drift_string_eq`/`drift_string_concat` declares once when needed; array helper declares remain module-level.
- Added LLVM IR tests for string len on a String operand and for string eq/concat; existing literal/pass-through tests remain green.
- All tests passing: PYTHONPATH=.. ../.venv/bin/pytest.
## 2025-12-08 – String ops via MIR, e2e len/eq/concat
- HIR→MIR now emits explicit `StringLen`, `StringEq`, and `StringConcat` for `len(s)`, `s == t`, `s + t` on strings; BinaryOpInstr no longer handles string operands.
- LLVM lowers these MIR ops: string len via `extractvalue %DriftString, 0`; eq/concat via runtime calls with module-level declares for `drift_string_eq` / `drift_string_concat`.
- E2E runner links string_runtime; added e2e cases for string len (literal/roundtrip), concat len, and eq; all passing. Added negative LLVM test for unsupported string binops.
- Array helper declares remain module-level; all tests green.
## 2025-12-09 – String hex escapes, Uint alignment, bitwise enforcement
- Parser now accepts `\xHH` hex escapes in string literals; added e2e `string_utf8_escape_eq` comparing a UTF-8 literal to its escaped form (equal at runtime) and adjusted UTF-8 multibyte e2e to check byte_length. Literal escaper continues to produce correct UTF-8 globals.
- Checker maps opaque/declared `Uint` to the canonical Uint TypeId (len/cap return types); bitwise ops are enforced as Uint-only with a clear op set. `String.EMPTY` handling in HVar inference simplified.
- `%drift.size` alias reinstated in IR (Uint carrier); string/array IR tests updated to expect `%drift.size` in `%DriftString`. ArrayLen lowering comment cleaned up (strings use StringLen MIR).
- All suites green after changes: just lang-codegen-test, lang-test, parser/checker/core/stage tests.
## 2025-12-09 – Parser diagnostics & shared typing cleanup
- Parser adapter now reports duplicate functions as diagnostics (with spans) instead of raising; parse_drift_to_hir returns diagnostics. E2E runner supports phase-aware diagnostic cases and matches stderr/exit for parser/checker failures; added duplicate_main e2e case.
- Added lang/driftc.py `--json` flag to emit structured diagnostics (phase/message/severity/file/line/column) for parser failures; CLI bootstraps sys.path for venv usage.
- Checker refactor: introduced shared _TypingContext + _walk_hir; array/bool validators share locals/diagnostics, and new tests cover param-indexed arrays and param-based if conditions.
- Parser now builds signatures and HIR from the same non-duplicate function set so duplicates can’t desync signature vs. body; parser tests updated and pass.
- All updated parser/checker/e2e tests passing (PYTHONPATH=. pytest ...; runner duplicate_main ok).
## 2025-12-25 – Generics, traits, visibility, and NLL-lite borrow polish
- Adopted **`<type …>` call-site generics** with hard `type` keyword, explicit type application in calls and callable refs, and parser guards against duplicate type-app suffixes; added UFCS calls (`Trait::method(...)`) and `use trait` directives for explicit trait scope.
- Introduced **TypeParamId/TypeVar** spine, explicit instantiation + inference via `InferContext/InferResult`, and centralized inference diagnostics with structured failure notes and new tests.
- Added **struct generics + impl matching** (including nested generic templates), impl requires and struct requires enforcement, and trait bounds as ambient assumptions with call-site proofing.
- Implemented **workspace-wide impl index + method resolution across modules**, method visibility (`pub` gating), and link-time duplicate inherent method checks with deterministic ambiguity diagnostics.
- Completed **visibility model** in code: `pub` eligibility + explicit exports, `export { module.* }` re-exports, module-only imports, and package payload export surfaces with trait exports/reexports and validation.
- NLL-lite borrow checker upgrades: per-ref live-region analysis, join/loop witness notes on conflicts, ref rebinding kills old loans, const-folded index disjointness, and `i != j` branch facts for disjoint indices (with new e2e tests).
## 2025-12-28 – Function type throw-mode hardening and entrypoint rule
- Enforced strict `fn` throw-mode handling: `fn_throws` is now a 2-state bool, rejects explicit nulls, and package codecs preserve `can_throw` with backwards-compatible defaults.
- Cross-module exported/extern calls now force can-throw at the boundary; LLVM trap fallback removed in favor of a hard compiler error for mis-lowered nothrow calls.
- Added entrypoint rules: exactly one `main`, it must return `Int`, and it must be declared `nothrow`; new e2e diagnostics cover missing/duplicate main cases.
- Updated tests to reflect strict throw-mode decoding and entrypoint enforcement.
- Catch event arms now accept unqualified event names (resolved to the current module) with spec updates.
- Added nothrow e2e coverage (throwing calls, try/catch ok, cross-module method requires try, same-module pub ok, can-throw→nothrow fnptr reject).
- Provider-emitted method boundary wrappers now exist for public NOTHROW methods, exported in package signatures and selected at cross-module call sites; cross-module method boundary e2e re-enabled with new try/catch and same-module guard cases.
## 2026-01-02 – Callsite IDs, CallInfo authority, and generics pipeline hardening
- Enforced callsite-id as the sole call-identity: TypedFn now stores call info and instantiations keyed by callsite_id only; node-id maps and adapters removed with guard tests.
- Checker is FunctionId-only; removed legacy name-based adapters and signature-object identity recovery; CallInfo is required in typed mode.
- Split base vs derived signatures (immutable base, derived synthesis only), centralized synthesized signature registration, and made stage2 read-only for signatures.
- Hidden lambdas now typecheck as separate functions with their own callsite maps; capture binding IDs are remapped to fresh function-local IDs; captures are PlaceKind.CAPTURE; capture order is deterministic.
- CallInfo/MIR invariants tightened: every M.Call has explicit can_throw; stage2 rejects call_resolutions in typed mode.
- TemplateHIR-v0 import path removed in CLI (hard error); import boundary is structured IDs only.
- byte_length now takes &String with lvalue auto-borrow; rvalue borrow rejected; entrypoint main remains nothrow Int.
## 2026-01-03 – Return arrow + Fn types migration
- Replaced `returns` with `->` across the surface language (parser, docs, examples, and tests) and adopted `Fn(...) -> T` for function types, including lambda return annotations.
- Updated parser/token handling to recognize `Fn` type constructors and `->` return signatures, with type-mode heuristics adjusted accordingly.
- Modernized the legacy grammar to use `move` and `->` member-through-ref; removed the old `->` as move operator.
- Added regression tests for `->` member access inside function bodies and lambda return annotations.
- Added a deterministic function-type throw-mode identity test and aligned pretty-printers/diagnostic strings with the new syntax.
- Tightened function-type construction APIs (`ensure_function`/`new_function`) to avoid string-typed constructor names and updated all call sites.
- Aligned the legacy grammar with `Fn` types, `nothrow` returns, and the `|>` pipeline token.

## 2026-01-04 – MVP polish: generics codegen stability + typed lowering
- Added stable, argument-sensitive type keys (with hashed LLVM names) for struct/variant caching and FnResult keying to avoid cross-instantiation collisions.
- Fixed struct constructor lowering to pass expected field types and record constructed struct types; tightened typed-mode rules (strict vs recover) and gated strict mode on error-free typechecking.
- Hard-stopped codegen on typecheck errors to avoid partial MIR/SSA emission.

## 2026-02-02 – Call/try plumbing, interfaces, concurrency/runtime, IO/net, and test hardening
- Introduced `use trait` import form for trait method visibility; added driver/e2e coverage for trait scope and UFCS resolution.
- Hardened callsite/callinfo invariants: all synthesized method calls now receive callsite ids; added MIR validators and regressions for missing CallInfo.
- Added structured debug toggles via `DRIFT_DEBUG` JSON and expanded debug channels (try_auto/borrow/ssa/package/stage2).
- Interface ABI stabilization: iface layout modeled as `{data_ptr, vtable_ptr, inline_payload, flags}`; inline flag bitfield; size/align modeling fixed for interface values.
- Implemented Throwing callback traits (FnThrow0/1/2) and `Result.on_error` with capture support; added tests for throw/recover paths and trait visibility.
- Added `std.core.Try` trait and try auto-unwrap behavior; enforced trait visibility in try-blocks; added regression tests.
- Result tombstone formalized with hidden tombstone state; kept tombstone unmatchable in user code; added tests and restored global droppable-variant requirement.
- Improved HIR/SSA lowering: lambda capture materialization, MIR validators for unresolved types, and stricter MoveOut rules for non-Copy by-value args.
- Concurrency runtime: fixed VT double-free and park/unpark races, corrected yield handling, added executor policy plumbing, and expanded join/cancel/timeout semantics with e2e coverage.
- Reactor + IO integration: block_on_io helpers, std.io/std.net nonblocking APIs, TCP/UDP tests, and stress connection e2e with try/on_error patterns.
- Parser enhancements: qualified ctor patterns, module-qualified ctor resolution without expected type, `TypeApp` before qualified member (`Optional<type T>::None()`).
- Added diagnostics: empty array literal requires element type; improved entrypoint checks and try/match value vs statement context.
- Added codegen e2e coverage for two instantiations of the same generic struct in one module.

## 2026-01-06 – Optional consolidation + module/diagnostic policy alignment
- Consolidated `Optional<T>` as a canonical variant (`None=0`, `Some(T)=1`), removed Optional-specific MIR ops/ABIs, and enforced generic variant copy/dup/drop invariants (including `Optional<Bool>` storage decoding).
- Pivoted DiagnosticValue optional ABI to out-params + `bool` return, removed `DriftOptional*` runtime structs, and aligned DV ctor/lookup ABI with isize/i8.
- Tightened type system and IR correctness: forward nominals (no scalar placeholders), reserved builtin names, Byte as a seeded builtin, generic-arg validation, and deterministic variant instantiation caching.
- Hardened array/iterator semantics (CopyValue insertion, auto-borrow for `iter()`, place-only `next()`, Uint-index compare), and made struct/variant layout deterministic for instantiated types.
- Enforced module identity from `module <id>` (one file per module), removed multi-file module merges, and switched trait scope/aliasing to module scope only.
- Removed filesystem paths from diagnostics/DMIR metadata using source labels (`<source>`, `<module>`), updated parsing order for determinism, and clarified spec text for type prelude, catch resolution, and script-only implicit `main`.

## 2026-01-06 – Optional consolidation detailed log
- Created Optional consolidation work-progress and recorded the full plan.
- Added the Optional layout contract and determinism guardrails (fixed `None=0`, `Some=1` tag order).
- Completed an inventory of Optional-specific logic across TypeTable, resolver, parser injection, MIR, stage2, ARC, LLVM, runtime, and tests.
- Enforced Optional arm order in prelude injection and removed MIR OptionalIsSome/OptionalValue ops and references.
- Pivoted DV Optional ABI to out-params + bool return; removed DriftOptional* runtime structs; updated DV lowering/tests; aligned DV ctor ABI; removed duplicate @dataclass.
- Fixed FnResult ok-zero defaults for Uint/Uint64/Float; corrected struct CopyValue/ZeroValue for Bool storage types; fixed instantiated struct size/align; seeded Byte; fixed 32-bit StringCmp cast; removed redundant pointer-null bitcasts; enforced fnptr signature metadata; restored ZeroValue pointer SSA emission; fixed ArrayLit insertvalue emission and ArrayLit CopyValue for Copy-but-not-bitcopy elements; added Array<String> literal retain IR checks; stored FnResult Bool ok as i8 with conversions; asserted Array<ZST> in codegen.
- Added stage2 Optional base seeding on demand; unified Optional instantiation in stage2 and type checker; removed Optional caches and TypeTable.new_optional; added optional mechanical tests and Optional<Bool> IR golden; documented Optional as standard variant in spec; added deterministic variant instantiation test.
- Updated spec for named variant ctor args (no mixing, source-order evaluation); added stage2 source-order evaluation test.
- Added forward nominal kind and upgraded ensure_named/declare_struct/declare_variant to reuse forward TypeIds; reserved builtin names; improved generic arg validation; added reserved names for exceptions.
- Removed multi-file module merge; enforced one-file-per-module; removed module id inference from paths; switched trait scopes/aliases to module scope; removed file-scoped trait scope param; updated driver/tests for module headers and module-scoped use-trait; updated e2e fixtures to micro-modules and merge-module patterns; refreshed expected diagnostics for new module rules.
- Removed filesystem paths from diagnostics/DMIR; introduced SourceLabel relabeling; updated parse order for determinism; removed string path scrubbing; added no-path-leak tests with absolute-path regex detection; updated CLI/spec for module discovery and script-only implicit main.
- Updated e2e fixtures: added exports for m_a/m_b; removed duplicate module headers; added explicit Maybe ctor type args/annotations; updated qualified ctor duplicate-type-args expected line/column.
- Fixed driver test workspace parsing to always pass module roots; repaired accidental module_paths insertion typos in trait tests.
- Updated method resolution e2e diagnostic test to include module/Point and assert the “no matching method” message via JSON.
- Added module headers to borrow checker lambda capture overlap tests; re-instated variant substitution via base instantiation when instances are missing.
- Clarified spec: Float is target-native (per-target ABI); fixed-width floats remain reserved in v1.
- Renamed module_root_mismatch e2e to module_root_unrelated_ok to reflect allowed behavior.
## 2026-01-06 – Optional-as-variant consolidation + package/link determinism hardening
- Consolidated Optional into regular variants (None=0/Some=1), removed Optional-specific MIR/LLVM/runtime paths, and added mechanical tests to ensure Optional ops/kinds are gone.
- Standardized variant lowering: deterministic arm order, non-bitcopy variants, zero-initialized variant construction, and stable copy/drop behavior (including Optional<Bool> storage handling).
- Package type tables and linker: mandatory provided_nominals, semantic TYPEVAR identity, struct schemas carry type exprs + base_id, struct/variant instantiation support (template vs concrete), and strict module-id ownership checks.
- Enforced module ownership determinism: module_ids globally unique, linker populates host.module_packages (lang.core seeded), type_key_string requires provider mapping for imports.
- Added template instantiation caching, deep has_typevar, module-scoped scalar nominals, and multiple regression tests to lock invariants.
- LLVM backend updates: float width support, export wrapper Bool ABI coercion, array drop helper SSA fix + verifier test, and variant payload alignment guards.
## 2026-01-13 – Iterators, move semantics, and exception payload plumbing
- Pinned iterator trait surfaces (`std.iter`) and `for` UFCS lowering with deterministic diagnostics; added driver/e2e coverage for shadowing, function-returned iterables, and capability gating.
- Established `std.core.Copy`/`std.core.Diagnostic` traits and centralized Copy checks in the compiler; added `E_USE_AFTER_MOVE` diagnostics and consuming-position move tracking in the borrow checker.
- Implemented non-Copy array mutation via move-out/tombstone semantics (String/Array/Struct/Variant with `@tombstone` arm), plus required schema validation.
- Added `std.err:IndexError` and `std.err:IteratorInvalidated` exception events; wired bounds checks and iterator invalidation to throw with structured attrs.
- Made array OOB catchable in MIR (`ArrayIndexLoadUnchecked`) and removed runtime bounds-check abort path.
- Centralized Array container_id (`std.containers:Array`) in compiler constants and pinned `IteratorOpId` numeric ABI mapping via `to_diag`.
- Added Copy-only array literal enforcement in typecheck and e2e coverage; kept codegen as internal backstop.
## 2026-01-15 – ArrayRange invalidation + borrow-check fixes
- Fixed ArrayRangeMut swap receiver to use `self.arr.swap(...)` (avoids non-lvalue deref receiver in MIR lowering).
- Updated MIR expr typing to prefer local binding types for `HVar` (stabilizes struct field access in stdlib lowering).
- Allowed mutable borrow for receivers typed as `&mut T` in type checker (removes false “mutable Array receiver” diagnostics).
- Added driver borrow-check tests for array element borrow conflicts/disjoint indices.
## 2026-01-16 – UFCS uniform call resolution
- Added `CallTargetKind.CONSTRUCTOR` to carry variant ctor metadata in CallInfo and lower constructor calls via CallInfo.
- Removed HQualifiedMember special-case lowering in MIR; qualified calls now route through uniform call resolution.
- Allowed trait UFCS calls on non-lvalue reference receivers (e.g., `Comparable::cmp(&T, &T)`).
## 2026-01-17 – Array header layout + LLVM test alignment
- Updated LLVM array header layout to include `gen` and fixed nested array drop helper extract indices.
- Updated LLVM array header tests for the new layout and skipped LLVM-verify test when llvmlite is unavailable.
- Synced runtime Array header layout for argv helpers and initialized gen in argv construction.
- Pinned gen semantics to “actual structural change” and added reserve no-op vs growth invalidation e2e.
## 2026-01-18 – Binary search in std.algo
- Implemented `std.algo.binary_search` on `BinarySearchable + Comparable`.
- Added e2e tests for basic/duplicate binary_search and driver diagnostics tests for missing Comparable and key-type mismatch.
## 2026-01-19 – Trait UFCS fixes for type-parameter receivers
- Fixed UFCS trait method resolution for type-param receivers by honoring require-bound type args; unblocked `BinarySearchable::compare_key` in std.algo and swap e2e coverage.
## 2026-01-20 – Diagnostic codes stabilization
- Added deterministic auto-codes for diagnostics without explicit codes (prefix-detected or hashed), ensuring stable `Diagnostic.code` values across phases.
## 2026-02-01 – Deque container + non-Array payload tests
- Added `Deque` container with `DequeRange`/`DequeRangeMut` and `DEQUE_CONTAINER_ID` in stdlib.

## 2026-02-03 – Diagnostic by-ref, IO/net tests, and example builds
- Switched `Diagnostic.to_diag` to a by-ref method (`self: &Self`) and updated all stdlib implementations (core, err, io, net, concurrent), plus added Copy for `DiagnosticValue`.
- Adjusted `Result`/`Try` paths to use by-ref diagnostics; updated driver test harness stubs and added a new driver regression for non-Copy Diagnostic by-ref implementations.
- Added/expanded std.io/std.net e2e tests for timeouts, nonblocking behavior, TCP/UDP flows, and a TCP stress test; fixed connect-timeout flakiness by accepting success as OK.
- Updated e2e runner debug behavior and test fixtures (e.g., buffer len updates, byte cast buffer write).
- Added file/udp/tcp examples under `examples/` and improved example build recipe output (now prints driftc invocations).
- Added non-Array OOB payload e2e test (`deque_index_error_payload_oob`).
- Added non-Array range invalidation e2e tests for `compare_at`/`swap` (`deque_range_compare_at_invalidated`, `deque_range_swap_invalidated`).
## 2026-02-02 – Module-qualified calls + struct-field gen access
- Module-qualified free calls now resolve via a global module-name map from signatures/registry, fixing `std.err.throw_iterator_invalidated` resolution in stdlib.
- HField len/cap/gen sugar now yields struct fields when present, allowing `Deque.gen` access without bogus `len(x)` errors.
## 2026-01-21 – Sort requirement simplification
- Removed the `Comparable` requirement from `std.algo.sort_in_place`; ordering is defined by `compare_at` on RandomAccess ranges.
## 2026-01-22 – Iterator work-progress cleanup
- Trimmed iterator work-progress to outstanding items only (no functional changes).
## 2026-01-23 – UFCS receiver fixes for std.algo sort_in_place
- Adjusted `sort_in_place` UFCS calls to use `r` (removed `&*r`) and allowed `&mut T` receivers to satisfy `&T` in UFCS compatibility checks.
- Relaxed trait impl visibility blocking so UFCS trait calls resolve against non-local impls.
- Updated driver tests for `sort_in_place` to allow can-throw entrypoints.
## 2026-01-14 – Mutable iteration + Optional<&mut> borrow tracking
## 2026-01-21 – Hashing surface + HashMap/HashSet MVP
- Added `std.core.hash` (Hasher/Hash/BuildHasher/DefaultHasher) with fixed `Uint64` hash output and seeded builder shape; `Hash` now generic over `Hasher`.
- Implemented HashMap/HashSet (BuildHasher stored; linear probing; iterator invalidation via gen).
- Added `String.bytes()` + `string_byte_at` intrinsic; `Hasher.write_u8` added; String hashing uses byte iteration + length delimiter.
- Introduced fixed-width `Uint64` constants for hashing; parser/MIR/LLVM support for Uint64 literals and returns.
- Added e2e cases for HashMap/HashSet (basic ops, collisions, resize, iterator invalidation, string keys/values, zero-capacity, repeated remove).

## 2026-01-22 – Hashing hardening, type aliases, and container infra fixes
- Implemented type aliases (module-scope) and rewired HashMap/HashSet to use aliases for ergonomic defaults.
- Added wrapping u64 intrinsics (wrapping_add/mul), MIR/LLVM support, and spec note; hash mixing uses explicit wrapping ops.
- Switched intrinsic dispatch to signature `intrinsic_kind` (no name/module string matching); validator enforces wrapping u64 operand types.
- Added field/index receiver auto-borrow for method calls; updated tests and removed stdlib self.map borrow workarounds.
- Stabilized trait solver with trait type args; UFCS trait calls enforce trait args and resolve against global trait world.
- Numerous stage1/2/typechecker fixes: ctor kwarg typing, param binding id seeding, canonical TypeParamId comparisons for mem intrinsics, array literal inference with Unknowns, try-expression call allowance.
- TreeMap groundwork: RB tree implementation with arena buffers, iterators, invalidation rules, and e2e coverage (basic, iter order, remove cases, iter invalidation).

## 2026-01-23 – TreeMap/TreeSet polish, EntryMut, and constructor renames
- Fixed RB insert fixup (recompute parent/grandparent after rotations) and added `__test_validate` invariants.
- Added RB invariant/stress tests: `treemap_rb_invariants`, `treemap_rb_stress`.
- Added TreeSet iter order test; TreeSet/TreeMap iter invalidation tests pass.
- Added TreeMap EntryMut API without reference returns (`entry_mut(&K)` + `insert/or_insert/remove`) and e2e coverage.
- Documented EntryMut semantics in stdlib spec; noted no TreeSet Entry API in MVP.
- Renamed free constructors: `hash_map`, `hash_set`, `tree_map`, `tree_set` (updated call sites/tests).
- Added `ArrayBorrowMutIter` and `Iterable<&mut Array<T>, &mut T>` in stdlib; exported mut iterator type for use in signatures.
- Borrow checker now treats Optional<&T>/Optional<&mut T> bindings as ref bindings and tracks borrows through explicit `&/&mut` call arguments.
- Added driver coverage for `for x in &mut xs`, `next()` re-entrancy errors, and safe `next()` after borrow scope ends.
## 2026-01-24 – Trait method resolution for instantiations + guard scoping
- Resolved trait-method dot calls in instantiated generic bodies to direct impls (avoids missing CallInfo in std.algo).
- Deferred diagnostics for ambiguous generic trait guards (OR/NOT), restoring guard scoping behavior.
## 2026-01-25 – Type checker method-call refactor
- Extracted `HMethodCall` handling into `_type_method_call` helper to reduce nesting and stabilize indentation in `type_checker.py`.
- Removed the unreachable post-method-call expr handling block from the helper (kept in `type_expr`).
## 2026-01-12 – Qualified-member call consolidation cleanup
- Prioritized trait UFCS resolution for `HCall` qualified members before variant constructor resolution, preventing false `E-QMEM-NONVARIANT` errors for `Trait::method(...)` (e.g., `cmp.Comparable::cmp`).
- Removed legacy qualified-member ctor resolution inside method-call handling that could leave `ctor_sig` uninitialized and reintroduce duplicate inference paths.
- Restored `for` AST → MIR CFG test by ensuring all stdlib UFCS calls produce CallInfo in typed mode.
## 2026-01-26 – Trait impl visibility + require-arg substitution for method calls
- Trait method resolution for type-parameter receivers now injects trait type arguments from `require` into method signatures in the fallback trait-resolution path.
- Public trait impls are now visible across modules for method resolution (removed module visibility gate for trait impl candidates).
## 2026-01-27 – Ref-mut preference + trait guard diagnostics alignment
- Method resolution now prefers `&mut` over shared `&` when both receivers match, fixing `Iterable::iter(&mut xs)` to resolve the mut iterator impl.
- Updated trait-guard scoping tests to expect missing-require diagnostics for OR/NOT guards.
- Trait dot-call tests now avoid `nothrow` so can-throw trait methods are accepted in MVP.
## 2026-01-28 – Trait method resolution for instantiations
- Relaxed trait impl visibility filtering during generic instantiations so std.algo method calls resolve against caller-provided impls.
## 2026-01-29 – test-build-only annotations
- Added @test_build_only annotation (grammar+parser) and compiler flag --test-build-only; non-test builds ignore annotated items.
- Filtered test-only items and exports during parse, and wired e2e runner to enable test-build-only.
## 2026-01-30 – Preserve marker trait impls under test-build-only filtering
- Kept empty `implement` blocks during @test_build_only filtering so marker traits (e.g., `Copy`) remain available; restored Copy query behavior in typed pipelines.
## 2026-01-30 – Constructor resolution consolidation
- Routed struct constructor argument mapping through call_resolver to reduce duplicate ctor resolution paths in the type checker.
## 2026-01-17 – Generic signature resolution + ctor inference fixes
- Signature normalization now resolves param/return TypeIds with impl/type param maps for generic signatures (prevents generic return types from collapsing to concrete bases).
- Instantiation substitution now maps impl/type params directly to impl_args/fn_args (ensures instantiated return types are concrete).
- Struct ctor resolution now prefers expected-type struct instances when base matches, fixing ArrayMoveIter ctor inference in return positions and restoring typed CallInfo in stdlib.
## 2026-01-21 – Array builtin stepping-stone pinned in spec
- Documented that builtin Array must mirror RawBuffer-backed semantics (initialized prefix + uninitialized capacity) while remaining a compiler-provided type.
- Pinned the long-term direction: indexing resolves via traits, and array literals should lower to a compiler payload that converts into stdlib containers later.
## 2026-01-21 – RawBuffer/Ptr foundation, Array/Deque semantics, and typed-call invariants
- Added `std.mem.RawBuffer<T>` + `MaybeUninit<T>` (trusted only) with intrinsic-backed alloc/read/write/ptr_at and typed GEP lowering; split ptr-at ref/mut intrinsics and added rawbuffer read/write e2e coverage (including Bool conversion).
- Introduced raw pointer kind `Ptr<T>` as a builtin (stdlib surface only), with Copy impl and LLVM lowering; added `ptr_read/ptr_write/ptr_offset/ptr_is_null` intrinsics.
- Rebuilt Deque on RawBuffer ring-buffer semantics (head/len/gen), fixed gen bump only on actual changes, and added wraparound/growth/invalidation/sort+search e2e tests.
- Reworked Array runtime semantics to “initialized prefix + uninitialized tail” with move-out on pop/remove; ABI flattened as `{len, cap, gen, ptr}` and spec updated; argv wrapper forwards gen and uses canonical header type.
- Added Array range/iterator invalidation tests and growth/no-op reserve canaries; move-out non-Copy e2e added (Array<String>/Array<Array<Int>>).
- Hardened call resolution: single CallInfo authority, trait UFCS/method handling cleanup, and MIR-bound validator rejects TRAIT CallTargets across all call forms.
- Standardized visibility filtering via a single `_candidate_visible` helper; restored impl-visibility and trait scope diagnostics with stable notes.
- Centralized unsafe gating into `checker/unsafe_gate.py` and removed std.* prefix trust; unsafe/rawbuffer access now uses trusted-module list + explicit flags/unsafe blocks.
- std.mem `swap/replace` signatures corrected to `&mut` forms; capacity reverted to a normal stdlib function (no intrinsic fast path).
- Clarified index-as-place in spec (`&arr[i]`/`&mut arr[i]`), kept `arr[i]` Copy-only; added borrow/move/for-iteration regression tests.
## 2026-01-26 – Callbacks, dynamic interfaces, ABI hardening, and compiler guardrails
- Added static `Fn0/Fn1/Fn2` traits and dynamic `Callback0/1/2` interfaces in std.core, plus explicit `callback0/1/2` intrinsics for owned-only boxing.
- Implemented dynamic interface values with vtable-backed dispatch, per-interface segments (drop slot 0), deterministic linearization, and upcast via vtable-pointer retargeting; added e2e coverage for inheritance/diamond/upcast/slot order and throwing interface calls.
- Added Arc/Mutex MVP stubs (single-threaded semantics) and Borrow/BorrowMut traits with argument coercion; updated effective-drift emitter example and e2e for callback + Arc<Mutex<...>> patterns.
- Hardened callback safety: borrowed captures rejected for owned callbacks (retaining param metadata), compile-fail e2e for borrowed capture boxing, and stage2 guard against REF/REF_MUT in callback envs.
- Added MIR guardrails: call invariants (can_throw/CallIface), call-type TypeVar checks for concrete calls, and interface init invariants; added Array Copy/alloc and wrapping-u64 invariants.
- ABI updates: interface inline storage (`INLINE_BYTES = pointer_width * 4`), ABI fingerprinting enforced across packages/toolchain, and dedicated runtime hooks for callback env frees.
- Package/boundary fixes: boundary upgrades use package identity only; module_packages now enforced centrally, with stdlib ownership derived from stdlib_root path (no std.* name heuristics); added regression tests.
- Resolver cleanup: qualified-member ctor resolution now relies on `resolve_opaque_type` (no lang.core/std.core fallbacks), plus new ctor-resolution tests (positive and error cases).
- For-loop lowering: borrow-by-default preserved with deterministic temp binding for borrowed temporaries; added stage1 regression.

## 2026-02-06 – Debug info, assert runtime, copy tri-state, gdb tooling
- Fixed DWARF line/locals fidelity for debug_1: preserved return spans through string ARC, added keepalive storage + dbg.declare for SSA locals so gdb can stop on correct lines and print structs.
- Added variant + array DWARF types and tests (debug variant union/tag/payload, array header layout).
- Introduced debug-only type provenance side table and audit (DRIFT_DEBUG type_prov) to trace where TypeIds are determined.
- Copy semantics: added tri-state copy_status with gated structural fallback for concrete resolvable structs/variants; array literals now emit COPY-UNKNOWN instead of misleading non-Copy; added regression tests for forward nominals and typevars; tightened/adjusted copy handling across stage2/typechecker/borrow checker.
- Assert system: SourceManager + span offsets for condition text; compiler passes expr text to runtime; assert output includes expression + message; stacktrace resolver wired via libdw/libunwind; updated runtime signature and e2e assert tests.
- Debug path fixes: corrected HIR->MIR flow bug from local_types_trace indentation and added timing diagnostics (DRIFT_DEBUG timing).
- Added deps check: tools/deps_check.py + just deps-check; hard-fails without ld.gold and required libs; README prerequisites updated.
- GDB tooling: tools/gdb/drift.py commands for strings/arrays; added gdb test runner with sandbox_blocks gating; gdb smoke case validates captures, arrays, floats, structs, variants, refs, function args, and line mapping; integrated into default test suite as last step.
- E2E runner linking now includes libunwind-x86_64 to resolve stacktrace symbols during codegen tests.
- Deps driver test now runs by default (skips only with DRIFT_DEPS_TEST=0) and fixes repo-root detection.
- Updated assert e2e expected stderr to include stacktrace output when available.
## 2026-02-09 – JSON/parse hardening, std.float, and parallel test artifact isolation
- Fixed JSON ordered key encoding for `JsonKeyOrder::OrderedLexUtf8` in `std.json` (no longer a no-op) and strengthened e2e coverage with multi-key deterministic ordering.
- Fixed `resolve_opaque_type` control-flow indentation bug that made core/unique nominal fallback resolution unreachable in `module_id` paths; added core regression test.
- Fixed codegen e2e runner `__ANY__` behavior: wildcard now applies per-stream without short-circuiting checks for the other stream; added driver regression tests for stdout/stderr mismatch cases.
- JSON parser now accepts syntactically valid numeric lexemes without `parse_float` gating (stores raw number text), including large exponents; added e2e regression for large-number raw preservation.
- Added/validated JSON numeric-form coverage (decimal/scientific/negative forms) with raw-lexeme assertions.
- Added `std.float` module (function-based non-finite API due to MVP const-literal limits): `nan`, `infinity`, `neg_infinity`, `is_nan`, `is_infinite`, `is_finite`; added e2e + driver API tests.
- Extended `std.parse.parse_float` to accept case-insensitive signed `nan`, `inf`, and `infinity`; updated numeric contract e2e accordingly.
- Kept `std.json` strict: added e2e regression rejecting non-finite JSON tokens (`NaN`, `Infinity`, `-Infinity`, `+Infinity`).
- Fixed JSON string encoding for control characters: now escapes `\b`, `\f`, and any remaining `< 0x20` bytes as `\u00xx`; added e2e regression.
- Removed temporary probe case `std_parse_float_nonfinite_probe` from e2e suite after investigation.
- Hardened driver clang e2e tests for parallel execution by moving fixed `a.out`/`ir.ll` artifacts to per-run temp directories in:
  - `lang/tests/driver/test_driftc_codegen_e2e.py`
  - `lang/tests/driver/test_driftc_codegen_void_e2e.py`
- Added next-focus planning doc for UTC-only minimal time support:
  - `work/time/work-progress.md`.
## 2026-02-09 – std.time UTC MVP implementation (phases 2-4)
- Added dedicated UTC runtime primitive path distinct from monotonic time:
  - `lang.thread.now_utc_ms()` intrinsic
  - LLVM lowering to `drift_time_now_utc_ms`
  - POSIX runtime implementation using `CLOCK_REALTIME`.
- Updated `std.time.now_utc()` to use UTC runtime source while keeping monotonic APIs on `now_ms()`.
- Implemented `std.time.format_iso8601_utc` canonical output:
  - `YYYY-MM-DDTHH:mm:ss.sssZ`
  - integer civil-date conversion from epoch milliseconds (UTC-only).
- Implemented `std.time.parse_iso8601_utc` strict parser:
  - accepts `YYYY-MM-DDTHH:mm:ssZ` and `YYYY-MM-DDTHH:mm:ss.sssZ`
  - rejects offsets/local forms and malformed/range-invalid fields
  - emits pinned tags: `invalid-syntax`, `invalid-range`, `invalid-utc-designator`, `unsupported-offset`.
- Added std.time e2e coverage:
  - `lang/tests/codegen/e2e/std_time_iso_parse_format/`
  - `lang/tests/codegen/e2e/std_time_iso_parse_invalid/`
  - retained `lang/tests/codegen/e2e/std_time_monotonic_smoke/`.
- Kept driver API compile coverage passing:
  - `lang/tests/driver/test_std_time_api.py`.
## 2026-02-09 – std.time deep hardening coverage
- Added strict parser error tag+offset regression coverage:
  - `lang/tests/codegen/e2e/std_time_iso_parse_error_offsets/`.
- Added broad valid corpus parse/format roundtrip coverage:
  - `lang/tests/codegen/e2e/std_time_iso_valid_corpus/`.
- Added duration/date-math edge coverage across leap/day/month/year boundaries:
  - `lang/tests/codegen/e2e/std_time_iso_duration_edges/`.
- Added negative-epoch behavior coverage for canonical formatting and signed deltas:
  - `lang/tests/codegen/e2e/std_time_iso_negative_epoch/`.
- Added Gregorian century leap-rule coverage (2000/2400 leap, 1900/2100/2200/2300 non-leap):
  - `lang/tests/codegen/e2e/std_time_iso_century_leap_rules/`.
- Added fixed-seed high-volume randomized corpus coverage:
  - `lang/tests/codegen/e2e/std_time_iso_random_corpus/` (3000 valid generated timestamps + 1000 generated invalid non-leap Feb-29 cases).
## 2026-02-09 – std.time Date MVP
- Added `Date` support to `std.time`:
  - `Date { year, month, day }`
  - `is_leap_year`, `days_in_month`, `is_valid_date`
  - `format_iso8601_date`, `parse_iso8601_date`.
- Added Date e2e coverage:
  - `lang/tests/codegen/e2e/std_time_date_parse_format/`
  - `lang/tests/codegen/e2e/std_time_date_invalid_offsets/`.
- Added driver API compile coverage:
  - `lang/tests/driver/test_std_time_date_api.py`.
## 2026-02-09 – Concurrency cancel-before-start runtime race fix
- Fixed an intermittent cancellation race in POSIX thread runtime that could cause double callback destruction and heap corruption (`malloc_consolidate(): unaligned fastbin chunk detected`) in cancel-before-start paths.
- Runtime change in `lang/language_runtime/posix/thread_runtime.c`:
  - guarded callback drop with `atomic_exchange(completed, 1)` in both worker pre-start-cancel handling and `drift_thread_cancel`, ensuring single-owner destruction.
- Added regression stress e2e:
  - `lang/tests/codegen/e2e/concurrent_cancel_before_start_race_stress/`
  - repeatedly exercises spawn→cancel→join_timeout(0) to lock in race behavior.
- Revalidated related cancellation cases:
  - `concurrent_cancel_before_start_join_timeout_zero_cancelled`
  - `concurrent_cancel_before_start_join_returns_cancelled`
  - `concurrent_cancel_before_start_join_timeout_nonzero_cancelled`
  - `concurrent_cancel_after_start_does_not_kill`
  - `concurrent_cancel_then_join_closed`.

## 2026-02-10 – Looping MVP completion and checker stabilization
- Landed looping syntax + lowering MVP:
  - counted/index form: `for var/val/type i = init; cond; step { ... }`
  - iterable shortcut form: `for val/type x : source { ... }`
  - legacy `for x in xs` preserved.
- Added parser/stage AST-HIR plumbing for typed/mutable loop binders and counted-loop init metadata.
- Fixed counted-loop scope leak so init binders do not escape loop scope.
- Added loop regression coverage:
  - parser valid/invalid header cases (`lang/tests/parser/test_parser_for_looping.py`)
  - stage1 scope regressions (`lang/tests/stage1/test_ast_to_hir.py`)
  - e2e behavior/typing cases:
    - `for_loop_colon_sum_int`
    - `for_count_loop_sum_int`
    - `for_count_loop_continue_break`
    - `for_count_nested_continue_break`
    - `for_count_outer_continue_step`
    - `for_iter_colon_typed_mismatch`
    - `for_count_typed_init_mismatch`
    - `for_count_loop_scope_unknown_name`.
- Added driver regression for unknown loop-scope names:
  - `lang/tests/driver/test_unknown_name_diagnostic.py`.
- Introduced checker `E-UNKNOWN-NAME` for unresolved user-style local names in function scope and stabilized it to avoid false positives:
  - skip plain callee-var traversal in generic call-walk path
  - suppress unknown-name checks in shallow/incomplete inference contexts (lambda internals, try/match arm-local inference paths).
- Fixed follow-up regressions surfaced by broader e2e runs:
  - prelude callable path (`byte_length`) false positive
  - match/arm inference regression that caused `cannot bind a Void value` in `array_string_pop`
  - restored passing concurrency/match/exception payload e2e families.
- Full test suite passed; looping branch marked ready to close.

## 2026-02-12 – Trait UFCS nothrow contract fix (`hashmap_collision`)
- Fixed `hashmap_collision` checker regression where `HashMapCore::find_slot` was reported as “declared nothrow but may throw” after `Equatable.eq` contract tightening.
- Root cause: checker nothrow analysis was overriding direct-call `CallInfo.can_throw` with callee metadata even when call-site trait contract had already resolved to non-throwing.
- Checker fix in `lang/driftc/checker/__init__.py`:
  - preserve explicit non-throwing call-site contracts during direct-call analysis;
  - only refine direct callee throw status when call was already marked can-throw.
- Hardened trait UFCS throw-effect computation in `lang/driftc/checker/call_resolver.py`:
  - trait metadata fallback to trait-world when trait-index method metadata is incomplete;
  - explicit `declared_nothrow`-driven `CallInfo.can_throw` for trait UFCS calls.
- Added parser-side impl signature hardening in `lang/driftc/parser/__init__.py`:
  - trait method `declared_nothrow` inheritance into impl method signatures when omitted by the impl method declaration.
- Added dedicated driver regression:
  - `lang/tests/driver/test_trait_impl_nothrow_inherits_interface.py`.
- Revalidated:
  - e2e `hashmap_collision` now passes;
  - `hashmap_clear`, `hashmap_iter_invalidate`, and `std_log_level_filtering` spot checks pass;
  - existing checker regression `test_equatable_nothrow_ssa_return_regression` remains passing.

## 2026-02-12 – Test hygiene + underscore semantics follow-up
- Stopped test-generated I/O artifacts from polluting repo root by moving fixed filenames to `/tmp` in affected tests:
  - `lang/tests/codegen/e2e/std_io_file_read_write/main.drift`
  - `lang/tests/codegen/e2e/std_io_file_builder_read_write_api/main.drift`
  - `lang/tests/codegen/e2e/std_io_file_builder_chunked_large/main.drift`
  - `lang/tests/codegen/e2e/std_io_stdin_line_edge_matrix/main.drift`
  - `lang/tests/codegen/e2e/std_io_buffer_len_updates/main.drift`
  - `lang/tests/codegen/e2e/std_io_double_close_ok/main.drift`
  - `lang/tests/driver/test_match_stmt_missing_return_repro.py`
- Removed underscore-prefixed special-casing from borrow liveness:
  - `lang/driftc/borrow_checker_pass.py` no longer shortens unused borrows for names starting with `_`; they are treated like ordinary bindings.
  - Added regression in `lang/tests/borrow_checker/test_regions.py`:
    - `test_unused_underscore_borrow_same_block_still_blocks_write`.
- Pinned and verified `Err(_)` pattern usage in expression match arms:
  - added e2e regression `lang/tests/codegen/e2e/match_result_err_underscore_expr_value`.
  - confirmed prior parse confusion was due to `return` inside expression-value match arms, not underscore binder parsing.
- Updated JSON wrapper roundtrip tests accordingly and kept `_` binder form:
  - `lang/tests/codegen/e2e/std_json_parse_into_wrappers/main.drift`
  - `lang/tests/codegen/e2e/std_json_wrapper_roundtrip_to_node_into/main.drift`.

## 2026-02-13 – std.runtime registry expect/tag slice
- Added `std.runtime` miss helper API:
  - `RegistryError(tag: String)`
  - `expect<T>(reg: &GlobalRegistry, tag: String) -> &T` (throws on miss).
- Added regression coverage for generic throws carrying string exception fields:
  - `lang/tests/driver/test_exception_string_generic_throw_regression.py`.
- Added e2e coverage for registry expect success + miss-tag behavior:
  - `lang/tests/codegen/e2e/std_runtime_global_registry_expect_tag`.
- Added runtime registry docs/examples:
  - `docs/effective-drift.md` registry section
  - `examples/runtime_registry/global_singleton.drift`
  - `examples/runtime_registry/per_thread_slots.drift`.
- Validation:
  - `std_runtime_global_registry_expect_tag` passes.
  - targeted driver regression subset passes (`13 passed`).
- Pinned limitation:
  - catch binders currently lower as `Error`; direct field access like `e.tag` in catch arms is not yet supported.
  - supported catch-path access remains `e.attrs["tag"]` + `as_*` extractors.

## 2026-02-13 – Macro/basic hardening + String byte-length API cleanup
- Macro/basic + caller metadata slice stabilized:
  - `std.meta` added with intrinsic `caller()` and `Caller` carrier (`module_id`, `file`, `line` accessors).
  - Added e2e coverage: `lang/tests/codegen/e2e/std_meta_caller_basic`.
- LANGUAGE_BUG fix (regression-first): discard-binding local alias corruption
  - Pinned regression: `lang/tests/codegen/e2e/discard_binding_rebind_noncopy_ir_stable`.
  - Fixed MIR local canonicalization for `val _ = ...` so discard bindings with no binding-id get unique hidden locals; removed `_` type alias back-propagation.
  - Eliminated invalid LLVM cleanup IR (`extractvalue` on pointer-typed `%self`).
- String byte-length API policy finalized:
  - Public user-facing API is `String.byte_length()`.
  - Global `byte_length(...)` is internal-only (`std.*`) and rejected in user modules with pinned diagnostic:
    - `global byte_length(...) is not exposed; use s.byte_length()`.
  - Added regressions:
    - e2e: `lang/tests/codegen/e2e/byte_length_global_rejected`
    - driver: `lang/tests/driver/test_string_byte_length_api.py`.
- Receiver autoborrow policy cleanup:
  - Removed hardcoded method-name exception from checker.
  - Shared `&self` method receivers now follow generic rvalue-shared-autoborrow path; `&mut self` still requires addressable place.
  - Updated/added driver coverage:
    - `lang/tests/driver/test_autoborrow_receiver_place.py`
    - `lang/tests/driver/test_method_call_nothrow_resolution.py`.
- Prelude/driver updates aligned with API:
  - `lang/tests/driver/test_prelude_flag.py::test_std_core_string_from_utf8_bytes_compiles` now uses `s.byte_length()`.
- Validation:
  - targeted driver + e2e subsets passed,
  - ASAN + alloc-track targeted e2e subset passed.

## 2026-02-14 – Trait UFCS fix for DiagnosticValue receiver
- Fixed LANGUAGE_BUG affecting generic/UFCS trait calls on `DiagnosticValue` through `Ref<...>` receivers.
- Pinned regression: `lang/tests/codegen/e2e/generic_debuggable_ref_ufcs` (previously failed with:
  - `no implementation for trait '__local__::std.log.Debuggable' on receiver Ref<DiagnosticValue>`).
- Root cause:
  - `GlobalTraitImplIndex._target_base_id` did not index impl targets with `TypeKind.DIAGNOSTICVALUE`.
- Fix:
  - `lang/driftc/trait_index.py` now maps `TypeKind.DIAGNOSTICVALUE` target types to their base id for trait impl candidate lookup.
- Validation:
  - e2e passed: `generic_debuggable_ref_ufcs`, `macro_log_app_logging_context`, `std_log_context_nested_scopes`.
  - macro diagnostics smoke remained passing:
    - `lang/tests/driver/test_macro_basic_diagnostics.py::test_macro_wrong_arity_reports_error`
    - `lang/tests/stage1/test_ast_to_hir.py::test_macro_log_wrong_arity_rejected`.

## 2026-02-14 – std.log ownership pivot to explicit create_logger
- Implemented point-1 API direction: no hidden std.log global init/main logger path.
- `std.log` surface changed to explicit logger creation and instance ownership:
  - added `create_logger(name: String, config: LoggerConfig) -> Logger`
  - `Logger` now carries runtime-state handle
  - added `Logger.flush(timeout: std.concurrent.Duration) -> Bool`
  - removed global shortcut path from `std.log`:
    - `init`, `logger_main`, `logger_named`
    - free-function `debug/info/error` (+ ctx variants)
    - global `flush(...)`
- Updated logging e2e/tests/examples to the explicit model:
  - e2e: `std_log_*`, `macro_log_app_logging_context`
  - driver: `test_std_log_api_smoke.py`, `test_macro_basic_diagnostics.py`, `test_map_literal_move_canonicalization.py`
  - examples: `examples/logging/basic_events.drift`, `examples/logging/debuggable_document.drift`, `examples/logging/pluggable_formatter.drift`
  - docs snippet updated in `docs/effective-drift.md`.
- LANGUAGE_BUG fixed (regression-first) discovered during this change:
  - symptom: shadowed lets with same source name could generate invalid drop-glue IR type mismatch.
  - regressions:
    - `lang/tests/codegen/e2e/let_shadow_drop_type_metadata`
    - `lang/tests/codegen/e2e/local_shadow_same_name_distinct_types_codegen`
  - root cause: `_visit_stmt_HLet` wrote `self._local_types[stmt.name]` alias, overwriting canonical-local type metadata under shadowing.
  - fix: removed source-name alias overwrite in `lang/driftc/stage2/hir_to_mir.py`.
- Validation:
  - updated logging e2e subset passed.
  - updated driver subset passed.

## 2026-02-14 – stdio capability install + one-time stderr resolve in std.log
- Added stdio capability install API in `std.io`:
  - `install_process_stdio(reg: &std.runtime.GlobalRegistry) -> Bool` (idempotent set-or-present semantics).
- Added capability carriers in `std.io`:
  - `ProcessStdinCapability`, `ProcessStdoutCapability`, `ProcessStderrCapability`.
- Logger integration:
  - `std.log::create_logger(...)` now performs one-time stdio capability install/resolve via `global_registry`.
  - `LoggerRuntimeState` stores resolved stderr capability.
  - log hot path uses stored capability for emission (no per-log registry lookup).
- Validation:
  - logging e2e subset passed (`std_log_*`, `macro_log_app_logging_context`).
  - driver subset passed (`test_std_log_api_smoke`, `test_macro_basic_diagnostics`).

## 2026-02-18 – Boundary hardening: module-const place bases in strict MIR lowering (LANGUAGE_BUG)
- Fixed strict stage2 failure for address-of on module consts (`&CONST`) that previously raised:
  - `internal: MIR lowering contract failure (typed_mode strict: missing binding_id for place base (checker bug))`.
- Root-cause fix:
  - `lang/driftc/stage2/hir_to_mir.py`
  - `_lower_addr_of_place(...)` now handles module-const bases (no local `binding_id`) by materializing const value into a local temp and taking its address.
  - strict `binding_id` guard remains enforced for true local-place cases.
- Added driver regressions:
  - positive: `lang/tests/driver/test_module_const_ref_place_binding.py::test_module_const_ref_place_does_not_hit_binding_id_contract`
  - positive (constructor-shape close to live TCP auth): `lang/tests/driver/test_module_const_ref_place_binding.py::test_module_const_and_borrowed_field_in_constructor_args_compile`
  - negative (checker-facing): `lang/tests/driver/test_module_const_ref_place_binding.py::test_mut_borrow_of_module_const_reports_checker_error_not_internal`
- Verified strict guard coverage still holds:
  - `lang/tests/driver/test_binding_id_strict_guard.py`.

## 2026-02-18 – Boundary policy + FnResult/Array contract alignment
- Added explicit boundary guardrails to repo policy:
  - `AGENTS.md` now requires positive+negative boundary regressions and stale-contract cleanup whenever stage-boundary support changes.
- Aligned FnResult ok-payload contract for arrays end-to-end:
  - codegen support includes `TypeKind.ARRAY` in FnResult ok mapping (`lang/codegen/llvm/llvm_codegen.py`).
  - updated stale docs/comments in LLVM codegen header/docs to include `Array<T>` support.
  - added positive e2e regression: `lang/tests/codegen/e2e/fnresult_ok_array_byte`.
  - updated negative LLVM unit to keep unsupported-shape guardrail via interface payload:
    - `lang/codegen/llvm/tests/test_llvm_codegen_negative.py::test_can_throw_fnresult_with_unsupported_interface_ok_type_is_rejected`.
  - added boundary driver regression:
    - `lang/tests/driver/test_codegen_boundary_diagnostics.py::test_codegen_pipeline_allows_fnresult_array_ok_payload`.

## 2026-02-18 – std.crypto SHA-1 for MySQL native auth path
- Added API:
  - `std.crypto.sha1(bytes: &Array<Byte>) -> Array<Byte>` in `stdlib/std/crypto/crypto.drift`.
- Added e2e coverage:
  - vectors: `lang/tests/codegen/e2e/std_crypto_sha1_vectors`
  - MySQL native token flow: `lang/tests/codegen/e2e/std_crypto_sha1_mysql_native_password_token`
- Validation completed in normal + memory/sanitizer modes for this subset:
  - `DRIFT_ASAN=1`, `DRIFT_ALLOC_TRACK=1`, `DRIFT_MEMCHECK=1`.

## 2026-02-18 – Shared env-bool parser cleanup
- Centralized Python env-flag truth parsing:
  - new shared helper: `lang/driftc/env_flags.py::env_true(...)`.
- Rewired call sites:
  - `lang/driftc/driftc.py`
  - `lang/tests/codegen/e2e/runner.py`
  - `lang/tests/driver/test_driftc_wrapper_env_modes.py`
- Hardened wrapper env-mode tests against inherited env state (notably `DRIFT_ASAN`) and variant-specific runtime archive assertions.

## 2026-02-19 – Exported type-alias constructor resolution via module aliases (LANGUAGE_BUG)
- Fixed parser/module-resolution gap where `pub type` aliases exported from a module were not callable as constructors through import aliases.
  - symptom: `module '<mod>' does not export symbol '<alias>'` on `api.X(...)` even when `X` was exported aliasing a struct.
- Root-cause fix in `lang/driftc/parser/__init__.py`:
  - module-qualified call rewrite now resolves exported alias ctor targets (including re-export origins) when alias ultimately resolves to a concrete struct.
  - export diagnostics now include exported type names (not only value/struct sets) for qualified-call reporting consistency.
- Regression added:
  - `lang/tests/driver/test_module_alias_exported_type_alias_ctor.py`.

## 2026-02-19 – Match lowering double-drop on Result<borrowed-aggregate> payload move (LANGUAGE_BUG)
- Fixed stage2 match-lowering bug where by-value binder extraction from variant payload could trigger premature/drop-duplicate destruction.
  - symptom (minimized): payload moved from `Result::Ok(Statement)` was destroyed before arm-body use, then destroyed again on later drop path.
  - observed as e2e failure and crash-class behavior with borrowed-aggregate destructors.
- Root-cause fix in `lang/driftc/stage2/hir_to_mir.py`:
  - binder move path now treats payloads requiring runtime drop as move-out candidates.
  - when payload is moved from arm scrutinee storage, lowering no longer emits immediate scrutinee drop in that path.
- Regression added:
  - `lang/tests/codegen/e2e/struct_ref_field_result_ok_move_drop_once`.
- Validation:
  - targeted e2e + stage2 tests pass.
  - regression passes under `DRIFT_ASAN=1` and `DRIFT_MEMCHECK=1`.

## 2026-02-19 – Match handoff state corruption from Result::Ok binder extraction (LANGUAGE_BUG)
- Pinned a deterministic e2e reproducer for live-observed state rollback after `Result::Ok(move conn)` bind:
  - `lang/tests/codegen/e2e/result_ok_move_conn_source_drop_regression`.
  - failure signature before fix: process exited `21` (post-bind state reverted).
- Root-cause fix in `lang/driftc/stage2/hir_to_mir.py` (`_lower_match`):
  - by-value match binders now always materialize/consume the arm-local scrutinee storage path instead of reading payload from the original scrutinee value path.
  - full-field binder arms are treated as scrutinee-consuming for drop scheduling, preventing the payload from being dropped out of the scrutinee while still in active arm use.
- Added/kept nearby non-network state-handoff coverage:
  - `lang/tests/codegen/e2e/rpc_connect_state_handoff_pure_inmemory`
  - `lang/tests/codegen/e2e/rpc_connect_state_handoff_nonnetwork_shape`
- Validation:
  - `result_ok_move_conn_source_drop_regression` now passes.
  - nearby regressions pass in normal and ASAN mode:
    - `treemap_entry_basic`
    - `treemap_entry_invalidate`
 - Boundary guardrail follow-up:
   - added negative checker-path regression for unsupported by-value copy through ref-scrutinee binder:
     - `lang/tests/codegen/e2e/match_ref_scrutinee_noncopy_copy_rejected`
   - added stage2 contract-shape assertion test pinning binder extraction path:
   - `lang/tests/stage2/test_hir_to_mir_match_requires_binder_indices.py::test_match_by_value_binder_extracts_via_addr_path_not_value_copy`

## 2026-02-20 – Match arm scrutinee-drop regression on Result::Ok payload binders (LANGUAGE_BUG)
- Fixed a new stage2 ownership regression where by-value `Result::Ok(...)` binder extraction could still drop the arm scrutinee before arm-body execution.
  - symptoms:
    - `result_ok_move_conn_source_drop_regression` failed with exit `21` (state corruption from premature payload destruction).
    - `struct_ref_field_result_ok_move_drop_once` failed with exit `11` (drop-once violation for borrowed aggregate payload path).
- Root-cause fix in `lang/driftc/stage2/hir_to_mir.py` (`_lower_match` binder extraction path):
  - when payload field is extracted via arm-local scrutinee address path (`VariantGetFieldAddr` + `LoadRef`), lowering now treats that payload extraction as scrutinee-consuming for cleanup ordering.
  - this prevents pre-arm scrutinee drop from destructing the `Ok` payload while the binder/local arm value is still in active use.
- Validation:
  - targeted e2e:
    - `result_ok_move_conn_source_drop_regression` (pass)
    - `struct_ref_field_result_ok_move_drop_once` (pass)
    - `result_ok_array_match_move_no_double_free` (pass)
  - boundary guardrail tests:
    - `lang/tests/driver/test_boundary_matrix_result_variant_contract.py` (pass)
    - `lang/tests/driver/test_codegen_boundary_diagnostics.py` (pass)
    - `lang/tests/driver/test_codegen_preemit_boundary_diagnostics.py` (pass)
    - `lang/tests/driver/test_result_ok_copy_struct_string_retain.py` (pass)

## 2026-02-17 – Checker hardening for non-Copy array index reads (LANGUAGE_BUG)
- Regression-first fix for internal crash path:
  - symptom: stage2 raised `NotImplementedError` for `HIndex` on `Array<T>` when element type was non-Copy.
  - pinned regression: `lang/tests/codegen/e2e/array_index_non_copy_read_rejected`.
- Root-cause fix in checker boundary:
  - `lang/driftc/checker/__init__.py` now emits normal typecheck diagnostics for non-Copy array index reads (`cannot copy value of type ...`) before stage2 lowering.
  - added assignment-target suppression for `HAssign` indexed lvalues so assignment type checks do not spuriously trigger copy diagnostics on target inference.
  - added structural typevar detection for generic contexts to avoid false `E-COPY-UNKNOWN` in unresolved type-parameter paths.
- Follow-up stability fix:
  - corrected checker enum reference from `TypeKind.TYPE_PARAM` to `TypeKind.TYPEVAR` (this unblocked `just deps-check`).
- Validation:
  - e2e: `array_index_non_copy_read_rejected`, `array_pop_move_out_non_copy`, `borrow_array_elem_mut` passed.
  - sanitizer/memory modes for new regression passed: `DRIFT_ASAN=1 DRIFT_ALLOC_TRACK=1`, `DRIFT_MEMCHECK=1`.
  - stage suites passed: `lang-stage1-test`, `lang-stage2-test`, `lang-stage3-test`, `lang-stage4-test`.

## 2026-02-16 – std.text safe bytes→UTF-8 API expansion
- Added safe range decode API in stdlib:
  - `std.text.utf8_from_bytes_range(input: &Array<Byte>, start: Int, end: Int) -> Result<String, Utf8Error>`.
- Kept user-land path safe (no unsafe/rawbuffer requirement) and aligned error shape with existing UTF-8 decoder behavior.
- Added e2e coverage:
  - `lang/tests/codegen/e2e/std_text_utf8_from_bytes_range`
  - `lang/tests/codegen/e2e/std_text_utf8_from_bytes_range_errors`
  - `lang/tests/codegen/e2e/std_text_utf8_from_bytes`
  - `lang/tests/codegen/e2e/std_text_utf8_error_shape`.

## 2026-02-16 – Byte semantics + typed const support
- Pinned unsigned byte semantics in e2e:
  - `lang/tests/codegen/e2e/byte_cast_int_unsigned_semantics`.
- Added typed const support coverage for MVP scalar literals:
  - byte literal accept/reject:
    - `lang/tests/codegen/e2e/const_byte_typed_literal_ok`
    - `lang/tests/codegen/e2e/const_byte_typed_literal_oob_rejected`
  - bool/float typed consts:
    - `lang/tests/codegen/e2e/const_bool_float_typed_literals_ok`.
- Parser + stage2 lowering updates landed to support those typed const forms.

## 2026-02-16 – LLVM codegen fix for nothrow Array return path (LANGUAGE_BUG)
- Fixed internal codegen failure when a non-throwing function returned `Array<T>` by value.
- Added regression e2e:
  - `lang/tests/codegen/e2e/array_return_nothrow`.
- Outcome: compile/run path now succeeds (instead of internal `NotImplementedError` codegen failure).

## 2026-02-16 – Toolchain UX: signing/trust/publish + local dist flow
- Added/expanded `drift` CLI operations and tests for key/trust/publish/fetch/vendor workflows:
  - `lang/tests/driver/test_drift_key_package_cli.py`
  - `lang/tests/driver/test_drift_publish_fetch_vendor.py`
  - `lang/tests/driver/test_drift_sign_cli.py`
  - `lang/tests/driver/test_drift_trust_cli.py`
  - `lang/tests/driver/test_drift_doctor.py`.
- Added local dist scaffold support in repo:
  - `dist/README.md`, `dist/release/.gitkeep`
  - just recipes: `dist-init`, `dist-index`, `dist-publish`, `dist-publish-stdlib`.
- Improved package index signature shape:
  - switched from negative flag (`unsigned`) to positive contract (`signed`) in index metadata.
- Added key listing UX:
  - `drift key list` with default marker + key id visibility.
- Added trust sidecar import UX:
  - `drift trust import ...` flow to import signer info from signature sidecars into trust store.

## 2026-02-16 – Runtime archive link mode + wrapper env handling
- Added runtime archive infrastructure and cache/build plumbing:
  - `lang/language_runtime/__init__.py`
  - `just runtime-libs` for explicit archive builds.
- Added driftc wrapper/runtime-link mode handling:
  - archive mode support in `lang/driftc/driftc.py` + `bin/driftc`.
  - explicit env handling for debug/sanitizer modes (including `DRIFT_ASAN=1`) in wrapper path.
- Added driver coverage for wrapper env behavior:
  - `lang/tests/driver/test_driftc_wrapper_env_modes.py`.
- Tooling docs updated:
  - `docs/toolchain-build-workflow.md`
  - `docs/design/drift-tooling-and-packages.md`.

## 2026-02-16 – Import diagnostic UX + task cleanup
- Improved import diagnostics for entry-module/module resolution edge case:
  - parser/driver updates for clearer module-not-found hint path.
  - coverage in `lang/tests/driver/test_import_module_not_found_hint.py`.
- Justfile cleanup:
  - renamed/streamlined recipes (including final cleanup of old deploy-oriented naming).

## 2026-02-20 – Concurrency queue-limit determinism + runtime race fix
- Fixed runtime queue-limit admission race in worker dequeue path:
  - `lang/language_runtime/posix/thread_runtime.c`
  - moved `running` accounting to the locked dequeue transition (and balanced early-cancel decrements), so `drift_exec_submit` queue-limit checks see consistent `queue_len + running`.
- Reworked queue-limit e2e to a deterministic direct-runtime submission shape:
  - `lang/tests/codegen/e2e/concurrent_queue_limit_enforced/main.drift`
  - validates second submit returns busy code under `queue_limit=1` without relying on wrapper-lifecycle timing.
- Validation:
  - `concurrent_queue_limit_enforced` (pass)
  - `DRIFT_ASAN=1 concurrent_queue_limit_enforced` (pass)
  - related concurrency checks: `concurrent_spawn_on_busy_timeout`, `concurrent_spawn_default_exec_busy`, `concurrent_default_executor_override` (pass).

## 2026-02-20 – Code-review residual risk closure (R1/R4/R5)
- Added mixed-payload multi-arm F1 regression:
  - `lang/tests/codegen/e2e/result_ok_mixed_payload_arms_drop_ordering`
  - covers `Result::Ok(Conn)` where nested variant has both `Copy(Int)` and `NonCopy(String)` arms; asserts lifetime/drop ordering across both arm paths.
  - validated in normal, `DRIFT_ASAN=1`, and `DRIFT_MEMCHECK=1` (pass).
- Added dedicated variant branch/drop stress regression:
  - `lang/tests/codegen/e2e/variant_multifield_drop_in_branch`
  - multi-field variant payload dropped from both `if`/`else` branch scopes in loop; normal + ASAN (pass).
- Completed optional LLVM verifier check for DV-drop helper path:
  - emitted IR from `diagnostic_value_object_nested_get` and verified with
    - `/usr/lib/llvm-20/bin/opt -passes=verify /tmp/dv_drop_verify.ll -disable-output` (pass).
