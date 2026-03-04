# EPOLLET Design Report: Persistent ET with Bounded Drain

**Date:** 2026-03-04
**Author:** Klaudia (compiler team review)
**Status:** MVP runtime milestone — design document, pending implementation plan approval
**Target:** MVP

---

## 1. Current state

### Raw TCP (single-connection keep-alive echo, loopback)

| Runtime | req/s | per-iter latency | ratio |
|---------|-------|------------------|-------|
| Go | ~116,000 | ~8.6 μs | 1.0x |
| Drift (optimized, baseline) | ~100,000 | ~10.0 μs | ~1.15x slower |
| Drift (optimized, ET experiment) | ~119,000 | ~8.4 μs | ~1.0x (parity) |

### HTTP (realistic server workload)

| Runtime | req/s |
|---------|-------|
| Go | ~46,000–50,000 |
| Drift | ~53,000–56,000 |

Drift HTTP is already competitive.  Raw TCP reaches Go parity under the ET
experiment, confirming the ceiling is real.

### What we already shipped (Phase A, 2026-03-03)

1. **Custom x86_64 context switch** — eliminated `rt_sigprocmask` churn.
2. **Worker-side polling** — idle worker calls `epoll_wait` directly.
3. **ExecNode freelist** — eliminated malloc/free on enqueue/dequeue.
4. **Redundant reactor wake reduction** — `in_wait` coalescing.

### What the ET experiment proved (2026-03-04)

A minimal runtime-only hack (`DRIFT_EXP_ET_PERSIST=1`) that registers fds
with `EPOLLET | EPOLLIN | EPOLLOUT` once and skips all hot-path
`epoll_ctl` arm/disarm:

| | Baseline | ET experiment |
|---|---|---|
| Median (5000 iters) | 50 ms | 42 ms |
| req/s | ~100,000 | ~119,000 |
| μs/iter | ~10.0 | ~8.4 |
| `epoll_ctl` calls | 28,824 (~5.7/iter) | 4 (setup only) |
| Total syscalls | 83,957 | 64,288 |

**16% throughput improvement, Go parity reached.**  No hangs, no stalls.
The benchmark's small payloads (64 bytes) drain completely per read, so
the edge is consumed naturally.  This is the ceiling; production correctness
requires the bounded-drain + replay design below.

### What is ruled out

| Candidate | Why |
|-----------|-----|
| Timer-node churn | Measured at 0.36 μs (2.3%), within noise |
| Persistent LT without disarm | Causes hot spin on ready fds |
| EPOLLET as a "minor optimization patch" | Requires deliberate fairness + replay design |

---

## 2. EPOLLET correctness model

### Edge-triggered semantics

Level-triggered epoll re-fires on every `epoll_wait` while the fd is ready.
Edge-triggered (`EPOLLET`) fires once when the fd *transitions* to ready,
then stays silent until the fd transitions back to not-ready (EAGAIN).

### The correctness contract

Under EPOLLET, the runtime must guarantee:

1. **Every edge is consumed.**  When a VT is woken by an edge, it must
   eventually drain the fd to EAGAIN — either in one scheduler turn or
   across multiple turns via internal replay.  If the edge is not consumed,
   the fd stalls permanently (no new kernel notification until the next
   ready→not-ready→ready transition).

2. **No VT parks on a consumed edge.**  If a VT calls `_block_on_io` but
   the fd has `pending_ready` set (edge not yet consumed to EAGAIN), the
   VT must not enter epoll wait.  It must be replayed immediately.

3. **Close/cancel clears pending state.**  If an fd is closed or a VT is
   cancelled while `pending_ready` is set, the pending state is cleared.
   No orphaned replay.

### Why this differs from current semantics

Current: one-shot LT.  Each `_block_on_io` registers the fd, parks, wakes
on any level-triggered readiness, disarms.  The VT retries one syscall and
returns.  Correctness is trivial — LT re-fires if data remains.

EPOLLET: the kernel fires once.  If the VT doesn't drain to EAGAIN, no
further notification arrives.  The runtime must track whether the edge has
been fully consumed and handle the case where it hasn't.

