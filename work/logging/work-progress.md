# Logging Work Progress

## Status

This is the single logger plan file.

Logger interface and baseline behavior are already in place and tested.
Remaining work is backend migration after atomics + std.json are complete.

## Completed Baseline (Pinned + Landed)

- MVP levels: `debug`, `info`, `error`.
- Default logger is `main`.
- Builder surface available:
  - `sink(...)`
  - `min_level(...)`
  - `queue_capacity(...)`
  - `write_timeout(...)`
  - `enqueue_timeout(...)`
  - `backpressure_policy(...)`
  - `build()`
- Backpressure policies pinned:
  - `BlockWithTimeout` (default)
  - `DropOldest`
  - `DropNewest`
- Event-first API available:
  - `log.debug(ev, attrs)`
  - `log.info(ev, attrs)`
  - `log.error(ev, attrs)`
- Attr map literal path available for logging, including empty map `{:}` with explicit type context.
- `Debuggable` conversion path pinned for attr values.
- `std.meta.caller()` pinned as optional source helper (no automatic compiler injection in MVP).
- Lifecycle pinning:
  - init idempotent for same config,
  - re-init with different config rejected,
  - no shutdown API in MVP.
- Structured JSON output currently emitted in runtime-backed path with:
  - `tm`, `level`, `ev`, `logger`, `attrs`, `tid`.
- Test support for nondeterministic fields is in place (`stderr_jsonl`, `__ANY__`).

## Dependencies Before Logger Backend Migration

1. Atomics + memory ordering track complete.
2. `std.json` track complete.

## Backend Pin (Updated)

- Logger backend is lock-free-first and uses `std.sync::MpscQueue` as the transport queue.
- This is an intentional stress path to exercise compiler/runtime with realistic concurrent load.
- Any discovered compiler/runtime defect follows regression-first LANGUAGE_BUG handling (no stdlib masking workaround).

## Work To Resume After Dependencies

1. Replace logger queue path with `std.sync::MpscQueue` (producer enqueue + single-consumer worker dequeue).
2. Integrate backpressure policy semantics on top of bounded MPSC behavior:
   - `BlockWithTimeout` (default),
   - `DropOldest`,
   - `DropNewest`.
3. Switch payload construction/encoding to `std.json`.
4. Port worker loop/orchestration to Drift around MPSC.
5. Remove logger-specific runtime queue/policy scaffolding.
6. Re-run full logger regression matrix and parity gates.

## Deferred Follow-Ups (After Migration)

- Refresh/add examples under `lang/examples/logging/`.
- Update effective-drift logger entry to final API/behavior.
- Add user-pluggable formatter example.
- Add sink composition examples (file/fanout/custom sink).

## Parity Gates

- Output JSON shape unchanged: `tm`, `level`, `ev`, `logger`, `attrs`, `tid`.
- Level filtering unchanged.
- Backpressure semantics unchanged.
- Flush/drain guarantees unchanged.
