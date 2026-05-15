# Slice plan — `std.concurrent.Condvar`

**Status:** proposed.  **Target:** 0.32.x line.  **Owners:** toolchain.
**Date:** 2026-05-16.

## Why this slice exists

The maria-rpc 0.4 `ConnectionPool` (and any future stdlib/user code
that needs a parking wait-queue) needs a POSIX-style condition
variable.  The runtime primitives already exist
(`lang.thread.vt_park` / `vt_unpark`), but the user-facing
ergonomic API does not.

Without this, the maria team has three options, all of which are
unacceptable for a v1 cut:

1. **Fail-fast `acquire()`** — pushes backpressure to every caller.
2. **Sleep-poll loop** — burns CPU and adds per-attempt latency
   bounded by the poll interval.  (Not a deadlock — `conc.sleep`
   parks the VT correctly — just an ergonomic + latency failure
   mode.)
3. **In-package hand-rolled `Condvar` over `lang.thread.vt_*`** —
   correct in principle but requires getting CAS-protected
   stale-token handling right.  Every package that needs a parking
   wait would re-implement the same primitive, with the same risk.

The right answer is a single, hardened, stdlib-owned `Condvar` that
every package can use.  This document is the slice plan to deliver
it.

The maria team has indicated they will wait for this slice rather
than ship the in-package stopgap, on the basis that the pool is a
v1-critical surface and shipping an interim implementation just
forces a migration later.

## Goal

Land `std.concurrent.Condvar` with POSIX/Mesa-style semantics:

```drift
var guard = pool.inner.lock();
while not _lease_available(&mut guard) {
    pool.ready.wait(&mut guard);   // unlock + park + relock, atomic from caller's view
}
return _take_lease(&mut guard);
```

```drift
var guard = pool.inner.lock();
_return_lease(&mut guard, conn);
pool.ready.signal_one();
```

## Public API

```drift
module std.concurrent;

pub struct Condvar { /* opaque */ }

pub fn condvar() nothrow -> Condvar;

implement Condvar {
    /// Atomically (from caller's view) releases `guard`, parks the
    /// calling VT, and reacquires the lock on wake.
    ///
    /// Returns `Ok(())` on normal wake.  Returns
    /// `Err(ConcurrencyError(kind=CLOSED))` if the Condvar has been
    /// closed.  Returns `Err(ConcurrencyError(kind=REQUIRES_VTHREAD))`
    /// if called outside a VT context.
    ///
    /// **Note:** Condvar does NOT surface `CANCELLED` from
    /// `vt_cancel`.  The runtime's scheduler kills parked VTs at
    /// worker-dispatch time after cancellation, before any
    /// user-code wake handler can run; consequently a cancelled
    /// waiter never returns from `wait` at all — its VT is reaped.
    /// Use `close()` for coordinated shutdown (see below); use
    /// `wait_timeout` for time-bound waits.
    ///
    /// **Spurious wakes are permitted.**  Callers MUST loop around
    /// the predicate:
    ///
    /// ```
    /// while not predicate(&guard) {
    ///     match cv.wait(&mut guard) {
    ///         Ok(_) => {},
    ///         Err(e) => return Err(e),
    ///     }
    /// }
    /// ```
    pub fn wait<T>(self: &Condvar, guard: &mut MutexGuard<T>) nothrow -> core.Result<Void, ConcurrencyError>;

    /// Like `wait`, but returns `Err(ConcurrencyError(kind=TIMEOUT))`
    /// if the duration elapses before a signal arrives.
    ///
    /// Matches the `Duration`-taking convention used elsewhere in
    /// `std.concurrent` (`sleep(Duration)`, `join_timeout(Duration)`).
    pub fn wait_timeout<T>(self: &Condvar, guard: &mut MutexGuard<T>, d: conc.Duration) nothrow -> core.Result<Void, ConcurrencyError>;

    /// Absolute-deadline variant; useful when a caller has a hard
    /// deadline shared across multiple waits.  `deadline_ms` is
    /// monotonic (compare against `thread.now_ms()`).
    pub fn wait_until<T>(self: &Condvar, guard: &mut MutexGuard<T>, deadline_ms: Int) nothrow -> core.Result<Void, ConcurrencyError>;

    /// Wakes one waiter.  No-op if no waiter is parked.  Wakes
    /// "some waiter" — ordering is unspecified; do not rely on
    /// FIFO.
    ///
    /// **Thread-safe.**  Does NOT require holding the associated
    /// `Mutex<T>`.  The Condvar's waiter list has its own
    /// internal lock, independent of the predicate's mutex.
    /// Callers typically signal while holding the predicate mutex
    /// (so the predicate update + signal happen atomically from
    /// the waiter's view), but this is a discipline imposed by
    /// the predicate, not a requirement of `signal_one` itself.
    pub fn signal_one(self: &Condvar) nothrow -> Void;

    /// Wakes all currently-parked waiters.  Future waiters (those
    /// who call `wait` after `signal_all` returns) are unaffected.
    ///
    /// Same thread-safety story as `signal_one` — does not
    /// require the predicate's mutex.
    pub fn signal_all(self: &Condvar) nothrow -> Void;

    /// Wakes all currently-parked waiters with
    /// `Err(ConcurrencyError(kind=CLOSED))`.  Future `wait` /
    /// `wait_timeout` / `wait_until` calls return `Err(CLOSED)`
    /// immediately without parking.  Idempotent; **one-way** —
    /// a closed Condvar cannot be re-opened or reused.
    ///
    /// **Note: this is a Drift extension, not POSIX condvar
    /// semantics.**  POSIX condvars do not own lifecycle state;
    /// "shutdown" is typically done by setting a shared flag and
    /// calling `broadcast()` (which Drift spells `signal_all`).
    /// Drift's `close()` is sugar for that exact pattern — flip
    /// an internal closed flag, wake everyone, make future waits
    /// fail-fast — combined into a single primitive because it's
    /// such a common idiom that getting it wrong (race between
    /// flag set and broadcast) is a common source of pool /
    /// channel / shutdown bugs.
    ///
    /// If a caller wants pure broadcast without a one-way close,
    /// use `signal_all` instead and manage the closed state
    /// themselves in the predicate.
    pub fn close(self: &Condvar) nothrow -> Void;
}
```

`ConcurrencyError` is the existing stdlib error type (already used by
other `std.concurrent` surfaces).  The `kind` field discriminates
between `CLOSED`, `CANCELLED`, and `TIMEOUT`.

## Internal data structures

```drift
struct Waiter {
    vt: thread.VtHandle,
    active: sync.AtomicBool,  // true = still in queue; false = claimed/cancelled
}

