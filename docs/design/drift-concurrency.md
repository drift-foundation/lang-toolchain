# Drift Concurrency

This document summarizes the concurrency surface and current runtime behavior.
It is intentionally minimal and should be treated as a contract for future runtime work.

## Goals

- Freeze the public API in `std.concurrent`.
- Keep expected outcomes as `Result` (no exceptions for normal scheduling outcomes).
- Provide stable type signatures so the runtime can be swapped in later.

## Current Status (OS-thread fallback)

Phase 1 uses OS threads as a fallback runtime. Behavior is still minimal but
no longer purely stubbed:

- `vt_spawn` creates a VT handle; execution starts on `exec_submit`.
- `spawn` / `spawn_on` execute the function on an OS thread (after submit).
- If the default executor submit fails, `spawn()` returns a `VirtualThread` whose `join()` yields `Err(Failed(ExecSubmitFailed(code)))`.
- `join` waits for completion and returns `Ok(T)` on the first call, then `Err(Closed)` afterward.
- `sleep` returns `Ok(Void)` when `duration.millis != 0`, `Timeout` when `millis == 0`.
- `set_default_executor` is a no-op.
- `join_timeout` returns `Timeout` when `duration.millis == 0`, otherwise delegates to `join()`.
- `spawn_on` maps executor submission status codes: `0 -> Ok`, `1 -> Busy`, `2 -> Timeout`, else `Failed(ExecSubmitFailed(code))`.
- Negative durations are invalid and return `Err(Failed(InvalidDuration(millis)))` in `sleep` and `join_timeout`.
- Test-only hook: `lang.thread.exec_submit_test_override(code)` forces `exec_submit` to return a fixed status for e2e validation.

These are placeholders and will change as the runtime evolves.

## Current Status (fiber-based scheduler, Linux)

Phase 2 replaces OS-thread-per-VT with a scheduler running VTs as fibers on
carrier threads (Linux, `ucontext`-based). This enables VT parking without
blocking an OS thread:

- Each VT is a fiber with its own stack.
- Worker threads dequeue VTs and `swapcontext` into their fibers.
- `vt_park`/`vt_park_until` yield back to the scheduler (no OS blocking).
- `vt_unpark` re-queues parked VTs on their executor.
- Cancellation is cooperative: cancelled-but-started VTs run to completion
  unless the task observes cancellation and returns early.

## VirtualThread result-ownership protocol (Drift-side, 0.33.1)

The `VirtualThread<T>` value's `state` field is an
`Arc<Mutex<ResultState<T>>>` co-owned by the returned handle and the
spawn-thunk closure.  `ResultState<T>` carries the underlying
`mem.RawBuffer<T>` plus two flags (`initialized`, `abandoned`).

**Single deallocation point.**  `Destructible for ResultState<T>` is
the only site that calls `mem.dealloc<T>` on the result buffer.  It
runs exactly once, when the last `Arc` clone dies — typically when
both the cb thunk has run to completion AND the handle has been
joined/dropped.  Other paths (`join` Ok, `join` cancelled,
`join_timeout` Ok / cancelled, `VirtualThread::destroy`) may
`mem.read` an initialized `T` out and run its `Destructible`, then
set `initialized = false`, but never deallocate the buffer
themselves.

**Coordination flow:**
- Cb thunk, terminal step: take state lock; if `abandoned` is true,
  drop the produced `T` locally (closes the drop-while-running UAF);
  otherwise `mem.write` to the buffer and set `initialized = true`.
- `VirtualThread::destroy`: take state lock; set `abandoned = true`;
  if `initialized`, `mem.read` the published `T` and let it drop;
  then `thread.vt_drop(handle)`.  The buffer survives until the
  cb thunk also drops its `Arc` clone and `ResultState::destroy`
  fires.
- `join` (and `join_timeout`) success path: `vt_join`, take lock,
  `mem.read` out the published `T`, set `initialized = false`,
  return `Ok(v)`.  Buffer dealloc happens when both Arcs go.
- `join` / `join_timeout` cancelled branch: `vt_join`, take lock,
  if `initialized` consume + drop the published `T`, set
  `initialized = false`, return `Err(CANCELLED)`.
- `FutureGroup.join_any`: per-future probe with three terminal cases.
  (1) `submit_error != 0` → delegate to `.join()` which routes the
  deferred submit error as `Err(FAILED)`; the polling loop never
  spins on `handle == 0` (R8).
  (2) `vt_is_completed != 0` AND `ResultState.initialized == true` →
  take an `&T` to the slot via `mem.ptr_at_ref` and Copy through the
  deref (legacy peek-without-consume; does not set
  `initialized = false`).  In Drift, `T: Copy` does **not** imply
  "no destructor" — `String` is Copy with retain/release semantics
  — so the read MUST route through `T`'s real Copy lowering
  (`drift_string_retain` for `String`, bitwise for trivial types)
  rather than a raw `mem.read` that would silently move the single
  ownership stake (R6).
  (3) `vt_is_completed != 0` AND `ResultState.initialized == false`
  → release the lock and delegate to `.join()`, which routes through
  the cancellation cleanup path and returns `Err(CANCELLED)` without
  reading uninitialised slot bytes (R7).

