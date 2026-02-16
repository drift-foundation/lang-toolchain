# Compiler Hardening Work Progress

## Status

Phase 1 started.

Current baseline snapshot (2026-02-14):
- driver: `lang/tests/driver/test_noncopy_field_projection_from_borrow.py` -> pass
- e2e: `macro_log_app_logging_context`, `treemap_remove_cases`, `std_log_preamble_registry_stderr_default` -> pass

No new implementation beyond baseline/test-inventory kickoff in this track.

Phase 1 progress snapshot (2026-02-14):
- Added boundary regression:
  - `lang/tests/driver/test_mir_validate_boundary_diagnostics.py`
  - pins that MIR validator assertion failures are reported as deterministic diagnostics (phase=`mir_validate`) instead of propagating `AssertionError`.
- Added codegen boundary regression:
  - `lang/tests/driver/test_codegen_boundary_diagnostics.py`
  - pins that LLVM lowering assertion failures are reported as deterministic diagnostics (phase=`codegen`) instead of propagating `AssertionError`.
- Implemented checker->MIR boundary handling in `compile_stubbed_funcs`:
  - wrapped `mir_validate` pass in assertion-to-diagnostic conversion.
  - returns early with collected diagnostics when boundary contract fails.
- Implemented MIR->LLVM boundary handling:
  - `compile_to_llvm_ir_for_tests` catches lowering `AssertionError` and appends phase=`codegen` diagnostic.
  - CLI package-merge codegen path now emits deterministic `codegen` diagnostics on lowering contract failures.
- Validation run:
  - `lang/tests/driver/test_mir_validate_boundary_diagnostics.py` -> pass
  - `lang/tests/driver/test_codegen_boundary_diagnostics.py` -> pass
  - baseline subset (`test_noncopy_field_projection_from_borrow.py`, `macro_log_app_logging_context`, `treemap_remove_cases`, `std_log_preamble_registry_stderr_default`) -> pass

Phase 1 progress snapshot (2026-02-15):
- Added explicit MIR->LLVM pre-emission boundary validator in `lang/driftc/driftc.py`:
  - `_validate_codegen_contract(...)` checks required hand-off metadata before calling LLVM lowerer:
    - type table presence,
    - SSA map presence + per-function coverage,
    - `FnInfo/signature` presence for emitted MIR functions,
    - direct MIR call target resolvability in `fn_infos`.
- Wired validator into both LLVM entrypoints:
  - `compile_to_llvm_ir_for_tests(...)`,
  - CLI package-merge codegen path in `driftc.main`.
- Added regression:
  - `lang/tests/driver/test_codegen_preemit_boundary_diagnostics.py`
  - pins that pre-emit boundary failures are surfaced as deterministic `phase=codegen` diagnostics and do not proceed to LLVM lowering.
- Validation run:
  - `lang/tests/driver/test_codegen_preemit_boundary_diagnostics.py` -> pass
  - `lang/tests/driver/test_codegen_boundary_diagnostics.py` -> pass
  - `lang/tests/driver/test_mir_validate_boundary_diagnostics.py` -> pass

Phase 1 progress snapshot (2026-02-16):
- Added/activated call/constructor span hardening via existing regression file:
  - `lang/tests/driver/test_call_ctor_diagnostics_span.py`
  - cases pinned:
    - unknown struct ctor field diagnostics carry file/line/column
    - missing struct ctor field diagnostics carry file/line/column
    - non-constructor keyword-arg diagnostics carry file/line/column
    - duplicate struct ctor field diagnostics carry file/line/column
    - mixed positional+named struct ctor diagnostics carry file/line/column
    - unknown variant ctor field diagnostics carry file/line/column
    - missing variant ctor field diagnostics carry file/line/column
- Implemented shared best-effort span selection in `lang/driftc/checker/call_resolver.py`:
  - added `_best_effort_span(...)`
  - replaced `Span()`/weak loc fallbacks in:
    - unknown/duplicate struct-field keyword diagnostics
    - missing struct-field diagnostics
    - non-constructor keyword-arg diagnostics
    - unknown/duplicate/missing variant ctor keyword diagnostics
    - mixed positional+named variant ctor diagnostics
  - threaded callsite span into `resolve_unqualified_variant_ctor(...)` so missing-field diagnostics still report location when keyword tokens do not carry loc.
  - replaced fixed-width reserved-type diagnostic fallback `Span()` with best-effort type-expression span.
- Additional span hardening in `lang/driftc/type_checker.py`:
  - fixed-width reserved-type diagnostic in variant generic-lowering path now uses `Span.from_loc(expr.loc)` instead of bare `Span()`.
  - instantiation-path internal diagnostic `"missing callsite_id on instantiation call node"` now carries callsite expression span (`callsite_span`) instead of bare `Span()`.
  - boundary scan diagnostics (`missing callsite_id on call nodes`, `missing CallInfo for callsite_id`) now anchor to first offending call node loc when available.
  - visibility-provenance internal diagnostic now anchors to function-body span when available.
  - `signature missing declared_can_throw` diagnostic now anchors to signature loc when available.
  - missing entrypoint diagnostic now anchors to first available signature span (instead of bare `Span()`).
  - Result: no remaining explicit `span=Span()` callsites in `lang/driftc/type_checker.py` or `lang/driftc/checker/call_resolver.py`.
- Validation run:
  - `PYTHONPATH=. ./.venv/bin/python3 -m pytest -q lang/tests/driver/test_call_ctor_diagnostics_span.py` -> pass
  - `PYTHONPATH=. ./.venv/bin/python3 -m pytest -q lang/tests/driver/test_call_function_diagnostics_span.py` -> pass
  - `PYTHONPATH=. ./.venv/bin/python3 -m pytest -q lang/tests/driver/test_fixed_width_reserved.py lang/tests/driver/test_call_function_diagnostics_span.py lang/tests/driver/test_call_ctor_diagnostics_span.py` -> pass
  - `PYTHONPATH=. ./.venv/bin/python3 -m pytest -q lang/tests/driver/test_entrypoint_diagnostics_span.py` -> pass
  - Guard re-runs:
    - `lang/tests/driver/test_driftc_codegen_e2e.py::test_driftc_codegen_callback_arc_mutex_stub` -> pass
    - `lang/tests/borrow_checker/test_move_tracking.py` -> pass
    - `lang/tests/driver/test_borrow_read_diagnostics_span.py` -> pass
    - `lang/tests/driver/test_borrow_write_diagnostics_span.py` -> pass
    - `lang/tests/driver/test_autoborrow_diagnostics_span.py` -> pass
    - `lang/tests/stage1/test_ast_to_hir.py::test_macro_log_wrong_arity_rejected` -> pass
  - Additional pin:
    - `lang/tests/driver/test_fixed_width_reserved.py` now asserts `E_FIXED_WIDTH_RESERVED` carries `phase=typecheck` and non-None line/column.
    - `lang/tests/driver/test_call_function_diagnostics_span.py` added to pin non-None spans for plain function-call arity/type mismatch diagnostics.

