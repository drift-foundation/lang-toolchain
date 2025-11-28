# E2E (SSA-first) Progress

## Scope
- E2E tests should exercise the SSA pipeline: parse/type-check → SSA lowering → SSA simplifier → SSA verifier. Codegen/runtime is optional and will be re-enabled once SSA-only coverage is solid.
- Legacy runner is intentionally ignored; new e2e harness lives under `tests/e2e` and `tests/e2e_runner.py`.

## Current status
- SSA e2e runner exists (`tests/e2e_runner.py`), always drives `driftc` with `--ssa-check --ssa-check-mode=fail --ssa-simplify` and `SSA_ONLY=1` to bypass legacy codegen/link. Handles expected compile failures via `expected.json`.
- Initial cases:
  - `hello/` (compiles cleanly under SSA-only).
  - `invalid_field/` (expected compile-time field error; passes).
- SSA program suite (`tests/ssa_programs_*`) remains separate for SSA-only regression (also SSA_ONLY=1).
- `just test-e2e` runs the new e2e runner; `just test-ssa` remains the main structural+SSA suite.

## Gaps / next steps
- Re-enable codegen/runtime in e2e once SSA coverage is broad enough: drop `SSA_ONLY`, link/run binaries, honor `run` expectations in `expected.json`.
- Add more positive e2e cases that mirror real programs SSA already supports (control flow, try/else, try/catch, field mutation, for-loops, refs).
- Add negative e2e cases beyond invalid_field (e.g., type mismatch, bad index type) with `compile_error` expectations.
- Consider skip metadata in `expected.json` (e.g., requires_llvmlite) when reintroducing codegen/run on platforms without toolchain.
- Keep the runner SSA-first; do not resurrect legacy `run_tests.py`.
