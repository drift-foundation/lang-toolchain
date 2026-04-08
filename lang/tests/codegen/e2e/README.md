Drift-source end-to-end tests.

Cases live under `lang/tests/codegen/e2e/<case>/` with:
- `main.drift`   — source program compiled through the full pipeline (AST→HIR→MIR→SSA→TypeEnv→throw-checks→LLVM→clang)
- `expected.json` — exit_code/stdout/stderr expectations

Runner: `lang/tests/codegen/e2e/runner.py` (invoked by `just lang-codegen-test`) builds IR via `compile_to_llvm_ir_for_tests`, compiles with clang, runs the binary, and compares results. Artifacts go under `build/tests/lang/tests/codegen/e2e/<case>/`.

Useful runner env toggles:
- `DRIFT_ALLOC_TRACK=1` — enable allocator tracking and leak checks for configured cases.
- `DRIFT_MEMCHECK=1` — run binaries under valgrind memcheck.
- `DRIFT_MASSIF=1` — run binaries under valgrind massif.
- `DRIFT_VALGRIND_SUPPRESS_FIBER=1` — opt-in suppressions for known valgrind false positives from fiber/ucontext stack switching.
- `DRIFT_ASAN=1` — compile+run binaries with AddressSanitizer (`-fsanitize=address -g`).
  - `DRIFT_ASAN` cannot be combined with `DRIFT_MEMCHECK`/`DRIFT_MASSIF`.
- `DRIFT_UBSAN=1` — compile+run binaries with UndefinedBehaviorSanitizer (`-fsanitize=undefined -fno-sanitize-recover=undefined -g`).
  - `DRIFT_UBSAN` cannot be combined with `DRIFT_MEMCHECK`/`DRIFT_MASSIF`. May be combined with `DRIFT_ASAN`.
- `DRIFT_DEBUG=1` — flip the entire suite into the debug-style runtime lane (links the `_debug` runtime archive variant; suppresses `-O2`).  Co-equal with `drift build --debug`.
  - **Default (env unset):** every binary-producing test path runs in the production "normal" lane — `-O2`, links the unsuffixed runtime archive `libdrift_rt_abi<N>.a` (which exports `__drift_rt_mode_normal`).
  - **`DRIFT_DEBUG=1 just test`:** every binary-producing test path runs in the explicit "debug-style" lane — no `-O2`, links `libdrift_rt_debug_abi<N>.a` (which exports `__drift_rt_mode_debug`).
  - Composability with sanitizer/valgrind test modes:
    - `DRIFT_ASAN=1 DRIFT_DEBUG=1` → sanitizer takes precedence at runtime variant selection (`asan` variant).  `-fsanitize=address` still applied; `-O2` suppressed because of `DRIFT_DEBUG=1`.
    - `DRIFT_UBSAN=1 DRIFT_DEBUG=1` → same shape, `ubsan` variant.
    - `DRIFT_MEMCHECK=1 DRIFT_DEBUG=1` → debug-style compile, valgrind memcheck wraps the run.
    - `DRIFT_MASSIF=1 DRIFT_DEBUG=1` → debug-style compile, valgrind massif wraps the run.
  - Existing sanitizer/valgrind mutual-exclusion rules are unaffected; `DRIFT_DEBUG` does not participate in them.

Memcheck policy:
- Default is strict: no suppressions.
- Only fiber/ucontext suppressions are supported via `DRIFT_VALGRIND_SUPPRESS_FIBER=1`.
- Non-fiber memcheck errors (stdlib/runtime/user paths) remain hard failures and must be fixed.
- Per-case opt-in is also supported in `expected.json` via:
  - `"valgrind_suppress_fiber": true`
  - Effective behavior is `env OR case flag`.
- Per-case memcheck skip is supported in `expected.json` via:
  - `"skip_memcheck": true`

Current cases:
- `simple_return`: `drift_main` returns 42; expect exit_code=42, empty stdout/stderr.