---

## 3. Design: bounded drain with internal ready replay

### 3.1 Persistent ET watch

Each fd gets a persistent watch for its lifetime:

- Created on first `drift_reactor_register_io` with
  `EPOLL_CTL_ADD(EPOLLET | EPOLLIN | EPOLLOUT)`.
- Deleted on `drift_reactor_forget_fd` with explicit `EPOLL_CTL_DEL`.
- No hot-path `epoll_ctl` between creation and deletion.

### 3.2 Per-direction watch state

The watch tracks waiter and readiness state per direction:

```
ReactorWatch {
    int fd;
    uint64_t read_vt;         // VT parked for EPOLLIN, or 0
    uint64_t write_vt;        // VT parked for EPOLLOUT, or 0
    uint8_t  pending_read;    // 1 = fd still readable, edge not consumed
    uint8_t  pending_write;   // 1 = fd still writable, edge not consumed
    ReactorWatch *next;
}
```

**Why per-direction:**  A socket can be writable almost always.
Write-readiness must not interfere with read wait semantics.  A VT
waiting for EPOLLIN must not be spuriously replayed because EPOLLOUT
arrived.  Per-direction state keeps the two independent.

**Event delivery mapping:**  When epoll returns events for an fd:
- `EPOLLIN` → resolve `read_vt`
- `EPOLLOUT` → resolve `write_vt`
- `EPOLLIN | EPOLLOUT` → resolve both
- `EPOLLERR | EPOLLHUP` → resolve both (error/hangup wakes all waiters)

### 3.3 Fairness budget

Each VT tracks cumulative successful IO bytes since its last scheduler
yield.  The budget is enforced in `_block_on_io`:

```
if vt.io_bytes_since_yield >= DRAIN_BUDGET_BYTES:
    mark watch pending_ready for this direction
    vt.io_bytes_since_yield = 0
    yield to scheduler (re-enqueue VT)
    return  // VT resumes on next scheduler turn
```

**Budget unit: bytes, not operations.**  A "max 8 reads" cap is unfair
across payload sizes (8 × 64B = 512B vs 8 × 64KB = 512KB).  Byte-based
budgeting gives consistent fairness regardless of message size.

**Initial default:** 64KB.  This is one typical socket buffer fill.
The value is an internal constant, not a public knob.

**Common case:** Most IO drains to EAGAIN well within 64KB (small HTTP
request/response, protocol frames).  The budget is never hit.  Zero
overhead — the check is a comparison against a per-VT counter.

**Hot-fd case:** A VT reading from a fast sender (e.g., large file
transfer, streaming response) hits the budget.  The VT yields, other
VTs get a turn, then this VT resumes and continues draining.

### 3.4 Internal ready replay

When a VT yields due to budget exhaustion (not EAGAIN):

1. Set `pending_read` or `pending_write` on the watch.
2. Reset `vt.io_bytes_since_yield = 0`.
3. Re-enqueue the VT to the executor run queue via `drift_exec_enqueue`.

**The replay queue is just the executor run queue.**  No separate data
structure.  The VT competes fairly with all other runnable VTs in FIFO
order.  No priority inversion, no special scheduling.

When the VT resumes and calls `_block_on_io` again:

1. Check `pending_read`/`pending_write` for this fd+direction.
2. If pending: clear the flag, return immediately (VT retries IO
   without parking).
3. If not pending: normal epoll park path.

**Replay terminates when:**
- The fd reaches EAGAIN (edge consumed, `pending` stays clear).
- The operation completes fully.
- Timeout expires, VT is cancelled, or fd is closed.

### 3.5 Stdlib interaction

**`TcpStream.read` and `TcpStream.write` do not change their return
semantics.**  They still return after one successful syscall.  The drain
happens naturally through the VT's loop:

```
// Application code (unchanged):
while need_more_data {
    stream.read(&mut buf, timeout);   // returns one read
    process(buf);
}
```

Each `read` call succeeds (data in kernel buffer), returns.  The VT calls
`read` again.  The budget counter increments.  When the budget is
exhausted, `_block_on_io` (called internally on the next EAGAIN or
budget-yield) forces a scheduler yield.

