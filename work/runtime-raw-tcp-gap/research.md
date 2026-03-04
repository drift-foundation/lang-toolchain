# Raw TCP Gap: Drift vs Go — Localization Note

**Date:** 2026-03-03
**Benchmark:** single-connection keep-alive, 64-byte synchronous echo, loopback
**System:** 16-core x86-64, Linux 6.17, CONFIG_HZ=1000, PREEMPT_DYNAMIC

## Numbers

| Metric | Go raw TCP | Drift optimized | Drift debug |
|--------|-----------|----------------|-------------|
| req/sec | ~116,000 | ~69,000–82,000 | ~98 |
| per-iter | ~8.6 μs | ~12.2–14.5 μs | ~10.2 ms |

Gap to close (optimized Drift vs Go): **~3.6–5.9 μs per iteration**.

Debug build is ~700× slower — entirely explained by missing `-O2`.
The rest of this note concerns only the optimized-build gap.

## Architecture comparison

### Go
- Goroutine context switch: register save/restore in assembly, **no syscall**.
  Signal mask is not part of goroutine state.
- netpoll integrated into scheduler: `findrunnable()` → `netpoll()` → `epoll_wait`.
  IO-ready goroutine goes onto local P runqueue — **no cross-thread signal**.
- Single-thread common case: same M that blocked the goroutine resumes it
  after the next `netpoll()` check.

### Drift
- VT context switch via glibc `swapcontext` → **2 × `rt_sigprocmask`** per call.
  Each is a kernel entry/exit (~0.2–0.4 μs each on this hardware).
- Separate reactor thread.  IO-ready VT must cross from reactor thread → executor
  thread via: reactor mutex → `enqueue` → `pthread_cond_signal` → worker
  `futex(WAKE)` → worker `pthread_cond_wait` returns → exec mutex → dequeue →
  `swapcontext` back to VT.
- Every server-side read that returns EAGAIN triggers the full park/unpark cycle.

## Per-iteration cost breakdown (server VT, optimized estimate)

Each benchmark iteration = client write → server read → server write → client read.
The server read hits EAGAIN once (data not yet arrived), triggering one park/unpark.

| Step | Syscalls | Est. cost |
|------|----------|-----------|
| `register_io`: reactor mutex + `epoll_ctl` + timer malloc | `epoll_ctl` | ~0.5–1.0 μs |
| `swapcontext` (park): VT → scheduler | `rt_sigprocmask` × 2 | ~0.4–0.8 μs |
| Scheduler: no work → `pthread_cond_wait` | `futex(WAIT)` | ~0.3 μs to enter |
| Reactor: `epoll_wait` returns (data ready) | `epoll_wait` | ~0.1 μs (data already there) |
| Reactor: cancel timer + find watch + unpark | mutex × 2, `epoll_ctl`, `futex(WAKE)` | ~0.8–1.5 μs |
| Worker: wake from futex → dequeue | `futex` return, mutex | ~0.3–0.8 μs |
| `swapcontext` (resume): scheduler → VT | `rt_sigprocmask` × 2 | ~0.4–0.8 μs |
| **Total overhead per EAGAIN cycle** | | **~2.8–5.2 μs** |

Go's equivalent (gopark + goready, same-M resume, no sigprocmask, no cross-thread):
estimated ~0.3–0.8 μs.

**Predicted gap: ~2.0–4.4 μs** — consistent with the observed ~3.6–5.9 μs.

## Ranked likely causes

### 1. `swapcontext` → `rt_sigprocmask` (high confidence)

**Evidence:** `objdump` confirms glibc `swapcontext` calls `sigprocmask` twice.
4 kernel entries per park/unpark cycle.  At ~0.3 μs each = **~1.2 μs/iter**.
Go pays zero here (userspace goroutine switch).

