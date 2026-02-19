# Compiler Hardening – recent item

## LANGUAGE_BUG: UAF on `Result::Ok` binder payload (Copy struct with `String`)

### Repro (external report mirrored locally)
- Source shape: `Result<ErrPacket, PacketDecodeError>` where `ErrPacket` is `Copy` and contains `String` fields.
- Pattern:
  - `match decode_err_packet(...) { Ok(v) => ... use v.sql_state / v.message ... }`
- Under `DRIFT_ASAN=1`, this produced:
  - heap-use-after-free in string compare path.
  - free originated from drop of the original `Result` value after binder extraction.

### Root cause
- LLVM lowering for `VariantGetField` used a raw payload load for non-bool fields.
- For non-bitcopy `Copy` structs (e.g. struct with `String`), this created aliasing instead of semantic copy.
- The source variant/scrutinee drop then released string storage still referenced by binder value.

### Fix
- File: `lang/codegen/llvm/llvm_codegen.py`
- In `VariantGetField` lowering:
  - detect non-bitcopy `Copy` struct payload fields,
  - emit semantic copy via `_emit_copy_value(...)` instead of raw shallow extraction.
- Added explanatory ownership comment at this lowering site to prevent future regressions.

### Regression coverage
- Added driver regression:
  - `lang/tests/driver/test_result_ok_copy_struct_string_retain.py`
- Pins end-to-end behavior:
  - `Result<Payload, Int>` where `Payload` is `Copy` and contains `String`,
  - `match ... Ok(v)` extraction in `main`,
  - emitted IR for `main` must include `drift_string_retain` for binder payload path.

### Validation
- Regression test passes.
- External mariadb-proto repro command now passes under ASAN (`exit 0`).
- Valgrind memcheck on fixed repro binary reports `0 errors`.

## LANGUAGE_BUG: leak on `Result::Ok` match binder cleanup for internal binder locals

### Repro
- External repro:
  - `tmp/tests/unit/com_query_fixture_replay_test.drift`
  - `decode_err_packet` path under valgrind reported definite leaks (64 bytes total).
- In emitted IR, `Result::Ok(v)` binder extraction retained `String` fields but return edges in that arm did not release binder-owned strings.

### Root cause
- `stage2/string_arc.py` excluded internal locals from destructible return cleanup.
- Match binders lower to internal locals (`__match_binder_*`), so they were skipped by `_drop_all_destructibles(...)` on return paths.
- This left semantic-copy binder payloads unreleased in early-return arms.

### Fix
- File: `lang/driftc/stage2/string_arc.py`
- Updated `destructible_locals` selection to include `__match_binder_*` locals (same policy already used for array-locals handling).

### Regression coverage
- Added e2e alloc-track regression:
  - `lang/tests/codegen/e2e/result_ok_copy_struct_string_match_return_no_leak/main.drift`
  - `lang/tests/codegen/e2e/result_ok_copy_struct_string_match_return_no_leak/expected.json`
- Pins: `Result::Ok(v)` where `v` is `Copy` struct containing `String`, with early return in arm; must be leak-free.

### Validation
- New e2e regression passes.
- External repro now passes:
  - ASAN run: clean (`exit 0`)
  - valgrind memcheck: `All heap blocks were freed -- no leaks are possible`.

## Boundary hardening follow-up: copy-struct-with-string binder cleanup matrix

### Added regressions (alloc-track)
- `lang/tests/codegen/e2e/optional_some_copy_struct_string_match_return_no_leak/main.drift`
- `lang/tests/codegen/e2e/result_nested_ok_copy_struct_string_match_return_no_leak/main.drift`
- `lang/tests/codegen/e2e/result_generic_ok_copy_struct_string_match_return_no_leak/main.drift`

All three set `"alloc_track_leak": true` in `expected.json` and pin early-return cleanup over match binders for `Copy` structs containing `String`.

### Validation
- Normal e2e subset: pass.
- ASAN subset (`DRIFT_ASAN=1`): pass.
- memcheck subset (`DRIFT_MEMCHECK=1`): pass.