**`_block_on_io` contract shift:**
- Current: "park and wait for readiness."
- New: "park and wait for readiness, OR return immediately if readiness
  is already pending from a prior edge."

The budget enforcement and replay check live inside `_block_on_io`.
No stdlib `read`/`write`/`accept` semantic change.

---

## 4. Timeout, close, and cancel interactions

### 4.1 Timeouts

Timeouts apply to the **blocking portion** of IO, not to active drain work.

- While a VT is actively draining (fd is ready, syscalls succeed), no
  timeout fires.  The VT is running, not parked.
- When the VT hits EAGAIN and parks on epoll, the timeout starts (via
  `drift_reactor_register_timer` as today).
- When the VT is replayed (budget yield, re-enqueued), no timeout is
  active — the VT is runnable, not blocked.

**State machine:**
```
DRAINING (active IO, budget counting)
  → EAGAIN: transition to PARKED, start timeout
  → budget hit: transition to QUEUED (re-enqueued), no timeout
  → success + more work: stay DRAINING

PARKED (waiting for edge)
  → edge arrives: transition to DRAINING
  → timeout fires: wake VT with timeout indication

QUEUED (pending-ready replay, waiting for scheduler turn)
  → dequeued by worker: transition to DRAINING
  → cancel: clear pending, do not resume
```

### 4.2 Close

`drift_reactor_forget_fd(fd)`:
1. Lock `r->mu`.
2. `EPOLL_CTL_DEL(fd)`.
3. Clear `read_vt`, `write_vt`, `pending_read`, `pending_write`.
4. Unlink and free watch.
5. Unlock.

If a VT is currently re-enqueued for replay on this fd, it will resume,
call `_block_on_io`, find no watch (fd closed), and return an error
through the normal stdlib error path.

### 4.3 Cancel

When a VT is cancelled (`drift_exec_cancel`):
1. Set `vt->cancelled` flag (existing mechanism).
2. If the VT is parked on a watch: clear `read_vt`/`write_vt` for that
   fd under `r->mu`.  Clear `pending_read`/`pending_write` if they
   reference this VT.
3. If the VT is re-enqueued for replay: the worker will dequeue it,
   see `cancelled`, and skip execution (existing T7 path).

No orphaned pending state.  No orphaned replay.

---

## 5. Benchmark and stress test matrix

### Correctness tests

| Test | What it validates |
|------|-------------------|
| Single-connection echo (existing `perf_vt_loopback_baseline`) | Basic ET + drain correctness |
| Multi-connection concurrent echo | Per-fd watch isolation |
| Large payload streaming (>64KB single write) | Budget enforcement triggers replay |
| Partial read under load | Pending-ready replay completes drain |
| Connection reset during drain | Close clears pending, no stall |
| Timeout during park after EAGAIN | Timer fires correctly under ET |
| Timeout during replay (budget yield) | No spurious timeout on active VT |
| Cancel during pending replay | Cancelled VT not resumed |
| Accept burst (many connections queued) | Accept drains to EAGAIN, replay if budget hit |
| Simultaneous read+write on same fd | Per-direction independence |

### Performance benchmarks

| Benchmark | Measures | Target |
|-----------|----------|--------|
| Raw TCP loopback (single conn, 64B) | Syscall overhead, ET ceiling | ≥115k req/s |
| Raw TCP loopback (single conn, 4KB) | Drain behavior, medium payload | No regression vs baseline |
| Raw TCP loopback (single conn, 64KB) | Budget enforcement, large payload | Bounded drain, no starvation |
| HTTP small payload (GET /health) | Full-stack ET correctness | No regression from current ~53k |
| Multi-connection concurrent (10 conns) | Fairness under load | p99 latency ≤2x p50 |
| Hot sender + idle receivers | Starvation resistance | Idle VTs make progress within 1 budget cycle |

### Stress tests

