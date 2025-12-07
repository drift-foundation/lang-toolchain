Drift-source end-to-end tests.

Cases live under `lang2/codegen/e2e/<case>/` with:
- `main.drift`   — source program compiled through the full pipeline (AST→HIR→MIR→SSA→TypeEnv→throw-checks→LLVM→clang)
- `expected.json` — exit_code/stdout/stderr expectations

Runner: `lang2/codegen/e2e/runner.py` (invoked by `just lang2-codegen-test`) builds IR via `compile_to_llvm_ir_for_tests`, compiles with clang, runs the binary, and compares results. Artifacts go under `build/tests/lang2/codegen/e2e/<case>/`.

Current cases:
- `simple_return`: `drift_main` returns 42; expect exit_code=42, empty stdout/stderr.
