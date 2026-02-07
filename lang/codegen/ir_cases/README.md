IR-based codegen cases (clang runner).

These are not “full” Drift e2e tests; they run prebuilt `ir.ll` under
`lang/codegen/ir_cases/<case>/` using the clang-based runner
`lang/codegen/ir_cases/e2e_runner.py`. Build artifacts go to
`build/tests/lang/<case>/`.

Once the parser/driver emits LLVM IR directly for tests, these cases can be
replaced by real Drift source-based e2e tests under a separate `e2e/` dir.
