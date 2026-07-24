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

## Supervised async primitives (`channel<T>` + `is_complete`, 0.33.2)

The supervisor-primitives slice adds two stdlib facilities for the
bookkeeper-team supervised-async pattern: a typed completion channel
for the channel-driven reaper shape, and a non-blocking
terminal-state predicate for the polling-driven reaper shape.

### `channel<T>` — unbounded MPSC typed channel

```drift
pub fn channel<T>() nothrow -> ChannelHalves<T>

pub struct Sender<T>      // move-only; share via Arc<Sender<T>>
pub struct Receiver<T>    // move-only; single consumer
pub struct ChannelHalves<T> {
    pub sender:   Optional<Sender<T>>,
    pub receiver: Optional<Receiver<T>>
}

impl<T> ChannelHalves<T>:
    fn take_sender(&mut self) -> Sender<T>
    fn take_receiver(&mut self) -> Receiver<T>

impl<T> Sender<T>:
    fn send(&self, var v: T) nothrow -> Result<Void, ConcurrencyError>

impl<T> Receiver<T>:
    fn recv(&mut self) nothrow -> Result<T, ConcurrencyError>
    fn recv_timeout(&mut self, d: Duration) nothrow -> Result<T, ConcurrencyError>
```

**Extraction shape.**  Drift does not permit `move self.field`
partial moves; `channel<T>()` returns a `ChannelHalves<T>` whose
fields are `Optional<...>` slots, and the `take_*` methods
`mem.replace` them out.  Calling either `take_*` twice asserts.

**Internals.**  One `Mutex<ChannelState<T>>` protects the entire
state — `queue: Array<T>`, `sender_closed: Bool`, `receiver_closed:
Bool` — so `send` / `recv` / sender-close (last `Sender` drop) /
receiver-close (receiver drop) linearize against each other.  A
`Condvar` parks blocked receivers; senders signal one after each
push.

**Close semantics.**
- Last `Sender<T>` drop (or last `Arc<Sender<T>>` if shared) sets
  `sender_closed = true` and broadcasts the condvar.  `recv`
  drains any queued values before returning `Err(CLOSED)`.
- `Receiver<T>` drop sets `receiver_closed = true` and **detaches**
  the queued values out of the channel state under the mutex via
  `mem.replace` (O(1) array-handoff pointer swap — no user-code
  execution while the lock is held).  The lock is then released,
  and the detached local array drops at fn-exit, running each
  queued `T`'s `Destructible` **outside** the channel state mutex.
  This is the load-bearing detach: a queued `T` whose destructor
  releases the last `Arc<Sender<T>>` for this channel (or
  otherwise re-enters the channel) must not execute under the
  state mutex, or `Sender::destroy` would spin-CAS forever trying
  to re-acquire it.  No signal needed in v1 (unbounded channel
  has no blocked senders).  Subsequent `send` returns
  `Err(CLOSED)`; the moved-in `var v: T` is dropped at fn-exit
  via T's `Destructible`.

**No `T` bound in v1.**  The channel transfers ownership of each
`T`, not clones — `Share` is the wrong gate.  A `T: Send` trait
when one exists will be the correct constraint; until then,
`channel<T>()` publishes with no bound.

**No bounded buffer, `try_send` / `try_recv`, `select`, or
`detach` in v1.**  These can be added later if a consumer asks;
the unbounded MPSC shape covers the bookkeeper-team's
supervised-reaper pattern (one send per completed worker).

### `is_complete` — non-blocking terminal-state predicate

```drift
impl<T> VirtualThread<T>: fn is_complete(&self) nothrow -> Bool
impl<T> Future<T>:        fn is_complete(&self) nothrow -> Bool
```

Returns `true` iff the handle has reached a terminal state:
- `joined == true` (handle already consumed by `.join`), OR
- `submit_error != 0` (deferred error queued to surface from
  `.join`), OR
- `handle == 0` (out-of-band runtime teardown), OR
- `thread.vt_is_completed(handle) != 0` (runtime has marked the
  task complete).

Returns `false` while the task is still running, **including** the
case where `.cancel()` has been requested but the runtime has not
yet observed the task complete — a cancellation *request* is not
completion.  This avoids the silent false-positive in a polling
reaper that would call `.join()` on a cancelled-but-still-running
handle.

**Canonical usage** (polling reaper, alternative to channel-driven):

