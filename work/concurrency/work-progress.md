# Work Progress (Concurrency)

Reference: `docs/design/spec-change-requests/virtual_threads_concurrency_spec.md`

## Status
Phase 2 runtime (scheduler + reactor + VT lifecycle) complete.

## Decisions (pinned)
- MVP concurrency includes runtime primitives: scheduler, reactor integration, and stack management.
- Blocking I/O must go through boundary helpers (park/unpark on would-block).
- Public API lives in `std.concurrent` with blocking `std.io`/`std.net` (no async/await surface).
- Post‑MVP: add ReentrantMutex (distinct from Mutex) with explicit semantics.
- Intrinsics live in `lang.thread` (not `std.runtime`); `lang.abi` stays memory/FFI only.

## Phase 0: Freeze list (surface + intrinsics)

### std.concurrent surface (minimum, stable)
- `VirtualThread<T>` (name frozen; no renames later).
- `spawn<T>(cb: core.Callback0<T>) -> VirtualThread<T>`
- `spawn_on<T>(exec: Executor, cb: core.Callback0<T>) -> Result<VirtualThread<T>, SpawnError>`
- `join(self: ref VirtualThread<T>) -> Result<T, WaitError>`
- `join_timeout(self: ref VirtualThread<T>, d: Duration) -> Result<T, WaitError>`
- `scope(f: fn(Scope) returns Void) -> Result<Void, WaitError>`
- `Scope.spawn<T>(f: fn() returns T) -> Result<VirtualThread<T>, SpawnError>`
- `sleep(d: Duration) -> Result<Void, WaitError>`
- `Executor`
- `ExecutorPolicy` + builder (min/max threads, queue_limit, timeout, on_saturation)
- Default executor getters/setters

### Error/outcome types (expected outcomes are Result, not throw)
- `variant ConcurrencyError { Timeout, Cancelled, Closed, Busy, Failed(err: Error) }`

### Runtime intrinsics (lang.thread)
- `vt_spawn(entry, exec) -> VtHandle`
- `vt_current() -> VtHandle`
- `vt_park(reason)` / `vt_park_until(deadline)`
- `vt_unpark(vt)`
- `exec_default_get/set`
- `exec_submit(exec, vt)` (or bind-on-spawn only)
- `reactor_default_get/set`
- `reactor_register_io(fd, interest, vt, deadline)`
- `reactor_register_timer(deadline, vt)` (sleep)

## Plan (phased, incremental)

### Phase 0: finalize surface + intrinsics
1) Freeze MVP API shape in `std.concurrent` (signatures above). (done: stubs added)
2) Define runtime intrinsic hooks in `lang.thread` (names above).
3) Document contracts: blocking functions must call the boundary helper.
4) Decide cancellation shape (stubs acceptable; must be in API).

## Notes (Phase 0 stubs)
- Added `std.concurrent` stubs in `stdlib/std/concurrent/concurrent.drift`.
- Added `Result<T, E>` to `std.core` export set in `stdlib/std/core/copy.drift`.
- Builder methods now return `&mut ExecutorPolicyBuilder` (chaining supported).
- `VirtualThread.join`/`sleep`/`scope` return `Err(...)` stubs; no runtime yet.
- `spawn`/`spawn_on` are non-executing stubs (do not call the function).
- `join` now returns `Err(WaitError::Cancelled())` on first call, `Err(WaitError::Closed())` on subsequent calls.
- `scope` returns `Ok(Void)` via `core.void_value()` helper (Phase 0 stub success path).
- `sleep` returns `Ok(Void)` when `d.millis != 0`, `Timeout` when `d.millis == 0` (Phase 0 stub).
- `ExecutorPolicy.timeout` now uses `Duration` (no `timeout_ms` Int field).
- `Duration` is `Copy` and exposes `pub millis` for construction in MVP.
- `SpawnError`/`WaitError` replaced with `ConcurrencyError` (single error variant).
- `VirtualThread.cancel()` stub added to freeze cancellation surface.
- `set_default_executor` is a no-op in Phase 0 stubs (documented; runtime will wire later).
- Added `lang.thread` intrinsic surface in `stdlib/lang/thread.drift` (vt/exec/reactor hooks).
- `std.concurrent` now wires `Executor` to `lang.thread` handles; `spawn_on` calls `vt_spawn` (still stubbed).
- Phase 1 OS-thread fallback runtime: added `thread_runtime.c` with `drift_thread_spawn` and `-pthread` for e2e builds.
- Added `vt_join` intrinsic and OS-thread join wiring (Phase 1).
- Added OS-thread fallback runtime hooks for `vt_current`, `vt_park`, `vt_park_until`, `vt_unpark` (sched_yield / nanosleep).
- `spawn`/`spawn_on` now take `Callback0<T>` directly (no Fn0 generic param); lambdas are wrapped into `callback0` when needed.
- `core.callback0/1/2` are now `nothrow` so nothrow callers can build callbacks without try/catch.
- HIR→MIR lowering now allows Void-valued constructor args when the expected field type is Void.

