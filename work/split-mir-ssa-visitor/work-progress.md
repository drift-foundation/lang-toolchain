# Split MIR/SSA Lowering & Visitor Refactor — Work Progress

Goal: Replace the monolithic `if isinstance` lowering in `lower_to_mir_ssa.py` with clearer phases (AST→HIR→MIR→SSA), per-node visitors/registries, and explicit MIR ops. This should make lowering less brittle and easier to extend.

## Plan

1) Introduce a desugared HIR layer  
   - Normalize surface sugar (dot-placeholder, method-call sugar, index sugar) into core forms: `Call`, `MethodCall(receiver, name, args)`, `Index(subject, index)`, `Field(subject, name)`, `DVInit(kind, args)`, etc.  
   - No placeholders in HIR; receiver reuse is explicit.

2) AST→HIR visitor/registry  
   - Implement per-node handlers (`lower_<Node>` or registry) with a fail-loud default for unhandled nodes.
   - Keep this pass sugar-only; no SSA or storage concerns here.

3) Define explicit MIR ops  
   - MIR ops for `LoadLocal`, `StoreLocal`, `AddrOfLocal`, `Call`, `MethodCall` (receiver explicit), `ConstructDV`, `Phi`, etc.  
   - Dot-placeholder, DV constructors, and address-taken locals become MIR ops rather than inline lowering tricks.

4) HIR→MIR visitor  
   - Map HIR nodes to MIR ops using a visitor/registry.  
   - Structured control flow (`If`, `Loop`) stays structured for the next pass.

5) Pre-analyses on MIR  
   - Address-taken analysis (locals needing slots).  
   - Can-throw flags (already present; reuse/clarify).  
   - Store results in side tables; MIR→SSA consults flags, doesn’t recompute.

6) MIR→SSA pass (separate module)  
   - Pure CFG + SSA construction (dominators, φ insertion) over MIR blocks/locals.  
   - No knowledge of AST/HIR shapes or sugar.

7) Small helpers and exhaustiveness checks  
   - Helpers for common cases (`lower_method_call`, `lower_index`, `lower_dv_ctor`, `lower_short_circuit`).  
   - Registry/visitor enforces exhaustiveness: unhandled node types fail loudly.

- (Checker/driver) Wire `declared_can_throw` from the checker (FnResult return types / throws clauses) into stage4 throw checks; keep the “no ConstructError in non-can-throw fns” and “no bare return in can-throw fns” invariants hard. Driver should call `run_throw_checks(funcs, summaries, declared_can_throw)` after stage3; the stub driver (`lang2/driftc.py`) now routes declared_can_throw through the checker stub (`lang2/checker/__init__.py`) to mirror the real layering.
- (Stage4) FnResult shape is enforced structurally for untyped/unit tests and type-aware when SSA + `TypeEnv` are provided (typed paths skip the structural alias/forward guard). The type-aware check is implemented; structural guard remains as a fallback until real types are available everywhere.
- (Checker) Add catch-arm validation: at most one catch-all, catch-all must be last, and (optionally) warn/error on duplicate/unknown event arms. Lowering now rejects multiple catch-alls and catch-all-not-last; checker should enforce the rest.
- (Stage2) Rethrow/unmatched semantics implemented: unmatched errors unwind to the nearest outer try (via the try stack) and only return FnResult.Err when no outer try exists. Nested-unwind tests cover this; checker should still validate catch-arm shapes.
- (Stage2 tests) Additional try/catch coverage now includes binder vs no-binder arms, unknown-event fallback (code 0), outer catch-all capturing propagated errors, and legacy scenarios (inner catches A / outer catches B, throw inside catch rethrows to outer, inner catch-all wins over outer specific arm). Structural FnResult alias/forwarding limits are pinned via throw-check tests until type-aware checks land.
- (Try sugar) Record the result-driven try desugaring pattern to adopt later (e.g. `let tmp = fallible(); if tmp.is_err() { throw tmp.unwrap_err(); } let x = tmp.unwrap();`), likely in a checker/HIR rewrite pass when adding `?`/try-expression sugar.

## HIR node set (finalize before coding)

**Expressions**
* `HVar(name)`
* `HLiteralInt`, `HLiteralString`, `HLiteralBool`, etc.
* `HCall(fn, args)`
* `HMethodCall(receiver, method_name, args)`
* `HField(subject, name)`
* `HIndex(subject, index_expr)`
* `HDVInit(dv_type, args)`
* `HUnary(op, expr)`
* `HBinary(op, left, right)`

**Statements**
* `HLet(name, value)`
* `HAssign(target, value)`
* `HIf(cond, then_block, else_block)`
* `HLoop(block)`
* `HBreak`, `HContinue`
* `HReturn(expr)`

