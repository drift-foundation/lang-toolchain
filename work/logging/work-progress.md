# Logging Work Progress

## Status

Logging interface baseline is in place and tested. Final logger internals are intentionally blocked on two prerequisite tracks.

## Hard Dependencies

1. `work/atomics-memory-ord/work-progress.md`
2. `work/stdlib-json/work-progress.md`

Logging migration/finalization starts only after both are complete.

## Completed Baseline

- MVP API surface available (`log.debug/info/error(ev, attrs)`, builders, logger derivation, flush/init behavior).
- Structured JSON emission present in current runtime-backed path.
- Logger e2e/driver coverage in place, including nondeterministic field masking (`tm`, `tid`).

## Deferred Until Dependencies Complete

- Move queue/backpressure orchestration to Drift atomics.
- Move worker coordination to Drift.
- Remove logger-specific runtime queue policy code.
- Replace runtime `DiagnosticValue -> JSON` helper with Drift-side JSON path built on `std.json`.
- Finalize custom sink mechanics on pure Drift backend.

## Resume Plan (After Both Dependencies)

1. Port queue state and producer policies to Drift atomics.
2. Switch payload construction to `std.json` value + encode.
3. Port worker loop/orchestration to Drift.
4. Remove logger-specific runtime scaffolding.
5. Re-run full logger regression matrix and parity gates.

## Parity Gates

- Output JSON shape unchanged: `tm`, `level`, `ev`, `logger`, `attrs`, `tid`.
- Level filtering unchanged.
- Backpressure semantics unchanged.
- Flush/drain guarantees unchanged.
