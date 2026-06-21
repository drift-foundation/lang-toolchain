# F5 — custom executor lifecycle + reactor integration — implementation-ready plan (design only)

Status: **design, do not implement until F3 is merged.** **Separate branch** from F4
(F5 is NOT required for F4 — F4 is a default-executor fairness fix; F5 is about *other*
executors). Grounded in `lang/language_runtime/posix/thread_runtime.c` +
`posix/blocking_pool.h`.

**Sequencing (firm): F3 cert → F4 → F5 Slice 1 → F5 Slice 2.** F5 is larger and riskier
than F4 and **must NOT start as a single broad runtime patch** — land Slice 1
(shutdown + thread-join, no reactor change) and Slice 2 (reactor poller decoupling, the
ABI-bumping part) as separate reviewed commits. Slice 2 depends on F3/F4 reactor +
scheduler stability (custom-executor socket I/O rides the same reactor path F3 hardened
and F4 makes fair).

## 0. The concrete failures this fixes (web-drift report §4.A)

- **No executor shutdown API.** "There is no executor shutdown API (per-server pools
  leak threads; a 2nd executor build/submit failed outright)." Custom executors
  (`build_executor` + `spawn_on`) leak their worker threads at end of life.
- **Custom-executor VTs don't service async socket I/O.** "Their worker threads do
  not reliably service async socket I/O (the epoll reactor appears tied to the
  default runtime → hung/flaky reads)." So you cannot run a `web.rest` server on a
  dedicated executor — only on the default one.

## 1. Runtime ground truth

- **One global reactor** (`drift_default_reactor`, a single epoll fd). Poll ownership
  is a 2-state handoff (`poll_owner`): `POLL_OWNER_REACTOR` (a reactor thread owns
  `epoll_wait`) or `POLL_OWNER_WORKER` (the **single idle default worker** owns it).
- The worker-owns-epoll path is **gated to a single-thread executor**:
  `… || exec->threads_count != 1) return 0;` (≈L745). A multi-thread or non-default
  executor can never take poll ownership.
- Edge delivery already dispatches a woken VT to **its own** executor
  (`drift_exec_enqueue(read_io_vt->exec, …)`), so cross-executor *dispatch* works —
  the gap is purely **who runs `epoll_wait`** when the default worker isn't the one
  idling.
- `DriftExec`: `mu, cv, head/tail, shutting_down, queue_len, queue_limit, running,
  threads_count, threads[], destroyed, node_freelist`. `drift_exec_create_internal
  (min,max,queue_limit,stack)`; `drift_exec_submit` (= `spawn_on`). Teardown today is
  `drift_exec_shutdown_all_atexit` (process exit only).
- **Blocking pool** (`posix/blocking_pool.h`, `drift_blocking_pool_quiesce`) is a
  separate thread pool for blocking fs ops; VTs park on it via a refcount protocol.

**Diagnosis:** the reactor is logically global but its *servicing* is entangled with
the default executor's idle worker. F5 must (a) guarantee an epoll poller exists
independent of the default executor, and (b) add a real executor lifecycle.

## 2. Public API shape

```drift
// std.concurrent (additive)
pub fn build_executor(min_threads: Int, max_threads: Int, queue_limit: Int)
    nothrow -> core.Result<Executor, ConcurrencyError>     // already exists in spirit; formalize
pub fn spawn_on<T>(exec: &Executor, var cb: core.Callback0<T>)
    nothrow -> core.Result<VirtualThread<T>, ConcurrencyError>   // exists

// NEW:
pub fn shutdown(self: &mut Executor, mode: ShutdownMode, timeout: conc.Duration)
    nothrow -> core.Result<ShutdownReport, ConcurrencyError>

pub variant ShutdownMode { Drain, Cancel }   // Drain = let queued+running finish; Cancel = cancel them
pub struct ShutdownReport {
    pub completed: Int,   // VTs that finished
    pub cancelled: Int,   // VTs cancelled
    pub timed_out: Bool   // deadline hit before quiescent
}
```
- **DECIDED (v1): `shutdown` is explicit and mandatory** for a non-leaking executor;
  it is **idempotent**. **No blocking destructor.** A `Destructible` impl, if added at
  all, may do *only* a **non-blocking best-effort cancel signal** (set
  `shutting_down=1` + `drift_thread_cancel` each owned VT) and **must not join/wait** —
  a destructor that could block on parked I/O / blocking-pool jobs is unacceptable
  (unpredictable drop latency). The atexit backstop reaps a leaked executor at process
  end, so forgetting `shutdown` leaks until exit (loud in the leak gate §7.1), not a
  silent hang.