This track is explicitly a compiler/runtime stabilization effort, not a stdlib feature effort.

---

## Why This Track Exists

Recent work repeatedly surfaced LANGUAGE_BUGs from ordinary stdlib usage:
- ownership/copy enforcement gaps (accepted by checker, broken by semantics)
- ref-return/escape-rule friction in practical patterns
- lowering/codegen mismatches after checker acceptance
- brittle behavior from large monolithic decision paths (`type_checker.py`, MIR lowering, LLVM emission)

The goal is to stop “fix one symptom, hit another” by enforcing clear phase contracts and invariant-driven testing.

---

## Scope (In)

1. Ownership and value-category correctness
- non-Copy extraction from borrowed base
- move/copy/read rules across field/index/projection chains
- by-value call argument legality and required move forms

2. Reference semantics correctness
- return-by-reference derivation and escape policy
- reference propagation through helper APIs and common wrappers
- compatibility between checker rules and real stdlib usage patterns

3. Checker ↔ MIR ↔ LLVM phase contract enforcement
- any checker-accepted program in supported subset must lower and codegen
- unsupported semantics must be rejected early in checker with deterministic diagnostics

4. Diagnostic quality for enforcement errors
- span attribution where currently `<source>:None`
- stable diagnostic categories for invariant violations

5. Regression hardening for known weak seams
- field projection and place classification
- method-call receiver ownership/autoborrow interactions
- match/try paths with ownership-sensitive payloads

---

## Scope (Out)

- New language features
- New stdlib APIs (except minimal test scaffolding)
- Broad refactors unrelated to invariant enforcement
- Performance tuning unless required to keep tests reliable

---

## Hard Freeze Policy During This Track

- No new feature surface in stdlib or language.
- Only bugfixes + enforcement + diagnostics + tests.
- If a new capability seems needed, pin it and defer.

---

## Guiding Principles

1. Regression-first always
- Add minimal failing test before fix.
- Confirm fail -> fix root cause -> confirm pass.

2. No semantic masking
- Do not patch stdlib/app code to avoid compiler defects.
- If workaround is requested, mark as temporary and keep bug open.

3. Phase-contract discipline
- Checker acceptance is a guarantee to MIR/codegen in supported subset.
- If MIR/LLVM cannot support a shape, checker must reject it explicitly.

4. Deterministic diagnostics
- Prefer explicit invariant error over downstream crashes/asserts.

5. Stage hand-off contracts are first-class deliverables
- Every phase boundary must have explicit accepted/rejected shape contracts.
- Handoff validation must run before entering the next phase.
- No “best effort” continuation when contract checks fail.

---

## Invariant Matrix (Source of Truth)

### A. Value Category and Ownership

A1. Non-Copy from borrowed base
- `&S` + `s.field` where field non-Copy and used as value: reject.

A2. Non-Copy from owned base
- owned local `s.field` by-value: allowed only via move semantics according to place rules.

A3. Copy from borrowed base
- `&S` + `s.field` where field Copy: allowed.

A4. Place projections
- field/index chains preserve correct value category and move requirements.

A5. By-value call arg rules
- non-Copy local by-value call requires legal move source and no borrowed overlap.

### B. Reference Semantics

B1. Ref return derivation
- returned reference must derive from permitted source according to rule.

B2. Escape diagnostics
- invalid ref-return should fail in checker with stable message + span.

B3. Helper indirection
- wrapper/helper patterns should not silently bypass or over-trigger escape rules.

### C. Phase Contracts

C1. Checker accepted => MIR lowerable (supported subset)
C2. MIR generated => LLVM emission succeeds (supported subset)
C3. Unsupported shape rejected in checker with deterministic diagnostics

### D. Diagnostics

D1. Ownership violations include clear action text (copy vs move).
D2. Diagnostics should carry file/line/column whenever possible.
D3. Remove `<source>:None` in these invariant paths.

---

## Work Plan

## Phase 1: Stage Hand-Off Contract Isolation (Top Priority)

Objectives
- Make checker->MIR and MIR->LLVM boundaries explicit, validated, and enforceable.
- Remove implicit assumptions currently scattered across downstream code.

Tasks
- Define contract schemas for:
  - checker output consumed by MIR lowering,
  - MIR output consumed by LLVM emission.
- Add boundary validators that run at phase entry:
  - reject unsupported/ambiguous shapes with deterministic diagnostics.
- Move acceptance policy upstream:
  - if MIR/LLVM cannot support a shape, checker must reject it by policy.
- Add negative tests that intentionally violate boundary contracts and verify clean rejection at the correct phase.
- Add positive “contract conformance” tests for representative real programs (including stdlib-heavy patterns).

Deliverables
- Documented boundary contract checklist in this file.
- Validator hooks enabled by default in test builds.
- Regression suite proving contract enforcement points.

Gate
- No checker-accepted program in hardening subset fails later due to unsupported shape mismatch.
- Boundary failures are diagnosable at the hand-off phase, not via downstream assertions/crashes.

Phase 1 contract checklist (draft)
- Checker -> MIR contract
  - Typed HIR only (`HTypeApp`, unresolved qualified members, kwargs-survivors rejected in checker).
  - All callsites have `CallInfo`; direct/iface targets are concrete and arity-compatible.
  - Ownership/place normalization complete for move/borrow-sensitive forms before MIR.
  - Expected outcome on violation: checker/typecheck diagnostic, not MIR assertion.