struct CondvarState {
    // Internal mutex guarding the waiter list.  We do NOT use
    // `sync.MpscQueue<Arc<Waiter>>` because (a) signal callers can
    // be many distinct VTs (multi-consumer), violating MpscQueue's
    // single-consumer contract, (b) MpscQueue is bounded, and we
    // need an unbounded wait queue for "hundreds of concurrent
    // acquirers", (c) wait_timeout / cancellation needs the
    // ability to traverse and skip-on-CAS-fail, not just FIFO pop.
    //
    // A Mutex-guarded list is the simplest correct shape.  The
    // critical section is short (push or pop one record), so the
    // spin-Mutex is fine.
    //
    // Future optimization: a proper lock-free MPMC queue with
    // skip-on-fail semantics could replace this; the API stays
    // the same.
    list_lock: conc.Mutex<WaiterList>,
    closed: sync.AtomicBool,
}

struct WaiterList {
    // Implementation note: Drift stdlib does not have a `Vec` type
    // distinct from `Array`.  `Array<T>` is the growable container.
    // The waiter list must be growable (unbounded waiters), allow
    // append at tail, and allow remove-from-anywhere (because
    // `pop_active` may need to skip past inactive records that
    // were self-CAS'd by their owning waiters and remove them).
    //
    // v1 shape: `Array<Arc<Waiter>>` with linear scan in
    // `pop_active`.  Operations:
    //   push(record) — array.push(record).  O(1) amortized.
    //   pop_active() -> Option<Arc<Waiter>> — scan from index 0;
    //     for each record, try active.compare_exchange(true, false);
    //     on success, remove that element via array.remove(i) and
    //     return it; on failure, that record is owned by a
    //     self-claimed waiter — remove it too (it's dead) and
    //     continue.  Returns None when array is empty.  O(n) per
    //     call, but the scan and removals are tight and the list
    //     length is bounded by concurrent waiters.
    //   drain_active() -> Array<Arc<Waiter>> — pop_active in a
    //     loop until empty; collect the successful claims.
    //     Used by signal_all and close.
    //
    // Performance note: O(n) pop_active is fine for v1.  The
    // waiter list is short under normal operation (active
    // contention bursts), and the scan is bounded by the number
    // of currently-parked or recently-cancelled waiters.  If
    // profiling later shows a hot spot, the follow-up is a proper
    // intrusive doubly-linked list with O(1) remove — the public
    // Condvar API does not change.
    records: Array<Arc<Waiter>>,
}