## Recent progress
- Phase 0 stdlib stubs compile and pass stage1/stage2/type_checker trio.
- Type checker allows `return self` for `&mut self` methods (enables builder chaining).
- E2E `concurrent_spawn_executes` now validates spawn/join linkage (not execution); add a real execution test once safe shared state/atomics are available.
- Fixed module package metadata for `lang.__internal` and forced boundary thunks to call exported wrappers; unblocked `fnptr_cross_module_wrapper` e2e.
- Added executor/reactor runtime plumbing (exec_default get/set/submit + reactor get/set/register) and LLVM lowering for `lang.thread` intrinsics.
- Updated `drift_thread_spawn` ABI to take a `DriftIface*` and pass callback by address from codegen.
- `concurrent_spawn_executes` validates real callback execution via `vt_spawn` + `vt_join` (stderr "spawned", stdout "done").
- Marked `std.concurrent.spawn` / `spawn_on` as `nothrow`.
- Inference fix: type-arg inference now unifies generic interface instances (TypeKind.INTERFACE) in call inference.
- Match constructor binders now support `var` and get real binding ids, so `move`/`&mut` on match binders work.
- Fn0 requirement now treats captureless lambdas as function values; `f.call()` on function values resolves directly; spawn_on no longer needs helpers.
- MIR lowering for callback env move-captures now uses direct `MoveOut` with binding-name seeding to avoid missing expr type on synthetic moves.
- `VirtualThread.join` now returns `Ok(T)` after `vt_join` by reading a result buffer; `Closed` on subsequent calls.
- `spawn`/`spawn_on` now require nothrow function types for `Fn0` (function types that can throw do not satisfy `Fn0` in trait solver).
- `join_timeout` returns `Timeout` when `d.millis == 0`, otherwise delegates to `join()` (Phase 0 stub behavior).
- Negative durations now fail fast: `InvalidDuration` is raised and returned as `Err(Failed(e))` from `sleep`/`join_timeout`.
- Cancellation contract pinned: `cancel()` is idempotent; cancellation is cooperative; after cancel, `join()` returns `Err(Cancelled)` unless already completed. Phase 0 stubs do not implement this yet.
- ConcurrencyError precedence pinned: `Failed` dominates; `Closed` only for misuse; `Timeout`/`Busy` only when no terminal state exists.
- `exec_submit` now returns a status code; `spawn_on` maps 0->Ok, 1->Busy, 2->Timeout, else Failed(ExecSubmitFailed).
- `sleep` is now `nothrow` (matches `_check_duration` behavior).
- Added e2e `concurrent_negative_duration` asserting `sleep(-1)` and `join_timeout(-1)` return `Err(Failed(InvalidDuration))`.
- `VirtualThread` now carries a `RawBuffer<T>` result slot, written by the spawned callback and read on join.
- `vt_spawn` is create-only; execution begins on `exec_submit` (spawn/spawn_on now call submit explicitly).
- Added `vt_drop` intrinsic to discard unsubmitted VTs; `spawn_on` drops VT and deallocs result buffer on submit failure.
- LLVM codegen now emits `drift_exec_submit`/`drift_thread_drop` calls correctly (fixed exec_submit handler indentation; added vt_drop declaration).
- Default executor submit failures now return a `VirtualThread` whose `join()` yields `Err(Failed(ExecSubmitFailed(code)))` (stored as `submit_error`).
- Added test-only `lang.thread.exec_submit_test_override` intrinsic and e2e `concurrent_spawn_default_exec_busy` to validate default executor failure path.
- Added e2e `concurrent_spawn_on_busy_timeout` to validate `spawn_on` Busy/Timeout paths.
- Added `vt_join_timeout` intrinsic + e2e `concurrent_join_timeout_nonzero` to validate nonzero timeout behavior.
- `vt_join_timeout` uses `pthread_timedjoin_np` on Linux; fallback joins on unexpected errors.
- Added e2e `concurrent_join_timeout_nonzero_ok` to validate `join_timeout` returns `Ok` when task completes before deadline.
- Added `vt_cancel` intrinsic + OS-thread runtime support; `VirtualThread.cancel(&mut)` now sets cancelled and `join`/`join_timeout` return `Cancelled` when appropriate.
- Added e2e `concurrent_cancel_join` and `concurrent_cancel_join_timeout`.
- Note: `core.NoThrow` trait is not defined/exported; `spawn`/`spawn_on` enforce nothrow via `Fn0` only (no explicit NoThrow bound yet).
- Trait enforcement now infers type params from `Fn0<T>` requirements for lambda args (fixes `spawn(| | => ...)` in enforcement pass).
- `VirtualThread<Void>` now works: MIR/LLVM can materialize void values (ConstVoid + zero-value support), so Void-returning tasks no longer need a workaround.
- Added executor creation plumbing: `lang.thread.exec_create`, `ExecutorPolicyBuilder.build_executor`, and `std.concurrent.build_executor` (default executor now created lazily with single-thread policy).
- Marked `ExecutorPolicy` and `Executor` as `Copy` (handle semantics; matches doc).
- Added reactor scaffolding in runtime: epoll/eventfd loop, timer list, and `reactor_register_io/timer` now enqueue and wake reactor.
- `vt_park`/`vt_unpark` now use per‑VT condvar + park tokens; TLS tracks current VT for park/unpark and `vt_current` now returns VT handle when on a VT.
- Runtime VT state tracking added (NEW/READY/RUNNING/PARKED/FINISHED/CANCELLED) and transitions wired in spawn/submit/park/unpark/cancel/worker execution.
- Executor queue limits now account for READY + RUNNING tasks; unpark skips FINISHED/CANCELLED VTs and join_timeout returns promptly when cancelled.
- Reactor registration now skips FINISHED/CANCELLED VTs (no IO/timer watches for dead tasks).
- Phase 2 scheduler now uses context-based fibers on Linux: worker threads swap into VT fibers; `vt_park`/`vt_park_until` yield to scheduler without blocking the OS thread.
- `vt_unpark` requeues parked VTs on their executor; cancelled-but-started tasks resume (cooperative cancellation).
- Added e2e stress test `concurrent_stress_join_all` to exercise fiber scheduler + reactor wakeups with many tasks.
- `std.concurrent.sleep` now registers a reactor timer (deadline = now_ms + duration) and parks the current VT; non‑VT path still uses `vt_park_until`.
- Runtime executor now has a queue + worker threads; `exec_submit` enqueues VTs and workers run callbacks (no per‑VT pthread creation).
- Added test intrinsics for reactor IO (`test_eventfd_*`, `test_timerfd_*`) and e2e tests for IO‑driven unpark.
- Added internal `std.concurrent.block_on_io` helper (register + park).
- Dropped public `std.io`/`std.net` block_on_* APIs; read/write now always block (VT‑aware). Internal waits use `std.concurrent.block_on_io`.
- NOTE: `concurrent_reactor_eventfd_unpark` currently annotates `var t: VirtualThread<Int>` due to `spawn` inference failing to infer `T` from lambda return; add a compiler fix and remove these annotations.
- Added e2e `concurrent_spawn_infers` to lock spawn() inference from lambda return (expected to pass once compiler fix lands).
- Added e2e `concurrent_spawn_future_capture_infers` to lock capture inference for `spawn_future` inside loops.
- Removed `CallbackN` implementations of `FnN` (no implicit Callback→Fn coercion); avoids invalid interface call stubs.
- Fixed expected-return inference mismatch to ignore incompatible expected types; `concurrent_stress_join_all` now infers `spawn_future` without explicit type args.
- Call resolver now allocates node_id + callsite_id for synthesized callback wrappers, preventing intrinsic CallInfo without call nodes.
- Lambdas with allow_capture_invoke now enforce nothrow expectations (throwing lambdas rejected when wrapped into Callback0).
- `concurrent_join_timeout_nonzero` uses `thread.now_ms() + 50` to avoid absolute deadline issues.
- MIR lowering now evaluates `match` arm result expressions even in statement position (fixes dropped side effects in match statements).
- MIR lowering uses inferred types as the expected type for `let` initializers with explicit type annotations (fixes forward-nominal array literal elem types in generic instantiations).
- Added `lang.thread.vt_is_completed` intrinsic + runtime hook; `FutureGroup.join_any` now polls completion via `vt_is_completed` and reads results without consuming.
- `concurrent_future_group` e2e now passes (join_any returns a value while join_all still succeeds).
- Fixed a NameError in `type_checker.py` for `let` bindings with declared types by removing a stray `ctx.locals[...]` write; binding types now flow through the normal path.
- Allowed shared borrows from rvalues via temp materialization in MIR lowering and auto-borrowing; `string_byte_at`/`byte_length` now accept rvalue strings.
- Added e2e `string_byte_at_rvalue` to lock rvalue borrow behavior.
- Fixed lambda-local binding/capture collision in stage2: locals are no longer resolved as capture slots (prevents buffer_write bounds abort in spawned lambdas).
- Added e2e `std_io_buffer_write_in_spawn` to lock local buffer writes inside spawned VTs.
- TODO: add examples in `docs/effective-drift.md` showing concurrency usage (publisher/worker, simulated server, etc.).
- TODO: after current inference/CallInfo bugs are resolved, sweep tests to reduce explicit type annotations (keep only where expected-type inference or diagnostics are the point).
 - Added `std.net.test_listener_port(&TcpListener)` as a test-only free function and updated net e2e tests to use it. This sidesteps a compiler CallInfo issue for test-only impl methods (see Workarounds).