```drift
while !vt.is_complete() {
    conc.sleep(conc.Duration(millis = 1));
}
match vt.join() {
    Ok(v)  => { /* completed normally */ },
    Err(e) => { /* CANCELLED, FAILED, etc. */ }
}
```

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
  then `thread.vt_drop(handle)`.  Dropping the handle detaches from
  the result; it does not cancel submitted work.  The buffer survives
  until the cb thunk also drops its `Arc` clone and
  `ResultState::destroy` fires.
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

- `vt_drop` is not a cancellation transition for submitted work.  It
  marks the raw handle as abandoned so the worker can reclaim the VT
  record after the task finishes; queued tasks still run.
- `CANCELLED` is terminal and implies `join`/`join_timeout` return `Err(Cancelled)`
  unless the task already finished.
- Phase 1 uses OS threads for execution; the state machine is still valid and is
  the basis for a future VT scheduler/reactor backend.

### Operator diagnostics

This lifecycle state, plus per-VT wait reasons and carrier-thread attribution,
is observable at runtime. Send `SIGUSR2` to a Drift process to dump a live
snapshot of the scheduler (carriers, VT states, what each parked VT is waiting
on, and whether the runtime is making progress) for diagnosing a stuck process
in production. See [Runtime liveness interrogator](../liveness.md).

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

Public stdlib I/O must never block a VT carrier thread (see the **Stdlib IO
contract** in [drift-stdlib-spec.md](drift-stdlib-spec.md)). Two distinct
mechanisms achieve this, chosen by whether the underlying descriptor/operation is
*pollable* (made ready-observable via the reactor) or *non-pollable* (a syscall
that can block in-kernel and is not reliably nonblocking):

#### 1. Pollable descriptors — nonblocking syscall + reactor readiness + retry

For sockets and other epoll-pollable fds, `std.concurrent.block_on_io(fd,
interest, deadline_ms)` is the internal helper:

- stdlib I/O performs a **non‑blocking** syscall;
- on `EAGAIN`/`EWOULDBLOCK`, it calls `block_on_io` and retries until ready or the
  deadline elapses;
- `block_on_io` registers interest with the reactor and **parks** the current VT;
- the reactor **unparks** the VT when the fd is ready (or the deadline elapses).

This keeps the implementation VT‑friendly while preserving a synchronous API
surface. Used by `std.net` (TCP/UDP) and the socket paths of `std.io`.

#### 2. Non-pollable operations — bounded blocking-pool job + parked VT

Some operations cannot be made nonblocking and are not reactor-pollable:
filesystem syscalls (`opendir`/`readdir`/`fstatat`/`closedir`, `open`/`read`/
`write` on regular files) and name resolution (`getaddrinfo`). A regular file or
directory fd is **not** made nonblocking by epoll, and NFS/FUSE/autofs can block
unpredictably inside the syscall. Running these inline on a carrier would stall
that carrier and every VT it hosts. They are therefore **offloaded** to a shared,
bounded **blocking-syscall worker pool**:

- the operation is packaged as **one job** and submitted to the pool
  (`drift_blocking_submit`); the pool has a **fixed** number of workers (4) and a
  **bounded** FIFO queue (64), so admission is bounded — submission past the bound
  returns a **saturation** error (surfaced as `EAGAIN` backpressure), never an
  unbounded queue or unbounded thread growth;
- the calling VT **parks** (optionally after registering a deadline timer);
- a pool worker runs the blocking syscall(s) off any carrier, builds the result or
  resolves a single error, then **unparks** the VT (or, if the VT abandoned the
  job, frees the result);
- on wake the VT consumes the result with **non-blocking, in-memory** accessors
  (no further syscalls), so result decode is carrier-safe.

**Deadline / cancellation.** A deadline timer or a cancellation unparks the VT,
which then **abandons** the job (logical cancellation): it drops its stake and
returns a timeout/cancellation error promptly. Physical cancellation of the
in-flight kernel syscall is **not** portably possible — the worker stays blocked
until the kernel returns, then discards the result. Job ownership is structured so
that an abandoned operation is memory-safe: the job owns its arguments and result
**independently** of any Drift value, and a reference count (VT stake + worker
stake; last release frees) guarantees no use-after-free or double-free in the
narrow window where the worker completes exactly as the deadline fires.

