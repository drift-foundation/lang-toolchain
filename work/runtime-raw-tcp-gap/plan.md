# Plan: Integrated Worker Polling Design Pass

## Context

We have narrowed the remaining raw-TCP gap versus Go to the VT/runtime hot path, not framework code:

| Benchmark | Go | Drift |
|-----------|----|-------|
| raw TCP | ~116k req/s | ~69k–82k req/s |
| HTTP | ~46k–50k req/s | ~53k–56k req/s |

That means:
- product/server performance is already competitive on the realistic HTTP workload
- the remaining performance question is specifically raw VT/runtime maturity

We also now have a benchmark-only worker-epoll experiment showing a large raw-TCP improvement when the cross-thread reactor -> executor handoff is bypassed.

Important interpretation:
- this experiment is **evidence of importance**
- it is **not** yet evidence that a production implementation would realize the full same gain

The hack is single-worker, benchmark-shaped, and removes more coordination than a correct general implementation can necessarily remove. We need a design pass before deciding whether this belongs pre-MVP or post-MVP.

## Objective

Design a production-correct architecture for reducing or removing the reactor -> executor cross-thread handoff on the hot I/O resume path, and decide whether the complexity is justified.

This is a design/review workstream first.

No implementation should start until the plan has survived review.

## What the experiment proved

The benchmark-only worker-epoll path strongly suggests that the current reactor-thread -> executor-worker handoff is the dominant remaining raw-TCP cost.

Current hot path on EAGAIN:
1. VT parks
2. worker blocks / yields
3. reactor thread gets epoll readiness
4. reactor clears watch/timer state
5. reactor unparks/enqueues VT
6. worker wakes, dequeues, resumes VT

Experimental hot path:
1. VT parks
2. same worker calls `epoll_wait`
3. worker gets readiness
4. worker resumes VT directly

This cuts out:
- separate reactor-thread wake path
- executor enqueue/dequeue
- futex wake for the common single-worker case
- some associated mutex/queue traffic

## What the experiment did **not** prove

The experiment does **not** prove that a production design will preserve the full measured speedup.

Reasons:
1. It only supports the single-worker benchmark shape.
2. The reactor thread is partially sidelined rather than cleanly integrated.
3. It does not solve the general timer/fairness/multi-worker ownership problem.
4. It resumes the ready VT in the best possible direct path with minimal scheduler ceremony.

So we must treat the experiment as:
- an upper bound signal
- not a final performance forecast

## Design target

We want to explore a design where idle executor workers participate in I/O waiting directly, so that the common path for I/O-ready VT resume avoids a separate reactor-thread handoff.

The design must preserve:
1. timer correctness
2. fair runnable-VT scheduling
3. shutdown semantics
4. multi-worker correctness
5. clean ownership of fd/watch state

## Candidate architecture

### High-level direction

Move toward an integrated scheduler+poll model:

1. If a worker has runnable VTs in its local queue:
   - run them normally
2. If a worker becomes idle:
   - it may poll for I/O readiness directly
3. When readiness arrives:
   - the same worker makes the VT runnable and resumes it without a cross-thread reactor handoff

The existing reactor thread may still remain initially for:
- timers
- fallback coordination
- future multi-worker balancing

But it should leave the common single-worker hot path.

### Possible phase structure

#### Phase A: Single-worker integrated polling

Goal:
- a correct single-worker implementation, no benchmark-only shortcuts

Shape:
1. one worker thread owns `epoll_wait`
2. ready VT is resumed by that same worker
3. reactor thread either:
   - disappears for this mode, or
   - is retained only for timer support, with clear separation

Why this phase:
- lowest complexity production-relevant subset
- directly tests whether the single-worker win survives when implemented cleanly

#### Phase B: Multi-worker ownership model

Goal:
- define how multiple workers interact with polling and ready VTs

Possible options to evaluate:
1. one poll owner, many workers
2. per-worker poller
3. shared poller with ownership transfer

This phase should not be started until Phase A semantics are considered acceptable.

## Critical design questions

### 1. Who owns epoll?

We need an explicit answer:
- one global epoll owner?
- one worker per executor acts as poll owner when idle?
- per-worker epoll sets?

This is the main architectural choice.

### 2. Who owns a parked VT?

When a VT parks on fd readiness:
- which thread is responsible for resuming it?
- can that ownership change?
- how is it synchronized?

The benchmark hack assumes “current worker resumes directly.” A real design needs this as a stated invariant.

### 3. What happens when another runnable VT exists?

If a worker is polling and another VT becomes runnable through some other path:
- do we interrupt polling?
- who notices and how?
- what wins: local runnable queue or fd readiness?

### 4. How are timers handled?

We already fixed the stale-timer bug, but timer correctness remains a central risk.

Questions:
1. does the poll-owning worker also compute the timeout?
2. is there still a separate timer owner?
3. if a new earlier deadline is registered, how is the sleeping poller woken safely?

### 5. How does shutdown work?

Need explicit semantics for:
- executor shutdown while worker is in poll
- runtime destroy while watches/timers exist
- VT cancellation while parked

### 6. What happens with multiple workers?

Even if Phase A is single-worker-first, the design must not accidentally dead-end future expansion.

We need to know whether Phase A:
- is a clean subset of a future multi-worker design
- or is a special-case architecture that would later need to be thrown away