## Workarounds (documented for cleanup)
- `call_resolver` Array element mismatch checks currently skip mismatches when `elem_def.name` is present in `ctx.type_param_map` to avoid false positives on generic Array<T> paths; remove once expected-type propagation and instantiation are fully aligned.
- `std.net` tests use `test_listener_port` free function to avoid `test_local_port` method calls (method call currently yields indirect CallTarget in CallInfo for test-only impl methods).

## Immediate work plan: or_throw ergonomics (Try trait)
Goal: reduce match noise for IO/Result by adding `or_throw` as a trait method.

Pinned decision:
- Option A2 for try-block auto-unwrap: in `try { ... }` blocks (and only when `use trait std.core.Try;` is in scope),
  Result-returning call statements are treated as value-position and auto-unwrapped.
  This avoids silent ignores and keeps code clean.
- If `Try` is not in scope, Result-returning call statements inside `try { ... }` must emit a diagnostic (no silent ignore).
- Add regression tests: one verifies the diagnostic without `use trait Try`, and one verifies auto-unwrap behavior with it.

Plan:
1) Add `std.core.Try` trait for `Result<T, E>`:
   - `or_throw(self) -> T` (throws original error)
   - `or_throw(self, f: Fn1<E, X>) -> T` (throws mapped error)
2) Ensure method overload resolution by arity selects the correct `or_throw` variant.
3) Ensure expected-type propagation for lambda arg in `or_throw(f)` in method-call position.
4) Confirm trait visibility: require `use trait std.core.Try;` (no prelude magic).
5) Add regression tests:
   - `Result<T,E>.or_throw()` compiles and throws on Err.
   - `Result<T,E>.or_throw(|e| MyError(...))` compiles and throws mapped error.

