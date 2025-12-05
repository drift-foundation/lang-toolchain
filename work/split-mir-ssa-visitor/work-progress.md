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

- (Checker/driver) Wire `declared_can_throw` from the checker (FnResult return types / throws clauses) into stage4 throw checks; keep the “no ConstructError in non-can-throw fns” and “no bare return in can-throw fns” invariants hard. Driver should call `run_throw_checks(funcs, summaries, declared_can_throw)` after stage3.
- (Stage4) Replace the structural FnResult check (`enforce_fnresult_returns_for_can_throw`, which rejects forwarding/aliasing) with a type-aware `enforce_fnresult_returns_typeaware` once SSA/type_env are threaded into stage4; keep the TODO stub in code to make this explicit.
- (Checker) Add catch-arm validation: at most one catch-all, catch-all must be last, and (optionally) warn/error on duplicate event arms. Lowering will then assume arms are well-formed.
- (Stage2) Catch-arm validation: multiple catch-alls are rejected in lowering; catch-all ordering/duplicate event arms should be enforced in the checker (currently a catch-all earlier than an event arm steals all control flow; later arms are dead).
- (Stage2) Rethrow/unmatched semantics implemented: unmatched errors unwind to the nearest outer try (via the try stack) and only return FnResult.Err when no outer try exists. Nested-unwind tests cover this; checker should still validate catch-arm shapes.
- (Stage2 tests) Additional try/catch coverage now includes binder vs no-binder arms, unknown-event fallback (code 0), and outer catch-all capturing propagated errors. Consider porting more legacy exception scenarios as HIR→MIR shape tests.

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
- Throw summaries are aggregated in stage3 (`ThrowSummaryBuilder`) and consumed in stage4 (`throw_checks`) to build function-level `FuncThrowInfo` and enforce can-throw invariants: (1) a function not declared can-throw must not construct an Error; (2) a can-throw function must not contain a bare `return` with no value; (3) a can-throw function must return a value produced by `ConstructResultOk/Err` (FnResult shape). Tests live in `lang2/stage4/tests/test_throw_checks.py`.  
- A placeholder type-aware FnResult return check (`enforce_fnresult_returns_typeaware`) exists in stage4/throw_checks.py; current structural check is intentionally over-strict and will be replaced once types reach stage4.  
- Stage-specific test dirs added (`lang2/stageN/tests/`); runtime artifacts for stage tests should go under `build/tests/stageN/`.
- Documentation tightened: all public AST nodes in stage0 carry docstrings; stage4 dominator/frontier comments corrected to match implementation/tests.

## Next steps (strict order)

1. **Freeze AST surface** during the rewrite.
2. **Finalize HIR nodes** (as above) and add `hir_nodes.py` stubs.
3. **Implement AST→HIR visitor/registry** with fail-loud default and unit tests (AST → expected HIR).
4. **Finalize MIR op list** (as above) and define the MIR instruction/block structures.
5. **Implement HIR→MIR lowering** (no SSA; structured control flow only).
6. **Implement pre-analyses on MIR** (address-taken, may-fail) storing flags in side tables.
7. **Implement MIR→SSA pass** in a new module: pure CFG + SSA construction using the pre-analysis flags.
8. **Remove legacy lowering**: delete or retire the monolithic `lower_to_mir_ssa.py` once new pipeline is green. Add exhaustiveness checks for handled node types.
