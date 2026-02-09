# std.time MVP (UTC-only)

Status: in progress (phases 1-4 completed)
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

1. API skeleton + type definitions + driver compile tests. (completed)
2. Runtime hooks for monotonic/utc now + basic wrappers. (completed)
3. ISO format implementation. (completed)
4. ISO parse implementation (strict forms only) + error tags. (completed)
5. Unit/e2e hardening and deterministic expectations. (in progress)
6. Logger follow-up branch: adopt `std.time` path where appropriate.

## Completed in this phase

- Added `stdlib/std/time/time.drift` skeleton with pinned public API.
- Added driver API compile test: `lang/tests/driver/test_std_time_api.py`.
- Added e2e monotonic smoke: `lang/tests/codegen/e2e/std_time_monotonic_smoke/`.
- Added dedicated UTC runtime intrinsic path:
  - `lang.thread.now_utc_ms()`
  - LLVM lowering for `now_utc_ms`
  - POSIX runtime `drift_time_now_utc_ms()` (`CLOCK_REALTIME`)
- Implemented strict UTC ISO formatting in `std.time::format_iso8601_utc`:
  - canonical `YYYY-MM-DDTHH:mm:ss.sssZ`
  - Gregorian civil date conversion from epoch milliseconds
- Implemented strict UTC ISO parsing in `std.time::parse_iso8601_utc`:
  - accepts `YYYY-MM-DDTHH:mm:ssZ` and `YYYY-MM-DDTHH:mm:ss.sssZ`
  - enforces pinned error tags:
    - `invalid-syntax`
    - `invalid-range`
    - `invalid-utc-designator`
    - `unsupported-offset`
- Added e2e coverage:
  - `lang/tests/codegen/e2e/std_time_iso_parse_format/`
  - `lang/tests/codegen/e2e/std_time_iso_parse_invalid/`
- Added deep hardening e2e coverage:
  - `lang/tests/codegen/e2e/std_time_iso_parse_error_offsets/` (tag+offset pinning for malformed/range/offset cases)
  - `lang/tests/codegen/e2e/std_time_iso_valid_corpus/` (broad valid parse/format roundtrip corpus)
  - `lang/tests/codegen/e2e/std_time_iso_duration_edges/` (duration invariants across day/month/leap/year boundaries)
  - `lang/tests/codegen/e2e/std_time_iso_negative_epoch/` (pre-epoch formatting and signed duration behavior)
  - `lang/tests/codegen/e2e/std_time_iso_century_leap_rules/` (Gregorian century leap rules: 1900/2000/2100/2400)
  - `lang/tests/codegen/e2e/std_time_iso_random_corpus/` (fixed-seed high-volume randomized corpus, valid roundtrip + invalid non-leap Feb-29)
- Added minimal Date API support in `std.time`:
  - `Date { year, month, day }`
  - `is_leap_year`, `days_in_month`, `is_valid_date`
  - `format_iso8601_date`, `parse_iso8601_date`
- Added Date coverage:
  - `lang/tests/codegen/e2e/std_time_date_parse_format/`
  - `lang/tests/codegen/e2e/std_time_date_invalid_offsets/`
  - `lang/tests/driver/test_std_time_date_api.py`

## Out of Scope (explicit)

- timezone name/region support
- DST/calendar arithmetic helpers
- RFC 2822 or other date-time formats
- leap-second handling beyond platform/runtime behavior

## Branching/Workflow

- Work on dedicated `time` branch.
- Regression-first for parser/formatter bugs.
- Keep logger mechanics unchanged until `std.time` is stable.
