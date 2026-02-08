# Atomics + Memory Ordering Work Progress

## Goal

Enable first-class atomics and explicit memory ordering in Drift so core systems (starting with logger) can be implemented in Drift rather than delegated to runtime C logic.

## Why This Track

- Logger queue/sink mechanics are currently runtime-backed.
- Real apps need lock-free/low-contention primitives in language space.
- We need a stable, testable platform for concurrent stdlib facilities (logging, reactors, pools, caches).

## Scope (MVP)

1. Language + type system support for atomic primitives.
2. Stdlib API surface for atomics with explicit memory orders.
3. Codegen/runtime ABI support for atomic ops.
4. Regression-first tests for semantics and concurrency.
5. Logger migration from runtime-backed queue mechanics to Drift atomics implementation.

## Non-Goals (This MVP)

- Full lock-free container library.
- Advanced wait-free algorithms.
- Cross-process/shared-memory atomics.

## API Pin (Draft for Implementation)

### Atomic types

- `std.sync.AtomicBool`
- `std.sync.AtomicInt`
- `std.sync.AtomicUint`
- `std.sync.AtomicUint64`

### Memory order enum

- `std.sync.MemoryOrder`:
  - `Relaxed`
  - `Acquire`
  - `Release`
  - `AcqRel`
  - `SeqCst`

### Core operations

- `load(order) -> T`
- `store(value, order) -> Void`
- `exchange(value, order) -> T`
- `compare_exchange(expected, desired, success_order, failure_order) -> Bool`
- `fetch_add(delta, order) -> T` (where applicable)
- `fetch_sub(delta, order) -> T` (where applicable)

### Construction

- `AtomicX::new(init)` constructor form.

## Semantics Pin

- Atomic ops are non-throwing.
- Ordering validity checks enforced (compile-time if possible, otherwise deterministic checker error).
- `compare_exchange` follows standard success/failure ordering constraints.
- No hidden default ordering in APIs; order must be explicit in MVP.

## Regression-First Test Plan

### Unit / checker

1. Reject invalid ordering combinations for `compare_exchange`.
2. Reject unsupported arithmetic ops on non-numeric atomics.
3. Ensure atomic methods are resolved with correct signatures.

### Codegen/runtime

1. Emit proper atomic IR/runtime calls for each op.
2. Validate type widths/layouts for `Atomic*` payloads.

### E2E concurrency

1. Multi-thread counter increment correctness (`fetch_add`).
2. CAS loop correctness under contention.
3. Message-passing pattern using `store(Release)` + `load(Acquire)`.
4. SeqCst determinism sanity test.

## Logger Migration Plan (After Atomics MVP Lands)

### Phase 1: Drift-side queue state

- Move queue depth/indices/drop counters to Drift atomics.
- Keep sink emission path runtime-backed temporarily.
- Prove parity against current logger e2e behavior.

### Phase 2: Drift-side producer policy

- Implement `block_with_timeout` / `drop_oldest` / `drop_newest` in Drift using atomics + existing wait primitives.
- Preserve non-throwing producer API semantics.

### Phase 3: Drift-side worker coordination

- Move worker dequeue orchestration into Drift.
- Keep only minimal OS thread/condvar primitives as intrinsics.

### Phase 4: Remove logger-specific runtime scaffolding

- Delete logger queue logic from `thread_runtime.c`.
- Keep generic thread/time/io intrinsics only.
- Re-run full logger matrix and targeted driver suites.

## Execution Steps

1. Add failing tests for atomic API + ordering constraints (unit/checker/e2e).
2. Implement type+checker surface for `Atomic*` and `MemoryOrder`.
3. Implement MIR/codegen/runtime support for atomic instructions.
4. Make tests pass for atomics MVP.
5. Start logger Phase 1 migration with parity tests first.
6. Continue through logger Phases 2-4 with no user-visible behavior regression.

## Parity Gates During Logger Migration

- JSON shape unchanged: `tm`, `level`, `ev`, `logger`, `attrs`, `tid`.
- Level filtering behavior unchanged.
- Backpressure policy outcomes unchanged.
- `flush(timeout)` guarantees unchanged.

## Exit Criteria

- Atomics MVP available in Drift stdlib with ordering controls.
- Logger queue/backpressure/worker internals moved to Drift.
- Logger runtime C path reduced to generic primitives only (no logger-specific queue policy logic).
- Existing logger e2e + driver suites remain green.
