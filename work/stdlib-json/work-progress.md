# Stdlib JSON Work Progress

## Goal

Provide a first-class `std.json` library in Drift that supports deterministic, machine-friendly JSON handling and unblocks logger formatter migration away from runtime-only JSON helpers.

## Dependency Position

- This track runs after: `work/atomics-memory-ord/work-progress.md`.
- Logger finalization depends on this track plus atomics.

## MVP Scope

1. JSON value model in Drift (`Null`, `Bool`, `Number`, `String`, `Array`, `Object`).
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
- Core value: `JsonValue` variant.
- Encode:
  - `encode(value: &JsonValue) -> String`
  - `encode_compact(value: &JsonValue) -> String`
- Parse:
  - `parse(text: &String) -> Result<JsonValue, JsonError>`
- Object helpers:
  - deterministic insertion/iteration contract for encoder output.

## Semantics Pin

- Encoder output must be deterministic for the same logical `JsonValue`.
- Object encoding uses a pinned key order policy (final choice to be documented before implementation; default target: lexical UTF-8 byte order).
- Parse errors are structured and non-panicking.
- No hidden runtime formatting side effects.

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

1. Add failing tests for encode/parse determinism and error surface.
2. Land `JsonValue` + encoder.
3. Land parser + structured errors.
4. Add integration tests for logger payload shapes.
5. Keep logger migration itself in logging track, not in this track.

## Exit Criteria

- `std.json` encode/parse APIs are stable and covered.
- Deterministic encoding policy is documented and tested.
- Logger can consume `std.json` for payload/attrs formatting in follow-up migration.