pub struct Condvar {
    state: conc.Arc<CondvarState>,
}
```

`Arc<CondvarState>` so a Condvar handle is cheaply cloneable (signals
from one VT, waits in another).  `Arc<Waiter>` so the record outlives
both sides — the list holds one ref, the parked VT's stack holds one
ref; either side drops its ref independently after the CAS resolves.

## Spurious wakes — what "consume the registration" means

Drift's Condvar follows Mesa semantics: `wait` may return `Ok` even
when no signal fired (cancellation we didn't surface, an
unrelated runtime unpark, an implementation-defined spurious
wake).  The caller MUST loop on the predicate:

```drift
while not predicate(&guard) {
    cv.wait(&mut guard)?;  // may return Ok spuriously
}
```

What happens to the waiter record on a spurious wake is important
for reasoning about correctness:

- The waiter wakes from `vt_park`.
- The self-CAS `active: true → false` succeeds (no signal claimed
  us).
- `wait` returns `Ok`.
- **The waiter is no longer in the queue's set of wake-targets**
  — its record is `active = false` and a future `signal_one`
  will skip it.

So one wait-registration is "consumed" by one wake, signal or not.
If the caller's loop re-enters `wait`, a **fresh** `Waiter` record
is pushed.  This is exactly POSIX-condvar behavior: each call to
`pthread_cond_wait` is one registration, woken at most once.

This matters in three ways:

1. **Liveness under spurious wakes.**  The caller's loop ensures
   that a spurious wake doesn't return control with the predicate
   still false — the next iteration re-parks against a fresh
   registration.
2. **Correctness under signal-and-no-progress.**  If `signal_one`
   wakes a waiter, the waiter checks the predicate, finds another
   acquirer raced and took the resource, the waiter must call
   `wait` AGAIN to re-register.  Without the fresh registration,
   the waiter would silently never wake again.
3. **No leaked records.**  Every wake — signal or spurious —
   removes the record from the active set via the CAS.  The
   queue does not accumulate dead records beyond what
   `signal_one` skips in its CAS-fail loop.

## Key invariant — stale-token avoidance

> `vt_unpark(handle)` is only ever called after a successful
> `compare_exchange(true, false)` on the corresponding `Waiter.active`.

This invariant is what makes the slice correct under timeout +
cancellation + spurious wakes.  Without it, a waiter that wakes
for ANY reason other than a normal signal (timeout, cancellation,
unrelated runtime unpark, spurious wake) leaves its `VtHandle` in
the queue; a later `signal_one` pops it and issues a spurious
`vt_unpark`, leaving a wake-token on a VT that has long since
moved on to other work — and that VT's next park returns
immediately for no reason.  (This is exactly the class of bug
0.31.86 + 0.31.89 fixed at the runtime layer; we don't want to
reintroduce it at the stdlib layer.)

The invariant is preserved by:

- **`signal_one` / `signal_all` / `close`** — pop waiter, CAS its
  `active` from true→false, only call `vt_unpark` on CAS success.
  CAS failure means the waiter already self-cancelled (timed out,
  spurious wake, or VT was cancelled) — skip silently.
- **EVERY wake path** — including plain `wait` — performs the
  self-CAS on own `active` true→false BEFORE returning to the
  caller.  This is true for `wait`, `wait_timeout`, and
  `wait_until` alike, even though only `wait_timeout` has a
  "natural timeout" wake source: ALL three can wake from
  cancellation, spurious unpark, or pre-empted future signaling.
  CAS success means we self-claimed (no signal will ever issue a
  stale unpark for us); CAS failure means a signal already
  claimed us and we look at `closed` / cancelled state to classify
  the return value.

## Semantics in pseudo-code

```drift
// Internal helper — common "enqueue + park + classify" body shared by
// wait / wait_timeout / wait_until.  `park_fn` is one of `vt_park(0)`
// or `vt_park_until(deadline_ms)`; `is_timeout_wake` tells the
// classifier whether to surface TIMEOUT vs just spurious.
fn _wait_inner(self, guard, park_fn, deadline_ms_opt) -> Result<Void, ConcurrencyError> {
    // Off-VT guard.
    let me = vt_current();
    if me == 0 {
        return Err(ConcurrencyError(kind=REQUIRES_VTHREAD));
    }

    // Pre-check closed (fast path).
    if self.state.closed.load() {
        return Err(CLOSED);
    }

    let waiter = Arc::new(Waiter { vt: me, active: AtomicBool::new(true) });
    {
        let mut list = self.state.list_lock.lock();
        list.records.push(waiter.share());
    }

    // Re-check closed AFTER push — closes the race where close()
    // ran between our top-of-fn check and our push, missing us.
    // If close set the flag but missed our record in its drain,
    // we self-CAS and return CLOSED.
    if self.state.closed.load() {
        if waiter.active.compare_exchange(true, false) {
            return Err(CLOSED);
        }
        // CAS failed: a signal beat us; fall through and consume
        // the wake normally.
    }

    guard.unlock_for_condvar();
    park_fn();   // vt_park(0) or vt_park_until(deadline_ms)
    guard.relock_for_condvar();

    // *** Self-CAS — runs on EVERY wake path, regardless of why we woke. ***
    // CAS success means we self-claimed; no future signal will issue a
    // stale vt_unpark for our record.
    // CAS failure means a signal already claimed us.
    let self_won = waiter.active.compare_exchange(true, false);

    // Classify in order: cancellation > closed > timeout > normal.
    if thread.vt_is_cancelled() {
        return Err(CANCELLED);
    }
    if self.state.closed.load() {
        return Err(CLOSED);
    }
    if self_won {
        // We weren't signaled.  Could be: timeout (if deadline elapsed),
        // cancellation we missed via vt_is_cancelled (unlikely), or
        // spurious wake.  Distinguish based on whether the caller
        // gave us a deadline.
        if deadline_ms_opt.is_some() && thread.now_ms() >= deadline_ms_opt.unwrap() {
            return Err(TIMEOUT);
        }
        // Spurious wake — return Ok and let caller re-check the predicate.
        return Ok(());
    }
    return Ok(());  // signaled normally
}

