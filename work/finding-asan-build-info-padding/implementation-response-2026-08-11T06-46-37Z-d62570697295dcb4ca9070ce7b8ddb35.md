# Gate 3 applied, Gate 4 GREEN — every final-review gate you listed, answered

## The whole producer change

`LlvmModuleBuilder.emit_build_info` now emits:

    @__drift_build_info = internal constant [N x i8] [...],
      section ".drift_build_info", align 1, no_sanitize_address

`lang/build_info.py` is UNTOUCHED. No tolerance path, no NUL trimming, no
compatibility reader, no second contract, no stdlib/app/PushCoin change. Exact
section name, byte length, and alignment contract unchanged.

    lang/codegen/llvm/llvm_codegen.py           | 15 ++++++-
    lang/tests/driver/test_abi_version_stamp.py | 61 +++++++++++++++++++++++++++++
    lang/versions.py                            |  2 +-

## Your gates, one by one

**GREEN for the new linked-ASAN regression.** RED -> GREEN:

    pytest test_build_info_survives_asan_link test_build_info_survives_link -q
    -> 2 passed in 46.33s

**Normal link and contract/negative regressions green.** Both build-info files
run WHOLE, not name-filtered — this is what proves the schema,
malformed-payload, duplicate-section and trailing-content negatives still hold:

    pytest -n16 lang/tests/codegen/test_build_info_stamp.py \
                lang/tests/driver/test_abi_version_stamp.py -q
    -> 63 passed in 76.76s  (pre-bump)
    -> 63 passed in 81.59s  (post-bump, re-run)

**Diff proves no reader tolerance / compatibility path / workaround.**
`lang/build_info.py` has zero diff lines. The negatives above passing unchanged
is the behavioral confirmation.

**ASAN instrumentation remains active elsewhere.** Post-fix, from real driftc
output:

| Profile | Size | Align | NULs | Reader | ASAN active |
| --- | ---: | ---: | ---: | --- | --- |
| none | 267 | 1 | 0 | OK | n/a |
| asan | 267 | 1 | 0 | OK | yes — 4320 instrumented globals |
| ubsan | 267 | 1 | 0 | OK | n/a |
| asan+ubsan | 267 | 1 | 0 | OK | yes — 4320 instrumented globals |

`@__asan_global___drift_build_info` is gone (this one global exempt) while 4320
other globals stay instrumented and `asan.module_ctor` / `__asan_init` are
still emitted. `@__drift_build_info` stays in `@llvm.used`. Standalone UBSAN
unchanged. ASAN and ASAN+UBSAN both covered, nothing beyond them.

**Version/ABI matches the actual diff.**

- `DRIFTC_VERSION` 0.35.0 -> **0.36.0**. `lang/versions.py` states the rule:
  before 1.0, actual or suspected user-visible impact bumps MINOR and resets
  PATCH. ASAN artifacts becoming inspectable is user-visible.
- `DRIFT_RT_ABI_VERSION` stays **22**. One LLVM attribute on one internal
  metadata global: no exported helper signature, no boundary data layout, no
  calling convention, no ownership/drop contract change. Checked against the
  final diff, not the plan's expectation.
- No test pins the literal `0.35.0` (three hits, all comment prose).

**Compiler-version evidence — narrow, as you instructed.** Verified only on
this host's deployed compiler: `Ubuntu clang version 20.1.8 (0ubuntu4)`, the
only clang installed (`/usr/bin/clang` = `clang-20`; the repo discovers it with
a bare `shutil.which("clang")` and declares no floor). I claim no broader
matrix. For the record and not as a verified claim:
`no_sanitize_address` as a textual global attribute requires LLVM >= 15.

## End-to-end: the downstream blocker is cleared

    driftc app/main.drift -o hello-normal
    DRIFT_ASAN=1 driftc app/main.drift -o hello-asan

    drift inspect build-info hello-normal --json  -> exit 0, profile "optimized"
    drift inspect build-info hello-asan   --json  -> exit 0, profile "asan"

Both documents complete and canonical at `abi:22`; both binaries still run
(exit 0), so the exemption did not disturb the ASAN runtime. That is exactly
PushCoin's blocked adoption step.

## Not done, deliberately

- `run_all_tests.sh` — agents do not run the full suite. **Ready for
  Slawomir's full-suite run.**
- Release-notes/announcement file and the downstream PushCoin evidence handoff
  — both are Gate 5 items I have not started; say the word and I will draft
  them, or leave them to you.
- Nothing is committed or staged. The index remains Slawomir's.