## Primary risks

### Risk 1: Overestimating the benchmark win

The experiment likely overstates what a production version will gain.

Mitigation:
- explicitly treat benchmark-hack gain as an upper bound
- require a clean Phase A implementation before making roadmap decisions

### Risk 2: Timer correctness regressions

This area already produced one major bug and several timing-sensitive optimizations.

Mitigation:
- timer semantics must be written down before coding
- timer wakeup races must be enumerated explicitly

### Risk 3: Multi-worker dead-end

A narrow single-worker solution may paint us into a corner.

Mitigation:
- Phase A must include a brief “how this extends or fails to extend to multi-worker” section

### Risk 4: Scheduler fairness regressions

Direct resume from poll can improve the benchmark while subtly harming fairness under mixed workloads.

Mitigation:
- define fairness expectations upfront
- do not optimize only for the single ping-pong benchmark without stating the tradeoff

## What success would look like

A production-worthy Phase A design should aim for:
1. clear correctness story
2. no benchmark-only shortcuts
3. measurable raw-TCP improvement
4. no regression on current HTTP/server benchmarks
5. bounded implementation complexity

We do **not** need perfect parity with Go.

A good stopping rule would be:
- reach roughly 80%–90% of Go raw-TCP throughput
- while keeping current strong HTTP performance
- without destabilizing runtime semantics

## What would make this not worth it

Push this to post-MVP if any of the following become true:
1. Phase A requires broad scheduler rewrite, not a contained change
2. timer and wakeup correctness become hard to reason about
3. the expected production gain shrinks far below the benchmark-hack signal
4. the complexity starts exceeding the product value, given that HTTP performance is already competitive

---

## Design Review: Iteration 1 (Klaudia)

### Summary judgment

**Likely worth implementing pre-MVP**, but Phase A as described in the plan is underspecified — it hand-waves the two hardest problems (timer ownership and wakeup-from-poll) and the candidate architecture section doesn't commit to a concrete design. The good news is that after reading the actual code, a correct Phase A is smaller and more contained than the plan implies. The bad news is that several assumptions in the plan are wrong or misleading.

Below: explicit answers to each critical question, then the five focus areas.

---

### Answers to critical design questions

#### Q1. Who owns epoll?

**Answer: shared epoll fd, conditional ownership of epoll_wait.**

The single epoll fd remains global (one `epoll_create1` in `drift_reactor_create`). `epoll_ctl` (arm/disarm) can be called from any thread — the kernel serializes it. The question is only who calls `epoll_wait`.

Concrete rule for Phase A (single-worker executor):
- When the worker's run queue is empty, the worker calls `epoll_wait`.
- When the worker has runnable VTs, the reactor thread calls `epoll_wait`.
- Ownership transfer happens via an atomic flag (`poll_owner`): the worker CAS's it to claim poll, the reactor checks it before entering `epoll_wait`.

This avoids per-worker epoll sets (duplicates kernel state, complicates fd lifecycle) and avoids the thundering-herd problem of multiple `epoll_wait` callers on one fd.

**Multi-worker extension:** In Phase B, this becomes "one idle worker can claim poll ownership, others stay on condvar." Still one poller at a time.  The design does not dead-end.

#### Q2. Who owns a parked VT?

**Answer: nobody exclusively. The poller (whoever holds poll ownership) resolves the watch and resumes the VT.**

Currently a parked VT has:
- A `ReactorWatch` in the reactor's watch list (protected by `r->mu`)
- Possibly a `ReactorTimer` in the timer list (protected by `r->mu`)
- State `DRIFT_VT_PARKED` (atomic)

The key invariant: exactly one thread resolves a watch for a given fd. Since only one thread calls `epoll_wait` at a time (enforced by `poll_owner`), only that thread receives the readiness event, locks `r->mu`, clears the watch, and resumes the VT. No ownership transfer of the VT itself is needed.

The plan's statement "current worker resumes directly" is correct for the single-worker case but misleading — it's not that the worker "owns" the parked VT. It's that the worker is the poll owner, so it's the one that gets the event.

#### Q3. What happens when another VT becomes runnable?

**Answer: write to `wake_fd` to interrupt `epoll_wait`.**

This is the exact mechanism the reactor already uses. When `drift_thread_unpark` enqueues a VT to the executor and signals the condvar, it can also write to `wake_fd` (if the worker is in epoll_wait rather than condvar). The `in_wait` atomic already guards this — it just needs to be set by the worker instead of (only) the reactor.

Concrete flow:
1. External event unparks VT X → `drift_exec_enqueue` → `pthread_cond_signal`
2. If worker is in `epoll_wait` (not condvar): condvar signal is lost, but...
3. ...`drift_reactor_wake(r)` writes to wake_fd → `epoll_wait` returns → worker sees wake_fd → checks run queue → finds VT X → runs it

**The plan identifies this as an open question but doesn't note that the mechanism already exists.** The only change needed: the worker sets `r->in_wait = 1` before its `epoll_wait` and `r->in_wait = 0` after, so that `drift_reactor_wake` knows to write to `wake_fd`. Currently only the reactor thread sets `in_wait`.