- MIR construction -> MIR validation contract
  - MIR `Call` instructions must carry `fn_id`, `can_throw`, and signature-compatible args.
  - Container/intrinsic invariants enforced by MIR validators (array alloc/copy, iface init, byvalue move rules).
  - Expected outcome on violation: deterministic `mir_validate` diagnostic (phase hand-off stop).
- MIR -> LLVM contract
  - Only lowered ops with LLVM mapping reach emitter.
  - Local/type metadata required by emission (including drop/destructible paths) must be present.
  - Debug-info attachments must be valid for emitted call/invoke instructions when debug is enabled.
  - Expected outcome on violation: deterministic pre-emission diagnostic (target for next Phase 1 step).

## Phase 0: Baseline and Test Inventory

Objectives
- Catalog current failures/flakes tied to ownership/ref semantics.
- Build a focused test list for fast iteration.

Tasks
- Create compiler-hardening subset list (driver + e2e + targeted codegen unit).
- Classify each test by invariant bucket (A/B/C/D).
- Record known intermittent cases separately from deterministic failures.

Deliverables
- Test manifest in this file (or adjacent note).
- Stable “quick-hardening” command set.

Baseline test manifest (initial)
- Driver
  - `lang/tests/driver/test_noncopy_field_projection_from_borrow.py` (A1/A4)
- E2E
  - `macro_log_app_logging_context` (B3/C1/C2)
  - `treemap_remove_cases` (C2/Phase-8 compile-cost sentinel)
  - `std_log_preamble_registry_stderr_default` (C1/C2 integration sanity)

Quick commands (current)
- `PYTHONPATH=. ./.venv/bin/python3 -m pytest -q lang/tests/driver/test_noncopy_field_projection_from_borrow.py`
- `PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py -j 1 macro_log_app_logging_context treemap_remove_cases std_log_preamble_registry_stderr_default`

Gate
- We can run all hardening subset tests repeatedly without harness ambiguity.

Phase 0 current outcome
- Gate met for initial subset above.
- Next immediate step: Phase 1 contract checklist draft + first boundary validator target selection (checker->MIR).

---

## Phase 2: Ownership Enforcement Correctness (Field/Projection First)

Objectives
- Centralize and stabilize non-Copy extraction checks for projection paths.

Tasks
- Audit type-checker field/projection paths for duplicate early-return logic.
- Ensure borrowed-subject + non-Copy by-value is consistently rejected.
- Ensure owned-subject paths still allow legal move semantics.
- Add paired positive/negative tests for nested projection chains.

Tests (minimum)
- Driver: non-Copy field from borrowed base rejected (new pinned regression).
- Driver/e2e: Copy field from borrowed base allowed.
- Driver/e2e: owned base extraction semantics remain correct.
- Existing regressions touching `HField`/place canonicalization.

Gate
- No `<source>:None` on these failures.
- All field/projection ownership tests pass in normal, ASAN, alloc-track modes.

Phase 2 kickoff snapshot (2026-02-14)

Phase 2 progress snapshot (2026-02-15):
- Centralized projected by-value non-Copy rejection in borrow checker:
  - `BorrowChecker._reject_noncopy_projected_byvalue_arg(...)`
  - removed duplicated inline logic across free-call/method-call arg paths.
- Hardened span attribution for projected by-value diagnostics:
  - borrow checker now uses best-effort expression span extraction with callsite fallback.
  - borrow checker now tracks current statement span and uses it as a fallback for synthesized expression nodes without `loc`, eliminating `<source>:None` in pinned projected-byvalue diagnostics.
- Fixed nested borrowed projection ownership hole in type checker:
  - nested chains like `w.p.xs` (where `w: &Wrapper`) now preserve borrowed-origin semantics and correctly require `Copy` for by-value reads.
  - implemented via `_expr_reads_through_ref_projection(...)` in field typing path.
- Added/expanded regressions:
  - `lang/tests/driver/test_noncopy_field_projection_from_borrow.py`
    - `test_nested_noncopy_projection_from_borrow_reports_span`
    - `test_owned_nested_noncopy_projection_byvalue_call_rejected_with_span`
    - `test_owned_nested_noncopy_projection_replace_extract_is_allowed`
    - `test_borrowed_index_noncopy_projection_reports_span`
    - `test_move_non_place_operand_reports_span`
    - `test_borrow_conflict_in_same_statement_reports_span`
    - `test_move_from_reference_type_reports_span`
    - `test_copy_non_place_operand_reports_span`
    - `test_borrow_mut_immutable_binding_reports_span`
    - `test_borrow_mut_through_shared_ref_reports_span`
  - `lang/tests/driver/test_autoborrow_diagnostics_span.py`
    - `test_autoborrow_mut_param_rvalue_reports_span`
    - `test_autoborrow_mut_receiver_rvalue_reports_span`
  - `lang/tests/driver/test_borrow_write_diagnostics_span.py`
    - `test_assign_while_borrowed_reports_span`
    - `test_augassign_while_borrowed_reports_span`
  - `lang/tests/driver/test_borrow_read_diagnostics_span.py`
    - `test_use_after_move_reports_span`
    - `test_use_of_uninitialized_reports_span` (pinned to a stable use-after-move shape)
    - `test_cannot_read_while_mutably_borrowed_reports_span` (current diagnostic text for this path)
    - `test_cannot_move_while_borrowed_reports_span`
    - `test_cannot_borrow_from_moved_reports_span`
    - `test_cannot_read_while_mutably_borrowed_without_reborrow_reports_span`
- Extended typecheck span fallback from field-only to projection/index paths:
  - `HPlaceExpr` and `HIndex` non-Copy by-value diagnostics now use best-effort expression spans.
- Extended typecheck span fallback to explicit move/copy diagnostics:
  - move/copy operand and move/copy type errors now use subject-first best-effort spans.
- Extended typecheck span fallback in borrow diagnostics:
  - conflict/non-place/&mut-through-deref borrow errors now use best-effort subject spans in the borrow branch.
- Extended borrow-checker assignment diagnostics span fallback:
  - `HAssign` write-while-borrowed and non-lvalue target diagnostics now fall back to current statement span when target loc is missing.
