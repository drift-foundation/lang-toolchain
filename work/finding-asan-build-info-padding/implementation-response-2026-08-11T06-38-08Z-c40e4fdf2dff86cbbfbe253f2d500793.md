# RED observed: linked-ASAN regression fails; defect reproduced, boundary pinned

Gate 1 is satisfied. No producer or codegen file has been modified; the only
tree change is the new test.

## Independent reproduction — downstream report CONFIRMED

Trivial `main`, linked twice through the repository's real lanes on 0.35.0 /
ABI 22, clang 20.1.8:

| Lane | Section size | Align | Trailing NULs | Production reader |
| --- | ---: | ---: | ---: | --- |
| normal (`default` archive) | 267 (`0x10b`) | 1 | 0 | accepted |
| ASAN (`asan` archive, `-fsanitize=address -g`) | 352 (`0x160`) | 32 | 85 | REJECTED |

    lang.build_info.BuildInfoError: build-info payload is not valid JSON:
    Extra data: line 1 column 268 (char 267)

PushCoin's numbers are arithmetically corroborated, so their report needs no
correction. ASAN's redzone rule is `RZ = max(32, 32*(size/32/4 + 1))` plus
`alignTo(size,32) - size`. Our 267-byte document: `288-267=21`, `RZ=64`,
total 352. Their 704-byte document: `0`, `RZ=192`, total 896 — exactly the
reported `0x380` with 192 NULs.

## First mutation boundary: the ASAN IR transform, at COMPILE time

The linker is not implicated. In order:

1. driftc emits the contract shape: `@__drift_build_info = internal constant
   [267 x i8] [...], section ".drift_build_info", align 1`.
2. `clang -fsanitize=address -S -emit-llvm` rewrites it to
   `{ [267 x i8], [85 x i8] }` and emits
   `@__asan_global___drift_build_info` recording `size=267,
   size_with_redzone=352`. The custom section name is carried onto the padded
   struct, so the SECTION grows.
3. The instrumented `.o` already reads 352 / align 32 / 85 NULs — pre-link.
4. The non-sanitized `.o` reads 267 / align 1 / 0 NULs.

`.drift_build_info` is not addressable program data — nothing loads it at
runtime — so a redzone around it buys no memory-safety coverage and only
breaks the exact-byte contract.

## Affected profile scope

| Profile | Size | Align | NULs | Reader |
| --- | ---: | ---: | ---: | --- |
| none | 267 | 1 | 0 | OK |
| asan | 352 | 32 | 85 | WOULD REJECT |
| ubsan | 267 | 1 | 0 | OK |
| asan+ubsan | 352 | 32 | 85 | WOULD REJECT |

Standalone UBSAN is clean. The fix covers ASAN and ASAN+UBSAN and must not
expand past them.

## The RED regression

`lang/tests/driver/test_abi_version_stamp.py::test_build_info_survives_asan_link`,
added beside the existing normal-link regression under the explicit test-edit
authorization. Real `asan` runtime archive variant, driftc's own
`-fsanitize=address -g`, read through production `extract_build_info` — not a
synthetic padded byte string, which could not reproduce a defect introduced
after the exact IR constant is emitted.

    PYTHONPATH=. .venv/bin/python -m pytest \
      lang/tests/driver/test_abi_version_stamp.py::test_build_info_survives_asan_link -x -q
    -> 1 failed in 18.85s
       BuildInfoError: build-info payload is not valid JSON:
       Extra data: line 1 column 268 (char 267)
       raised at lang/build_info.py:345 via extract_build_info

## Gate 2 candidate, validated OUT OF TREE only

LLVM's `no_sanitize_address` global attribute, applied to a scratch copy of the
emitted IR and re-run through `clang -fsanitize=address -g`:

- section returns to 267 / align 1 / 0 trailing NULs;
- the module stays ASAN-instrumented (`asan.module_ctor`, `__asan_init`
  present) — this does not disable the sanitizer;
- `@__drift_build_info` stays in `@llvm.used`, so retention is preserved.

Expected Gate 3 patch shape: emit that attribute from
`LlvmModuleBuilder.emit_build_info`. The reader is untouched.

Still to prove before landing: the attribute's textual spelling is accepted
across every clang/LLVM version this repository supports, not only 20.1.8 on
this host. If any supported version rejects it, I will report back rather than
pick a fallback unilaterally.

Proceeding to Gate 3 unless you object.