**Fix:** Replace `swapcontext`/`makecontext` with a custom assembly context switch
that saves/restores only callee-saved registers + stack pointer, skipping the
signal mask.  This is what Go, libaco, Boost.Context, and libco all do.
Expected savings: **~1.0–1.2 μs/iter**.

### 2. Cross-thread reactor → executor handoff (high confidence)

**Evidence:** Drift's unpark path requires: reactor mutex lock, timer cancel
(O(n) scan), watch clear, `epoll_ctl` MOD, executor mutex lock, enqueue,
`pthread_cond_signal` (→ `futex(WAKE)`), executor mutex unlock, reactor mutex
unlock.  Go's integrated netpoll avoids the cross-thread signal entirely in the
common case.

**Fix (incremental):** Let executor workers call `epoll_wait` directly when the
run queue is empty, instead of blocking on condvar.  When epoll returns with
IO-ready fds, the worker resumes the VT immediately without a cross-thread
signal.  The dedicated reactor thread becomes a fallback for timers and for when
all workers are busy.  Expected savings: **~1.0–2.0 μs/iter** (eliminates
futex WAIT/WAKE + 2 mutex acquisitions).

**Fix (timer management):** Replace malloc'd timer linked list with a
slot-per-VT deadline field.  Eliminates malloc/free per IO op and the O(n)
timer scan on IO completion.  Expected savings: **~0.2–0.5 μs/iter**.

### 3. `epoll_ctl` churn (moderate confidence)

**Evidence:** Each EAGAIN cycle does: `epoll_ctl(MOD, fd, EPOLLIN)` to arm,
then reactor does `epoll_ctl(MOD, fd, 0)` to disarm after wakeup.  Two
`epoll_ctl` syscalls per IO op.  Go uses `EPOLLET` (edge-triggered) and
avoids rearming entirely for active connections.

**Fix:** Switch to edge-triggered (`EPOLLET`) + persistent registration.  The
watch stays armed; reactor doesn't need to disarm after each IO event.
Expected savings: **~0.3–0.6 μs/iter**.

## Ruled out

| Candidate | Why |
|-----------|-----|
| Reactor `in_wait` overhead | Relaxed atomic, unmeasurably cheap in optimized build |
| ExecNode malloc/free | Freelist already eliminates this on hot path |
| Main-thread 10ms nanosleep poll | Client reads succeed without EAGAIN in this benchmark (server response arrives before client retries); measured 0 nanosleep calls on main thread in strace |
| Kernel scheduling latency (CFS) | HZ=1000 + PREEMPT_DYNAMIC, futex WAKE is fast; confirmed by the optimized build achieving 69–82k not ~100 |
| TCP_NODELAY | Already set in the benchmark |

## Main-thread IO polling (separate concern)

The main-thread IO path (`_park_main_thread_io`) uses `nanosleep(10ms)` polling
instead of epoll registration.  This doesn't affect the benchmark (client reads
succeed without blocking), but it will be a bottleneck for any main-thread VT
that does IO-heavy work with EAGAIN.  Worth fixing separately:
`vt_current() == 0` should fall back to a direct `poll(fd, events, timeout)`
instead of blind nanosleep.

## Recommendation: priority order

1. **Custom asm context switch** — drop-in replacement for `swapcontext`/`makecontext`
   that skips `rt_sigprocmask`.  ~50 lines of x86-64 assembly.  Biggest
   single-item win (~1.0–1.2 μs).

2. **Inline epoll into executor idle path** — when run queue is empty, worker
   calls `epoll_wait` instead of condvar.  Eliminates cross-thread handoff on
   the hot path (~1.0–2.0 μs).

3. **Edge-triggered epoll** — switch to `EPOLLET`, persistent watch registration,
   remove per-IO-op `epoll_ctl` arm/disarm (~0.3–0.6 μs).

4. **Slot-based timer** — replace malloc'd timer list with per-VT deadline field
   (~0.2–0.5 μs).

Items 1 + 2 together should close the bulk of the gap (est. 60–80% of the
~3.6–5.9 μs difference).