- Extended borrow-checker read/loan diagnostics span fallback:
  - `_consume_place_use`, `_force_move_place_use`, `_force_move_place_use_implicit`, and `_borrow_place` now fall back to current statement span when expression spans are missing.
- Validation run:
  - `lang/tests/driver/test_noncopy_field_projection_from_borrow.py` -> pass
  - `lang/tests/driver/test_driftc_codegen_e2e.py::test_driftc_codegen_callback_arc_mutex_stub` -> pass
  - `lang/tests/borrow_checker/test_move_tracking.py::{test_noncopy_subplace_move_via_by_value_call,test_noncopy_index_move_via_by_value_call}` -> pass
- Expanded projection ownership matrix in:
  - `lang/tests/driver/test_noncopy_field_projection_from_borrow.py`
- Added pinned cases:
  - borrowed non-Copy nested projection rejected (`cannot copy value of type ...`)
  - borrowed Copy nested projection allowed
  - owned non-Copy extraction via legal path (`std.mem.replace`) allowed
- Current status:
  - `PYTHONPATH=. ./.venv/bin/python3 -m pytest -q lang/tests/driver/test_noncopy_field_projection_from_borrow.py` -> pass (3 tests)

Phase 2 checker-audit progress (2026-02-14)
- Consolidated duplicated `HField` projection handling in `lang/driftc/type_checker.py`:
  - unified struct-field fallback path to reuse already-computed subject type/ref state (`inner_ty`, `inner_def`, `subject_is_ref`) instead of recomputing via `sub_ty`/`sub_def`.
  - removed duplicated early-return branch drift in this path.
- Fixed visibility-check enforcement hole in early struct-field fast path:
  - now returns Unknown when `_ensure_field_visible(...)` fails before returning field type.
- Targeted validation:
  - `lang/tests/driver/test_noncopy_field_projection_from_borrow.py` -> pass
  - `lang/tests/driver/test_member_visibility.py` -> pass
  - `lang/tests/driver/test_driftc_codegen_e2e.py::test_driftc_codegen_scalar_main` -> pass
  - e2e sanity subset (`macro_log_app_logging_context`, `treemap_iter_order`, `std_log_preamble_registry_stderr_default`) -> pass
- Added visibility regression pin:
  - `test_private_field_access_through_borrow_is_error` in `lang/tests/driver/test_member_visibility.py`
  - confirms private field access is rejected even when field projection is through `&T`.

Phase 2 index-path consolidation (2026-02-14)
- Added regression coverage:
  - `lang/tests/driver/test_index_diagnostics.py`
    - `test_array_index_requires_int`
    - `test_indexing_requires_array_value`
- Consolidated index enforcement in `lang/driftc/type_checker.py`:
  - introduced shared helpers:
    - `_require_int_index_type(...)`
    - `_array_element_type(...)`
  - reused helpers across both direct `HIndex` and projected `HPlaceIndex` paths.
- Validation:
  - `test_index_diagnostics.py` -> pass
  - `test_noncopy_field_projection_from_borrow.py` + `test_member_visibility.py` -> pass
  - e2e sanity subset (`macro_log_app_logging_context`, `treemap_iter_order`, `std_log_preamble_registry_stderr_default`) -> pass

Phase 2 deref-path consolidation (2026-02-14)
- Added regression coverage:
  - `lang/tests/driver/test_deref_diagnostics.py`
    - `test_deref_requires_reference_value`
    - `test_deref_of_noncopy_from_ref_requires_copy`
- Consolidated deref validation in `lang/driftc/type_checker.py`:
  - introduced shared helper `_deref_inner_type(...)`.
  - reused in both:
    - `HUnary(UnaryOp.DEREF)`
    - `HPlaceExpr` with `HPlaceDeref` projections.
- Validation:
  - `test_deref_diagnostics.py` -> pass
  - `test_index_diagnostics.py` + `test_noncopy_field_projection_from_borrow.py` + `test_member_visibility.py` -> pass
  - e2e sanity subset (`macro_log_app_logging_context`, `treemap_iter_order`, `std_log_preamble_registry_stderr_default`) -> pass

Phase 2 LANGUAGE_BUG fix: index on `&Array<T>` across checker->MIR (2026-02-14)
- Regression added:
  - `lang/tests/driver/test_index_ref_subject.py::test_index_on_ref_array_subject_is_allowed`
- Initial failure pinned two stage-boundary defects:
  1. checker local inference (`checker/__init__.py`) treated `&Array<T>` as non-array in `HIndex`.
  2. Stage2 lowering (`stage2/hir_to_mir.py::_infer_array_elem_type`) did not unwrap refs, causing:
     - `AssertionError: MIR lowering invariant violated: unresolved Copy status for array index read`.
- Root-cause fixes:
  - `lang/driftc/checker/__init__.py`: unwrap `Ref` before array-kind check in `HIndex` inference.
  - `lang/driftc/stage2/hir_to_mir.py`: unwrap `Ref` in both fast and general paths of `_infer_array_elem_type`.
- Validation:
  - `test_index_ref_subject.py` -> pass
  - projection/index/deref/member visibility driver subset -> pass
  - e2e sanity subset (`macro_log_app_logging_context`, `treemap_iter_order`, `std_log_preamble_registry_stderr_default`) -> pass

Phase 2 checker parity cleanup: captures/attrs index inference dedup (2026-02-14)
- Refactored `_TypingContext` in `lang/driftc/checker/__init__.py`:
  - added `_unwrap_ref_typeid(...)` and `_is_error_subject(...)` helpers.
  - replaced repeated Error-subject ref-unwrapping blocks in `HIndex` (`captures`/`attrs`) inference path with shared helper usage.
- Intent: reduce branch drift between checker local inference and `type_checker.py` without changing diagnostics.
- Validation:
  - driver subset (`test_index_ref_subject.py`, `test_index_diagnostics.py`, `test_deref_diagnostics.py`) -> pass
  - e2e exception attrs/captures subset (`exception_attrs`, `exception_capture_missing_frame`, `exception_result_error_param_by_ref`) -> pass
  - e2e sanity subset (`macro_log_app_logging_context`, `treemap_iter_order`, `std_log_preamble_registry_stderr_default`) -> pass

