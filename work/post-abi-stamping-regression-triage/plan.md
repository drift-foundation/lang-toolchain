# Post-ABI-Stamping Regression Triage

## Bucket 1 — Driver `test_driftc_codegen_*` (7 tests)

**Failures:**
- `test_driftc_codegen_scalar_main`
- `test_driftc_codegen_can_throw_callee_ok`
- `test_driftc_codegen_callback_indirect`
- `test_driftc_codegen_callback_indirect_zero_and_two_args`
- `test_driftc_codegen_callback_stored_in_struct`
- `test_driftc_codegen_callback_param`
- `test_driftc_codegen_callback_param_zero_and_two_args`

**Error:** `RuntimeError: clang failed: warning: overriding the module target triple with x86_64-pc-linux-gnu [-Woverride-module]`

**Root cause:** Entry-wrapper gating change in `compile_to_llvm_ir_for_tests`. Plain helper path (`enforce_entrypoint=False`) no longer emits OS `@main`. Driver tests compile IR with bare clang and expect runnable binary or `define i64 @main(...)` in IR. Linker fails with undefined reference to `main`.

**Fix area:** `lang/driftc/driftc.py` — `compile_to_llvm_ir_for_tests` entry-wrapper emission, or driver test harness clang invocation flags.

---

## Bucket 2 — `assert_expr_text` / `assert_expr_msg_text` (e2e)

**Failures:**
- `assert_expr_text`: FAIL (stderr mismatch)
- `assert_expr_msg_text`: FAIL (stderr mismatch) / timeout

**Root cause (stderr mismatch):** Runtime assert output now includes extra libc frames (`__libc_start_call_main`, `_start`). Test expected output is exact-match. Assertion semantics are correct; test contract is too strict.

**Root cause (timeout):** `assert_expr_msg_text` stalls in `build_type_env_from_ssa` at `lang/driftc/checker/__init__.py:3751` during hash/key operations on `(fn_id, instr.src)` map access. Separate compile-time perf/pathological-hashing regression.

**Fix area:** Update `expected.json` stderr to tolerate extra stack frames. Investigate SSA type-env build hang separately.

---

## Bucket 3 — `test_result_ok_copy_struct_string_retain`

**Failure:** `assert -1 >= 0`

**Root cause:** IR-level assertion about retain/release emission patterns. The test expects `define i64 @main(` to appear in emitted IR at a non-negative offset. Entry-wrapper gating change means `@main` is no longer emitted in the helper/test path.

**Fix area:** Same root cause as Bucket 1 — entry-wrapper emission in `compile_to_llvm_ir_for_tests`.

---

## Bucket 4 — MariaDB context

Their runtime crash signal can be explained by:
- Wrapped/encoded exit values (`code % 256`) from app-level error paths
- Compiler instability around boundary/helper codegen paths
- The `buffer_set_len` clobber bug (now fixed via `buffer_commit_read`)

Not enough evidence for a raw memory crash in this latest set.

---

## Priority order

1. **Bucket 1+3** (driver tests) — single root cause, highest count (8 tests)
2. **Bucket 2 stderr** (assert_expr_text) — test fixture update
3. **Bucket 2 timeout** (assert_expr_msg_text) — separate SSA perf investigation
