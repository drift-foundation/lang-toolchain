# Reproduction and evidence

## Upstream minimal reproduction

**Observed by PushCoin; pending independent Drift rerun.** Use a dependency-free Drift application whose `main` prints `hello` and returns 0. Build the same source twice:

```text
drift build                    --package-root "$DRIFT_PKG_ROOT" -o hello-normal
drift build --sanitize address --package-root "$DRIFT_PKG_ROOT" -o hello-asan

drift inspect build-info hello-normal --json
# expected and observed: exit 0 with one canonical drift-build-info/v1 document

drift inspect build-info hello-asan --json
# expected: exit 0 with the same valid document shape and ASAN profile
# observed: exit 1; build-info payload is not valid JSON: Extra data ...
```

The exact package-root setup and trivial source must be pinned in the regression rather than relying on PushCoin's Bookkeeper tree.

## Binary evidence reported downstream

| Artifact | Section size | Alignment | Reader result |
| --- | ---: | ---: | --- |
| Normal Bookkeeper | `0x2c7` | 1 | accepted |
| ASAN Bookkeeper | `0x380` (896) | 32 | rejected at JSON character offset 704 |

Corrected forensic measurement: the ASAN section contains 706 valid canonical JSON bytes (704 decoded characters) followed by 190 NUL bytes. The payload contains one em dash, which is one decoded character but three UTF-8 bytes; Python's JSON diagnostic reports the character offset, not the ELF section byte offset. The original upstream report's 704-byte / 192-NUL statement is superseded by PushCoin Baton correction `d902e39734aa14caae971af4fbb48c53`. This is not a reader bug: trailing bytes violate the checked-in exact-byte contract.

## Current producer/consumer map

- Producer: `lang/codegen/llvm/llvm_codegen.py`, `LlvmModuleBuilder.emit_build_info`.
- Producer output: `@__drift_build_info = internal constant [N x i8] [...], section ".drift_build_info", align 1`, retained in `@llvm.used`.
- Container reader: `lang/build_info.py`, `read_build_info_section` / `extract_build_info`.
- Payload validator: `lang/build_info.py`, `validate_build_info_payload`.
- Existing normal-link regression: `lang/tests/driver/test_abi_version_stamp.py::test_build_info_survives_link`.
- Missing red-first companion: proposed `test_build_info_survives_asan_link` using the production `extract_build_info` path.

## Regression acceptance criteria

1. Link the existing trivial stamped program with AddressSanitizer enabled through the repository's real helper/profile path.
2. Confirm the pre-fix binary fails through `extract_build_info`; record the exact command, exit/error, section size, and section alignment in `PROGRESS.md`.
3. After the root fix, parse the linked binary through `extract_build_info` and assert the full document, including an ASAN build profile.
4. Keep the normal linked-binary test green.
5. Keep or add a negative that proves genuinely trailing bytes remain rejected; the fix must not weaken the reader.
6. Check ASAN+UBSAN and standalone UBSAN if the same metadata transform applies, without expanding the fix beyond reproduced profiles.

## Source report references

The Baton REFERENCES leaf named:

- `pushcoin.source:bookkeeper/justfile`
- `pushcoin.source:bookkeeper/tests/microflows/run_ledger_stress.py`
- `pushcoin.source:work/finding-driftc-0350-adoption/review-2026-08-11T05-58-27Z.md`
- `lang.source:lang/build_info.py`
- `lang.source:lang/codegen/llvm/llvm_codegen.py`
- `lang.source:lang/tests/driver/test_abi_version_stamp.py`
