# lang2 directory layout

- `core/`: shared core shims (diagnostics, TypeTable/TypeEnv protocol, TypeEnv test impls). Tests in `core/tests/`.
- `parser/`: self-contained copy of the Drift parser + grammar (no runtime deps on `lang/`), with adapter to `lang2.driftc.stage0` AST. Tests in `parser/tests/`.
- `stage0/`: lang2 AST definitions (surface syntax mirror for the refactor).
- `stage1/`: HIR definitions and AST→HIR lowering; try-sugar rewrite lives here. Tests in `stage1/tests/`.
- `stage2/`: MIR definitions and HIR→MIR lowering. Tests in `stage2/tests/`.
- `stage3/`: MIR pre-analysis (address_taken, may_fail) and throw summaries. Tests in `stage3/tests/`.
- `stage4/`: SSA transform (acyclic CFGs), dominators/frontiers, throw checks (structural + type-aware). Tests in `stage4/tests/`.
- `checker/`: stub checker/TypeEnv glue and catch-arm validation; produces `CheckedProgram`/`FnInfo` for the pipeline.
- `codegen/`:
  - `llvm/`: SSA-first LLVM emitter.
  - `tests/`: LLVM backend unit tests (SSA→LLVM).
  - `ir_cases/`: IR-only e2e runner + prebuilt IR cases.
  - `e2e/`: Drift-source e2e runner + cases (source→AST→HIR→MIR→SSA→LLVM→clang).
- `driver/`: driftc/driver integration tests using the stub pipeline.

Top-level helpers:
- `driftc.py`: stub driver/scaffold (`compile_stubbed_funcs`, `compile_to_llvm_ir_for_tests`).
- `work-progress.md`: running plan/status document for this refactor.

Justfile targets (select):
- `lang2-test`: runs all stage/unit/integration/core/driver/codegen + parser tests.
- `lang2-parser-test`, `lang2-core-test`, `lang2-driver-test`, `lang2-codegen-test`: individual suites.

Artifacts:
- Test artifacts (codegen) go under `build/tests/lang2/...` and are cleaned by `lang2-codegen-test`.