| Test | What it catches |
|------|-----------------|
| ASAN build, full matrix above | Use-after-free on watch/VT lifecycle |
| Valgrind memcheck on loopback | Memory leaks in watch lifecycle |
| 100k iterations, single connection | Long-running stability |
| Rapid connect/close cycling | Watch create/destroy under ET |
| `strace -c -f` before/after | Confirm `epoll_ctl` eliminated from hot path |

---

## 6. Scope estimate

| Component | Lines (est.) | Risk |
|-----------|-------------|------|
| Persistent ET watch lifecycle (`thread_runtime.c`) | 40–60 | Low — experiment already proved runtime half works |
| Per-direction waiter + pending state | 30–50 | Low — struct changes + event handler updates |
| Budget tracking in `_block_on_io` (`std.io`) | 20–30 | Low — per-VT counter + comparison |
| Replay check in `_block_on_io` | 20–30 | Medium — must interact correctly with timeout path |
| `forget_fd` + cancel cleanup | 20–30 | Medium — must clear all pending state |
| Timer interaction (no timeout during active drain) | 20–40 | Medium — state machine must be precise |
| Tests (correctness + stress) | 200–400 | Time cost, not complexity risk |
| Benchmark campaign (tuning drain budget) | — | Calendar time |

**Total runtime+stdlib: ~150–240 lines of production code.**
**Total including tests: ~400–600 lines.**

This is a contained runtime milestone, not a scheduler rewrite.

---

## 7. Configuration policy

**No public drain budget knob in v1.**

The drain budget (initially 64KB) is an internal runtime constant.  It is
not exposed as a socket option, executor setting, or environment variable.

**Rationale:**
1. It is a low-level scheduler/netpoll policy, not an app-facing option.
2. Users lack context to tune it correctly.
3. A bad value silently trades throughput for fairness or vice versa.
4. Exposing a knob too early freezes an interface before the right model
   is known.

**Tuning process:**
1. Ship with a fixed internal default (64KB).
2. Benchmark across the matrix in section 5.
3. Look for the knee of the curve: smallest budget that captures most
   throughput benefit without visible fairness regression.
4. Adjust the internal constant based on data.
5. Only consider exposing a knob if real-world workloads demonstrate that
   one default cannot serve all cases — and if so, expose it as
   executor-level tuning, not per-socket.

The constant is per-VT (stored on the VT struct), which naturally supports
future per-executor configuration without global atomic contention.

---

## 8. Recommendation

**Target: MVP.**

### Why MVP, not post-MVP

The original report recommended deferral based on a 0.3–0.6 μs estimate.
The experiment measured **1.6 μs/iter (16%)**, reaching Go parity.  Three
things changed:

1. **The gain is larger than estimated.**  16% on raw TCP is not a
   micro-benchmark curiosity — it's the difference between "slightly
   slower than Go" and "at parity."  For a systems language, raw IO
   performance is a credibility signal that directly affects adoption.

2. **The design is now complete.**  The bounded-drain + internal-replay
   model resolves the fairness concern that was the strongest argument
   for deferral.  This is not "ship EPOLLET and hope fairness works out."
   It is a deliberate runtime architecture with explicit correctness
   contracts.

3. **Runtime ownership is an MVP concern.**  If Drift ships with a
   runtime that is architecturally limited to LT arm/disarm on every IO
   cycle, that becomes the baseline users benchmark against.  Fixing it
   post-MVP means re-learning the reactor internals, re-validating all IO
   paths, and shipping a behavioral change to existing users.  Doing it
   now — while Phase A is fresh and the runtime is under active
   development — is cheaper.

### What MVP means concretely

1. Persistent ET registration (runtime layer).
2. Per-direction watch state with pending-ready tracking.
3. Bounded drain with byte-based budget (internal constant, 64KB default).
4. Internal replay via executor re-enqueue.
5. Correct timeout/close/cancel interaction.
6. Full correctness + stress test coverage.
7. Benchmark campaign to validate the default budget.

### What MVP does not include

- Public configuration knob for drain budget.
- Multi-worker-specific optimizations (Phase B concern).
- VT preemption (separate, larger project — bounded drain is the
  cooperative alternative).
- Stdlib API changes (read/write still return after one successful call).
