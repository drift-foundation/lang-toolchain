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

## Current Status

- MVP JSON APIs are in place and green on targeted parse/encode/e2e coverage.
- Previously pinned JSON leak/crash regressions are resolved in current branch state.
- Alloc-tracked sampled sweep (JSON + concurrency + logging-adjacent cases) is green after runtime/codegen fixes, including prior timeout leak and callback double-free repros.
- One environment-sensitive test remains outside JSON scope (`std_net_tcp_read_write_roundtrip` can fail with listen error in restricted environments).
- Full-suite alloc-tracking confirmation remains pending user run (`DRIFT_ALLOC_TRACK=1 just`).

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

## Remaining JSON Work (Post-MVP / Pending)

1. Add logger-focused integration coverage using real log payload shapes (attrs/event fields).
2. Add broader encode determinism snapshots for complex nested objects/arrays. ✅
3. Optional API polish:
   - decide whether `entries()` should expose a dedicated item type alias in `std.json` for nicer ergonomics.
   - decide whether any additional navigation helpers are needed beyond pinned MVP (`get_path` key-only + `as_array` index navigation).
4. Documentation pass in effective-drift for final `std.json` surface and error-tag contract.