- `timeout <= 0` → bounded "best effort, return immediately with `timed_out` if not
  already quiescent" (no implicit unbounded wait).

## 3. Executor lifecycle states + per-VT behavior

Executor states: **Running → Draining/Cancelling → Joined(destroyed).**

| VT state at shutdown | Drain | Cancel |
|---|---|---|
| **queued** (in ready list, not started) | run to completion | mark cancelled; dispatch so it observes cancel at entry and unwinds; OR drop unstarted (decide: drop is cheaper, must run drop-thunks for captured Arcs) |
| **running** (on a worker) | run to completion | set `cancelled`; it observes cancel at its next park site (cooperative) |
| **completed** | counted, reaped | counted, reaped |
| **cancelled** (already) | reaped | reaped |
| **I/O-parked** (in `reactor_wait_park` / `_block_on_io`) | Drain waits for the I/O to complete or `timeout` | **cancel via the reactor claim path** (F3: `reactor_wait_park` re-checks `cancelled` and returns CANCELLED — already implemented), then unwind |
| **timer-parked** (`conc.sleep`) | wait or timeout | cancel wakes it (existing) |
| **blocking-pool-parked** (fs op) | wait for the job; cannot cancel a syscall in flight | mark cancelled; the job completes and unparks; the VT observes cancel after — `drift_blocking_pool_quiesce` is the teardown primitive |

**Invariant:** `shutdown` returns only when every VT submitted to this executor is in
`completed`/`cancelled` AND all its worker threads are joined — or `timeout` elapsed
(then `timed_out=true`, and the executor enters a "lingering" state that the atexit
backstop still reaps; document that returning on timeout does NOT free the executor).

## 4. Reactor integration — who polls, who owns wakeups, off-default I/O

**Decision: a dedicated, always-available reactor poller, decoupled from the default
worker.** Two viable shapes; recommend (A):

- **(A) Persistent reactor thread owns `epoll_wait` whenever ANY executor has
  I/O-parked VTs.** Keep the `POLL_OWNER_WORKER` fast path *only* for the default
  single-worker uncontended case (latency); for every other case the reactor thread
  polls. Concretely: drop the `exec->threads_count != 1` gate's effect on *whether
  the reactor is serviced* — instead track a global `io_parked_count`; while it is
  > 0 and no worker holds poll ownership, the reactor thread runs `epoll_wait` and
  dispatches woken VTs to their own executors (the dispatch already does this). This
  makes socket I/O work for VTs on **any** executor with no per-executor reactor.
- (B) Per-executor reactor (one epoll fd each). Rejected: multiplies fds/threads,
  splits timer/signal handling, and the fd→watch model is global today; far more
  invasive.

**Ownership of wakeups:** unchanged from F3 — the reactor (thread or worker) claims
the VT (`drift_vt_claim_for_resume`) and enqueues to `vt->exec`. F5 only guarantees a
*poller exists*; it does not change the claim/enqueue contract. F4's fairness gate
composes (the gate is per-`exec`, so each executor stays FIFO-fair independently).

**Off-default socket I/O:** a VT on a custom executor calls `_block_on_io` /
`poll_many` → registers on the global reactor (works today) → parks → the reactor
thread services epoll → claims + enqueues to the custom executor → its worker runs
it. The only change needed is guaranteeing the reactor thread polls (A above).

## 5. Shutdown semantics (precise)

1. **Mode = Drain:** set `exec->shutting_down = 1` but keep dispatching; stop
   accepting new `spawn_on` (return `Err(shutting_down)`). Wait until `queue_len==0`
   and `running==0` or `timeout`. Then join worker threads.
2. **Mode = Cancel:** set `shutting_down=1`; for every VT owned by this executor
   (queued + running + parked), `drift_thread_cancel` it (routes through the F3
   reactor-claim path for I/O-parked VTs). Wait for quiescence or `timeout`, then
   join workers.
3. **Timeout:** absolute deadline; on expiry return `ShutdownReport{timed_out=true}`
   without force-killing threads (no `pthread_cancel` — unsafe with fibers). The
   executor stays registered for the atexit backstop. Document: a timed-out shutdown
   may leak until process exit; the caller chose too short a deadline.
4. **Parked I/O VTs:** Cancel path uses F3's no-token reactor claim (already correct);
   Drain path lets them complete. A VT whose fd never becomes ready under Drain will
   hold shutdown until `timeout` — that is correct (Drain means "let work finish").
5. **Blocking-pool jobs:** cannot be interrupted mid-syscall. `shutdown` must
   `drift_blocking_pool_quiesce`-style wait for in-flight jobs of this executor's VTs
   before joining workers, else a job's completion-unpark targets a freed VT/exec
   (UAF). Teardown ordering (§6) handles this.
