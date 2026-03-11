# `use_driftc_json: false` Case Inventory

**Date:** 2026-03-11

## Summary

15 cases currently set `use_driftc_json: false`. **All 15 are convertible** —
none reflect an intentional contract boundary or a real tooling limitation.

| Category | Count | Action |
|----------|-------|--------|
| Diagnostic cases — remove flag only | 9 | Delete `use_driftc_json: false`; expectations already in JSON-compatible format |
| Success cases — dead flag | 6 | Delete `use_driftc_json: false`; flag has no effect on non-diagnostic cases |
| Blocked by tooling limitation | 0 | — |
| Should remain non-JSON by design | 0 | — |

Converting all 15 would move the PEX runner from 999/14/0 to 1008/5/0
(the remaining 5 skips are `package_consumer_only`).

## Why these were `use_driftc_json: false`

Historical artifact. These cases were written before the `--json` diagnostic
fast path existed in the in-process runner, or before the entrypoint validation
diagnostics flowed through the standard `Diagnostic` pipeline. The flag was
never revisited after those capabilities landed.

No case tests human-readable stderr text that differs from `--json` output.
All diagnostic strings are identical between the in-process `Diagnostic.message`
field and the `--json` `message` field.

## Per-Case Classification

### Category A: Diagnostic cases (9) — convertible, remove flag only

All 9 have `diagnostics` arrays with `message_contains` / `phase` entries that
are already in the format the `--json` path expects. The only change needed is
deleting `"use_driftc_json": false` from expected.json.

| Case | Diagnostic tested | Emitter location |
|------|------------------|-----------------|
| `entry_missing_main` | `"missing entry point 'main' for code generation"` (typecheck) | type_checker.py:9953 |
| `entry_main_wrong_params_rejected` | `"entrypoint main has invalid signature"` (typecheck) | type_checker.py:10020 |
| `entry_main_too_many_params_rejected` | `"entrypoint main has invalid signature"` (typecheck) | type_checker.py:10020 |
| `entry_main_argv_wrong_type_rejected` | `"entrypoint main has invalid signature"` (typecheck) | type_checker.py:10020 |
| `entry_main_wrong_return_rejected` | `"entrypoint main must return Int"` (typecheck) | type_checker.py:10002 |
| `main_missing_nothrow_rejected` | `"entrypoint main must be declared nothrow"` (typecheck) | type_checker.py:10030 |
| `entry_multiple_main_rejected` | `"duplicate entry point definition for 'main'"` (typecheck) | type_checker.py:9970 |
| `duplicate_main` | `"duplicate entry point definition for 'main'"` (typecheck) | type_checker.py:9970 |
| `module_id_invalid_reserved_prefix` | `"reserved module namespace"` (package) | driftc.py:7410 |

All these diagnostics flow through the standard `Diagnostic` pipeline and appear
in `--json` output via `_diag_to_json`. The `message_contains` substrings in the
existing expected.json files match the `--json` message field exactly.

### Category B: Success/runtime cases (6) — dead flag, remove only

These cases have no `diagnostics` key. They compile and run, asserting exit code
and/or stdout. `use_driftc_json: false` has no effect on the in-process runner's
code path for non-diagnostic cases — the flag only gates the diagnostic fast
path (runner.py:488), which these cases never enter.

| Case | Tests | Exit code |
|------|-------|-----------|
| `bare_block_empty` | Empty `{}` compiles and runs | 1 |
| `bare_block_basic` | Nested scopes with variable declarations | 10 |
| `bare_block_for_in_nothrow` | `for x in xs` in nothrow function | 60 |
| `bare_block_early_drop_no_leak` | RAII drop at block close (alloc_track) | 3 |
| `callable_fn_ptr_throwing_field_nothrow_via_refmut` | Fn-ptr coercion via &mut | 0 |
| `callable_fn_ptr_throwing_field_nothrow_via_branch` | Fn-ptr from phi/branch | 0 |

Removing the flag from these cases is a no-op for the in-process runner and
un-skips them in the PEX runner (where they will enter the compile+run path
and pass normally).

## Questions Answered

**Are these cases semantically representable in `--json` output today?**
Yes, all 9 diagnostic cases. The 6 success cases don't need `--json` at all.

**Would conversion be straightforward?**
Yes — delete one line (`"use_driftc_json": false`) from each expected.json.
No expectation reshaping needed. The `diagnostics` arrays are already in the
correct `[{message_contains, phase}]` format.

**Are any kept non-JSON only because of harness history?**
All 15. None reflect a real need.

**Is there a coherent reason to preserve a non-JSON subset?**
No. There is no human-CLI-surface behavior being tested that differs from the
`--json` output. These are not smoke tests of the non-JSON error rendering
pipeline — they assert diagnostic content, not formatting.

## Recommendation

Convert all 15 by removing `"use_driftc_json": false`. This is a one-line
deletion per expected.json file. No fixture source changes needed.