fn wait(self, guard) -> Result<Void, ConcurrencyError> {
    return self._wait_inner(guard, || vt_park(0), None);
}

fn wait_until(self, guard, deadline_ms) -> Result<Void, ConcurrencyError> {
    if thread.now_ms() >= deadline_ms {
        return Err(TIMEOUT);
    }
    return self._wait_inner(guard, || vt_park_until(deadline_ms), Some(deadline_ms));
}

fn wait_timeout(self, guard, d: Duration) -> Result<Void, ConcurrencyError> {
    let deadline_ms = thread.now_ms() + d.millis;
    return self.wait_until(guard, deadline_ms);
}

// signal_one — pop active waiter, CAS-then-unpark.
fn signal_one(self) {
    let mut list = self.state.list_lock.lock();
    while let Some(w) = list.records.pop_front() {
        if w.active.compare_exchange(true, false) {
            // Drop the lock BEFORE vt_unpark so the woken waiter
            // doesn't contend with us on relock.
            drop(list);
            vt_unpark(w.vt);
            return;
        }
        // CAS failed: skip and try next.
    }
}

// signal_all — drain list, unpark each claimed waiter.
fn signal_all(self) {
    // Drain to a local buffer under the lock; release the lock;
    // then unpark.  This avoids a thundering-herd contention on
    // relock_for_condvar (each woken waiter would race us for
    // the list_lock if we held it across the unparks).
    let claimed: Vec<Arc<Waiter>>;
    {
        let mut list = self.state.list_lock.lock();
        claimed = Vec::with_capacity(list.records.len());
        while let Some(w) = list.records.pop_front() {
            if w.active.compare_exchange(true, false) {
                claimed.push(w);
            }
        }
    }
    for w in claimed {
        vt_unpark(w.vt);
    }
}

// close — set flag, then drain like signal_all.  Idempotent.
fn close(self) {
    self.state.closed.store(true);
    self.signal_all();
}
```

## MutexGuard prerequisite — explicit state invariant

Condvar needs `MutexGuard<T>` to expose unlock / relock methods so
`wait` can release the lock before parking and reacquire on wake.
These are not currently in the public API; they need to be added,
with a privileged-stdlib annotation so user code cannot misuse them.

**Critical:** the guard must track its own lock state.  Without
this, a buggy Condvar implementation that returns from `wait`
without re-locking, or a panic between unlock and relock, would
leave the guard's destructor double-unlocking (or unlocking an
unlocked mutex, which is UB on most implementations).

Proposed shape — the guard carries an internal `locked: bool` and
all paths that read the guarded value, drop the guard, or
manipulate the lock check/maintain this invariant:

```drift
struct MutexGuard<T> {
    mutex: &Mutex<T>,
    locked: Bool,  // invariant: lock is held iff locked == true
}