Phase 6 diagnostic wording normalization: index message parity (2026-02-15)
- Normalized checker-local `HIndex` diagnostic text in `lang/driftc/checker/__init__.py`:
  - from `array index must be Int`
  - to `array index must be an Int`
- Updated checker tests that pin this wording:
  - `lang/tests/checker/test_array_type_checks.py`
  - `lang/tests/checker/test_array_string_negatives.py`
- Validation:
  - `PYTHONPATH=. ./.venv/bin/pytest -q lang/tests/checker/test_array_type_checks.py lang/tests/checker/test_array_string_negatives.py` -> pass
  - `PYTHONPATH=. ./.venv/bin/pytest -q lang/tests/driver/test_index_diagnostics.py` -> pass

---

## Phase 3: Ref-Return/Escape Rule Rationalization

Objectives
- Make ref-return behavior predictable and usable for real patterns.

Tasks
- Pin exact MVP escape-rule policy in docs/spec note.
- Ensure checker implementation matches pinned policy exactly.
- Add tests for helper/wrapper patterns (registry-derived refs, parameter-derived refs, invalid locals).
- Explicitly track logger helper ergonomics case:
  - desired shape: helper APIs like `app.logging.security(reg: &GlobalRegistry) -> &Logger`.
  - current status: blocked by MVP escape-rule enforcement shape; e2e uses inline registry fetch + `&state.security` workaround form.
  - hardening target: either allow this derivation form safely, or reject it with intentional, stable diagnostics and documented rule rationale.
- If policy is intentionally strict, ensure diagnostics guide users to valid pattern.

Tests (minimum)
- Valid ref return from parameter-derived chains.
- Invalid ref return from temporaries/locals.
- Registry/wrapper usage cases pinned to policy (accepted or rejected by design).
- Include `macro_log_app_logging_context` helper-return variant as a tracked policy test (or explicit rejection test if intentionally unsupported).

Gate
- No surprise accept/reject drift across equivalent forms.
- Diagnostics deterministic and actionable.

Phase 3 policy-pinning progress (2026-02-15)
- Added e2e acceptance case for registry-derived reference through Optional match:
  - `lang/tests/codegen/e2e/ref_return_registry_match_ok`
  - pins supported shape: helper returns `Optional<&T>` from `rt.get<type T>(...)` + `match Some(...)`.
- Added e2e rejection case for logger helper returning bare `&Logger` from registry fetch:
  - `lang/tests/codegen/e2e/return_ref_registry_logger_helper_rejected`
  - pins current MVP escape-rule rejection with deterministic typecheck diagnostic:
    - `reference return must be derived from a reference parameter`.
- Validation:
  - `macro_log_app_logging_context` + both new e2e cases -> pass.

Phase 3 diagnostic-hardening progress (2026-02-16)
- Added driver span regressions:
  - `lang/tests/driver/test_ref_return_diagnostics_span.py`
  - pins non-None file/line/column for:
    - `reference return must be derived from a reference parameter`
    - `mutable reference return must derive from an &mut parameter`
- Validation:
  - `PYTHONPATH=. ./.venv/bin/python3 -m pytest -q lang/tests/driver/test_ref_return_diagnostics_span.py` -> pass
  - e2e guards:
    - `return_ref_local_rejected` -> pass
    - `return_ref_registry_logger_helper_rejected` -> pass
    - `return_self_requires_mut_receiver_rejected` -> pass

Phase 1/6 boundary+span hardening additions (2026-02-16)
- Added `lang/tests/driver/test_entrypoint_diagnostics_span.py`:
  - `missing entry point` diagnostic reports non-None line/column.
  - `duplicate entry point definition` diagnostic reports non-None line/column.
- Validation:
  - `PYTHONPATH=. ./.venv/bin/python3 -m pytest -q lang/tests/driver/test_entrypoint_diagnostics_span.py` -> pass
  - `lang/tests/driver/test_no_blank_span_fallbacks.py` added as a guard to prevent reintroduction of bare `span=Span()` in:
    - `lang/driftc/type_checker.py`
    - `lang/driftc/checker/call_resolver.py`

---

## Phase 4: Checker→MIR Contract Guards

Objectives
- Fail early in checker instead of MIR assertion gaps.

Tasks
- Add contract assertions/tests for shapes known to cause MIR invariant failures.
- Normalize by-value arg legality checks in one checker seam.
- Remove duplicated ad hoc checks in lowering where checker can own policy.

Tests (minimum)
- Existing MIR invariant regressions for non-Copy by-value call args.
- Callback/lambda capture by-value ownership tests.
- Match/try ownership-sensitive payload paths.

Gate
- No MIR invariant violation for checker-accepted programs in targeted subset.

Phase 4 progress: interface by-value non-Copy arg contract (2026-02-15)
- Added regression coverage:
  - Driver: `lang/tests/driver/test_implicit_move_var_requirement.py`
    - non-Copy by-value direct call from `val` allowed
    - non-Copy by-value indirect call from `val` allowed
    - non-Copy by-value interface call from `val` allowed
  - E2E: `lang/tests/codegen/e2e/interface_call_byvalue_noncopy_from_val`
- Root-cause fix:
  - `lang/driftc/stage2/hir_to_mir.py`
  - `_lower_iface_call(...)` now lowers args via `_lower_call_arg(...)` with param types, matching direct/indirect call lowering.
  - This closes a MIR contract hole where checker-accepted interface calls could hit:
    - `internal: MIR validation contract failure (MIR invariant violation: by-value arg must MoveOut non-Copy local ...)`.
- Validation:
  - `pytest -q lang/tests/driver/test_implicit_move_var_requirement.py` -> pass
  - e2e subset: `interface_call_nothrow`, `interface_call_throw`, `interface_call_byvalue_noncopy_from_val` -> pass

Phase 4 follow-up: invoke/projection + interface-return lowering gaps (2026-02-15)
- Added e2e invoke cluster:
  - `lang/tests/codegen/e2e/invoke_byvalue_noncopy_local`
  - `lang/tests/codegen/e2e/invoke_byvalue_noncopy_projection`
  - `lang/tests/codegen/e2e/invoke_byvalue_noncopy_callback_return`
- LANGUAGE_BUG #1 (codegen):
  - non-throw return of interface values (`%DriftIface`) failed LLVM lowering allowlist.
  - Fix: `lang/codegen/llvm/llvm_codegen.py` `_lower_term(Return)` now accepts `DRIFT_IFACE_TYPE`.
