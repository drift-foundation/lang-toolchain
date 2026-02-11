# Stdlib JSON Work Progress

## Goal

Provide a first-class `std.json` library in Drift that supports deterministic, machine-friendly JSON handling and unblocks logger formatter migration away from runtime-only JSON helpers.

## Dependency Position

- This track runs after foundational scalar parsers land in `std.parse`.
- Logger finalization depends on this track plus atomics.

## MVP Scope

1. JSON node model in Drift (`Null`, `Bool`, `Number`, `String`, `Array`, `Object`).
2. Deterministic JSON encoding API.
3. JSON parsing API with clear error surface.
4. Deterministic object key ordering policy for encoded output.
5. Navigation/extractor APIs (`get`, `get_path`, `entries`, `as_*`, `expect_*`).
6. Bridge helpers for logging attrs conversion.

## Non-Goals (MVP)

- Streaming parser/encoder.
- JSON schema validation.
- JSONPath/query language.

## Draft API Surface

- Module: `std.json`
- Core node: `JsonNode` variant.
- Encode:
  - `encode(value: &JsonNode) -> String`
  - `encode_compact(value: &JsonNode) -> String`
- Parse:
  - `parse(text: &String) -> Result<JsonNode, JsonError>`
- Access:
  - `get(key: String) -> Optional<&JsonNode>`
  - `get_path(path: Array<String>) -> Optional<&JsonNode>` (key segments only in MVP)
  - `entries() -> JsonEntriesIter` (empty iteration for non-object)
  - `is_null() -> Bool`
- Extractors:
  - safe probes: `as_bool/as_int/as_float/as_decimal/as_string/as_array/as_object -> Optional<...>`
  - strict extractors: `expect_bool/expect_int/expect_float/expect_decimal/expect_string/expect_array/expect_object`
  - `as_*` returns `Optional::None` on type mismatch, including `Null`
  - no `as_null()` in MVP
- Object helpers:
  - deterministic insertion/iteration contract for encoder output.

## Semantics Pin

- Encoder output must be deterministic for the same logical `JsonValue`.
- Duplicate object keys on parse: keep-last (later key overwrites earlier key).
- Object encoding supports config policy:
  - `unordered` (default)
  - `ordered_lex_utf8` for canonical/signing use cases.
- `get_path` is key-only in MVP; index traversal is via `as_array()/at(...)`, not path segments.
- Parse errors are structured and non-panicking.
- No hidden runtime formatting side effects.
- Strict extractor failures throw `std.json:JsonError` (single event type).
- `JsonError` carries machine tags:
  - required `tag` field in kebab-case (example: `invalid-datatype`)
  - stable, append-only tag namespace
  - optional structured context (`offset`, `line`, `col`, `path`, `key`, `expected`, `actual`, ...).

## Regression-First Test Plan

### Unit

1. Primitive round-trips (`null`, bool, number, string).
2. String escaping correctness.
3. Array/object nested encode/decode.
4. Deterministic object key order in output.

### Negative

1. Invalid JSON syntax diagnostics.
2. Invalid escape sequence handling.
3. Number format rejection cases.

### E2E / integration

1. Logger-targeted payload samples round-trip through `std.json`.
2. Stable snapshot tests for encoded outputs.

## Completed Since Plan Start

1. Core `JsonNode` model and encoder landed.
2. Parse API landed with structured `JsonErrorData` and machine-tag surface.
3. Duplicate-key parse policy pinned and implemented as keep-last.
4. Ordered object encoding (`OrderedLexUtf8`) implemented.
5. Parse error position (`offset`, `line`, `col`) implemented and covered.
6. JSON string encoder control-char escaping fixed to valid JSON behavior.
7. Non-finite JSON numbers are rejected in parser (JSON-compliant).
8. `entries()` landed via `JsonEntriesIter` with empty semantics for non-object.
9. Compiler regression uncovered and fixed in MIR array ownership join (`LoadLocal` -> `MoveOut`) that caused JSON parse double-free on error paths.
10. LANGUAGE_BUG fixed in LLVM lowering: variant `DropValue` no longer injects inline CFG labels that corrupt PHI predecessors; lowering now routes variant drop through helper call path.
11. Alloc-tracking hardening for assert/abort paths: assert runtime now emits alloc stats before abort to keep leak-signal tests reliable.
12. Runtime lifecycle hardening landed for leak sweeps:
   - logger worker + queue teardown at exit
   - default reactor teardown at exit
   - virtual-thread registry cleanup at exit
   - cancel/join race fix for prestart queued tasks
13. `VirtualThread<T>` destructor semantics added so dropped-but-unjoined threads release handles/result buffers deterministically.
14. LANGUAGE_BUG fixed in match lowering cleanup: non-Copy match binders now register for scope-based drops, so early-return match arms no longer leak moved payloads (pinned by `std_json_parse_duplicate_only_no_access` under alloc tracking).
15. Interface-owned callback lifetime fix landed:
   - interface runtime-drop participation re-enabled in stage2 drop planning
   - iface-init MIR validation aligned with canonicalized param/local names and hidden lambda callback locals
   - pinned callback-env leak (`result_on_error_capture`) now passes with `DRIFT_ALLOC_TRACK=1`.
