# std.time MVP (UTC-only)

Status: planned (next focus)
Scope: minimal, systems-first, deterministic UTC support for stdlib and logger integration.
Non-goals: timezone database, locale formatting, offset conversions, calendaring beyond strict ISO UTC.

## Goals

- Provide a small, stable `std.time` API for:
  - monotonic timing (timeouts/retries/latency)
  - UTC wall-clock timestamps (logs/events)
  - strict ISO-8601 UTC parse/format for interchange
- Keep behavior explicit and predictable across platforms.
- Unblock logger evolution without adding compiler magic.

## Pinned API (MVP)

Module: `stdlib/std/time/time.drift`

### Types

- `pub struct Instant { ... }` (opaque monotonic timestamp)
- `pub struct UtcTimestamp { ... }` (opaque UTC wall-clock timestamp)
- `pub struct TimeParseError { tag: String, offset: Int }`

### Functions

- `pub fn now_monotonic() nothrow -> Instant`
- `pub fn elapsed_ms(start: &Instant) nothrow -> Int`
- `pub fn now_utc() nothrow -> UtcTimestamp`
- `pub fn duration_ms_between(a: &UtcTimestamp, b: &UtcTimestamp) nothrow -> Int`
- `pub fn format_iso8601_utc(ts: &UtcTimestamp) nothrow -> String`
- `pub fn parse_iso8601_utc(s: String) nothrow -> core.Result<UtcTimestamp, TimeParseError>`

## ISO Parse/Format Contract

### Accepted parse forms

- `YYYY-MM-DDTHH:mm:ssZ`
- `YYYY-MM-DDTHH:mm:ss.sssZ`

### Rejected in MVP

- timezone offsets (`+02:00`, `-0500`, etc.)
- local/naive timestamps (no `Z`)
- alternate separators/formats
- fractional precision other than exactly 3 digits when present

### Output format

- `format_iso8601_utc` emits canonical UTC form with milliseconds:
  - `YYYY-MM-DDTHH:mm:ss.sssZ`

## Error Tags (structured, no prose)

- `invalid-syntax`
- `invalid-range`
- `invalid-utc-designator`
- `unsupported-offset`

`offset` points to the first offending byte when available; otherwise `-1`.

## Runtime/Platform Notes

- Monotonic and UTC retrieval come from runtime shims (POSIX first).
- `Instant` math must not depend on wall-clock adjustments.
- `UtcTimestamp` uses Unix epoch milliseconds internally (implementation detail).

## Integration with Logger

- Current logger timestamp path remains runtime-driven for now.
- After `std.time` MVP lands and is validated, logger can optionally route `tm` formatting through `std.time::format_iso8601_utc` to converge behavior.
- No compiler magic required.

## Test Matrix (before logger migration)

### Unit

- ISO parser:
  - accepts pinned valid forms
  - rejects non-UTC/offset/local forms
  - rejects malformed/range-invalid fields
  - validates error tags + offsets
- ISO formatter:
  - canonical output shape with `Z` and `.sss`
- Roundtrip:
  - `parse(format(ts))` stability for representative values

### E2E

- smoke: `now_utc()` + format returns valid canonical string
- parse known valid sample strings
- reject known invalid strings (offset/local forms)
- monotonic elapsed non-negative progression

### Driver

- API shape compile tests for `std.time`

## Implementation Phases

1. API skeleton + type definitions + driver compile tests.
2. Runtime hooks for monotonic/utc now + basic wrappers.
3. ISO format implementation.
4. ISO parse implementation (strict forms only) + error tags.
5. Unit/e2e hardening and deterministic expectations.
6. Logger follow-up branch: adopt `std.time` path where appropriate.

## Out of Scope (explicit)

- timezone name/region support
- DST/calendar arithmetic helpers
- RFC 2822 or other date-time formats
- leap-second handling beyond platform/runtime behavior

## Branching/Workflow

- Work on dedicated `time` branch.
- Regression-first for parser/formatter bugs.
- Keep logger mechanics unchanged until `std.time` is stable.