- LANGUAGE_BUG #2 (ownership/runtime safety):
  - implicit non-Copy move from projected places (e.g., `f(w.p)`) reached runtime and could double-free.
  - Fix: `lang/driftc/borrow_checker_pass.py` `_force_move_place_use_implicit(...)` now rejects projected implicit moves with deterministic diagnostic:
    - `move of a projected place is not supported in MVP; move a local/param or use swap/replace`
- Validation:
  - e2e subset:
    - `invoke_byvalue_noncopy_local` -> pass
    - `invoke_byvalue_noncopy_projection` -> pass (expected borrowcheck diagnostic)
    - `invoke_byvalue_noncopy_callback_return` -> pass
    - `interface_call_byvalue_noncopy_from_val` -> pass
  - driver: `test_implicit_move_var_requirement.py` -> pass

Phase 4 LANGUAGE_BUG fix: interface method-call by-value projection enforcement gap (2026-02-16)
- Regression-first:
  - Added e2e cases:
    - `lang/tests/codegen/e2e/method_call_byvalue_noncopy_from_val` (accept)
    - `lang/tests/codegen/e2e/method_call_byvalue_noncopy_projection` (reject)
    - `lang/tests/codegen/e2e/interface_call_byvalue_noncopy_projection` (reject)
  - Confirmed failure before fix:
    - `interface_call_byvalue_noncopy_projection` was accepted (`exit 0`) instead of borrowcheck rejection.
- Root cause:
  - `borrow_checker_pass.py` method-call arg checking relied on `MethodResolution` metadata; interface-value dispatch can have no `MethodResolution`, so non-Copy projected by-value args escaped `_reject_noncopy_projected_byvalue_arg(...)`.
- Fix:
  - In `lang/driftc/borrow_checker_pass.py` (`HMethodCall` branch), added narrow fallback to callsite `CallInfo` only for interface-value indirect dispatch (`CallTargetKind.INDIRECT` + interface receiver), with arg indexing modeled as args-only (no receiver slot).
- Refactored/centralized call-arg handling to reduce branch drift:
    - `_call_info_for_expr(...)`
    - `_visit_call_arg_with_param(...)`
    - `_method_call_param_layout(...)`
  - Both `HCall` and `HMethodCall` now use the same arg-processing helper for:
    - ref-arg temporary borrows
    - projected non-Copy by-value rejection
    - by-value consume behavior
  - Kept non-interface method paths on resolution-based receiver modeling to avoid behavior drift.
- Validation:
  - `PYTHONPATH=. ./.venv/bin/python3 -m pytest -q lang/tests/driver/test_implicit_move_var_requirement.py lang/tests/driver/test_noncopy_field_projection_from_borrow.py` -> pass
  - `PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py -j 1 method_call_byvalue_noncopy_from_val method_call_byvalue_noncopy_projection interface_call_byvalue_noncopy_projection interface_call_byvalue_noncopy_from_val invoke_byvalue_noncopy_projection` -> pass
  - Added e2e pin for current MVP method-kwarg policy:
    - `lang/tests/codegen/e2e/interface_call_byvalue_noncopy_projection_kw` -> typecheck rejection (`keyword arguments are not supported for method calls in MVP`) -> pass
  - Added driver span regression for this policy:
    - `lang/tests/driver/test_call_function_diagnostics_span.py::test_method_keyword_args_rejected_reports_span`
  - Hardened diagnostic span in `lang/driftc/checker/call_resolver.py` for:
    - `keyword arguments are not supported for method calls in MVP` (now `_best_effort_span(first_kw, expr)`).

Phase 4/contract-hardening progress: call-metadata param-layout enforcement (2026-02-16)
- Added checker-side CallInfo contract guard in `lang/driftc/checker/__init__.py` (`_validate_calls`):
  - validates effective call argument count against `CallInfo.sig.param_types` using target-kind aware rules:
    - `HCall`: args (+kwargs)
    - `HMethodCall`: `receiver+args` for non-INDIRECT, `args` for INDIRECT (+kwargs)
    - `HInvoke`: `callee+args` when `includes_callee=true`, else `args` (+kwargs)
  - emits deterministic checker diagnostics on mismatch:
    - `internal: CallInfo param layout mismatch for <call-kind> (checker bug)`
  - rejects impossible metadata shape:
    - `internal: CallInfo includes_callee set on call/method call (checker bug)`
  - walks child expressions and returns early after contract error to avoid cascading mismatched call-signature diagnostics.
- Added regression file:
  - `lang/tests/driver/test_callinfo_param_layout_contract.py`
  - pins:
    - malformed INDIRECT method-call param layout is rejected deterministically.
    - illegal `includes_callee` on `HCall` is rejected deterministically.
    - valid direct method-call layout does not emit contract diagnostics.
- Validation:
  - `PYTHONPATH=. ./.venv/bin/python3 -m pytest -q lang/tests/driver/test_callinfo_param_layout_contract.py` -> pass
  - `PYTHONPATH=. ./.venv/bin/python3 -m pytest -q lang/tests/driver/test_interface_ufcs_callinfo_hole.py lang/tests/driver/test_callinfo_try_block.py` -> pass

Phase 5 bridge progress: invoke/constructor call-metadata coverage + conformance e2e (2026-02-16)
- Extended driver regression coverage in:
  - `lang/tests/driver/test_callinfo_param_layout_contract.py`
- New pinned cases:
  - `HInvoke` mismatch when `includes_callee=true` but `CallInfo.sig.param_types` omits callee slot -> deterministic checker contract diagnostic.
  - constructor-target `HCall` mismatch (`CallTargetKind.CONSTRUCTOR`) -> deterministic checker contract diagnostic with `target_kind=CONSTRUCTOR` note.
- Refactor/centralization in checker:
  - moved arg-layout contract logic from local closures to class helpers:
    - `_expected_call_param_count(...)`
    - `_validate_callinfo_param_layout(...)`
  - `_validate_calls(...)` now uses the shared helper, reducing future drift across call-kind handling.
- Added e2e conformance bundle:
  - `lang/tests/codegen/e2e/call_contract_conformance_mix`
  - validates one program exercising:
    - direct call,
    - method call,
    - interface method dispatch,
    - invoke through function value.