implement MutexGuard<T> {
    // --- existing public API ---
    pub fn ...                     // (deref, etc., all assert locked == true)

    // --- destructor ---
    implement core.Destructible for MutexGuard<T> {
        pub fn destroy(var self: MutexGuard<T>) nothrow -> Void {
            if self.locked {
                self.mutex._unlock();
                self.locked = false;
            }
            // else: already-unlocked guard (e.g. unlock_for_condvar
            // was called without a matching relock_for_condvar — a
            // contract violation that we drop on the floor here to
            // avoid double-unlock; the bug surfaces as a deadlock
            // on the next lock() call, which is at least loud).
        }
    }

    /// **Internal — for `std.concurrent.Condvar` only.**  Releases
    /// the underlying lock without dropping the guard.  Must be
    /// paired with `relock_for_condvar` before the guard is
    /// dropped or any guarded value is read.
    ///
    /// Asserts at runtime that the guard is currently locked
    /// (calling on an already-unlocked guard is a contract
    /// violation and panics).
    pub(stdlib) fn unlock_for_condvar(self: &mut MutexGuard<T>) nothrow -> Void {
        assert(self.locked, "unlock_for_condvar on already-unlocked guard");
        self.mutex._unlock();
        self.locked = false;
    }

    /// **Internal — for `std.concurrent.Condvar` only.**  Reacquires
    /// the lock after an `unlock_for_condvar` call.
    ///
    /// Asserts at runtime that the guard is currently unlocked
    /// (calling without a matching prior unlock is a contract
    /// violation and panics).
    pub(stdlib) fn relock_for_condvar(self: &mut MutexGuard<T>) nothrow -> Void {
        assert(not self.locked, "relock_for_condvar on already-locked guard");
        self.mutex._lock();
        self.locked = true;
    }
}
```

If Drift does not have `pub(stdlib)` or equivalent visibility
modifier today, the methods land doc-hidden with a `_` prefix
convention AND the runtime `assert` is the contract enforcer.
The state invariant is enforced by the runtime checks, not the
visibility — so even if a user calls the internal methods, the
guard cannot end up in a broken state silently.  Long-term, a
proper visibility annotation tightens the contract from
"runtime-checked" to "compile-time-rejected."

Open question for the slice author: should `wait` accept a
closure-based shape (`guard.with_released(|| vt_park(0))`) instead
of exposing unlock/relock?  The closure shape hides the guard
state-machine but is harder to reason about for `wait_timeout`'s
"classify what happened" branch and the close-race re-check.
Recommendation: explicit unlock/relock with runtime-enforced
state invariant.

## Prerequisite runtime intrinsic — `vt_is_cancelled`

The pseudocode uses `thread.vt_is_cancelled() -> Bool` to classify
post-wake state.  This intrinsic does NOT exist in `lang.thread`
today — the runtime maintains an atomic `cancelled` flag per VT
(set by `drift_thread_cancel`), but no Drift-level read accessor
is exported.

**This slice adds the intrinsic:**

```drift
// in stdlib/lang/thread.drift:
@intrinsic pub fn vt_is_cancelled() nothrow -> Bool;
```

Backed by a one-liner in `lang/language_runtime/posix/thread_runtime.c`:

```c
int64_t drift_thread_is_cancelled(void) {
    DriftVt *vt = drift_vt_tls_get();
    return vt ? atomic_load(&vt->cancelled) : 0;
}
```

Trivial to implement and orthogonal to the Condvar logic, but
required for Condvar to surface `CANCELLED` cleanly.  Without it,
a cancelled waiter cannot distinguish "I was cancelled" from
"I was signaled" and returns Ok, which leaks the cancellation up
to the caller as a silent no-op rather than a typed error.

If the slice author prefers, this can land as a separate prep PR
before the Condvar slice.

## Off-VT call handling

`vt_current()` returns `0` (or equivalent sentinel) when the
calling thread is not running on a VT.  The pseudocode handles
this at the top of `_wait_inner`:

```drift
let me = vt_current();
if me == 0 {
    return Err(ConcurrencyError(kind=REQUIRES_VTHREAD));
}
```

This MUST happen before any state mutation — we don't enqueue a
`Waiter { vt: 0, ... }` record (which would be unparkable garbage)
and we don't park (which would deadlock since there's no VT to
unpark).  `REQUIRES_VTHREAD` is the existing pattern used by
`std.net` for the same condition.

`signal_one` / `signal_all` / `close` do NOT require a VT — they
can be called from any thread.  Only the `wait*` family needs the
VT context.

## Spin-Mutex is fine; parking-Mutex is a separate workstream

`std.concurrent.Mutex` is currently a spinlock.  Condvar built on a
spinlock is correct as long as critical sections are short
(microseconds), which is the case for every Condvar use the pool
needs (push/pop a queue, set an atomic flag).

A parking Mutex would be a larger workstream (it interacts with the
fairness story for the scheduler).  Don't gate Condvar on it.  When
parking-Mutex lands later, Condvar's `wait` implementation can swap
the unlock/relock pair transparently — the public Condvar API does
not change.

## Test plan

A new `lang/tests/codegen/e2e/condvar_*` family pinning the
following scenarios.  Each MUST be deterministic (no flakes); use
the `lang.thread.test_eventfd_*` harness pattern where helpful
(same as `conc_sleep_after_fast_io_direct_resume` does for the
0.31.89 fix).

| Test | Pin |
|---|---|
| `condvar_basic_signal` | one waiter, one signaler → wake → predicate satisfied |
| `condvar_signal_one_wakes_exactly_one` | N waiters, signal_one → exactly 1 wakes; N-1 still parked |
| `condvar_signal_all_wakes_all` | N waiters, signal_all → all wake |
| `condvar_close_wakes_all_with_closed` | N waiters, close() → all wake with Err(CLOSED) |
| `condvar_wait_after_close_returns_immediately` | close, then wait → Err(CLOSED), never parks |
| `condvar_wait_close_race` | close races with wait's enqueue → wait returns Err(CLOSED), never hangs |
| `condvar_signal_before_wait` | signal_one called with no waiters → no-op; subsequent wait still parks |
| `condvar_wait_timeout_fires` | wait_timeout(50ms) with no signaler → Err(TIMEOUT) at ~50ms |
| `condvar_wait_timeout_signaled` | wait_timeout(500ms) with signaler at 50ms → Ok at ~50ms |
| `condvar_wait_until_absolute_deadline` | wait_until matches wait_timeout shape; deadline passed → Err(TIMEOUT) without parking |
| `condvar_stale_token_no_leak_plain_wait` | **plain wait** wakes spuriously (no signal), then waiter does unrelated `conc.sleep` → sleep parks for real duration, not 0ms.  Pins that plain `wait` also performs the self-CAS. |
| `condvar_stale_token_no_leak_timeout` | wait_timeout times out, then unrelated signal_one runs, waiter does unrelated `conc.sleep` → sleep parks for real duration. |
| ~~`condvar_cancel_during_wait`~~ | removed: scheduler kills cancelled parked VTs before wait can return; no testable CANCELLED surface |
| `condvar_spurious_wake_predicate_loop` | wake without signal, predicate fails → caller's loop re-parks |
| `condvar_off_vt_returns_requires_vthread` | call wait from a non-VT context → Err(REQUIRES_VTHREAD) without parking |
| `condvar_mutex_guard_invariant_holds` | unlock_for_condvar / relock_for_condvar pair preserves locked state; double-unlock panics; drop-after-unlock-without-relock doesn't double-unlock |
| `condvar_concurrent_signal_one` | multiple VTs call signal_one concurrently → list_lock serializes; no race / double-wake |
| `condvar_stress_100_acquirers` | 100 waiters, 5 signalers, 30s — no hangs, exact account of signals → wakes |
| `condvar_stress_concurrent_close` | close mid-stress → all waiters drain to CLOSED, no hangs |

The `condvar_stale_token_no_leak_*` pair is load-bearing — they're
the tests that catch a CAS-discipline regression in the Condvar
implementation itself.  Pattern: a waiter wakes from a non-signal
source (spurious unpark for `_plain_wait`; timeout for `_timeout`),
then does `conc.sleep(50ms)` and asserts elapsed >= 40ms; in
parallel, after the wake but before the sleep, call
`cv.signal_one()`.  Pre-fix-discipline: the signal pops the stale
waiter record, calls `vt_unpark`, leaks a token, the unrelated
sleep returns 0ms → test fails.  With the self-CAS in EVERY wake
path, the signal's CAS finds `active=false` (already self-claimed
by the waiter's wake handler), skips the unpark → no leak → sleep
parks for real → test passes.

The `_plain_wait` variant specifically pins that plain `wait` —
which has no "natural" timeout source — STILL performs the
self-CAS to defend against spurious wakes and external
cancellation.  Without that test, an implementation that
optimizes the self-CAS out of plain `wait` would pass everything
else and silently regress the stale-token discipline.

## Implementation phases

Recommend two PRs for review surface area:

**PR 1 — prerequisites (small, independently mergeable):**
1. `thread.vt_is_cancelled()` intrinsic + runtime helper.
2. `MutexGuard<T>::unlock_for_condvar` / `relock_for_condvar` with
   internal `locked` state field and runtime assertions.
3. Tests for both: cancellation observable from Drift; guard
   state invariant catches misuse.

**PR 2 — Condvar itself:**
4. `Waiter` + `WaiterList` + `CondvarState` + `Condvar` struct +
   `condvar()` constructor.
5. `signal_one` / `signal_all` / `close`.
6. `_wait_inner` helper + `wait` / `wait_timeout` / `wait_until`.
7. Test suite from the table above.

PR 1 is self-contained value — `vt_is_cancelled` and explicit
guard state are both useful beyond Condvar.  PR 2 lands the full
user-facing surface.

## Toolchain prerequisite

driftc 0.31.89+ — the 0.31.89 fix closed the FAST-I/O
direct-resume stale-token leak.  Without that fix, ANY code built
on `vt_park` / `vt_unpark` (including Condvar) inherits the
underlying scheduler bug and the Condvar's correctness
discipline can't compensate.  The 0.31.89 toolchain pin is hard;
the slice cannot ship against older toolchains.

## Out of scope for this slice

- **Parking Mutex** — separate workstream.  Spin-Mutex is fine
  for Condvar correctness given short critical sections.
- **FIFO signal ordering** — `signal_one` wakes "some waiter".
  FIFO is strictly stronger and not needed for the pool's
  correctness (which uses loop-after-wait + predicate re-check,
  the standard Mesa discipline).  If a future use case demands
  FIFO, it can be added later (the queue is already FIFO; the
  CAS-may-fail-and-skip path just means weakly-FIFO under
  contention, which is the documented contract).
- **`Semaphore` / `Barrier` / `RwLock`** — separate slices once
  Condvar lands.  Each is a smaller follow-up that can build on
  Condvar's wait-queue patterns.
- **Condvar-on-RwLock** — only Mutex-paired Condvar in v1.
- **Reentrant Mutex** — separate.
- **async/await-style API** — Drift is fiber-based, not
  futures-based; Condvar's blocking shape IS the idiomatic API.

## Migration path for downstream

The maria-rpc team's `ConnectionPool` integration is mechanical
once this slice lands.  Suggested code shape (final form):

```drift
import std.concurrent as conc;