This design closes the R1-R8 ownership matrix
(`work/stdlib-concurrency/plan.md` §5.2):
- R1 (drop-while-running UAF): `abandoned` flag steers late-publishing
  thunk into drop-locally branch.
- R2 (submit-error double-free): spawn no longer manually deallocs;
  Destructible-for-ResultState is the unique free site.
- R3 (completed-unjoined drop leak): destructor mem.read+drops any
  initialized T.
- R4 / R5 (cancel-publish join-CANCELLED / join_timeout-CANCELLED
  leak): cancellation branches now consume the published T before
  returning Err.
- R6 (FutureGroup<String>::join_any double-release): `join_any`
  reads via `*ref` (Copy semantics) instead of raw `mem.read`, so
  Copy's retain fires and the buffered stake stays intact for
  subsequent `join_all`.
- R7 (FutureGroup<T>::join_any uninit-read for cancel-before-start):
  the peek path is gated by `ResultState.initialized`; cancelled
  futures route through `.join()` instead of dereferencing
  uninitialised slot bytes.
- R8 (FutureGroup<T>::join_any hang on submit-error future): the
  per-future probe checks `submit_error != 0` first and delegates to
  `.join()`, bypassing the unreachable `vt_is_completed(0)` probe.

No runtime ABI changes — the entire protocol is in
`stdlib/std/concurrent/concurrent.drift`.

## VirtualThread lifecycle (runtime view)

Virtual threads are modeled as task records with explicit states. The runtime
tracks the lifecycle to support scheduling, parking, and cancellation.

### States

- `NEW`: created by `vt_spawn`, not yet submitted to an executor.
- `READY`: enqueued via `exec_submit`, eligible to run.
- `RUNNING`: currently executing on a carrier thread.
- `PARKED`: blocked in `vt_park` / `vt_park_until` awaiting unpark or timeout.
- `FINISHED`: task completed (result stored).
- `CANCELLED`: task cancelled before completion.

### State transitions (Phase 1 behavior)

- `vt_spawn` → `NEW`
- `exec_submit` → `READY`
- worker begins execution → `RUNNING`
- `vt_park` / `vt_park_until` → `PARKED`
- `vt_unpark` → `READY` (unless already completed/cancelled)
- task returns normally → `FINISHED`
- `vt_cancel` (pre-start or during park) → `CANCELLED`

### Notes

- `CANCELLED` is terminal and implies `join`/`join_timeout` return `Err(Cancelled)`
  unless the task already finished.
- Phase 1 uses OS threads for execution; the state machine is still valid and is
  the basis for a future VT scheduler/reactor backend.

## Public API (MVP surface)

Module: `std.concurrent`

Types:

- `struct Duration { pub millis: Int }`
- `pub error ConcurrencyError { kind: String, code: Int }` — flat shape; `kind` is one of `CONCURRENCY_KIND_TIMEOUT` / `CONCURRENCY_KIND_CANCELLED` / `CONCURRENCY_KIND_CLOSED` / `CONCURRENCY_KIND_BUSY` / `CONCURRENCY_KIND_FAILED`
- `struct Executor`
- `struct ExecutorPolicy`
- `struct ExecutorPolicyBuilder`
- `struct VirtualThread<T>`
- `struct Future<T>`
- `struct FutureGroup<T>`
- `struct Scope`

Functions:

- `executor_policy_builder() -> ExecutorPolicyBuilder`
- `build_executor(policy: ExecutorPolicy) -> Executor`
- `spawn<T>(cb: core.Callback0<T>) -> VirtualThread<T>`
- `spawn_on<T>(exec: Executor, cb: core.Callback0<T>) -> Result<VirtualThread<T>, ConcurrencyError>`
- `spawn_future<T>(cb: core.Callback0<T>) -> Future<T>`
- `spawn_future_on<T>(exec: Executor, cb: core.Callback0<T>) -> Result<Future<T>, ConcurrencyError>`

You can pass a lambda directly; the compiler will wrap it as a `core.callback0(...)` when needed.
- `future_group<T>() -> FutureGroup<T>`
- `scope(f: Fn1<Scope, Void>) -> Result<Void, ConcurrencyError>`
- `sleep(d: Duration) -> Result<Void, ConcurrencyError>`
- `default_executor() -> Executor`
- `set_default_executor(exec: Executor) -> Void`