- Validation:
  - `PYTHONPATH=. ./.venv/bin/python3 -m pytest -q lang/tests/driver/test_callinfo_param_layout_contract.py` -> pass
  - `PYTHONPATH=. ./.venv/bin/python3 -m pytest -q lang/tests/driver/test_callinfo_try_block.py lang/tests/driver/test_interface_ufcs_callinfo_hole.py lang/tests/driver/test_mir_validate_boundary_diagnostics.py lang/tests/driver/test_codegen_preemit_boundary_diagnostics.py` -> pass
  - `PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py -j 1 call_contract_conformance_mix` -> pass

Phase 5 bridge progress: callback/intrinsic seam target-shape checks (2026-02-16)
- Added checker-side target-shape contract helper in `lang/driftc/checker/__init__.py`:
  - `_validate_callinfo_target_shape(...)`
- New enforced invariant:
  - `HInvoke` callsites must carry `CallInfo.target.kind == INDIRECT`; otherwise deterministic checker diagnostic:
    - `internal: invoke CallInfo target must be INDIRECT (checker bug)`
- Additional protected invariant:
  - `HMethodCall` must never carry constructor target; deterministic checker diagnostic:
    - `internal: method call CallInfo target must not be CONSTRUCTOR (checker bug)`
- Clarified current accepted shape:
  - `HCall` with `INDIRECT` target is currently valid in typed pipeline (function-value call representation still appears as `HCall` in some paths), so no rejection is applied for this shape.
- Regression coverage extended in:
  - `lang/tests/driver/test_callinfo_param_layout_contract.py`
  - added cases:
    - malformed `HInvoke` direct-target rejection
    - valid `HCall` indirect-target acceptance
    - intrinsic callback target acceptance on `HCall`
- Validation:
  - `PYTHONPATH=. ./.venv/bin/python3 -m pytest -q lang/tests/driver/test_callinfo_param_layout_contract.py` -> pass
  - `PYTHONPATH=. ./.venv/bin/python3 -m pytest -q lang/tests/driver/test_callback_dynamic_dispatch.py::test_callback_dynamic_dispatch lang/tests/driver/test_callback_dynamic_dispatch.py::test_callback_call_is_nothrow lang/tests/driver/test_std_mem_swap_replace.py::test_std_mem_swap_is_intrinsic` -> pass
  - `PYTHONPATH=. ./.venv/bin/python3 -m pytest -q lang/tests/driver/test_interface_ufcs_callinfo_hole.py lang/tests/driver/test_callinfo_try_block.py` -> pass
  - `PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py -j 1 call_contract_conformance_mix` -> pass

Phase 5 boundary pinning: stage2 call-target shape assertions (2026-02-16)
- Added explicit HIR->MIR call-metadata contract assertions in `lang/driftc/stage2/hir_to_mir.py`:
  - `_call_info_for_invoke(...)` now asserts:
    - `CallTargetKind.INDIRECT` is required for `HInvoke`.
    - `CallSig.includes_callee` must be false for `HInvoke`.
  - `_call_info_for_method(...)` now asserts:
    - `HMethodCall` must not carry constructor `CallTarget`.
- Added regression-first stage2 coverage in `lang/tests/stage2/test_callinfo_cutover.py`:
  - `test_invoke_rejects_non_indirect_call_target`
  - `test_invoke_rejects_includes_callee_flag`
  - `test_method_call_rejects_constructor_call_target`
- Validation:
  - `PYTHONPATH=. ./.venv/bin/python3 -m pytest -q lang/tests/stage2/test_callinfo_cutover.py` -> pass
  - `PYTHONPATH=. ./.venv/bin/python3 -m pytest -q lang/tests/stage2/test_hir_to_mir.py lang/tests/stage2/test_fnptr_const_lowering.py` -> pass
  - `PYTHONPATH=. ./.venv/bin/python3 -m pytest -q lang/tests/driver/test_callinfo_param_layout_contract.py` -> pass

Phase 5 boundary pinning: compile-path normalization of stage2 assertion failures (2026-02-16)
- LANGUAGE_BUG pinned with new driver regression:
  - `lang/tests/driver/test_mir_validate_boundary_diagnostics.py::test_mir_lowering_contract_failure_is_diagnostic_not_assert`
  - Test monkeypatches `TypeChecker.check_function` to corrupt a `main` callsite `CallInfo` into constructor target and asserts compile path reports deterministic diagnostic instead of propagating raw `AssertionError`.
- Root-cause fix in `lang/driftc/driftc.py`:
  - Wrapped stage2 lowering calls in `compile_stubbed_funcs` with assertion-to-diagnostic conversion:
    - primary function lowering (`lower.lower_function_body(...)`)
    - hidden-lambda lowering (`_lower_lambda_block(...)`)
    - captureless-lambda lowering (`_lower_lambda_block(...)`)
  - On assertion, compiler now appends:
    - `internal: MIR lowering contract failure (...)`
    - `phase="mir_validate"`
  - Then exits compile path deterministically (same boundary behavior as MIR validator failures).
- Validation:
  - `PYTHONPATH=. ./.venv/bin/python3 -m pytest -q lang/tests/driver/test_mir_validate_boundary_diagnostics.py::test_mir_lowering_contract_failure_is_diagnostic_not_assert` -> pass
  - `PYTHONPATH=. ./.venv/bin/python3 -m pytest -q lang/tests/driver/test_mir_validate_boundary_diagnostics.py lang/tests/stage2/test_callinfo_cutover.py` -> pass
  - `PYTHONPATH=. ./.venv/bin/python3 -m pytest -q lang/tests/driver/test_codegen_boundary_diagnostics.py lang/tests/driver/test_codegen_preemit_boundary_diagnostics.py lang/tests/driver/test_callinfo_param_layout_contract.py` -> pass

Phase 5 boundary pinning: compile_to_llvm_ir path regression for stage2 assertion normalization (2026-02-16)
- Added driver regression:
  - `lang/tests/driver/test_codegen_boundary_diagnostics.py::test_codegen_pipeline_surfaces_mir_lowering_contract_failure_as_diagnostic`
  - Test monkeypatches `TypeChecker.check_function` to corrupt `main` callsite `CallInfo` and verifies `compile_to_llvm_ir_for_tests(...)`:
    - returns empty IR,
    - surfaces deterministic diagnostic with `phase=mir_validate`,
    - includes `MIR lowering contract failure` message.