However, there is a **race between condvar and wake_fd** that doesn't exist today: if unpark signals condvar AND writes wake_fd, but the worker is transitioning from condvar_wait to epoll_wait, the condvar signal could be consumed in the transition window. The fix: after the worker CAS's `poll_owner` and before calling `epoll_wait`, it must re-check `exec->head` under the lock. This is a standard "check-before-sleep" pattern.

#### Q4. How are timers handled?

**Answer: the poll-owning thread computes `epoll_wait` timeout from the timer list. The reactor thread only processes timers when it owns the poll.**

This is where the plan is weakest. It asks three sub-questions but doesn't commit to an answer. Here's the concrete answer:

When the worker owns the poll:
1. Worker locks `r->mu`, scans timer list for minimum deadline, computes timeout.
2. Worker calls `epoll_wait(epoll_fd, events, N, timeout)`.
3. After `epoll_wait` returns (event or timeout): worker locks `r->mu`, collects expired timers, unlocks, fires them.

This is **exactly what the reactor thread does today**, just done by the worker instead. The logic moves, the thread changes, the code stays the same.

When the reactor owns the poll (because the worker has runnable VTs and didn't claim it):
- Reactor does everything it does today. No change.

**New timer registered while worker is in `epoll_wait`:** `drift_reactor_register_timer` calls `drift_reactor_wake(r)`, which writes to `wake_fd`. Worker's `epoll_wait` returns, worker re-scans timers, re-computes timeout, goes back into `epoll_wait` with the tighter deadline. This already works correctly because the wake mechanism is event-driven, not sleep-based.

**Hidden race:** If a VT parks with a timeout (`drift_thread_park_until`), it calls `drift_reactor_register_timer` which calls `drift_reactor_wake`. But the VT hasn't `swapcontext`'d back to the worker yet — the timer registration happens inside the VT's execution. The wake_fd write will be consumed by the worker's current `epoll_wait`, potentially before the VT actually parks. This is **not a new bug** — the same race exists in the current reactor design and is handled by the double-check in `drift_thread_park_until` (check `park_token` after setting state to PARKED). But it's worth stating explicitly that this race is preserved, not introduced.

#### Q5. How does shutdown work?

**Answer: same as today, with one additional step.**

Current shutdown: set `shutting_down=1`, broadcast condvar, join workers.

With poll integration: the worker might be in `epoll_wait` instead of `condvar_wait`. The broadcast won't wake it. Fix: after setting `shutting_down`, also write to `wake_fd`. Worker's `epoll_wait` returns, worker re-checks `exec->shutting_down` (which it already does at the top of the loop), breaks out.

This is trivial. The plan correctly identifies it as a question but it's not a hard one.

#### Q6. Multi-worker viability?

**Answer: Phase A is a clean subset. Not a dead-end.**

Phase A introduces: "the single worker can claim poll ownership from the reactor when idle." Phase B generalizes to: "any idle worker can claim poll ownership." The `poll_owner` atomic becomes a compare-and-swap from REACTOR to WORKER_N. Only one worker polls at a time, others stay on condvar. If a polling worker gets a VT that belongs to a different worker's affinity (future work), it enqueues to the shared queue rather than resuming directly.

The Phase A code (claim/release poll ownership, compute timeout from timer list, handle wake_fd interruption) is exactly the code Phase B reuses. Nothing is thrown away.

---

### Focus area analysis

#### 1. Ownership model

The plan says the worker "owns" `epoll_wait` when idle. This is roughly correct but imprecise in two ways:

**Problem 1: Ownership transfer is not atomic with state change.**
The plan doesn't describe how the worker transitions between "waiting on condvar" and "waiting on epoll." If this transition isn't atomic with respect to the unpark path, signals are lost.

Concrete scenario:
1. Worker is on condvar. `poll_owner = REACTOR`.
2. Worker wakes (spurious or condvar signal), finds queue empty.
3. Worker decides to claim poll: sets `poll_owner = WORKER`.
4. Between step 3 and entering `epoll_wait`, another thread calls `drift_thread_unpark`:
   - Sees `poll_owner = WORKER`, skips condvar signal, writes to wake_fd instead
   - But worker hasn't entered `epoll_wait` yet — the wake_fd write sits unconsumed
   - Worker enters `epoll_wait` — **wakes immediately** from the pending wake_fd write
   - This is actually fine. The wake_fd is level-triggered (eventfd), so the write is not lost.

Conclusion: **this race is benign because wake_fd events are persistent (eventfd counter stays >0 until drained).** But the plan should state this explicitly.

**Problem 2: The reactor thread also needs to know when to yield.**
When `poll_owner = WORKER`, the reactor must not call `epoll_wait`. The experiment hack does this by having the reactor `nanosleep` instead. A production version needs the reactor to condvar-wait on a "I no longer own the poll" signal, or simply spin-check `poll_owner` with a timer-driven sleep.

Simpler alternative: **don't change the reactor thread at all.** Instead, use `EPOLLONESHOT` semantics: when the worker disarms a watch after handling an event, the reactor won't see that fd again until it's re-armed. The worker and reactor can both call `epoll_wait` simultaneously — the kernel delivers each event to exactly one waiter. This eliminates the ownership-transfer complexity entirely.

However, `EPOLLONESHOT` was tested in a previous reactor optimization attempt and was part of a set of changes that caused a 2x regression. The regression was attributed to the full set (seq_cst atomics, wake coalescing, EPOLLET+EPOLLONESHOT), not EPOLLONESHOT alone. **Worth re-evaluating EPOLLONESHOT in isolation** — it might have been unfairly blamed for a regression caused by the other changes.

#### 2. Timer semantics

The plan flags timer correctness as a risk but doesn't enumerate the actual races. Here they are:

**Race 1: Timer fires while worker is resuming the same VT from epoll.**
- Worker gets epoll event for VT X, starts resolving watch.
- Simultaneously, reactor (if it still has a timer for X from `drift_reactor_register_io`'s deadline path) fires the timer and calls `drift_thread_unpark(X)`.
- Worker resumes VT X. Reactor also tries to unpark VT X.
- `drift_thread_unpark` checks `state == DRIFT_VT_PARKED`. If the worker already set it to RUNNING, unpark is a no-op. **Safe.**
- But: if the worker hasn't set state yet (it's between resolving the watch and changing state), unpark sees PARKED, enqueues VT X to the executor queue, worker also resumes it directly. **VT X runs twice. Corruption.**

**Mitigation:** The experiment code cancels timers when resolving a watch (matching the reactor's existing behavior). This is necessary but not sufficient — the timer could fire between the worker's `epoll_wait` return and the `pthread_mutex_lock(&r->mu)` that starts timer cancellation. The window is small (nanoseconds) but real.

**Correct fix:** Under `r->mu`, atomically: resolve watch, cancel timers, set VT state to RUNNING. This is what the reactor does today (lines 808-837: lock, resolve watch + cancel timers, unlock, then unpark). The worker must do the same.

The experiment code takes `r->mu` when resolving the watch and canceling timers, but sets VT state **outside** the lock. This is technically racy but practically safe because the reactor isn't calling `epoll_wait` in experiment mode. **A production version cannot rely on this.**

**Race 2: Timer registered after worker's timeout computation.**
- Worker computes `epoll_wait` timeout = 5000ms (earliest timer is 5s out).
- VT Y immediately registers a new timer with deadline 10ms.
- `drift_reactor_register_timer` writes to wake_fd.
- Worker's `epoll_wait` returns immediately from wake_fd.
- Worker re-scans timers, finds the 10ms one, adjusts. **Safe.**

This race is handled correctly by the wake_fd mechanism. No issue.

**Race 3: VT parks-with-timeout but IO completes before timeout.**
- VT calls `drift_reactor_register_io(fd, interest, vt, deadline_ms)`.
- This registers both an epoll watch AND a timer.
- IO completes → epoll event → worker resolves watch and cancels timer.
- Timer is already cancelled. **Safe.**

This is the same pattern the reactor uses today. The worker must replicate the timer cancellation (which the experiment code does).

**Phase A dependency on benchmark shortcut?** Yes, partially. The experiment sidelined the reactor thread entirely, which eliminated Race 1 by construction. A production Phase A cannot sideline the reactor (it still needs to process timers when the worker is busy running VTs). So Race 1 must be solved explicitly. The fix (set VT state under `r->mu`) is straightforward but adds one atomic store to the critical path. Cost: negligible.

#### 3. Fairness / runnable scheduling

**Scenario:** Worker is in `epoll_wait`. VT Z becomes runnable via a non-IO path (e.g., another VT's `conc.spawn` enqueues Z to the executor queue and signals condvar).

Today: condvar signal wakes the worker immediately. With poll integration: the condvar signal is lost (worker is in `epoll_wait`, not `condvar_wait`). VT Z sits in the queue until either:
- An IO event happens to arrive and wakes `epoll_wait`
- The `epoll_wait` timeout expires

**Fix:** `drift_thread_unpark` (or `drift_exec_enqueue`) must also call `drift_reactor_wake(r)` when it detects that the worker is in poll mode. The `in_wait` atomic is already designed for exactly this. But today, `drift_thread_unpark` doesn't call `drift_reactor_wake` — it only signals the condvar. This needs to change.

Specifically, in `drift_thread_unpark`, after `drift_exec_enqueue`, add:
```c
drift_reactor_wake(r);
```

This is cheap (one atomic exchange + conditional write(8 bytes)) and ensures the worker wakes from `epoll_wait` promptly when non-IO work arrives.

**Starvation risk:** If the worker always prioritizes `epoll_wait` over queue draining, IO-heavy workloads could starve non-IO VTs. The plan doesn't address this. Go's solution: always drain the local run queue first, only poll when idle. The plan's candidate architecture says the same thing ("if a worker has runnable VTs, run them normally"), but the experiment code doesn't implement this check inside the epoll path — it checks `!exec->head` before entering epoll but doesn't re-check between resumed-VT iterations. In a production version, after resuming a VT from epoll and having it park again, the worker should check the run queue before going back to `epoll_wait`.

#### 4. Multi-worker viability

Phase A **is** a clean subset. Reasoning:

Go's architecture is the proof-of-existence: their netpoller uses a single shared `epoll_fd`. When a P (processor, analogous to our worker) has no runnable goroutines, it queries the netpoller (`netpoll(timeout)`). If events are available, they're distributed to the calling P's local run queue. If multiple P's are idle, only one calls `epoll_wait` with a blocking timeout — others call with `timeout=0` (non-blocking check) or go to sleep.

Drift's Phase A does exactly this for the single-worker case. Phase B would add:
- A `poll_owner` atomic that idle workers CAS for
- Workers that fail the CAS go to condvar (or `epoll_wait(timeout=0)` + condvar fallback)
- The poll-owning worker distributes events: resume its own VTs directly, enqueue others' to the shared queue

Nothing in Phase A's design (shared epoll fd, worker computes timeout from timer list, wake_fd interrupts poll) conflicts with this extension.

**What would be a dead-end:** Per-worker epoll sets. These duplicate kernel state, complicate fd deregistration (which epoll set owns the fd?), and don't match Go's proven architecture. The plan correctly lists this as an option to evaluate but I recommend ruling it out now.

#### 5. Realism of the benchmark signal

The experiment measured:
- Baseline: 77 ms median (15.4 μs/iter)
- Worker-epoll: 48 ms median (9.6 μs/iter)
- Go: 44 ms (8.8 μs/iter)

**What's real:**
- The cross-thread handoff (reactor→executor) is genuinely the dominant cost. The experiment eliminates it and gets within 10% of Go. This directional signal is trustworthy.
- The gain comes from eliminating: futex-wake of the worker (the `pthread_cond_signal` which is a `futex(FUTEX_WAKE)` syscall), the mutex acquire/release pair for executor enqueue/dequeue, and the ExecNode alloc/free. These costs are real and don't depend on benchmark shape.

**What's optimistic:**
- The experiment skips timer computation on the poll path. A production version must scan timers under `r->mu` to compute `epoll_wait` timeout. Cost: O(n) timer scan + mutex. For the keep-alive benchmark (few timers), this is ~0.1-0.5 μs. Small but not zero.
- The experiment doesn't re-check the run queue between VT resumptions within the epoll path. A production version must, adding one lock+check per iteration. Cost: ~0.1 μs.
- The experiment disables reactor `epoll_wait` entirely, so there's zero contention on `r->mu`. A production version has the reactor still taking `r->mu` for timer processing. Cost depends on how often timers are processed — likely negligible for the keep-alive benchmark, but non-zero under mixed workloads.

**Realistic estimate:** A clean Phase A should realize 70-85% of the experiment's gain. The experiment saved ~6 μs/iter (from 15.4 to 9.6). A production version should save 4-5 μs/iter, landing around 10.5-11.5 μs/iter. That's ~75-82k req/s, versus Go's 114k. Not parity, but a meaningful improvement from the current 62-69k.

The HTTP benchmark should not regress — the framework overhead dominates there, and the runtime change only affects the I/O resume path (which is already fast enough that it's not the bottleneck in the HTTP case).

---

### Revised recommendation

**Proceed to implementation of Phase A**, with these constraints:

1. **Scope it as a contained change.** The modifications touch:
   - `drift_exec_worker`: add poll-claim logic when queue is empty
   - `drift_thread_unpark`: add `drift_reactor_wake` call for poll-mode wakeup
   - `drift_reactor_thread_entry`: check `poll_owner` before `epoll_wait` (yield to condvar if worker owns poll)
   - Reactor `poll_owner` atomic and `in_wait` semantic extension

   This is ~100-150 lines of C, not a scheduler rewrite.

2. **Solve Race 1 (timer-fires-during-worker-resume) explicitly.** Set VT state to READY under `r->mu` when resolving a watch, before releasing the lock.

3. **Add `drift_reactor_wake` to the unpark path** to prevent fairness regressions when the worker is in `epoll_wait`.

4. **Re-check run queue after each VT resume from epoll** before going back to `epoll_wait`.

5. **Measure with a clean implementation.** If the clean version gets <50% of the experiment's gain (i.e., less than 3 μs/iter improvement), reconsider whether the complexity is justified. This is the "abort criterion."

**Why pre-MVP:** HTTP performance is competitive, but raw-TCP performance is a credibility signal. Going from 1.75x-of-Go to 1.2x-of-Go (estimated) is meaningful for the "systems language" positioning. The implementation is contained (~150 LOC), the design is proven by Go's architecture, and the abort criterion provides a clean off-ramp if the gain doesn't materialize.

**Why not post-MVP:** The custom context switch is already landed. The reactor code is fresh in working memory. The VT runtime is stable. Deferring to post-MVP means re-learning the reactor/executor interaction, which has already bitten us multiple times. The cost of doing it now is lower than the cost of doing it later.

---

### What would change my mind

- If the clean Phase A gets <3 μs/iter improvement → defer to post-MVP (complexity not justified for <20% gain)
- If timer correctness proves harder than described here (e.g., if the Race 1 fix causes mutex contention that erodes the gain) → defer
- If the implementation starts exceeding ~200 LOC of net-new C → stop and re-evaluate scope

---

## Design Review: Iteration 2 — State/Ownership Protocol (Klaudia)

This iteration pins down the three artifacts requested: (1) the VT state-transition
table with synchronization rules, (2) the poll-owner transition protocol as an
ordered step sequence, and (3) the reactor's exact role in Phase A.

---

### 1. VT state-transition table

The runtime VT states are: `NEW`, `READY`, `RUNNING`, `PARKED`, `FINISHED`, `CANCELLED`.

Phase A does not introduce new states.  It changes **who** performs certain
transitions and **under which lock**.

#### Transitions

| # | From | To | Who performs | Synchronization | Invariant / notes |
|---|------|----|-------------|-----------------|-------------------|
| T1 | NEW | READY | Any thread (`drift_exec_submit`) | Atomic store. Enqueue under `exec->mu`. | VT must not be started or cancelled. `h->exec` is set under `exec->mu` before enqueue. |
| T2 | READY | RUNNING | Worker thread (dequeue path in `drift_exec_worker`) | Atomic store. Dequeue under `exec->mu`. | Only the worker that dequeued the VT performs this. One ExecNode per enqueue guarantees single dequeue. |
| T3 | RUNNING | PARKED | VT itself (`drift_thread_park` / `drift_thread_park_until`) | Atomic store. No lock (fiber path). | The VT sets its own state. The double-check pattern (check `park_token` both before and after the store) prevents missed wakeups. After the store, the VT calls `drift_swapcontext` back to the worker's `sched_ctx`. |
| T4a | PARKED | READY | Reactor thread (current path: IO event or timer) | **Under `r->mu`**: resolve watch, cancel timers, set state to READY, then unlock. Enqueue under `exec->mu`. | `r->mu` serializes against the worker's poll-resume path (T4b). Only the thread that clears the watch can transition the VT. This prevents double-resume. |
| T4b | PARKED | RUNNING | Poll-owning worker (Phase A addition: direct resume from `epoll_wait`) | **Under `r->mu`**: resolve watch, cancel timers, set state to RUNNING. Unlock. Resume VT via `drift_swapcontext`. | Same lock as T4a. Since exactly one thread holds `r->mu` when clearing the watch and setting state, and only one thread calls `epoll_wait` at a time (`poll_owner` CAS), the timer-vs-IO race (Race 1) is eliminated. **The VT is never enqueued to the executor queue in this path — the worker resumes it directly.** |
| T5 | PARKED | READY | Reactor thread (timer expiry, no IO event) | `r->mu` for timer collection. Enqueue under `exec->mu`. | Timer fires → `drift_thread_unpark` → enqueue. If the watch was already resolved (IO arrived first), the timer's VT pointer was cleared during watch resolution; `drift_thread_unpark` sees `completed` or non-PARKED state and is a no-op. |
| T6 | RUNNING | FINISHED | VT itself (`drift_vt_fiber_entry`) | Atomic store. No lock. | The VT stores FINISHED, then `drift_swapcontext` back to the worker. The worker sees FINISHED on return and runs cleanup. |
| T7 | RUNNING | CANCELLED | Worker thread (pre-start cancellation in `drift_exec_worker`) | Atomic store. `vt->mu` for completion signaling. | If `cancelled` was set before `started`, the worker skips fiber entry and transitions directly to CANCELLED. |
| T8 | NEW/READY | CANCELLED | External thread (`drift_exec_cancel`) | Atomic store of `cancelled` flag. No direct state change to CANCELLED — the worker observes `cancelled` at T7. | Cancellation is advisory: it sets a flag, the worker acts on it at the next scheduling point. |

#### Race 1 resolution (timer-fires-during-worker-resume)

The critical invariant is: **the transition from PARKED to {READY, RUNNING} happens
under `r->mu`, atomically with clearing the watch and cancelling timers.**

- T4a (reactor path): lock `r->mu` → find watch → clear watch → cancel timers for this VT → set state READY → unlock → enqueue.
- T4b (worker poll path): lock `r->mu` → find watch → clear watch → cancel timers for this VT → set state RUNNING → unlock → `drift_swapcontext`.

Because both paths hold `r->mu` when transitioning from PARKED, and the timer-firing
path (T5) also takes `r->mu` to collect expired timers, there is no window where
two threads both see a VT as PARKED and both attempt to transition it.

Specifically: if the reactor's timer-collection sweep (under `r->mu`) collects a
timer for VT X, it releases the lock and calls `drift_thread_unpark(X)`.
`drift_thread_unpark` checks `state == DRIFT_VT_PARKED`; if the worker already
transitioned X to RUNNING under `r->mu`, unpark sees RUNNING and is a no-op.
If the worker hasn't taken `r->mu` yet, it will find the timer already collected
(removed from list) and the watch still present — it resolves the watch and
transitions as normal.  The VT runs exactly once.

#### `park_token` double-check pattern (preserved, not changed)

This pattern in `drift_thread_park` / `drift_thread_park_until` exists today and is
unchanged by Phase A:

```
if (park_token > 0) { park_token--; return; }     // check 1
atomic_store(state, PARKED);
if (park_token > 0) { park_token--; state=RUNNING; return; }  // check 2
drift_swapcontext(...)
```

Check 1 catches tokens from prior unparks.  Check 2 catches a token that arrived
between check 1 and the state store.  This is a single-writer pattern (only the VT
thread writes `park_token` in the decrement path; only `drift_thread_unpark`
increments it, and only after setting state to READY).  Phase A does not change
this pattern.

---

### 2. Poll-owner transition protocol

New state: `Reactor.poll_owner` — an `atomic_int` on the Reactor struct.

Values:
- `POLL_OWNER_REACTOR = 0` (default)
- `POLL_OWNER_WORKER = 1`

#### Protocol: worker claims poll ownership

Precondition: worker holds `exec->mu`, has observed `exec->head == NULL` and
`!exec->shutting_down`.

```
Step W1.  [holding exec->mu]
          Read exec->head again (re-check under lock).
          If non-NULL: release exec->mu, dequeue normally. DONE.

Step W2.  [holding exec->mu]
          CAS poll_owner from REACTOR to WORKER.
          If CAS fails (another worker already owns poll — Phase B concern,
          not possible in Phase A single-worker): release exec->mu, fall
          through to condvar_wait. DONE.

Step W3.  [holding exec->mu, poll_owner == WORKER]
          Set r->in_wait = 1 (relaxed store).
          Release exec->mu.
          — At this point, any drift_reactor_wake() call will write to wake_fd.
          — Any drift_exec_enqueue() + pthread_cond_signal() that happened
            between W1 and W3 was under exec->mu, so exec->head is non-NULL
            and was caught by the re-check at W1.
          — Any enqueue that happens AFTER W3 releases exec->mu will see
            poll_owner == WORKER.  The enqueue path (drift_thread_unpark)
            signals condvar AND calls drift_reactor_wake(r).  The condvar
            signal is lost (worker is not in condvar_wait), but the wake_fd
            write is persistent (eventfd counter > 0 until drained).

Step W4.  Lock r->mu.
          Compute epoll_wait timeout from timer list (minimum deadline − now).
          Unlock r->mu.

Step W5.  Call epoll_wait(r->epoll_fd, events, 16, timeout_ms).

Step W6.  Set r->in_wait = 0 (relaxed store).

Step W7.  Process events:
          - wake_fd event: drain eventfd, then check exec->head under lock.
            If non-NULL: release poll ownership (step W9), dequeue normally.
            If NULL and !shutting_down: goto W4 (re-compute timeout, re-poll).
          - IO event: lock r->mu, resolve watch, cancel timers, set VT state
            to RUNNING (transition T4b), unlock r->mu.  Disarm fd via
            epoll_ctl.  Resume VT via drift_swapcontext.  On VT return:
            if FINISHED/CANCELLED → cleanup; then check exec->head under
            lock before going back to W4.
          - timeout (n == 0): lock r->mu, collect expired timers, unlock.
            Fire timers via drift_thread_unpark (which enqueues VTs).
            Then check exec->head under lock.

Step W8.  After processing, if exec->head is non-NULL or exec->shutting_down:
          goto W9 (release poll ownership and return to normal dequeue loop).
          Otherwise: goto W4 (stay in poll mode).

Step W9.  [releasing poll ownership]
          Lock exec->mu.
          Store poll_owner = REACTOR (relaxed or release store).
          — From this point, the reactor thread is free to call epoll_wait.
          Fall through to the condvar_wait loop or dequeue, as appropriate.
```

#### Protocol: reactor observes ownership

The reactor thread's main loop is modified:

```
Step R1.  Lock r->mu.
          Check r->stopping → break if true.
          Compute timeout from timer list.
          Unlock r->mu.

Step R2.  Read poll_owner (relaxed load).
          If poll_owner == WORKER:
              — Do NOT call epoll_wait.
              — Wait on a condvar (r->cv) with the computed timeout.
                This handles timer expiry: the wait times out at the earliest
                deadline, and the reactor processes timers at R4.
              — When the worker releases poll ownership (W9), it is NOT
                required to signal r->cv.  The reactor's timed wait will
                return at the next timer deadline regardless.  If there are
                no timers, the reactor uses a long timeout (e.g., 1 second)
                and re-checks poll_owner periodically.
              Goto R4.

Step R3.  [poll_owner == REACTOR]
          Set r->in_wait = 1.
          epoll_wait(r->epoll_fd, events, 16, timeout_ms).
          Set r->in_wait = 0.
          Process IO events: resolve watches, cancel timers (T4a), unpark
          VTs via drift_thread_unpark.
          — This is the existing reactor code, unchanged.

Step R4.  Lock r->mu.
          Collect expired timers.
          Unlock r->mu.
          Fire timers via drift_thread_unpark.
          Goto R1.
```

#### Why no wake is lost in the W3→W5 transition window

The concern: after the worker releases `exec->mu` (W3) and before it enters
`epoll_wait` (W5), an external thread enqueues work.  The condvar signal from
`drift_exec_enqueue` is lost because the worker is not in `condvar_wait`.

This is safe because:
1. `r->in_wait` was set to 1 at W3, **before** releasing `exec->mu`.
2. The enqueue path calls `drift_reactor_wake(r)` (new addition in Phase A).
3. `drift_reactor_wake` sees `in_wait == 1`, does `atomic_exchange(in_wait, 0)`,
   writes to wake_fd.
4. The wake_fd write is persistent: the eventfd counter increments and stays >0
   until drained by `read()`.
5. When the worker enters `epoll_wait` at W5, the wake_fd is already readable.
   `epoll_wait` returns immediately.
6. The worker processes the wake_fd event at W7, checks `exec->head`, finds the
   enqueued VT, and handles it.

Even if multiple wakes occur before W5, the eventfd counter accumulates them all
(counter is uint64_t, write adds 1, read drains to 0).  No wake is lost.

If the wake arrives between W5 and W6 (during `epoll_wait`): `epoll_wait` returns
normally with the wake_fd event.  Also safe.

#### Required change to `drift_thread_unpark`

Currently `drift_thread_unpark` only signals `exec->cv`.  Phase A adds:

```c
// In drift_thread_unpark, after drift_exec_enqueue + pthread_mutex_unlock:
Reactor *r = drift_default_reactor_ptr;
if (r) drift_reactor_wake(r);
```

`drift_reactor_wake` is cheap: one atomic exchange (relaxed).  If `in_wait == 0`
(nobody is polling), it returns immediately without a write() syscall.  If
`in_wait == 1`, it writes 8 bytes to the eventfd.  This is the same cost as the
existing timer-registration wake path.

---

### 3. Reactor role statement for Phase A

**In Phase A, the reactor thread is responsible for:**

1. **Timer expiry** — always.  Whether or not the worker owns the poll, the
   reactor thread processes expired timers.  When the worker owns the poll, the
   reactor uses `pthread_cond_timedwait(&r->cv, &r->mu, ...)` to sleep until
   the next timer deadline instead of `epoll_wait`.  When the reactor owns the
   poll, it processes timers after each `epoll_wait` return (existing behavior).

2. **Fallback IO polling** — when `poll_owner == REACTOR` (i.e., the worker
   has runnable VTs and did not claim the poll).  In this mode, the reactor does
   everything it does today: `epoll_wait`, resolve watches, cancel timers,
   unpark VTs via `drift_thread_unpark`.  Fully unchanged from the current
   codebase.

3. **Shutdown coordination** — unchanged.  `drift_reactor_destroy` sets
   `r->stopping`, writes to wake_fd, joins reactor thread.

**In Phase A, the reactor thread is NO LONGER responsible for:**

1. **IO polling when the single worker is idle** — the worker calls `epoll_wait`
   directly in this case.  The reactor yields by checking `poll_owner` before
   entering `epoll_wait` (step R2 above).

2. **Resuming/unparking VTs that the worker resolves from poll** — when the
   worker resolves a watch (T4b), it resumes the VT directly via
   `drift_swapcontext`.  The reactor is not involved.

**Shared responsibilities and boundaries:**

- **Watch list (`r->watches`)**: shared between reactor and worker.  Access
  is serialized by `r->mu`.  Whoever holds the poll and receives an epoll event
  takes `r->mu`, resolves the watch, cancels associated timers, and sets VT
  state — all under the lock.

- **Timer list (`r->timers`)**: shared.  `r->mu` protects it.  Timer
  registration (`drift_reactor_register_timer`) can happen from any thread.
  Timer collection (scanning for expired timers) happens in whatever thread
  is processing the poll timeout — the worker if it owns the poll (W7 timeout
  path), the reactor if it owns the poll (R3→R4), or the reactor via
  condvar-timedwait expiry (R2→R4).

- **`epoll_ctl` (arm/disarm)**: can be called from any thread at any time.
  The kernel serializes concurrent `epoll_ctl` calls on the same epoll fd.
  This is unchanged from today.

- **`in_wait` atomic**: set by whoever is about to enter `epoll_wait` (worker
  or reactor).  Read by `drift_reactor_wake`.  Semantics: "someone is in
  epoll_wait and will be woken by writing to wake_fd."

- **`poll_owner` atomic**: set by the worker (CAS REACTOR→WORKER to claim,
  store REACTOR to release).  Read by the reactor (to decide whether to
  call `epoll_wait` or condvar-wait).  **Only one writer transitions
  REACTOR→WORKER (the single worker).  Only that same writer transitions
  WORKER→REACTOR.**  The reactor thread is read-only on this flag.

---

### 4. Implementation abort criteria

**Performance gate:**
- If the clean Phase A implementation achieves less than ~3 μs/iter improvement
  on the VT loopback benchmark (5000 iters, both client and server on spawned
  VTs), defer to post-MVP.  3 μs/iter is ~50% of the benchmark hack's gain and
  represents a ~20% improvement over baseline.  Below that threshold, the
  complexity is not justified given that HTTP performance is already competitive.

**Complexity gate:**
- If the implementation exceeds ~200 lines of net-new C in `thread_runtime.c`
  (excluding the experiment hack code being removed), stop and reassess.  The
  protocol above is designed to be contained; if it is growing beyond that, the
  design is likely wrong rather than just large.

**Correctness gate:**
- If the state/ownership protocol requires additional atomic orderings beyond
  relaxed (i.e., if acquire/release or seq_cst fences become necessary to
  prevent races), stop and re-examine whether the `r->mu` serialization
  is actually sufficient.  The protocol is designed so that all shared-state
  mutations happen under `r->mu` or `exec->mu` — atomics are used only for
  advisory flags (`poll_owner`, `in_wait`, VT `state`).  If that invariant
  breaks, the design needs revision, not more fences.

**Regression gate:**
- If any existing e2e tests regress (especially `concurrent_spawn_executes`,
  `concurrent_spawn_join_ordering`, `perf_vt_loopback_baseline`), the
  implementation is incorrect and should not proceed until the regression is
  understood and fixed.

**Scope gate:**
- Phase A is single-worker only.  If during implementation it becomes clear
  that the single-worker constraint is being worked around rather than
  respected (e.g., adding multi-worker CAS paths "just in case"), stop.
  Phase B is a separate design pass.