HIR must be sugar-free: placeholders, receiver reuse, and DV constructor sugar are all desugared here.

## MIR op schema (finalize before coding)

**Value-producing**
* `ConstInt`, `ConstString`, `ConstBool`
* `LoadLocal`, `AddrOfLocal`
* `LoadField`, `LoadIndex`
* `Call`, `MethodCall` (receiver explicit)
* `ConstructDV`
* `UnaryOp`, `BinaryOp`

**Side effects**
* `StoreLocal`
* `StoreField`
* `StoreIndex`

**Control flow**
* `Goto`
* `If` (or explicit branch/blocks)
* `Return`
* `Phi` (added during SSA construction)

MIR should be explicit and simple enough that lowering is mostly a mechanical mapping.

## Pre-analysis outputs (on MIR)
* `local.address_taken: bool`
* `expr.may_fail: bool` (can-throw/fail flags)

## Status

- Plan written (this file).  
- HIR skeleton added under `lang2/stage1/hir_nodes.py` with base classes, operator enums, expressions, statements, and `HBlock`/`HExprStmt`.  
- Local AST copy added under `lang2/stage0/ast.py` to keep the refactor isolated; all public nodes are documented for linting/QA.  
- AST→HIR visitor under `lang2/stage1/ast_to_hir.py` now lowers literals, vars, unary/binary ops, field/index, let/assign/if/while/for/return/break/continue/expr-stmt, plain/method calls, ternary expressions, try/throw, and ExceptionCtor → DV. For-loops desugar to `iter()/next()/is_some()/unwrap()` over Optional using HLoop/HIf/HBreak and scoped temps. Remaining sugar (raise/rethrow, TryCatchExpr, array literals) still stubbed. Basic unit tests live in `lang2/stage1/tests/test_ast_to_hir*.py`.  
- MIR schema defined under `lang2/stage2/mir_nodes.py` (explicit ops, blocks, functions).  
- HIR→MIR builder/skeleton under `lang2/stage2/hir_to_mir.py` lowers straight-line HIR (literals/vars/unary/binary/field/index + let/assign/expr/return), `if` with branches/join, `loop`/break/continue, basic calls/DV construction, ternary expressions (diamond CFG storing into a hidden temp), and `throw` (ConstructError + ResultErr + Return). `try` now supports multiple catch arms: a try stack routes `throw` to a dispatch block that compares `ErrorEvent` codes against per-arm constants (from the optional exception env; fallback 0), jumps to the matching catch or catch-all, and **unwinds to an outer try when no arm matches, returning FnResult.Err only when no outer try exists**. Catch blocks bind their binder (if any) and project the error code via `ErrorEvent`. Malformed catch arm shapes (multiple catch-alls or catch-all not last) are rejected in lowering. More advanced try/rethrow/array literals remain TODO. Unit tests in `lang2/stage2/tests/test_hir_to_mir*.py` cover these paths (including unmatched-event, nested-unwind, catch-all validation, binder/no-binder, unknown-event, and outer catch-all cases).  
- MIR pre-analysis under `lang2/stage3/pre_analysis.py` now tracks address_taken locals, marks may_fail sites (calls/method calls/DV construction/ConstructError), and records throw sites + exception DV names when the event code is a known constant (code→exception map passed in). Unit tests in `lang2/stage3/tests/test_pre_analysis.py` and `test_throw_summary.py`.  
- MIR dominator analysis added under `lang2/stage4/dom.py`, computing immediate dominators for MIR CFGs; unit tests in `lang2/stage4/tests/test_dominators.py`.  
- MIR dominance frontier analysis added under `lang2/stage4/dom.py` for SSA φ placement; unit tests in `lang2/stage4/tests/test_dominance_frontiers.py` cover straight-line, diamond, and loop shapes.  
- MIR→SSA now rewrites single-block straight-line MIR into SSA by replacing local load/store with `AssignSSA` moves, recording SSA versions per local and per-instruction (`value_for_instr`). Multi-block SSA is now supported for acyclic if/else CFGs (diamonds): backedges/loops are rejected; φ nodes are placed using dominators + dominance frontiers and renamed via a dominator-tree pass. Covered by `lang2/stage4/tests/test_mir_to_ssa.py` (single-block) and `lang2/stage4/tests/test_mir_to_ssa_multi_block.py` (diamond CFG).  
- Throw summaries are aggregated in stage3 (`ThrowSummaryBuilder`) and consumed in stage4 (`throw_checks`) to build function-level `FuncThrowInfo` and enforce can-throw invariants: (1) a function not declared can-throw must not construct an Error; (2) a can-throw function must not contain a bare `return` with no value; (3) thrown events must be a subset of declared events when provided; (4) non-can-throw functions must not return a value typed as FnResult on typed paths; (5) FnResult shape is enforced structurally for untyped/unit tests and type-aware when SSA + `TypeEnv` are provided (typed paths skip the structural alias/forward guard and additionally compare the returned FnResult parts against the checker-declared `return_type_id` when available). The shared `TypeEnv` protocol lives in `lang2/types_protocol.py` (type-centric with `is_fnresult`/`fnresult_parts`). Testing impls live in `lang2/types_env_impl.py`: `SimpleTypeEnv` for manual tagging and `InferredTypeEnv`/`build_type_env_from_ssa` to derive FnResult types from SSA + signatures; checker-owned TypeCore (`lang2/types_core.py`) + TypeEnv bridge (`lang2/checker/type_env_bridge.py`) wrap inferred types into checker TypeIds, and a minimal checker TypeEnv builder tags return SSA values with signature TypeIds. Tests live in `lang2/stage4/tests/test_throw_checks.py`, `test_throw_checks_typeaware.py`, `test_throw_checks_typeaware_forwarding.py`, `lang2/stage4/tests/test_declared_events_invariant.py`, `lang2/stage4/tests/test_non_can_throw_returns_fnresult.py`, `lang2/stage4/tests/test_scalar_return_mismatch_typeaware.py`, `lang2/types_env_impl_tests/test_inferred_type_env.py`, `lang2/checker/tests/test_checker_type_env.py`, and the try-sugar positive integration in `lang2/tests/test_driftc_try_sugar_ok_integration.py`.
- Initial LLVM codegen scaffold lives under `lang2/codegen/llvm/` with a text IR emitter for the ABI v1 subset (Int/Bool, Error, FnResult<Int, Error>, and basic MIR ops). Basic smoke tests assert emitted prototypes/returns; real end-to-end clang/lli runs are still TODO.
- Throw summaries are aggregated in stage3 (`ThrowSummaryBuilder`) and consumed in stage4 (`throw_checks`) to build function-level `FuncThrowInfo` and enforce can-throw invariants: (1) a function not declared can-throw must not construct an Error; (2) a can-throw function must not contain a bare `return` with no value; (3) thrown events must be a subset of declared events when provided; (4) non-can-throw functions must not return a value typed as FnResult on typed paths; (5) FnResult shape is enforced structurally for untyped/unit tests and type-aware when SSA + `TypeEnv` are provided (typed paths skip the structural alias/forward guard and additionally compare the returned FnResult parts against the checker-declared `return_type_id` when available). The shared `TypeEnv` protocol lives in `lang2/types_protocol.py` (type-centric with `is_fnresult`/`fnresult_parts`). Testing impls live in `lang2/types_env_impl.py`: `SimpleTypeEnv` for manual tagging and `InferredTypeEnv`/`build_type_env_from_ssa` to derive FnResult types from SSA + signatures; checker-owned TypeCore (`lang2/types_core.py`) + TypeEnv bridge (`lang2/checker/type_env_bridge.py`) wrap inferred types into checker TypeIds, and a minimal checker TypeEnv builder tags return SSA values with signature TypeIds. Tests live in `lang2/stage4/tests/test_throw_checks.py`, `test_throw_checks_typeaware.py`, `test_throw_checks_typeaware_forwarding.py`, `lang2/stage4/tests/test_declared_events_invariant.py`, `lang2/stage4/tests/test_non_can_throw_returns_fnresult.py`, `lang2/types_env_impl_tests/test_inferred_type_env.py`, `lang2/checker/tests/test_checker_type_env.py`, and the try-sugar positive integration in `lang2/tests/test_driftc_try_sugar_ok_integration.py`.
- Stage2→3→4 integration is exercised in `lang2/stage4/tests/test_throw_checks_integration.py`, covering can-throw vs non-can-throw functions, try/catch paths with explicit `FnResult.Ok` returns, result-driven try sugar (via `normalize_hir`) flowing through lowering and throw checks, and the structural rejection of FnResult forwarding/aliasing on untyped paths (typed paths now pass via TypeEnv). A stub driver (`lang2/driftc.py`) and checker placeholder (`lang2/checker/__init__.py`) illustrate where throw intent and catch validation come from in the real compiler: the driver now normalizes HIR, collects catch arms via `lang2/stage1/hir_utils.collect_catch_arms_from_block`, and passes signatures/catch arms/exception catalog to the checker. Checker-side catch-arm validation helpers live in `lang2/checker/catch_arms.py` (duplicate/unknown events, catch-all rules) and are invoked from `Checker.check`; `CheckedProgram` carries diagnostics, exception catalog, and slots for a concrete `TypeEnv`. Shared test helpers in `lang2/test_support/__init__.py` build `FnSignature`s, derive `declared_can_throw` via the checker, assemble exception catalogs, and can synthesize signatures/catalogs from decl-like stubs for future front-end tests. A driver flag (`build_ssa=True`) runs MIR→SSA, prefers a checker-owned TypeEnv (built from signature TypeIds/return values), and only falls back to checker SSA inference as a last resort; decl-based integration coverage (happy + mismatch) lives in `lang2/tests/test_driftc_decl_based.py`.
- Stage-specific test dirs added (`lang2/stageN/tests/`); runtime artifacts for stage tests should go under `build/tests/stageN/`.
- Documentation tightened: all public AST nodes in stage0 carry docstrings; stage4 dominator/frontier comments corrected to match implementation/tests.
- Result-driven try sugar scaffolding added: AST supports `TryExpr`, HIR adds `HTryResult`, and a stage1 rewrite (`lang2/stage1/try_result_rewrite.py`) desugars it to explicit HIR (`is_err`/`unwrap_err` throw + `unwrap`). Tests in `lang2/stage1/tests/test_try_result_rewrite.py` capture the shape; `normalize_hir` wraps the rewrite for use by drivers/tests (integration test in `test_try_result_integration.py` lowers desugared HIR to MIR). The checker now rejects try-sugar operands that are not known FnResult-returning calls via signatures (diagnostics surfaced in `lang2/tests/test_driftc_try_sugar_typecheck.py`). A positive full-pipeline case now exists via `HResultOk` + try sugar (`lang2/tests/test_driftc_try_sugar_ok_integration.py`), lowering cleanly to `ConstructResultOk`/Return with no diagnostics on typed paths. Full type-aware gating beyond signature-based checks remains to be done.