- Purpose:
  - pins that compile-to-LLVM helper does not leak stage2 `AssertionError` and preserves boundary-diagnostic contract through the codegen-facing API.
- Validation:
  - `PYTHONPATH=. ./.venv/bin/python3 -m pytest -q lang/tests/driver/test_codegen_boundary_diagnostics.py` -> pass
  - `PYTHONPATH=. ./.venv/bin/python3 -m pytest -q lang/tests/driver/test_mir_validate_boundary_diagnostics.py lang/tests/driver/test_codegen_preemit_boundary_diagnostics.py` -> pass
  - `PYTHONPATH=. ./.venv/bin/python3 -m pytest -q lang/tests/stage2/test_callinfo_cutover.py lang/tests/driver/test_callinfo_param_layout_contract.py` -> pass

---

## Phase 5: MIR→LLVM Contract Guards

Objectives
- Ensure LLVM emitter sees only canonical, supported shapes.

Tasks
- Add/strengthen pre-emission canonical checks where needed.
- Convert current crash-prone paths to explicit, tagged diagnostics where checker contract cannot yet guarantee shape.
- Add unit/e2e coverage for historically fragile lowering/canonicalization interactions.

Tests (minimum)
- Existing llvm_codegen e2e regressions related to ownership/projection/callback paths.
- Repeat-run flaky-hunter subset on critical tests.

Gate
- No clang/LLVM crashes from accepted hardening-subset programs.

---

## Phase 6: Diagnostic Hygiene

Objectives
- Remove anonymous/unattributed diagnostics in touched paths.

Tasks
- Thread span data through ownership/ref-return diagnostics.
- Standardize message wording for invariant classes.
- Add driver checks for key message fragments + phase attribution.

Gate
- No `<source>:None` for hardening-subset ownership/ref diagnostics.

---

## Phase 7: Stability Qualification

Objectives
- Prove hardening improvements are durable under stress/sanitizers.

Tasks
- Run hardening subset:
  - normal mode
  - `DRIFT_ASAN=1`
  - `DRIFT_ALLOC_TRACK=1`
  - combined mode where supported
- Repeat-run flaky subset N times (recommend 20+) on hottest cases.

Gate
- Stable pass rate in repeat runs.
- No new memory leaks from hardening changes.

---

## Phase 8: Compile-Path Efficiency and IR Bloat Reduction (Later Step)

Objectives
- Reduce avoidable compile/link wall-time without changing language semantics.
- Identify and trim IR/codegen expansion hotspots in generic/container-heavy paths.

Tasks
- Add compile-cost profiling for selected heavy e2e cases (IR size, IR line count, clang wall-time).
- Track “budget” regressions for heavy cases (e.g., `treemap_remove_cases`) separately from functional regressions.
- Audit redundant monomorphization/emission patterns that inflate IR.
- Reduce duplicate helper emission and repeated glue where safe.
- Keep runtime behavior/tests unchanged; this phase is efficiency-only.

Validation
- Heavy-case compile wall-time trends improve or remain bounded.
- No semantic regressions in affected cases.
- ASAN/alloc-track subsets remain clean after efficiency changes.

Notes
- This is intentionally sequenced after correctness hardening phases.
- Timeout overrides (e.g., for very large IR cases) are operational guardrails, not the final optimization strategy.

---

## Test Strategy

1. Driver tests
- Pin checker policy and diagnostics precisely.

2. E2E tests
- Validate end-to-end behavior with real lowering/codegen/runtime.

3. LLVM/codegen unit tests
- Protect canonical lowering assumptions.

4. Sanitizer/memory modes
- Catch ownership/lifetime regressions not visible in normal runs.

---

## Proposed Initial Test Buckets

Bucket A: Ownership/Copy from Borrowed Paths
- new: `test_noncopy_field_projection_from_borrow.py`
- existing move/borrow regressions (borrow-after-move, move-while-borrowed, copy-noncopy)

Bucket B: Macro/Wrapper Ref Usage
- `macro_log_app_logging_context`
- related macro logger diagnostics

Bucket C: MIR Invariant Sensitivity
- callback move-capture regressions
- by-value non-Copy call arg regressions

Bucket D: LLVM Fragility
- targeted llvm_codegen_e2e ownership paths

---

## Risks

1. Hidden coupling in monolithic checker code
- Mitigation: narrow-scope edits + immediate regression coverage.

2. Over-correction causing legitimate patterns to fail
- Mitigation: paired positive/negative tests for each rule.

3. Diagnostic churn breaking tests
- Mitigation: assert invariant meaning; keep message fragments stable.

4. Flaky concurrency/network e2e noise
- Mitigation: isolate hardening subset; classify unrelated flakes separately.

---

## Exit Criteria

This track is complete when:
- invariant matrix A/B/C/D is covered by deterministic tests,
- no known checker-accepted ownership/ref cases fail later in MIR/LLVM in the subset,
- diagnostics for these rules are attributable and stable,
- sanitizer/memory runs for subset are clean,
- no workaround-only open items remain for these invariant classes.

---

## Deferred Follow-Ups (Post-Hardening)

- Larger structural decomposition of `type_checker.py` into policy modules.
- Optional stronger ref-return model evolution (if MVP escape rule remains too restrictive).
- Broader compiler architecture cleanup beyond invariant-critical paths.

---

## Commands (Planned Quick Iteration)

Note: run commands are intentionally short and repeatable.

- Driver single file:
  - `PYTHONPATH=. ./.venv/bin/python3 -m pytest -q <driver_test_file>`

- E2E single case:
  - `PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py -j 1 --debug <case>`

- E2E subset:
  - `PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py -j 1 <case1> <case2> ...`

- Sanitizer/memory variant:
  - `DRIFT_ASAN=1 DRIFT_ALLOC_TRACK=1 PYTHONPATH=. ./.venv/bin/python3 lang/tests/codegen/e2e/runner.py -j 1 <subset>`

No implementation work starts on this track until explicit go-ahead.