6. **Teardown ordering (critical — mirrors the 0.32.7 atexit-hang fix):**
   `shutting_down=1` → stop new submits → cancel/drain VTs → **quiesce blocking pool
   for this exec** → ensure reactor has no registrations referencing this exec's VTs
   (they were cleared on VT destroy via `forget_vt`) → join worker threads →
   free `DriftExec`. Never free the exec while the reactor or blocking pool can still
   reference its VTs.

## 6. ABI / runtime symbols

**ABI bump REQUIRED → next ABI after F3 (F3 is 18, so F5 = 19** unless F5 ships in the
same release as another bump). New runtime-exported intrinsics:
- `exec_build(min,max,queue_limit) -> ExecHandle` (formalize existing internal),
- `exec_shutdown(exec, mode, deadline_ms) -> packed report` (new),
- possibly `exec_running_count(exec)` / `exec_queue_len(exec)` for the stdlib driver
  + test probes.

Boundary-behavior changes (no new symbol, but ABI-relevant): reactor poller decoupling
(§4A); `DriftExec` gains lifecycle fields (`atomic io_parked_count` global, per-exec
`draining`/`report` counters). `Executor`/`ShutdownReport`/`ShutdownMode` stay **pure
Drift stdlib types** if `shutdown` is authored in Drift over the intrinsics (keep them
out of the ABI surface, as with F3's PollEntry/PollReady). Per the ABI policy, the new
exported intrinsics + reactor-servicing change ⇒ **bump + rebuild through cert.**

## 7. Leak / UAF / valgrind gates

1. **No thread leak:** build N executors, `spawn_on` work, `shutdown(Drain)` each;
   assert process-wide thread count returns to baseline (probe `/proc/self/task` or a
   runtime `live_worker_threads()` test counter). The §0 leak must be gone.
2. **Shutdown-Cancel UAF:** `spawn_on` an I/O-parked VT (blocked on a never-ready fd),
   `shutdown(Cancel, 1s)`; **valgrind clean** — the reactor must hold no dangling
   reference to the cancelled VT/exec after free (relies on F3 `forget_vt`).
3. **Blocking-pool race:** `spawn_on` a VT doing a slow fs op, `shutdown(Cancel)`
   while the job is in flight; valgrind-clean, no UAF on the completion-unpark
   (teardown ordering §6).
4. **Off-default socket I/O:** a full `web.rest`-style echo server on a *custom*
   executor (not default) completes N round-trips — proves §4 reactor integration.
   Plain + valgrind `--fair-sched=yes`.
5. **Double-shutdown / shutdown-after-timeout:** idempotent, no crash, no double-free.
6. **Submit-after-shutdown:** `spawn_on` returns `Err`, no enqueue, no leak.
7. **Default-executor untouched:** all F3/F4 tests still green (F5 must not regress
   the default single-worker path — the `POLL_OWNER_WORKER` fast path stays).

## 8. Smallest viable slice + ordering vs F4

- F5 is **independent of F4** and larger/riskier; sequence it **after** F4.
- Slice 1: **`Executor.shutdown(Drain/Cancel, timeout)` + thread-join** (fixes the
  leak, §0 first bullet) — without the reactor decoupling. Custom-exec VTs that do
  *no* socket I/O work fully; gates #1/#5/#6 pass. **This slice carries the ABI bump**
  (it introduces the `exec_shutdown` intrinsic — the first new exported symbol); ship
  it through cert.
- Slice 2: **reactor poller decoupling (§4A)** so custom-exec socket I/O works
  (fixes §0 second bullet); gates #2/#3/#4. Highest-risk runtime change; **no
  *additional* ABI bump** if it adds no new exported symbol (boundary *behavior* only)
  — but it ships on the already-bumped ABI from Slice 1, so the two slices land in one
  ABI generation. Isolate it as its own reviewed commit regardless.
- Rollback: each slice is a separable commit; Slice 2 can be reverted (reactor
  reverts to default-worker-owned epoll) leaving Slice 1's shutdown intact.

## 9. Open questions for review
1. Cancel-mode treatment of **unstarted queued** VTs: drop (cheap, must run capture
   drop-thunks) vs dispatch-to-observe-cancel (uniform). Recommend dispatch for
   uniformity unless it shows as a teardown latency cost.
2. Bundle the F5 ABI bump with another close-by bump vs its own — release timing.

(Resolved: Destructible → explicit-mandatory shutdown, non-blocking best-effort
cancel only, §2.)