The two consumers of this pool today are `getaddrinfo` (DNS resolve, behind
`std.net` connect-by-hostname) and `std.fs.read_dir`. They share one pool; per-
category fairness is a known limitation (a directory stall can delay DNS and vice
versa) tracked for a future slice. User code calls the synchronous-looking API
with an explicit deadline; the boundary keeps it VT-safe.

## Blocking FFI from virtual threads

Non-pollable blocking C FFI (database engines, legacy libraries — anything with no
readiness handle) must never run on a cooperative carrier. The standard facility is
`std.concurrent.BlockingExecutor`: a dedicated, bounded, **named** executor whose
workers are expected to be pinned by C calls.

```drift
var b = conc.blocking_executor_builder();   // fixed workers, queue 64, Block 5s
val ex = conc.build_blocking_executor(b.build(), "storage-lmdb");
val r = conc.run_blocking_on(&ex, "lmdb.write_txn", core.callback0(|| => {
    return commit_txn_ffi(...);            // named extern wrapper
}));
```

Semantics:

- **Bounded admission is the executor's job.** `Block(timeout)` (the default) parks
  the submitter — without pinning its carrier — until capacity is transferred (FIFO)
  or the deadline passes (`Err(TIMEOUT)`); `ReturnBusy` fails immediately
  (`Err(BUSY)`). Do not build app-level queues/retries around the executor.
- **Errors are ordinary Results.** Domain errors travel inside `T` (return
  `core.Result<Payload, DomainError>` from the closure); `ConcurrencyError` is
  infrastructure only (`busy`/`timeout`/`cancelled`).
- **In-flight C calls are not cancellable.** Deadlines time out the waiter, never the
  C call; the bounded worker count is the containment for a wedged call.
- **Boundary rules:** submit structural batch closures (one transaction per closure);
  never wrap individual FFI calls on thread-affine handles (an `MDB_txn` must not
  span workers); never hold a transaction across arbitrary application logic; and
  perform NO cooperative operation (channel op, sleep, yield, join) inside a closure
  holding a thread-affine C resource — carrier migration only happens at park
  points, and blocking C calls do not park, so a closure free of cooperative calls
  provably runs on one thread start-to-finish.

### Making blocking FFI diagnosable

**Scheduler isolation without diagnostic labeling is incomplete. A blocked worker is
acceptable only if operators can identify which subsystem, which operation, which
extern call, and where in Drift source — from `kill -USR2 <pid>` alone.**

The required pattern (see `examples/blocking_ffi/`):

1. **Name the executor** for the subsystem: `build_blocking_executor(policy,
   "storage-lmdb")`.
2. **Label every submission**: `run_blocking_on(&ex, "lmdb.write_txn", …)` — labels
   are required parameters, ≤ 48 bytes, dot-namespaced by convention.
3. **Call extern C through named Drift wrapper functions**, not anonymous unsafe
   blocks scattered through app logic. The compiler brackets user-module
   `extern "C"` calls automatically, so liveness reports the extern symbol and the
   wrapper's file:line while the call is in flight (stdlib/`@intrinsic` externs are
   not instrumented — user FFI is where operators get stuck).
4. **Correlate with your logs**: include request/scope identifiers in application
   logs around submission/join, so liveness `vtid`/labels join to service logs.

What `kill -USR2` then shows (`drift.liveness.v1`):

```json
"execs": [{"id": 2, "name": "storage-lmdb", "queue_len": 0, "running": 1,
           "queue_limit": 1, "waiters": 1, "workers": 1}]
{"vtid": 2, "state": "RUNNING", "carrier_tid": 12345,
 "op": "lmdb.write_txn", "submitter": 1, "exec_id": 2,
 "ffi": {"symbol": "mdb_txn_commit", "file": "src/storage.drift", "line": 212}}
{"vtid": 3, "state": "PARKED",
 "wait": {"kind": "blocking-admission", "exec_id": 2, "deadline_ms": 4180},
 "op": "lmdb.read_txn"}
```

The bad pattern — an unnamed executor, unlabeled submits, raw unsafe blocks inline —
degrades that to `RUNNING carrier_tid=…` and nothing else: an anonymous stuck worker.

**Limitations:** Drift identifies the Drift-visible operation and extern
symbol/callsite. It cannot see C-library internals — if `mdb_txn_commit` is stuck in
`fdatasync`, attach gdb/perf/strace to the reported `carrier_tid` to refine.

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
