# Logger Work Progress

## Status Summary

- This track is split into:
  1. interface + behavior baseline (completed),
  2. mechanics migration to pure Drift (deferred until atomics/memory-ordering lands).

## Completed (Pinned + Implemented)

- [x] MVP levels pinned: `debug`, `info`, `error`.
- [x] Global default logger plus named/sub-loggers pinned (`main` default).
- [x] Builder surface pinned and implemented:
  - `sink(...)`
  - `min_level(...)`
  - `queue_capacity(...)`
  - `write_timeout(...)`
  - `enqueue_timeout(...)`
  - `backpressure_policy(...)`
  - `build()`
- [x] Backpressure policy pinned (`BlockWithTimeout`, `DropOldest`, `DropNewest`), default = block.
- [x] Event-first API pinned and implemented for MVP shape:
  - `log.info(ev, attrs)` / `debug` / `error`
  - attrs passed as typed map/object value.
- [x] Attr literal dependency unblocked:
  - map literals landed for logging path,
  - empty map spelling pinned as `{:}` (with explicit type context required).
- [x] `Debuggable` trait contract pinned and used for attrs value conversion.
- [x] `std.meta.caller()` pinned as compiler primitive for optional source injection (no automatic compiler magic in MVP).
- [x] Default logger sink pinned to `stderr`.
- [x] Lifecycle pinning done:
  - init idempotent for same config,
  - re-init with different config rejected,
  - no shutdown API in MVP.
- [x] Interface-owned storage blocker resolved (constructor/assignment coercion), unblocking sink interface usage patterns.
- [x] Runtime-backed emission now produces structured JSON records including:
  - `tm`, `level`, `ev`, `logger`, `attrs`, `tid`.
- [x] E2E deterministic masking support added (`stderr_jsonl` + `__ANY__`) for non-deterministic fields (`tm`, `tid`).
- [x] Regression coverage in place (driver + `std_log_*` e2e suite currently green).

## Intentionally Deferred Until After Atomics/Memory-Ordering

- [ ] Move logger queue/backpressure core from runtime C to Drift atomics-based implementation.
- [ ] Move worker dequeue coordination/orchestration to Drift (retain only minimal OS primitives as intrinsics).
- [ ] Remove logger-specific queue policy logic from runtime C.
- [ ] Replace temporary runtime `DiagnosticValue -> JSON` conversion path with Drift-side formatter path.
- [ ] Finalize full custom sink contract behavior on pure Drift mechanics (ownership/move semantics are pinned; backend mechanics migration remains).

## Still Open (Post-Atomics Logger Follow-Ups)

- [ ] Add/refresh examples under `lang/examples/logging/`.
- [ ] Add effective-drift book entry for final logger API and patterns.
- [ ] Add user-pluggable formatter example (default remains JSON ISO timestamp format).
- [ ] Add/expand sink examples (file/fanout/custom sink composition).

## Resume Point

- Active next work is in `work/atomics-memory-ord/work-progress.md`.
- Once atomics MVP is complete and tested, resume logger at:
  1. Drift-side queue state,
  2. Drift-side producer backpressure policies,
  3. Drift-side worker coordination,
  4. runtime logger-path removal.
