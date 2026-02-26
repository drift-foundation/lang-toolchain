# Plan: std.time Unix Epoch Accessors for UtcTimestamp (JWT NumericDate unblock)

Date: 2026-02-25
Owner: stdlib/compiler team
Status: completed (2026-02-25)

## 1) Goal
Expose public epoch accessors in `std.time` so user-space JWT libraries can source NumericDate (`exp`/`nbf`/`iat`) from stdlib without private/internal APIs.

## 2) Requested MVP API
Add to `stdlib/std/time/time.drift` exports and implementation:

1. `pub fn utc_unix_seconds(ts: &UtcTimestamp) nothrow -> Int`
2. `pub fn utc_unix_seconds_now() nothrow -> Int` (convenience)

Optional (same batch if low-risk):
- `pub fn utc_unix_millis(ts: &UtcTimestamp) nothrow -> Int`

## 3) Semantics contract
- UTC only.
- Return type: signed `Int`.
- Conversion must be deterministic:
  - `utc_unix_seconds(ts)` = integer truncation toward zero of epoch-milliseconds / 1000
  - preserve pre-1970 behavior via signed arithmetic
- `nothrow`.

## 4) Compatibility / design notes
- Do not expose mutable internals of `UtcTimestamp`; accessor-only surface.
- Keep behavior stable across target word sizes as far as `Int` range allows.
- Avoid timezone/localtime coupling; this is pure UTC epoch extraction.

## 5) Implementation steps
1. Inspect current `UtcTimestamp` representation in `std.time`.
2. Add public accessor(s) in `std.time`.
3. Add export entries for new functions.
4. Implement `utc_unix_seconds_now()` as `utc_unix_seconds(&now_utc())` (or equivalent with minimal copies).
5. If adding millis accessor, ensure naming/docs clearly indicate epoch milliseconds.

## 6) Regression-first test matrix
Add e2e tests under `lang/tests/codegen/e2e/` (and unit/driver if useful):

### Positive tests
1. `utc_unix_seconds` on known fixed timestamp value gives expected integer.
2. `utc_unix_seconds_now()` returns a plausible non-zero epoch value.
3. (Optional) `utc_unix_millis` agrees with `utc_unix_seconds * 1000` bounds relationship.

### Edge/contract tests
4. Pre-1970 timestamp conversion uses signed result and truncates toward zero correctly.
   - Example shape: `-1500 ms -> -1 sec` (toward zero), not floor-to-`-2`.
5. Large timestamp round-trip sanity within Int range.

### API surface tests
6. Public export resolution test from external module (`import std.time as time; time.utc_unix_seconds(...)`).

## 7) Documentation updates
- Update `docs/effective-drift.md` (or relevant std.time section) with examples:
  - `val now_s = time.utc_unix_seconds_now();`
  - JWT call-site example using `now_s`.
- Add history entry once landed.

## 8) Definition of done
- New API exported and callable from user code.
- Tests covering positive + edge semantics are green.
- Docs/history updated.
- JWT team can source NumericDate from stdlib without workarounds.

## 9) Out of scope
- Full date/time calendar formatting APIs.
- Timezone conversions.
- JWT logic itself (belongs to user-space `web.auth.jwt`).

## 10) Implementation outcome (completed)
- Added public `std.time` accessors in `stdlib/std/time/time.drift`:
  - `utc_unix_seconds(ts: &UtcTimestamp) nothrow -> Int`
  - `utc_unix_seconds_now() nothrow -> Int`
  - `utc_unix_millis(ts: &UtcTimestamp) nothrow -> Int` (optional companion, included)
- Export surface updated accordingly.
- Semantics match plan:
  - UTC epoch-only access
  - signed `Int` return
  - deterministic integer conversion (`epoch_ms / 1000`, truncation toward zero)
- Added regression coverage:
  - `lang/tests/codegen/e2e/std_time_epoch_accessors/`
  - covers fixed known timestamp, `now()` plausibility, sub-second truncation, pre-1970 negative timestamp truncation, and epoch boundary zero case.
