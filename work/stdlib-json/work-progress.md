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
5. Bridge helpers for logging attrs conversion.

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
  - `entries() -> Iterator<(String, &JsonNode)>` (empty iteration for non-object)
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
