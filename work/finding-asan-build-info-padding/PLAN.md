# Plan

This is a queued LANGUAGE_BUG plan. The implementer must revalidate every claim against the then-current tree and record contrary evidence or plan changes in implementer-owned `PROGRESS.md`.

## Gate 0 — ownership and scheduling

- [ ] `lang.implementer` creates and exclusively owns `PROGRESS.md`.
- [ ] Slawomir confirms this finding is the next serial item; do not interrupt an active finding.
- [ ] Read the whole folder fresh, including any later reviewer evidence or review journal.
- [ ] Repeat the `doc/refactor_triggers.md` scan and record the result.
- [ ] Obtain explicit confirmation before editing the existing `lang/tests/driver/test_abi_version_stamp.py` test file.

## Gate 1 — red linked-binary regression

- [ ] Add `test_build_info_survives_asan_link` beside the existing normal-link regression.
- [ ] Use the repository's actual ASAN runtime/archive and link flags rather than a synthetic padded byte string.
- [ ] Read the executable through production `extract_build_info`.
- [ ] Run only the new test first and capture the expected pre-fix failure.
- [ ] Record section bytes/size/alignment at the emitted IR, object, and executable boundaries as needed to locate the first mutation.

The regression must be red on the current behavior before any compiler/codegen change. A synthetic validator-only test is insufficient because the defect is introduced after the exact IR constant is emitted.

## Gate 2 — root-cause proof

- [ ] Determine whether padding is introduced by ASAN's IR transform, object emission, or the linker.
- [ ] Identify the supported LLVM/clang mechanism for excluding this non-addressable metadata global from sanitizer widening while preserving `@llvm.used` retention.
- [ ] Confirm that normal, ASAN, and relevant combined-sanitizer profiles preserve exactly one section containing exactly the canonical payload.
- [ ] Document why the chosen mechanism is stable across the compiler versions supported by this repository.

Do not alter `validate_build_info_payload`, trim NUL bytes, add a length-prefix compatibility path, or accept a second contract.

## Gate 3 — minimal producer fix

- [ ] Apply the smallest producer/codegen correction justified by Gate 2.
- [ ] Keep the canonical document and section name/layout contract unchanged.
- [ ] Keep the reader fail-closed and preserve existing malformed/trailing-byte negative coverage.
- [ ] Avoid stdlib, application, or PushCoin changes.

If Gate 2 disproves the suspected producer-side shape, update `PROGRESS.md` with the boundary evidence and revise this plan before editing another subsystem.

## Gate 4 — focused verification

- [ ] New ASAN linked-binary regression passes.
- [ ] Existing `test_build_info_survives_link` passes.
- [ ] Existing build-info schema, malformed payload, duplicate section, and trailing-content tests pass.
- [ ] Run focused sanitizer variants implicated by the root-cause evidence.
- [ ] Run a compiler/CLI smoke build and `drift inspect build-info ... --json` on normal and ASAN outputs.

Agents do not run `run_all_tests.sh`; report readiness for Slawomir's full-suite run.

## Gate 5 — version, ABI, and downstream handoff

- [ ] Make the compiler minor-version decision before staging; expected `0.35.0` to `0.36.0` unless already folded into a newer unreleased minor.
- [ ] Leave ABI 22 unchanged if the final patch only changes internal sanitizer/codegen behavior.
- [ ] If any compiler/runtime boundary signature, layout, calling convention, or ownership contract changes, stop and apply the ABI bump plus mismatch-regression rules.
- [ ] Add a `/tmp/drift-announce/<iso-utc>-drift-lang-release-notes.md` announcement describing the affected ASAN artifact contract, version/ABI result, regressions, and downstream migration status.
- [ ] Send PushCoin the pinned regression and root-fix evidence through Baton.
- [ ] Request independent review using Baton protocol 10; materialize the durable review in this finding and answer it through `PROGRESS.md`.

## Completion criteria

- A linked-ASAN regression failed before the fix and passes afterward.
- The compiler/codegen/link root cause is fixed; there is no reader or downstream workaround.
- Normal and negative build-info contracts remain green.
- Version/ABI bookkeeping matches the actual final boundary.
- PushCoin and Slawomir receive the durable completion handoff.
- The finding remains until the branch merges to main and closure is confirmed; no permanent tree artifact may reference this ephemeral folder.
