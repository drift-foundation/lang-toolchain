# Code Review Request (Klaudia)

## Objective
Deep-review the Drift toolchain for compiler correctness, architecture quality, and maintainability. Focus on places where we recently had regressions: ownership/move/drop semantics across stage boundaries, match lowering, `Result` payload handling, boundary diagnostics, and duplicated call-shape logic.

Please prioritize finding:
- correctness bugs (especially silent miscompiles or double-drop/double-free classes),
- brittle cross-stage contracts,
- duplicated logic that should be centralized,
- weak guardrails/tests that allow regressions,
- places where diagnostics hide root cause or lose source span.

## Review Scope (Priority Order)

1. `lang/driftc/stage2/hir_to_mir.py`
- Purpose: core lowering from typed HIR to MIR. Contains move/copy/drop decisions, match lowering, try/throw lowering, binder extraction.
- Why high risk: most recent defects/regressions came from ownership/drop behavior here.
- Please focus on:
  - `match` arm lowering and binder extraction (`VariantGetFieldAddr`, `LoadRef`, `CopyValue`, scrutinee cleanup ordering).
  - rules for when payload is considered moved vs copied.
  - local drop registration and scope pop behavior (`_register_drop_local`, `_needs_runtime_drop`, scope cleanup).
  - possible duplicated ownership logic across array/variant/tuple/struct paths.
  - opportunities to split into smaller, testable helper modules.

2. `lang/codegen/llvm/llvm_codegen.py`
- Purpose: MIR -> LLVM IR codegen; drop glue emission, ABI mapping, destructors, call lowering, debug info.
- Why high risk: prior arg mismatch and drop helper omissions surfaced here.
- Please focus on:
  - drop helper generation consistency across arrays/variants/diagnostic values/interfaces.
  - ABI/result lowering consistency (FnResult, variant payloads, by-value aggregates).
  - alias/phi/copy emission paths where temps are pre-collected.
  - duplicated type-shape logic that should be centralized in one mapping function.

3. `lang/driftc/checker/__init__.py`
- Purpose: typed checker + diagnostics + many semantic constraints.
- Why high risk: very large/high-entropy module; source of multiple internal errors in recent work.
- Please focus on:
  - ownership rules for non-Copy values (index read/move diagnostics, assignment-target suppression, ref-vs-value expectations).
  - diagnostic quality (internal IDs leaking, missing source spans, inconsistent error taxonomy).
  - hotspots that duplicate logic now also present in stage2 and borrow checker.

4. `lang/driftc/borrow_checker_pass.py` and `lang/driftc/borrow_checker.py`
- Purpose: borrow/move invariants after typecheck.
- Why high risk: interactions with new borrowed-aggregate support (`struct { x: &mut T }`).
- Please focus on:
  - provenance/origin checks through wrappers (`Result`, `Optional`), pattern matches, method receivers.
  - prevention of hidden escapes (containers, globals, callbacks/closures/spawn paths).
  - consistency with checker and stage2 assumptions.

5. `lang/driftc/driftc.py`
- Purpose: driver/orchestration, boundary diagnostics, build modes, wrapper behavior.
- Why high risk: boundary contract diagnostics and policy wiring live here.
- Please focus on:
  - all boundary failures routing through centralized helper(s) (no ad-hoc diagnostics).
  - phase/message taxonomy consistency.
  - span attachment quality for contract failures.
  - env-mode parsing and behavior toggles (avoid distributed policy checks).

6. `lang/driftc/call_contract.py`
- Purpose: shared call-shape/metadata contracts across stages.
- Why high value: this is our intended central seam to reduce duplication.
- Please focus on:
  - whether checker/stage2/borrow checker truly use this as single source of truth.
  - missing validations that still happen ad-hoc elsewhere.

7. `lang/driftc/mir_validate.py`
- Purpose: MIR invariant validation before LLVM lowering.
- Why high value: guardrail layer for catching lowering contract breakage early.
- Please focus on:
  - missing invariants for ownership-sensitive instructions.
  - contract clarity vs later LLVM assertion failures.

## Stage-Contract Seams (Must Review)
Please explicitly evaluate contract soundness between:
- checker -> stage2
- stage2 -> MIR validate
- MIR validate -> LLVM lowering

For each seam, identify:
- shape assumptions that are implicit vs encoded,
- where an unsupported shape becomes an internal failure instead of checker diagnostic,
- minimal additional guardrails to fail earlier with actionable diagnostics.

## Tricky/Complex Areas to Audit
- `Result::Ok(payload)` match-binder ownership paths (move/copy/drop ordering).
- borrowed aggregates (`struct` fields with refs) and non-escape restrictions.
- destructor invocation ordering across early returns, match arms, and try/catch.
- by-value aggregate returns crossing call boundaries.
- variant payload extraction from ref/non-ref scrutinees.
- diagnostics for boundary/internal failures (message stability + source spans).

## Duplicate-Logic Reduction Targets
Please call out concrete centralization opportunities, especially:
- call argument layout/metadata checks duplicated across checker/stage2/borrow checker,
- ownership/drop decisions encoded in more than one layer,
- type-shape predicates repeated in checker + codegen.

If possible, propose a small refactor sequence (safe increments) rather than a large rewrite.

## Guardrails/Test Expectations
When recommending changes that alter boundary shape support, include test expectations:
1. positive regression (supported shape passes e2e/driver),
2. negative regression (unsupported shape fails with clear non-internal diagnostic),
3. boundary diagnostic assertions (phase/message/span correctness),
4. no contradictory tests/docs after behavior change.

## Useful Test Areas for Review Validation
- `lang/tests/codegen/e2e/*result*`
- `lang/tests/codegen/e2e/*struct_ref_field*`
- `lang/tests/driver/test_boundary_matrix_result_variant_contract.py`
- `lang/tests/driver/test_codegen_boundary_diagnostics.py`
- `lang/tests/driver/test_codegen_preemit_boundary_diagnostics.py`
- `lang/tests/stage2/test_hir_to_mir_match_requires_binder_indices.py`

## Deliverable Requested
Please provide:
1. top critical findings (ordered by severity),
2. probable root-cause file/function for each,
3. proposed remediation sequence (small, safe steps),
4. regression tests to add/adjust,
5. architectural recommendations (centralization/de-dup/guardrails).