## Next steps (forward-looking)

- **Driver/checker integration:** The stub driver (`lang2/driftc.py`) and checker scaffold (`lang2/checker/__init__.py`) already mirror the future shape: `FnInfo`/`CheckedProgram` (exception catalog placeholder, TypeEnv slot, diagnostics via `lang2/diagnostics.py`) and `run_throw_checks(...)` called after stage3 with a diagnostics sink (driver path no longer raises `RuntimeError`). Driver now normalizes HIR once, collects catch arms via `lang2/stage1/hir_utils.collect_catch_arms_from_block`, and passes signatures/catch arms/exception catalog into the checker; the checker validates arms and populates `declared_can_throw`, `declared_events`, and `return_type` from `FnSignature`, assigning TypeIds via `TypeTable`. An optional `build_ssa=True` path runs MIR→SSA, first asks the checker to build a full SSA TypeEnv (`Checker.build_type_env_from_ssa`, using the checker’s TypeTable/signatures), and only falls back to a minimal checker TypeEnv that tags return SSA values with signature TypeIds when SSA typing yields nothing. The legacy `InferredTypeEnv` + bridge now lives only in stage4 unit tests. Decl-based integration coverage now includes both happy and mismatch cases (`lang2/tests/test_driftc_decl_based.py`). **Remaining work:** replace the test shims (`declared_can_throw` maps / string/tuple signatures / `exc_env`) with real signatures, TypeEnv, spans, and a real exception catalog. **TODO:** replace the placeholder `Diagnostic` (message/severity/span/notes) with a port of the real `lang/` diagnostic structure once available.
- **Type-aware FnResult returns:** Typed paths already run `enforce_fnresult_returns_typeaware` when SSA + TypeEnv are provided; structural alias/forwarding guard is only enforced on untyped/unit-test paths. Next step: rely solely on the type-aware path for typed pipelines once a real TypeEnv is available everywhere (structural guard becomes a fallback/debug check).
- **Catch-arm validation in checker:** Lowering already rejects multiple catch-alls and catch-all-not-last. Checker-side helpers in `lang2/checker/catch_arms.py` now enforce duplicate/unknown event arms and emit `Diagnostic`s; the driver collects HIR catch arms and feeds them to the checker with the exception catalog. **Remaining work:** back this with real spans/exception catalog from the parser/type checker instead of `exc_env`/synthetic `CatchArmInfo`.
- **Result-driven try sugar:** Integrate the desugaring pattern (`val tmp = fallible(); if tmp.is_err() { throw tmp.unwrap_err(); } val x = tmp.unwrap();`) into the pipeline (likely a checker/HIR rewrite using the new `HTryResult`), including type checks that the operand is `FnResult<_, Error>`. Stage1 already exposes `normalize_hir` for driver use and tests the rewrite path.
- **Surface exception scenarios:** Keep porting legacy exception cases as HIR→MIR shape tests (multi-event with catch-all, inner/outer catch ordering, throw from inside catch, inner catch-all vs outer specific) to guard semantics.
- **More integration coverage:** Add stage2→3→4 integration cases as needed (key paths covered: throw-only, try/catch + Ok, result-try sugar, and expected-fail forwarding/aliasing). Remaining coverage depends on future checker/type-aware work.
