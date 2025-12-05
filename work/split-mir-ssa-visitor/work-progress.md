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
- AST→HIR visitor under `lang2/stage1/ast_to_hir.py` now lowers literals, vars, unary/binary ops, field/index, let/assign/if/while/for/return/break/continue/expr-stmt, and plain/method calls; for-loops desugar to `iter()/next()/is_some()/unwrap()` over Optional using HLoop/HIf/HBreak and scoped temps. Remaining sugar (ternary, try/throw/raise, array literals) still stubbed. Basic unit tests live in `lang2/stage1/tests/test_ast_to_hir.py`.  
- MIR schema defined under `lang2/stage2/mir_nodes.py` (explicit ops, blocks, functions).  
- HIR→MIR builder/skeleton under `lang2/stage2/hir_to_mir.py` lowers straight-line HIR (literals/vars/unary/binary/field/index + let/assign/expr/return), `if` with branches/join, `loop`/break/continue, and basic calls/DV construction into MIR blocks; remaining sugar (try/ternary/etc.) still TODO. Unit tests in `lang2/stage2/tests/test_hir_to_mir.py` and `lang2/stage2/tests/test_for_ast_to_mir.py` cover these paths.  
- MIR pre-analysis under `lang2/stage3/pre_analysis.py` now tracks address_taken locals and marks may_fail instruction sites (calls/method calls/DV construction; conservatively extensible). Unit tests in `lang2/stage3/tests/test_pre_analysis.py`.  
- MIR dominator analysis added under `lang2/stage4/dom.py`, computing immediate dominators for MIR CFGs; unit tests in `lang2/stage4/tests/test_dominators.py`.  
- MIR dominance frontier analysis added under `lang2/stage4/dom.py` for SSA φ placement; unit tests in `lang2/stage4/tests/test_dominance_frontiers.py` cover straight-line, diamond, and loop shapes.  
- MIR→SSA skeleton added under `lang2/stage4/ssa.py` (single-block straight-line SSA with load/store version checks, local version tables, and per-instruction SSA name tracking via `value_for_instr`; multi-block functions are still rejected in the main entry point). An experimental multi-block path (`run_experimental_multi_block`) now places Φ nodes for locals defined along multiple branches in a simple diamond CFG; covered by `lang2/stage4/tests/test_mir_to_ssa_multi_block.py`.  
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
