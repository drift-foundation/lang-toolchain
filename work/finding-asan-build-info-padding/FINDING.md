# ASAN padding breaks the `.drift_build_info` exact-byte contract

## Status

- **State:** queued LANGUAGE_BUG; implementation has not started.
- **Filed:** 2026-08-11 from Baton publication `fcfad62b3e95c44286a900209a9c425c` sent by `pushcoin.reviewer` to `lang.reviewer` and `human.slawomir`.
- **Downstream blocker:** PushCoin cannot validate AddressSanitizer builds through the supported `drift inspect build-info <binary> --json` interface.
- **Serial-work gate:** do not displace an active finding. The implementer must re-read this whole folder when Slawomir schedules it.
- **Progress ownership:** `lang.implementer` owns `PROGRESS.md`; the reviewer must not create or update that file.

The byte-exact durable Baton report is materialized as `report-2026-08-11T05-58-56Z-ac530b2bf623ea2401d819da4556fde1.md`. Its references leaf is preserved beside it.

## Classification and stop-and-confirm record

**LANGUAGE_BUG:** a compiler-produced, linked ASAN executable violates Drift's own executable metadata contract. No stdlib, application, reader-tolerance, or source-level workaround is approved.

- **Minimal repro:** compile the same dependency-free `main` normally and with `drift build --sanitize address`, then run `drift inspect build-info <binary> --json` on both. The normal binary succeeds; the ASAN binary fails because its section contains the canonical JSON followed by NUL padding. See `REPRO.md`.
- **Failing regression path:** proposed `lang/tests/driver/test_abi_version_stamp.py::test_build_info_survives_asan_link`. It must be added and observed red before a root fix. Editing this existing test file requires Slawomir's explicit confirmation under the repository rules.
- **Suspected subsystem:** LLVM code generation / AddressSanitizer global instrumentation at `LlvmModuleBuilder.emit_build_info`, plus the final link boundary.
- **Required resolution:** preserve the exact section bytes through an ASAN link. Do not trim trailing NULs or otherwise weaken `lang.build_info`'s fail-closed reader.

This record satisfies the first-notification gate. It does not authorize implementation or an existing-test edit.

## Evidence by epistemic status

### Observed by PushCoin

- A normal Bookkeeper binary is accepted; its `.drift_build_info` section size is `0x2c7` with alignment 1.
- An ASAN Bookkeeper binary is rejected at JSON character offset 704; its section size is `0x380` (896) with alignment 32.
- Corrected forensic measurement: the ASAN section contains a 706-byte canonical JSON document (704 decoded characters) followed by 190 NUL bytes. The two-byte byte/character difference is the UTF-8 encoding overhead of one em dash.
- PushCoin's adoption steps 6-8 are held; no app-side workaround was approved.

The original materialized report incorrectly described the decoder's character offset as a byte offset and therefore stated 704 bytes plus 192 NULs. PushCoin corrected that evidence in Baton message `d902e39734aa14caae971af4fbb48c53`; the 896-byte section, defect classification, and producer fix are unchanged. Drift reviewer reproduction on the current tree remains pending.

### Confirmed in the current Drift tree

- `lang/build_info.py` defines the section as exactly the canonical UTF-8 JSON bytes, with no terminator or framing bytes, and the production reader rejects surrounding/trailing bytes.
- `LlvmModuleBuilder.emit_build_info` emits an exact-length LLVM array in `.drift_build_info` with `align 1` and retains it through `@llvm.used`.
- `test_build_info_survives_link` exercises the production reader after a normal link, but explicitly selects `asan_enabled=False`; there is no linked-ASAN companion.
- The current identities are `DRIFTC_VERSION = 0.35.0` and `DRIFT_RT_ABI_VERSION = 22`.

### Inferred

- AddressSanitizer instrumentation or its link-time handling is widening/re-aligning the metadata global despite the emitted exact-length, align-1 IR.
- A producer-side exclusion of the metadata global from sanitizer instrumentation is the likely patch shape, but the implementer must prove the actual transformed IR/object/ELF path before choosing an LLVM mechanism.

### Open

- The first boundary at which the section changes: compiler IR, ASAN-instrumented object, or final executable.
- The supported LLVM representation needed to keep this metadata global uninstrumented across the repository's clang/LLVM matrix.
- Whether any non-ASAN sanitizer or combined sanitizer profile has the same defect.
- Whether the eventual fix changes only internal lowering/link metadata or any compiler/runtime ABI boundary. The expected answer is internal-only, but it must be rechecked against the final diff.

## Contract guardrails

- Preserve the exact canonical section and fail-closed reader contract.
- Add a linked-binary positive regression for ASAN and keep the existing malformed/trailing-content negatives intact.
- Do not edit the language specification; this is compiler/tooling conformance, not a language-semantic proposal.
- Do not add a compatibility reader, NUL trimming, fallback, or app/stdlib workaround.
- User-visible impact requires a compiler SemVer minor bump. If the fix is not already folded into an unreleased minor train, the expected version move is `0.35.0` to `0.36.0`.
- Do not bump `DRIFT_RT_ABI_VERSION` for a producer-only sanitizer/codegen correction with unchanged boundary signatures/layouts. Reassess if the actual fix changes a compiler/runtime boundary.

## Refactor-trigger scan

`doc/refactor_triggers.md` was scanned on 2026-08-11. No entry names executable build-info sections, sanitizer padding of metadata globals, or an equivalent trigger shape. The two ASAN mentions are historical verification notes, not triggers. Repeat the scan when implementation starts because the registry may change.
