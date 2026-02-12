# Lock-Free Foundations Work Progress

## Goal

Provide the minimum language+stdlib foundation required to build correct, high-performance lock-free data structures in Drift without relying on runtime-only escape hatches.

This track starts **after current branch close**.

## Why This Track

Current `std.concurrent` atomics are sufficient for counters/flags and some coordination patterns, but not enough for general lock-free containers. Missing pieces include pointer atomics, richer CAS results, explicit fences, and safe memory reclamation support.

## Scope (Foundations Only)

1. Atomic pointer/reference primitives.
2. CAS result shape improvements (observed value on failure).
3. Explicit fence intrinsics/API.
4. One reclamation strategy suitable for stdlib lock-free structures (epoch-based first).
5. One small proving structure (MPSC queue) as integration target.

## Non-Goals (This Track)

1. Full lock-free map/set implementations.
2. Multiple reclamation schemes in one pass.
3. Aggressive perf tuning before correctness/coverage is pinned.

## Pinned Direction

### A) Atomic coverage

- Add atomics for pointer-like carriers (or typed pointer wrappers) with load/store/exchange/CAS.
- Keep memory-order surface aligned with existing `MemoryOrder`.
- Avoid hidden compiler magic in user APIs.

### B) CAS result contract

- Add/upgrade CAS API to return enough information for lock-free retry loops:
  - success flag
  - observed/current value on failure
- Keep existing bool-return CAS available as ergonomic wrapper where useful.

### C) Fences

- Add explicit fence API in `std.concurrent`/`std.sync` backed by `lang.atomic` intrinsics.
- Support at least `Acquire`, `Release`, `AcqRel`, `SeqCst` where valid.

### D) Reclamation

- Implement epoch-based reclamation first (global epoch + thread participation + deferred retire lists).
- Define ownership/lifetime contracts so retired nodes are reclaimed safely without UAF.
- Keep API machine-oriented and minimal.

### E) Initial proving target

- Implement a bounded or unbounded MPSC queue (choose one during API pin) using the above primitives.
- This serves as end-to-end proof that foundations are sufficient.

## Regression-First Plan

### Phase 1: Atomics surface regressions

1. Add e2e tests that require pointer atomics (compile + runtime behavior).
2. Add negative tests for invalid memory-order combos where applicable.
3. Add checker/codegen tests for CAS observed-value contract.

### Phase 2: Fence semantics regressions

1. Message-passing style e2e cases that require fences (not just atomic RMW).
2. Stress loops to catch reordering-sensitive bugs.

### Phase 3: Reclamation regressions

1. Deterministic retire/reclaim tests in single-threaded simulated schedules.
2. Multithread stress with alloc tracking + ASan gates.
3. Leak/UAF regressions pinned in e2e.

### Phase 4: MPSC proving target

1. Functional tests (ordering, progress, close/shutdown behavior).
2. Contention stress (many producers, one consumer).
3. Memory safety sweeps (alloc tracking + ASan).

## Validation Matrix

1. Unit/driver:
   - API typing/inference/coercion for new atomics primitives.
   - Intrinsic lowering/ABI checks.
2. Codegen e2e:
   - correctness under normal mode.
   - ASan mode (`DRIFT_ASAN=1`).
   - alloc tracking mode (`DRIFT_ALLOC_TRACK=1`).
3. Optional deep diagnostics:
   - valgrind memcheck/massif for selected stress cases.

## Open Pins (To Finalize At Track Start)

1. Exact pointer-atomic type shape:
   - dedicated `AtomicPtr<T>` vs generic atomic carrier.
2. CAS API naming and return type:
   - tuple-like struct vs variant result.
3. Fence API placement:
   - `std.concurrent` only vs shared with `std.sync`.
4. Reclamation API surface:
   - epoch guard ergonomics and retire callback model.
5. First proving queue shape:
   - bounded ring vs linked-list MPSC.

## Exit Criteria

1. Pointer atomics + fences + rich CAS are implemented and documented.
2. Epoch-based reclamation is implemented with deterministic safety tests.
3. MPSC queue passes correctness + stress + memory safety gates.
4. No skipped/placeholder tests in the new lock-free suite.