Methods:

- `ExecutorPolicyBuilder.min_threads/max_threads/queue_limit/timeout/on_saturation -> &mut ExecutorPolicyBuilder`
- `ExecutorPolicyBuilder.build() -> ExecutorPolicy`
- `ExecutorPolicyBuilder.build_executor() -> Executor`
- `VirtualThread.join() -> Result<T, ConcurrencyError>`
- `VirtualThread.join_timeout(d: Duration) -> Result<T, ConcurrencyError>`
- `VirtualThread.cancel() -> Void` (requires `&mut self`)
- `Future.join() -> Result<T, ConcurrencyError>`
- `Future.join_timeout(d: Duration) -> Result<T, ConcurrencyError>`
- `Future.cancel() -> Void`
- `Future.is_done() -> Bool`
- `FutureGroup.add(Future<T>) -> Void`
- `FutureGroup.join_all() -> Result<Array<T>, ConcurrencyError>`
- `FutureGroup.join_any() -> Result<T, ConcurrencyError>`

## Error Model

Expected outcomes (timeouts, saturation, cancellation, closed handles) are represented
via `ConcurrencyError` in `Result` values. These do not throw.

### Outcome precedence (frozen)

When multiple outcomes could apply, the ordering is:

1. `Failed(err)` dominates all other outcomes.
2. `Closed` is only for misuse (e.g., double join).
3. `Timeout` / `Busy` apply only when no terminal state exists.

## Cancellation Semantics (frozen surface)

- `cancel()` is idempotent.
- Cancellation is cooperative (tasks must observe it; no forced unwind).
- After cancellation, `join()` returns `Err(Cancelled)` unless the task already completed.
- Runtime may still return `Err(Failed(err))` for scheduler/runtime failures (not user code).

Phase 0 stubs do not implement cancellation yet; the above is the contract for Phase 1+.

## Executor Identity Semantics (frozen)

- `Executor` is a thin handle type.
- Identity is defined by `handle` only.
- Copying `Executor` is allowed and refers to the same underlying runtime executor.
- `ExecutorPolicy` is immutable after build; changing policy requires building a new executor.

## Forward Plan (Phase 1+)

- Implement runtime in `lang.thread` (OS-thread fallback first).
- Wire scheduling, reactor integration, and stack management.
- Replace stubs with real execution semantics.
- Finalize cancellation behavior and scope failure propagation.

## `lang.thread` intrinsics (Phase 0 surface)

`lang.thread` is the runtime substrate for virtual threads and scheduling.
It is not user-facing; `std.concurrent` is the public API.

Types:

- `VtHandle`
- `ExecutorHandle`
- `ReactorHandle`

Intrinsics:

- `vt_spawn(entry, exec) -> VtHandle`
- `vt_join(vt)`
- `vt_join_timeout(vt, timeout_ms) -> Int`
- `vt_cancel(vt) -> Int`
- `vt_drop(vt)`
- `vt_current() -> VtHandle`
- `vt_park(reason)` / `vt_park_until(deadline_ms)`
- `vt_unpark(vt)`
- `exec_default_get/set`
- `exec_submit(exec, vt)`
- `exec_submit_test_override(code)` (test-only hook)
- `reactor_default_get/set`
- `reactor_register_io(fd, interest, vt, deadline_ms)`
- `reactor_register_timer(deadline_ms, vt)`

### Internal blocking boundary

`std.concurrent.block_on_io(fd, interest, deadline_ms)` is an internal helper used by stdlib I/O:

- stdlib I/O performs a non‑blocking syscall
- on `EAGAIN`/`EWOULDBLOCK`, it calls `block_on_io` and retries until the timeout elapses
- `block_on_io` registers interest with the reactor and parks the current VT
- the reactor unparks the VT when the fd is ready (or deadline elapses)

User code calls std.io/std.net methods with an explicit timeout. Those methods may park the current VT and return `WouldBlock` on timeout. The boundary helper keeps the implementation VT‑friendly while preserving a synchronous API surface.

## Runtime target boundary

The concurrency model described in this document has a concrete implementation
boundary: the custom VT backend (fiber context switch, `epoll` reactor,
worker-side polling) is currently supported on **x86_64 Linux only**. There is
no general-purpose `ucontext` or `kqueue` fallback; the Valgrind compatibility
path is a tooling-specific shim, not a portability layer. Target selection is
currently enforced by a host-based check, which is not sufficient for
cross-compilation. See
[drift-runtime-targets.md](drift-runtime-targets.md) for the full support
policy, enforcement mechanism, and future requirements.
