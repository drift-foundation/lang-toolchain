# F4 — scheduler fairness — implementation-ready plan (design only)

Status: **design, do not implement until F3 is merged.** Start the F4 branch from the
certified F3 base. Grounded in `lang/language_runtime/posix/thread_runtime.c`.

## 0. The concrete failure this fixes

From the web-drift report §4.A (one-fiber-per-connection server, co-located
client+server on the default single worker):
- A freshly-spawned (or freshly-woken) connection fiber **never gets its first
  slice** — it starves behind already-ready I/O fibers. A `sleep`-based handshake
  papers over the *first* scheduling but not later keep-alive requests.
- Measured: original inline server 0/10 plain + 3/3 valgrind stable; **per-connection
  fiber server 1/5 plain + 5/5 valgrind FAIL** (intermittent `client.send` failures
  on the 3rd+ pooled request, deterministic under valgrind).
- Net effect: the fair one-fiber-per-connection design is unusable; the team had to
  fall back to a single inline fiber.

## 1. Root cause (verified in the runtime)

The ready queue is **already FIFO** (`DriftExec.head/tail`; dequeue from head
`~L1009`, enqueue at tail `drift_exec_enqueue ~L1793`). FIFO alone should be fair —
but the **reactor's FAST-I/O direct-resume path bypasses the queue entirely**:

- When an fd becomes ready, edge delivery sets `direct_vt` and `swapcontext`s
  *straight into* the woken VT (`~L878`, the "FAST-I/O direct-resume: the
  swapcontext IS the wake" path), instead of enqueuing it at the tail.
- A hot I/O fiber (e.g. the server's accept/read fiber) is therefore resumed
  **immediately, ahead of everything already queued**, every time its fd is ready.
  On a single worker, a newly-`spawn`ed connection fiber sits at the tail and the
  worker is perpetually in direct-resume chains for the hot fiber → the queued
  fiber starves. Under valgrind (serialized, no timing slack) this is deterministic.

So this is **not** a FIFO-vs-LIFO problem; it is a **queue-bypass** problem: the
direct-resume jumps the FIFO line.

## 2. Recommended approach — ready-age FIFO fairness (NOT multi-worker)

**Recommendation: gate the direct-resume on the ready queue, keeping FIFO honest.**
Direct-resume is allowed only when it does not jump a waiting VT; otherwise the
woken VT is **enqueued at the tail** (the path already exists — `enqueue_vt`) and the
worker drains the queue in FIFO order.

Rejected alternative — **multi-worker reactor-integrated scheduling**: bigger and
riskier (the report itself found custom multi-thread executors "do not reliably
service async socket I/O … hung/flaky reads"); introduces cross-worker reactor
ownership races and needs careful work-stealing. It does not fix the *fairness*
bug more cheaply than gating direct-resume, and F4's goal is the starvation fix.
Multi-worker throughput scaling is a separate, later concern (note it, don't build
it here).

### 2.1 The rule
**Reuse the existing `DriftExec.queue_len`** — confirmed to be exactly the ready-queue
(`ExecNode` head/tail list) length: it is `++`'d in `drift_exec_enqueue` (~L1806),
`--`'d on dequeue (~L1016, ~L1828), reset to 0 on drain (~L1665), all **under
`exec->mu`**. (`exec_ready_queue_len` already exports it; `queue_len + running` is the
total-work metric at ~L2632.) **Do NOT add a duplicate `ready_count`.**

> **A woken VT may be direct-resumed (swapcontext) ONLY if, observed under the target
> executor's `exec->mu`, `queue_len == 0`. Otherwise it is enqueued at the tail**
> (under that same `exec->mu` hold) and the worker picks it up in FIFO order after the
> currently-queued VTs.

This preserves the fast path for the uncontended case (no queued work → direct
resume, zero latency) and guarantees that **no VT jumps a VT that was already
waiting** — bounding starvation to "at most the current queue depth ahead of you."

### 2.1a Locking (must be explicit, not hand-wavy)
The check and the resulting action are a **single locked decision** on the target
executor:
```
// Edge delivery already holds r->mu and has CLAIMED the VT (PARKED->READY).
// r->mu -> exec->mu is the established lock order (existing enqueue path).
DriftExec *ex = vt->exec;
int direct = 0;
pthread_mutex_lock(&ex->mu);
if (ex->queue_len == 0) {
    direct = 1;                     // uncontended snapshot -> fast path
} else {
    drift_exec_enqueue(ex, vt);     // contended -> FIFO tail (++queue_len), under exec->mu
}
pthread_mutex_unlock(&ex->mu);
if (direct) direct_vt = vt;         // swapcontext AFTER r->mu release (existing fast path)
```
- The enqueue-vs-direct decision is made from the **`queue_len == 0` observation taken
  while holding `exec->mu`**, so it cannot race a concurrent enqueue/dequeue.
- A VT enqueued by another thread *after* we observed `queue_len == 0` and chose
  direct-resume is a *newly-arrived* waiter — direct-resuming our (earlier-observed-
  ready) VT ahead of it does not violate the invariant (our readiness preceded its
  arrival). The invariant is about not jumping VTs *already waiting at decision time*.
- **Lifetime:** the claim (`PARKED→READY`) already prevents `drift_vt_destroy` →
  `forget_vt` from freeing the VT before it runs (it would need the state to flip),
  so the post-`r->mu` swapcontext is safe — same guarantee the existing direct-resume
  relies on. Keep the `exec->mu` section nested inside `r->mu` exactly as the current
  loop-2 enqueue does (the lifetime-barrier comment at ~L1419).

### 2.2 Optional refinement (only if §2.1 underserves latency)
A pure "queue-empty" gate can ping-pong a hot fiber when the queue briefly drains.
If needed, add a **per-resume fairness budget** reusing the existing
`io_bytes_since_yield` / `DRIFT_IO_BUDGET_BYTES` machinery: a fiber that has been
direct-resumed N consecutive times (or drained > budget bytes) is force-enqueued
even if the queue is empty, giving the reactor a chance to surface other readiness.
Keep this OFF in the first slice; add only if a latency test demands it.

## 3. Scheduler invariants (post-F4)

1. **FIFO-honest:** if VT A is enqueued-ready before VT B becomes ready, A runs
   before B. (Direct-resume may only occur into an otherwise-empty queue.)
2. **Single-claim:** every wake still resolves through `drift_vt_claim_for_resume`
   (CAS PARKED→READY) so a VT is enqueued/resumed exactly once across racing
   resumers (edge / timer / cancel / join). F4 changes *where* the won VT goes
   (queue vs swapcontext), never *whether* it is claimed.
3. **No queue-bypass under contention:** `queue_len > 0` (observed under `exec->mu`)
   ⇒ no direct-resume; the woken VT is enqueued at the tail.
4. **Progress:** a READY VT is dispatched within at most `ready_count` worker turns
   of becoming ready (no unbounded starvation).

## 4. Existing paths affected (and the change to each)

| Path | Today | After F4 |
|---|---|---|
| ready queue (`drift_exec_enqueue`/dequeue) | FIFO head/tail + `queue_len` (under `exec->mu`) | **unchanged** — reuse `queue_len`, no new counter |
| reactor edge delivery (both loops) | sets `direct_vt` → swapcontext | direct-resume only if `queue_len==0` observed under `exec->mu`; else enqueue at tail (§2.1a) |
| `yield_now` (`drift_thread_yield`) | re-enqueues self at tail + swapcontext to sched | unchanged — already FIFO-fair (it is the model F4 generalizes) |
| timer wake (`fire_timer`, W7) | claim + enqueue (wait-set) / `drift_thread_unpark` (legacy) | unchanged (already enqueues); ensure no direct-resume added |
| cancel wake (`drift_thread_unpark`) | claim PARKED→READY + enqueue | unchanged (already enqueues) |
| join wake (`drift_thread_unpark` of `join_waiter`) | claim + enqueue | unchanged |
| F3 `reactor_wait_park` resume | scheduler picks enqueued VT via swapcontext | unchanged; benefits from fairness automatically |

**Note:** most wake paths *already* enqueue; the ONLY queue-bypass is the reactor
edge-delivery direct-resume. F4 is therefore a **narrow change to two edge-delivery
sites** (the `direct_vt` decision) plus a `ready_count` counter. This is why it is
the minimal approach.

## 5. Deterministic tests (not perf)

A reproducer that **fails today, passes after F4**, asserted on observable state (not
timing):

1. **Starve-the-newcomer (the core repro).** The hot fiber must **actually park and be
   reactor-woken each iteration** — a busy loop on an always-ready fd never parks, so
   it would NOT exercise direct-resume and would not reproduce the bug. Construct it as:
   the hot fiber owns an **eventfd** (or socketpair); each iteration it **drains to
   EAGAIN, registers read interest (parks in the wait-set), and a peer fiber/thread
   re-arms readiness (writes the eventfd)** so the reactor wakes it via the
   direct-resume path. Meanwhile spawn a newcomer fiber that increments a shared
   `AtomicInt` once and exits. Assert the newcomer's increment is observed within a
   bounded number of hot-fiber wake iterations (hot fiber publishes an iteration
   counter; assert `newcomer_ran` before `hot_iters > K`). **Today:** each wake
   direct-resumes the hot fiber ahead of the queued newcomer → newcomer never runs
   within K → fail. **After F4:** with the newcomer queued, `queue_len>0` forces the
   hot fiber's wake to enqueue → FIFO → newcomer runs → pass. Run under valgrind
   `--fair-sched=yes` (deterministic there today).
2. **FIFO order pin.** Enqueue three fibers that each append their id to a shared
   log as they first run; with a hot I/O fiber also active, assert the log is in
   spawn order (no I/O fiber jumping ahead of an already-queued fiber).
3. **No-regression fast path.** Single fiber doing blocking reads with no other
   ready work: assert it still direct-resumes (latency unchanged) via a
   `@test_build_only` counter `reactor_direct_resumes()` (incremented on the
   swapcontext path) — proves §2.1 didn't disable the fast path when uncontended.
4. **Keep-alive sequence (the team's failure).** Co-located `web.client` + a
   per-connection-fiber echo server, 3rd+ pooled request must succeed. Port the
   team's `spike` repro; assert all N round-trips complete. Under valgrind.

Add `@test_build_only` probes: `reactor_direct_resumes()` and
`reactor_fair_enqueues()` (direct-resume suppressed due to `ready_count>0`) so the
fairness decision is assertable, not inferred from timing.

## 6. ABI

**No ABI bump.** The change is internal scheduler logic: the direct-resume gate in
edge delivery reading the **existing** `DriftExec.queue_len` (no new field). No new
runtime-exported intrinsic, no boundary struct/layout the compiler emits, no calling-
convention change. The two new probes are `@test_build_only` (debug-build symbols,
not part of the release ABI surface). **`DRIFT_RT_ABI_VERSION` stays at F3's value.**
(If §2.2's budget refinement later needs a tunable exposed to Drift, that would be a
new intrinsic → re-evaluate; not in the first slice.)

## 7. Smallest viable slice

1. In **both** edge-delivery loops, replace the unconditional `direct_vt = rv`
   assignment with the §2.1a locked decision: under the claimed VT's `exec->mu`
   (nested in the held `r->mu`), `if (queue_len == 0) direct_vt = rv; else
   drift_exec_enqueue(ex, rv);`. **Reuse `queue_len`; add no field.**
2. Add the two `@test_build_only` probes (`reactor_direct_resumes`,
   `reactor_fair_enqueues`).
3. Land tests #1 and #3 (core repro + fast-path no-regression). Tests #2/#4 follow.

That is ~20–40 lines of runtime change (two edge-delivery sites) + tests. No new
struct field, no stdlib, no codegen, no ABI.

## 8. Rollback plan

- The entire behavior is behind the `ready_count == 0` gate in two edge-delivery
  sites. **Rollback = revert those two sites to unconditional direct-resume** (and
  drop the counter). No data-structure migration, no ABI implication, so rollback is
  a clean revert of the F4 commit with zero downstream effects.
- Risk containment: ship behind a runtime env flag (`DRIFT_SCHED_FAIR=0/1`, default
  1) for the first release so a regression can be toggled off in the field without a
  rebuild, then remove the flag once soaked.

## 9. Explicitly out of scope for F4
- Multi-worker / work-stealing throughput scaling (separate effort; note that the
  fairness gate here is compatible with it later).
- Priority scheduling, deadline scheduling.
- Custom-executor I/O integration — that is **F5** (`F5-executor-lifecycle-plan.md`),
  independent of F4.
