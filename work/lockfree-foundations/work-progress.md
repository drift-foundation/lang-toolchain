# Lock-Free Foundations Work Progress

## Goal

Provide the minimum language+stdlib foundation required to build correct, high-performance lock-free data structures in Drift without relying on runtime-only escape hatches.

This track starts **after current branch close**.

## Why This Track

Current atomics now cover counters/flags and typed handle CAS loops (`AtomicHandle<T>` + observed-CAS), but foundations are still incomplete for general lock-free containers. Remaining missing pieces are explicit fences and a restricted atomic-reference model with safe reclamation support.

## Scope (Foundations Only)

1. Restricted atomic-reference primitives (`RefToken`/`AtomicRef` model; not plain `&T`).
2. Explicit fence intrinsics/API.
3. One reclamation strategy suitable for stdlib lock-free structures (epoch-based first).
4. One small proving structure (MPSC queue) as integration target.

## Non-Goals (This Track)

1. Full lock-free map/set implementations.
2. Multiple reclamation schemes in one pass.
3. Aggressive perf tuning before correctness/coverage is pinned.

## Pinned Direction

### A) Atomic coverage

- `AtomicHandle<T>` is in place as a typed pointer-like carrier over `Uint` with load/store/exchange/CAS/observed-CAS.
- Next is restricted atomic reference support, explicitly *not* ordinary `&T`.

### B) CAS result contract

- Implemented: observed-CAS API returns the observed/current value for retry loops.
- Existing bool-return CAS remains available as wrapper API.

### C) Restricted AtomicRef Model

- `AtomicRef<T>` must not be ordinary `&T` (hard rule).
- Use token/guard model:
  - opaque `RefToken<T>` carrier
  - atomic ops over tokens (`load/store/exchange/CAS/observed-CAS`)
  - guard-gated read access (`load_ref(..., &guard)`)
  - no `&mut T` derivation from atomic token loads
- Enforce boundaries in checker/type system (no implicit token <-> reference coercions).

### D) Fences

- Add explicit fence API in `std.sync`/`lang.atomic` intrinsics.
- Support at least `Acquire`, `Release`, `AcqRel`, `SeqCst` where valid.

### E) Reclamation

- Implement epoch-based reclamation first (global epoch + thread participation + deferred retire lists).
- Define ownership/lifetime contracts so retired nodes are reclaimed safely without UAF.
- Keep API machine-oriented and minimal.

### F) Initial proving target

- Implement a bounded or unbounded MPSC queue (choose one during API pin) using the above primitives.
- This serves as end-to-end proof that foundations are sufficient.

## Current Progress (Pinned)

1. Implemented and validated:
   - observed-CAS for bool/int/uint/uint64 (`lang.atomic`, `std.sync`, runtime, llvm codegen)
   - `Handle<T>` and `AtomicHandle<T>` in `std.sync`
   - explicit fence APIs:
     - `lang.atomic.thread_fence(order)` / `lang.atomic.signal_fence(order)`
     - `std.sync.thread_fence(order)` / `std.sync.signal_fence(order)`
     - runtime + LLVM codegen wiring (`drift_atomic_thread_fence` / `drift_atomic_signal_fence`)
2. Lock-free viability probes in e2e:
   - `lockfree_mpsc_probe_index_handle_smoke`
   - `lockfree_mpsc_probe_tagged_handle_smoke`
   - `lockfree_mpsc_probe_atomic_handle_smoke`
   - `lockfree_mpsc_probe_atomic_ref_missing` (updated to positive availability probe)
   - `lockfree_atomic_ref_token_surface_smoke`
   - `lockfree_atomic_ref_token_type_mismatch`
   - `lockfree_atomic_ref_retry_loop_smoke`
   - `lockfree_atomic_ref_store_type_mismatch`
   - `lockfree_atomic_ref_store_uint_rejected`
3. Memory safety checks for probe subset:
   - `DRIFT_ALLOC_TRACK=1`: clean
   - `DRIFT_ASAN=1`: clean
4. Fence regressions pinned in e2e:
   - `lang_atomic_fence_api_smoke`
   - `std_sync_atomic_fence_api_smoke`
   - both pass in normal, `DRIFT_ASAN=1`, and `DRIFT_ALLOC_TRACK=1` runs
   - `std_sync_atomic_fence_message_passing_release_acquire`
   - `std_sync_atomic_fence_message_passing_stress`
   - both pass in normal, `DRIFT_ASAN=1`, and `DRIFT_ALLOC_TRACK=1` runs

5. Implementation note:
   - `std.sync` constructors that wrap call expressions into generic wrapper structs use typed temporaries first (`val x = ...; Struct(field = x);`) due to a current checker/codegen sensitivity. Keep this pattern until a dedicated compiler regression is pinned and fixed.

## Regression-First Plan

### Phase 1: AtomicRef token/guard regressions

1. Add e2e/driver tests that pin token-only atomic ref semantics.
2. Add negative tests for forbidden token/reference coercions.
3. Add guard-gated deref tests and no-`&mut` derivation tests.

### Phase 2: Fence semantics regressions

1. Message-passing style e2e cases that require fences (not just atomic RMW).
2. Stress loops to catch reordering-sensitive bugs.

Status:
- Implemented with release/acquire fence handoff tests above.
- Fixed: tighter single-thread fence+atomic hot-loop crash repro was caused by per-iteration variant literal stack allocation in LLVM lowering (`ConstructVariant` for zero-payload enums).
- Added regressions:
  - `std_sync_atomic_fence_atomic_hotloop_stack`
  - `std_sync_atomic_signal_fence_atomic_hotloop_stack`
  - both pass in normal, `DRIFT_ASAN=1`, and `DRIFT_ALLOC_TRACK=1` runs.

### Phase 3: Reclamation regressions

1. Deterministic retire/reclaim tests in single-threaded simulated schedules.
2. Multithread stress with alloc tracking + ASan gates.
3. Leak/UAF regressions pinned in e2e.

### Phase 4: MPSC proving target

1. Functional tests (ordering, progress, close/shutdown behavior).
2. Contention stress (many producers, one consumer).
3. Memory safety sweeps (alloc tracking + ASan).

### Phase 5: Docs follow-up (after API stabilization)

1. Update language spec with final `AtomicRef` token/guard semantics, constraints, and fence contracts.
2. Update effective-drift with practical lock-free usage examples (container user API + brief internals sketch).

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

1. Exact token naming:
   - `RefToken<T>`/`AtomicRef<T>` vs `Handle<T>` alias strategy.
2. Fence API placement:
   - `std.sync` only vs shared with `std.concurrent`.
3. Reclamation API surface:
   - epoch guard ergonomics and retire callback model.
4. First proving queue shape:
   - bounded ring vs linked-list MPSC.

## Exit Criteria

1. Restricted `AtomicRef` token/guard model is implemented and documented.
2. Fences are implemented and validated via reordering-sensitive tests.
3. Epoch-based reclamation is implemented with deterministic safety tests.
4. MPSC queue passes correctness + stress + memory safety gates.
5. No skipped/placeholder tests in the new lock-free suite.