16. Move-capture double-drop LANGUAGE_BUG fixed in lambda lowering:
   - lambda capture prologue no longer registers scope drops for `captures(move ...)` locals
   - ownership stays with callback env drop thunk, preventing double-free in callback/concurrency paths (for example `byte_capture_add_uint`).
17. Timeout-path leak regressions (`concurrent_join_timeout_nonzero`, `concurrent_sleep_task_join_timeout_regression`) are now green under `DRIFT_ALLOC_TRACK=1` with runtime cleanup sequencing hardening.
18. Broader deterministic encode snapshot coverage added for deep mixed nested JSON structures with differing insertion order constructions (objects/arrays/null/bool/number/string/empty containers), pinned by `std_json_encode_determinism_deep_mixed_snapshot`.
19. Valgrind validation run for `std_json_encode_determinism_deep_mixed_snapshot` is clean (`in use at exit: 0 bytes`, `ERROR SUMMARY: 0`).
20. LANGUAGE_BUG fixed for `for`-iteration over already-borrowed iterables (`&Array<T>`), which blocked natural JSON usage (`for val item : users` after `expect_array(...)`):
   - added regressions `for_iter_json_expect_array` and `for_iter_ref_array_local`
   - fixed UFCS `for_iter` receiver normalization/coercion for nested refs so lowering no longer passes `&&T` where `&T` is required.
21. Added broader `&Array<JsonNode>` usage-matrix e2e regression (`ref_array_jsonnode_usage_matrix`) covering:
   - direct function calls with `&Array<JsonNode>`
   - nested-ref call coercion (`&&Array<JsonNode>` to `&Array<JsonNode>`)
   - direct call-expression arguments from `expect_array(...)`
   - ref pass-through return from helper
   - iteration via `for val item : users`
   - valgrind memcheck run is clean (`in use at exit: 0 bytes`, `ERROR SUMMARY: 0`).
22. Added dedicated dot-call iterator regression on `&Array<JsonNode>`:
   - `ref_array_dot_iter_next` covers `users.iter()` + `it.next()` flow
   - pinned required trait-scope behavior (`use trait iter.Iterable; use trait iter.SinglePassIterator;`)
   - valgrind memcheck run is clean (`in use at exit: 0 bytes`, `ERROR SUMMARY: 0`).
23. Added ASan execution mode to codegen e2e runner (`DRIFT_ASAN=1`) with:
   - clang/runner sanitizer wiring (`-fsanitize=address -g`)
   - incompatibility guard against valgrind modes (`DRIFT_MEMCHECK`/`DRIFT_MASSIF`)
   - stderr normalization for known `swapcontext` ASan warning noise.
24. Fixed runtime LANGUAGE_BUG in virtual-thread/reactor lifetime handling that caused intermittent `tcache_thread_shutdown(): unaligned tcache chunk detected` under cancel-before-start stress:
   - reactor now forgets vt-owned watches/timers during VT destroy
   - worker completion snapshots dropped state before completion publish
   - executor teardown sequencing avoids stale VT deref windows.
25. Fixed post-join cancel UAF path in `std.concurrent`:
   - `VirtualThread.join` / `join_timeout` clear native handle after join
   - `cancel()` no-ops when joined/handle-cleared.
26. Fixed logger shutdown nondeterminism causing stderr snapshot mismatches:
   - logger worker drains queue before exit on shutdown.
27. Resolved alloc-tracked regressions discovered in broad sweeps (JSON/concurrency/logging-adjacent) and validated targeted clean runs with both allocator tracking and valgrind on representative JSON determinism/iteration cases.

## Current Status

- JSON branch scope is complete for MVP: parse/encode/navigation/error-tag/determinism are implemented and covered.
- Pinned JSON-related LANGUAGE_BUGs are fixed at compiler/runtime root cause with regressions in place.
- Runner diagnostics modes are now documented and operational (`DRIFT_ALLOC_TRACK`, `DRIFT_MEMCHECK`, `DRIFT_MASSIF`, `DRIFT_ASAN`).
- Full-suite runs are green in normal mode; alloc/ASan sweeps are now actionable via env toggles without test harness patching.
- Remaining leak/perf/concurrency improvements are tracked outside this JSON plan and should continue on dedicated branches.

## Logger Migration Hooks (Post-JSON)

- Replace runtime `DiagnosticValue -> JSON` conversion path with Drift-side conversion feeding `std.json`.
- Replace manual JSON string concatenation in `std.log` payload building with typed `JsonValue` construction + `std.json` encode.

## Execution Steps

1. Land `std.parse` foundation first (separate track/module).
2. Add failing tests for `std.json` encode/parse determinism and error surface.
3. Land `JsonNode` + encoder + ordering configuration.
4. Land parser + structured `JsonError` tags + keep-last duplicate handling.
5. Land navigation/extractor APIs (`get`, `get_path`, `entries`, `as_*`, `expect_*`).
6. Add integration tests for logger payload shapes.
7. Keep logger migration itself in logging track, not in this track.

## Exit Criteria

- `std.json` encode/parse APIs are stable and covered.
- Deterministic encoding policy is documented and tested.
- Logger can consume `std.json` for payload/attrs formatting in follow-up migration.

## Remaining JSON Work (Post-MVP / Deferred)

1. Logger-focused integration coverage using real production payloads remains optional and is best owned by the logging track.
2. Any additional API polish (`entries` aliasing, extra helpers) is deferred until real-user feedback indicates need.