pub struct ConnectionPool {
    inner: conc.Mutex<PoolInner>,
    ready: conc.Condvar,
    // ...
}

pub fn acquire(self: &mut ConnectionPool) -> core.Result<LeasedConn, PoolError> {
    var guard = self.inner.lock();
    loop {
        if let core.Option::Some(conn) = guard.available.pop() {
            return core.Result::Ok(LeasedConn(conn));
        }
        if guard.active_count < guard.max_conns {
            // open fresh — release guard before slow syscall
            // (separate concern; not Condvar-specific)
        }
        if guard.closed {
            return core.Result::Err(PoolError(kind=CLOSED));
        }
        match self.ready.wait(&mut guard) {
            core.Result::Err(e) => return core.Result::Err(PoolError::from(e)),
            core.Result::Ok(_) => {} // loop
        }
    }
}

pub fn release(self: &mut ConnectionPool, conn: RpcConnection) nothrow -> Void {
    var guard = self.inner.lock();
    guard.available.push(conn);
    self.ready.signal_one();
}

pub fn close(self: &mut ConnectionPool) nothrow -> Void {
    var guard = self.inner.lock();
    guard.closed = true;
    self.ready.close();   // wakes all waiters with Err(CLOSED)
}
```

No hand-rolled wait queue.  No stale-token discipline at the
package layer.  The pool ships exactly the user-facing semantics
the maria team needs.

## Risks

1. **`MutexGuard` surface change**.  Adding
   `unlock_for_condvar` / `relock_for_condvar` to a public type
   needs careful visibility scoping.  If Drift's visibility model
   doesn't have a stdlib-private modifier today, the
   implementation lands with the `_` convention and the
   runtime `assert(self.locked, ...)` / `assert(not self.locked, ...)`
   pair as the contract enforcer.  The runtime check is the
   correctness story; visibility tightening is ergonomic
   follow-up.

2. **Stale-token CAS discipline correctness**.  This is the part
   most likely to harbor a subtle race.  Mitigation: the
   `condvar_stale_token_no_leak_*` tests pin the specific failure
   shape for BOTH plain `wait` and `wait_timeout`; the stress
   tests exercise the rest.  Code review must specifically check
   that every wake path (plain wait, timeout wait, cancellation
   wake, spurious wake) performs the self-CAS before returning.
   The most likely regression shape is "plain `wait` optimizes
   the self-CAS out because there's no obvious need" — the
   `_plain_wait` test exists specifically to catch this.

3. **Spurious wakes from `vt_unpark` race windows**.  If a
   parallel runtime path issues an `unpark` for unrelated
   reasons, the waiter wakes early.  Handled by the
   loop-after-wait pattern in the caller contract — normal
   Mesa discipline.  Internally, also handled by the self-CAS:
   the waiter claims its own record, future signals skip it.

4. **Internal waiter-list lock contention**.  The Mutex-guarded
   `WaiterList` serializes all signal/wait operations on a given
   Condvar.  This is the simplest correct shape; if profiling
   shows the list_lock becomes a bottleneck under hundreds of
   concurrent acquirers, a follow-up slice can swap in a
   lock-free MPMC queue with skip-on-CAS-fail semantics — the
   public Condvar API does not change.

5. **`vt_is_cancelled` intrinsic addition**.  Adds one method to
   `lang.thread`'s public surface.  Trivial to implement; only
   risk is breaking a downstream that exports its own
   `vt_is_cancelled` from `lang.thread` (unlikely; the symbol is
   new).  Land as PR 1 prerequisite.

6. **Off-VT call paths**.  Tests must cover the
   `vt_current() == 0` case explicitly.  An implementation that
   forgets the check enqueues a `Waiter { vt: 0 }`, parks (on
   the OS thread — undefined behavior since `vt_park` requires
   a VT context), and may hang the process.  The
   `condvar_off_vt_returns_requires_vthread` test pins this.

7. **Toolchain pin (0.31.89+)**.  Tooling/CI must enforce.  The
   slice's package metadata declares the dependency; users on
   older toolchains see a clean version error rather than a
   silent miscompile.

## Estimated effort

Two PRs, one engineer:

**PR 1 (prerequisites):**
- `vt_is_cancelled` intrinsic + runtime helper + test: 0.5-1 day.
- `MutexGuard` state invariant + unlock/relock pair + tests:
  1-2 days.

**PR 2 (Condvar):**
- `Waiter` + `WaiterList` + `Condvar` struct + signal/close:
  2 days.
- `_wait_inner` + `wait` / `wait_timeout` / `wait_until` with
  full self-CAS discipline on every wake path: 2-3 days.
- Test suite (16 cases including off-VT and guard-invariant
  tests): 3-4 days.
- Code review + iteration: 3-4 days.

Total: **~12-17 working days** end-to-end.  Realistic landing
window: **~3-4 weeks** from kickoff, accounting for the
two-PR cycle and unforeseen interactions with the scheduler /
runtime.

## Sign-off

Ready for kickoff on toolchain-team approval of:
- Public API surface (above).
- MutexGuard internal-surface additions.
- 0.32 target.

Once approved, maria team gates its v1 cut on this slice and
implements `ConnectionPool` against the final API directly — no
in-package stopgap.

---

## Revision history

**2026-05-16 v4** — PR1 implementation findings:

1. **`CANCELLED` dropped from Condvar's error surface.**  PR1
   implementation work revealed that the runtime scheduler's
   worker-dispatch cancelled-check
   (`thread_runtime.c:858-872`) kills parked VTs before any
   user code runs after a wake.  Concretely: a VT parked inside
   `vt_park_until`, then cancelled by another VT, is reaped at
   the next worker re-dispatch (timer-driven unpark → enqueue →
   worker pulls → sees `cancelled` set → kills) WITHOUT giving
   the VT's code a chance to call `vt_is_cancelled` and route
   the error.  So `Condvar.wait` cannot surface `CANCELLED` via
   the planned `vt_is_cancelled()` post-wake classification.
   The intrinsic is still useful for compute-loop cooperative
   cancellation (the test pins this), but is removed from
   Condvar's contract.  `wait` and `wait_timeout` surface only
   `CLOSED`, `TIMEOUT`, and `REQUIRES_VTHREAD`.
2. Implementation choice: `MutexGuard<T>` now carries `locked: Bool`
   (not AtomicBool — the guard is owned by one VT at a time, no
   cross-VT access).  `unlock_for_condvar` and `relock_for_condvar`
   shipped; both assert against double-unlock / double-relock.
   Destructor respects the flag and skips the unlock if the guard
   was left in the unlocked-for-condvar state.

**2026-05-16 v3** — final review pass before kickoff:

1. Added explicit "Spurious wakes — what 'consume the
   registration' means" section, documenting Mesa semantics: each
   `wait` call is one registration, woken at most once; spurious
   wake consumes the registration; caller's predicate loop
   re-enqueues a fresh registration if needed.  Removes ambiguity
   in the stale-token discipline for plain `wait`.
2. `WaiterList` data structure made concrete: `Array<Arc<Waiter>>`
   with linear-scan `pop_active`.  Documented as v1 shape with
   O(1)→O(n) tradeoff explicit; future intrusive-list optimization
   noted as no-API-change.
3. `signal_one` / `signal_all` docstrings now explicitly state
   they are thread-safe and do NOT require the predicate mutex.
   Holding the predicate mutex is a caller-side discipline, not a
   Condvar requirement.
4. `close()` docstring marks it as a Drift extension (not POSIX
   condvar shape).  Explicit: a closed Condvar is **one-way**,
   not reusable.  Documents the equivalent POSIX pattern
   (separate closed flag + `signal_all`/broadcast) for callers
   who want pure broadcast.

**2026-05-16 v2** — corrections incorporated:

1. Plain `wait` now performs the same self-CAS on `Waiter.active`
   as `wait_timeout`.  Every wake path (signal, timeout, cancel,
   spurious) self-claims before returning to prevent stale-token
   leaks.  New `condvar_stale_token_no_leak_plain_wait` test pins
   this for the cancellation/spurious-wake case.
2. `MpscQueue<Arc<Waiter>>` replaced with
   `Mutex<WaiterList>` (Vec/list under a spin-Mutex).  MpscQueue's
   single-consumer contract is violated by multiple concurrent
   signal callers, and the bounded queue can't hold "hundreds of
   acquirers"; the simple Mutex-guarded list is the correct shape
   with room to optimize later via a real MPMC primitive.
3. `MutexGuard<T>` carries an explicit `locked: Bool` state with
   runtime assertions in `unlock_for_condvar` / `relock_for_condvar`
   and a destructor that respects the state, so misuse cannot
   silently double-unlock.
4. `wait_timeout` now takes `Duration` (matches stdlib idiom of
   `sleep(Duration)` / `join_timeout(Duration)`).  Absolute-deadline
   variant available as `wait_until(deadline_ms)`.
5. Cancellation detection requires a new `vt_is_cancelled()`
   intrinsic in `lang.thread` — added as PR 1 prerequisite.  Off-VT
   call paths return `Err(REQUIRES_VTHREAD)` without parking.
6. Motivation section corrected: sleep-polling is bad for CPU +
   latency but does NOT deadlock — `conc.sleep` parks the VT
   correctly.

**2026-05-16 v1** — initial draft.