### Phase 1: runtime scaffold (OS-thread fallback)
1) Implement runtime stubs that execute VTs as OS threads (no epoll yet).
2) Provide `ExecutorPolicy` plumbing but default to single-thread policy.
3) Ensure public API semantics match final design (no signature changes later).

### Phase 2: reactor + VT scheduler
1) Implement epoll reactor and timer wheel.
2) Add VT scheduler and carrier thread pool.
3) Integrate blocking boundary: non-blocking syscall → if EAGAIN/EWOULDBLOCK, park VT and register interest.

## Phase 2 remaining tasks (detailed)
- (none) Phase 2 is complete; next work is Phase 3 wiring + tests and Post‑MVP items.

### Phase 3: stdlib wiring + correctness tests
1) `std.net` sockets and `std.io` streams call boundary helpers. (done)
2) `std.concurrent.sleep` uses timer wheel + park/unpark.
3) Tests:
   - spawn/join correctness (ordering + return values)
   - blocking I/O parking (mock fd readiness)
   - sleep timing (coarse assertions)
4) Phase‑3 correctness test suite (planned):
   - spawn/join ordering: multiple tasks, join reverse order; join single‑use returns Closed.
   - cancellation: idempotent, cancel‑after‑completion, cancel‑before‑start join_timeout(0) → Cancelled.
   - IO parking: deadline timeout path (no readiness) + existing eventfd readiness tests.
   - timing: sleep(0) → Timeout, sleep(>0) → Ok, join_timeout(0) → Timeout, join_timeout(>0) → Ok.
   - Added: `concurrent_spawn_join_ordering`, `concurrent_join_twice_closed`, `concurrent_sleep_zero_timeout`,
     `concurrent_sleep_nonzero_ok`.
   - Added: `concurrent_vt_current_in_task`, `concurrent_many_short_tasks` (500 tasks, sum check), `concurrent_park_nonvt_noop`.
   - Added: `concurrent_join_timeout_after_completion`.
   - Added: `concurrent_cancel_join_timeout_zero`.
   - Added: `concurrent_queue_limit_enforced`, `concurrent_default_executor_override`.
   - Added: `std_net_tcp_stress_connections` (500 clients, mixed payload sizes) with comment map.
   - Adjusted TCP read/write roundtrip tests to use larger buffers (len tracks actual bytes written).
   - Added: `byte_capture_arithmetic` (capture Byte, cast to Int, arithmetic in lambda).

## Post‑MVP
- ReentrantMutex.
- Structured cancellation semantics (error aggregation).
- Non-epoll backends (kqueue/IOCP).
