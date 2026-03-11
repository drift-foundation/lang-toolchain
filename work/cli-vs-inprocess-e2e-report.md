# CLI vs In-Process E2E Runner: Parity Report

**Date:** 2026-03-11
**Context:** PEX e2e runner (`lang/tests/codegen/e2e/pex_e2e_runner.py`) exercises
1013 e2e cases through the CLI `driftc` binary.

**Final results:** 999 passed, 14 skipped, 0 failed. CLI_KNOWN_SKIP is empty.

## Parity Status

Full CLI parity achieved. All 1013 cases either pass through the CLI path or are
excluded by contract (not by CLI limitation).

The 14 skipped cases are **outside the CLI parity contract**:

- **9 cases** with `use_driftc_json: false` — these test entry-point validation
  diagnostics using an in-process-only JSON format. The CLI path is not expected
  to support this mode.
- **5 cases** with `package_consumer_only` — require signed package consumer flow,
  which runs through a separate runner (`pkg_consumer_runner.py`).

These skips are structural: they use runner features that the CLI path does not
and should not replicate. They are not unowned debt.

## Gaps Resolved

### Gap 1: borrow_nll_* compile-only semantics (3 cases)

**Cases:** `borrow_nll_branch_field_write_ok`, `borrow_nll_disjoint_field_after_branch_ok`,
`borrow_nll_loop_borrow_does_not_leak`

**Root cause:** Not a codegen bug. These cases have `"diagnostics": []` in
expected.json, which the in-process runner treats as a compile-success-only
assertion — it verifies zero diagnostics and never runs the binary. The PEX
runner was treating `bool([])` as falsy, entering the compile+run path, and
comparing the binary's exit code (1, the correct return value of `main()`)
against `expected_exit` (0, meaning "compilation succeeds").

LLVM IR was confirmed identical across both paths.

**Fix:** Added a `compile_only` code path in the PEX runner. When
`"diagnostics"` key is present but empty, verify compilation succeeds with
zero diagnostics; do not run the binary.

### Gap 2: cycle_direct / cycle_indirect_3way (2 cases)

**Root cause:** Not a CLI gap. The PEX runner already discovers all `.drift`
files via `rglob()` and passes `-M case_dir`. These cases work through CLI
as-is — the skip was unnecessary.

**Fix:** Removed from CLI_KNOWN_SKIP.

**Latent test quality issue:** Both cycle cases have export-name case mismatches
(`export { afn }` vs `pub fn aFn()`). They currently pass because the
export-validation error satisfies `exit_code: 1, phase: parser`, but they are
not actually testing cycle detection. This is a test-quality issue, not a CLI
parity issue. Fixture edits to correct the export names and add
`"message_contains": "import cycle detected"` would lock down the intended
assertion. Pending authorization for test edits.

### Gap 3: module_id_invalid_inferred_from_path (1 case)

**Root cause:** PEX runner gated `-M` flags on `has_module_decl`, but this
diagnostic case intentionally lacks module declarations to test that the
compiler rejects them in workspace mode. Without `-M`, the compiler entered
multi-file mode and produced a different diagnostic message.

**Fix:** Pass `-M` flags unconditionally when `has_diags and module_paths`,
mirroring the in-process runner behavior.

## Other Fixes Applied During Investigation

- **70 cases:** workspace mode (`-M`) was forced on files without `module`
  declarations. Made `-M`/`--entry` conditional on `has_module_decl`.
- **11 cases:** `--test-build-only` flag wasn't being passed. `@test_build_only`
  stdlib symbols were invisible.
- **~15 cases:** stderr exact-match failures. Added `__ANY__` and
  `stderr_contains` support.
- **2 cases:** expected-failure cases misclassified as unexpected failures.
- **PYTHONPATH poisoning:** PEX child processes inherited `PYTHONPATH=.` and
  crashed. Stripped from subprocess env.
- **byte_length not found:** `--stdlib-root` was only passed for explicit
  `import std.*` cases. Now always passed.

## Key Findings

1. **`--test-build-only` is critical for CLI e2e coverage.** Without it, 11 cases
   fail because `@test_build_only` stdlib symbols are stripped.

2. **`"diagnostics": []` means compile-success-only.** The in-process runner
   short-circuits on this pattern. The PEX runner now mirrors this.

3. **No codegen divergence found.** The borrow_nll cases were a runner semantics
   mismatch, not a compiler bug.

4. **No `byte_length` regression.** The initial failure was runner
   misconfiguration (missing `--stdlib-root`), not a prelude regression.

## Infrastructure Changes

- Removed all `clang-15` references from active build infrastructure (20 files).
  System `clang` (clang-20) is now the sole default. Historical entries in
  `docs/history.md` preserved.
- `just lang-codegen-test-pex` uses `PEX_E2E_JOBS` with `$(nproc)/2` default
  for parallelism.
