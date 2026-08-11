# Review: upstream 0.36.0 root-fix notice

**Review state:** Root fix accepted in principle; changes requested to the promised regression coverage. PushCoin remains held pending correction and the full release/certification gate.

## Independently verified

- **Confirmed:** The upstream producer diff adds only `no_sanitize_address` to the exact-length, align-1 `@__drift_build_info` global. The reader is unchanged; there is no NUL trimming or tolerance path.
- **Confirmed:** The new linked-ASAN regression exists at `lang/tests/driver/test_abi_version_stamp.py::test_build_info_survives_asan_link`, uses the real ASAN runtime archive and `-fsanitize=address -g`, and reads the linked binary through production `extract_build_info`.
- **Observed:** The new regression independently passes on the final diff (`1 passed in 19.27s`). `git diff --check` passes.
- **Confirmed:** The version change is 0.35.0 → 0.36.0 with ABI 22 unchanged, consistent with a user-visible compiler fix that changes no compiler/runtime ABI boundary.
- **Confirmed from upstream append-only review:** `lang.reviewer` independently observed the regression RED before the producer change and ran both build-info test files GREEN (`63 passed`) afterward.
- **Confirmed:** The fix is not yet staged, committed, released, or certified. The upstream notice explicitly says PushCoin may resume only after Slawomir's full-suite/release gate.

## Regression coverage discrepancy

The upstream finding/notice says the linked-ASAN regression proves the extracted document reports profile `asan`. The checked-in test asserts only toolchain version and ABI. Its independently produced binary extracts successfully but reports:

`"build":{"profile":"optimized", ...}`

This does not invalidate the byte-contract root fix: the regression genuinely pins the ASAN transform/link path that caused the padding. But it does mean the durable claim “the pinned regression reports ASAN profile” is false, and the finding's stated acceptance criterion is not yet met. The separate reported CLI smoke is useful evidence, not a pinned regression.

Upstream must either make this regression compile/stamp through the ASAN profile path and assert `doc["build"]["profile"] == "asan"`, or explicitly narrow the finding/notice acceptance claim and add an equivalent automated test that pins the profile relied on by downstream gates. Re-run focused review after that correction.

## Downstream disposition

- Keep PushCoin Steps 6–8 held; do not consume the in-tree compiler or apply a workaround.
- Require a second durable notice after the regression coverage correction and after 0.36.0 clears the full-suite release/certification gate.
- Once certified, refresh the tracked current-main toolchain, rebuild `bookkeeper-asan`, and independently verify the external stamp reports profile `asan` before resuming the parent battery.
