# Reload Substrate — signal-driven configuration/plugin reload

This document specifies the **reload coordinator protocol**: how a long-running
Drift service rescans on-disk state (configuration, plugin descriptors, …) in
response to a process signal, and atomically swaps it in without blocking readers
or corrupting live state. It composes existing primitives only — there is no new
runtime surface beyond `ProcessSignal::User1` (SIGUSR1) and `std.fs.read_dir`.

## Pieces it composes

- **`conc.await_signal()` → `ProcessSignal::User1`** (SIGUSR1): the reload trigger.
  Linux-only, single-waiter, `nothrow` (misuse aborts). See
  [drift-concurrency.md](drift-concurrency.md) and the `std.concurrent` reference.
- **`std.fs.read_dir(path, timeout)`**: a VT-safe, deterministic directory snapshot
  used to discover the new on-disk state. Takes an explicit `timeout: conc.Duration`
  (IO-contract clause). Offloaded to the blocking pool so a stalling config
  directory never blocks a carrier (see the **Stdlib IO contract** in
  [drift-stdlib-spec.md](drift-stdlib-spec.md)).
- **`conc.channel<T>()`**: an MPSC channel carrying reload requests from the signal
  VT to the worker VT.
- **`core.Arc<conc.Mutex<State>>` + `mem.replace`**: the published live state and
  its atomic swap.

## The protocol

### 1. Signal VT — notify only

A dedicated virtual thread is the single `await_signal()` waiter. It loops:

```
loop {
    match await_signal() {
        ProcessSignal::User1() => { sender.send(ReloadRequest()); }
        _ => { /* shutdown handling, etc. */ }
    }
}
```

It does **nothing else** — no filesystem access, no staging, no swap. This mirrors
the liveness discipline of keeping the signal-triggered path minimal, and respects
the single-waiter rule (it is the one `await_signal()` caller). The VT runs on the
normal signalfd/reactor path (it *parks*); it is not an async-signal-handler
context, so calling `send()` is safe.

**Idempotent-trigger discipline.** Standard (non-realtime) signals **coalesce**:
several rapid SIGUSR1s while no waiter is parked may surface as a **single**
`await_signal()` return. Therefore `ReloadRequest` is a content-free *"rescan
current state"* edge, **not** a per-event command. The worker always re-reads the
*current* directory state on each request, so dropping a request because two
signals merged is harmless — the surviving request rescans the latest state.

**Channel.** This slice uses the existing **unbounded** channel as-is (no
`try_send`, no bounded capacity). Duplicate reload requests are harmless: each one
costs only a redundant rescan, never incorrectness. A bounded/pending-bit
deduplication optimization is deferred.

### 2. Worker VT — stage → verify → swap

Receives a `ReloadRequest` via `recv()`, then:

- **Stage.** `read_dir(config_dir, timeout)` (deterministic snapshot) → build the new state
  into a *local* `Staged` value. All filesystem work is offloaded by `read_dir`;
  the worker VT parks rather than blocking a carrier.
- **Verify.** Validate the staged state. On failure, `return`/`continue` — the local
  `Staged` drops via RAII and the live state is **untouched**.
- **Atomic swap.** Take `Arc<Mutex<State>>.lock()`, `mem.replace(&mut *guard,
  new_state)` to publish and obtain the `old_state`, release the lock, **then drop
  `old_state` outside the lock**. This is the same discipline the channel
  `Receiver` destructor uses: detach under-lock, drop outside the lock, so user
  destructors never run under the state mutex.

  **Scoping matters.** Locals drop in reverse declaration order, so declaring
  `old` in the *same* scope as the guard would drop `old` *first* — while the
  guard still holds the lock. Put the guard in an inner block and hold `old` in
  an outer binding so the lock releases before `old` drops:

  ```drift
  var old_holder: Optional<State> = Optional::None();
  {
      var guard = conc.lock(state.get());
      old_holder = Optional::Some(mem.replace(guard.get_mut(), move staged));
  }                                  // guard drops here → lock released
  // old_holder (the displaced State) drops below, with the lock free; its
  // Destructible::destroy runs outside the critical section.
  ```

  The regression `test_reload_coordinator_sequential_reloads_and_failure` pins
  this with a `State` whose `destroy()` re-acquires the live mutex: a drop inside
  the lock would self-deadlock, so the test completing proves drop-outside-lock.

Readers elsewhere hold the same `Arc<Mutex<State>>` and observe either the whole
old or the whole new state (a single `mem.replace` under the lock is an atomic
publish — never a half-updated state).

### 3. Failure isolation

A verify failure or a `read_dir` error **never** mutates the live state; the
coordinator logs and waits for the next signal. Channel close (the last `Sender`
Arc dropped) ends the worker loop cleanly via `recv()` returning `Err(CLOSED)`.

## Sequence

```
   SIGUSR1 ──► [signal VT] await_signal()=User1 ──► sender.send(ReloadRequest)
                                                         │ (channel)
                                                         ▼
                            [worker VT] recv() ─► read_dir(dir)  (offloaded, VT parks)
                                                  │
                                                  ├─ verify fails ─► drop Staged (RAII), live state intact
                                                  │
                                                  └─ verify ok ─► lock; old = mem.replace(guard, new); unlock
                                                                   └─ drop old outside lock
   readers ◄────────────── Arc<Mutex<State>> (whole-old or whole-new) ──────────────►
```

## Caveats (frozen)

- **Single waiter.** At most one VT may be parked in `await_signal()`; the signal
  VT is that waiter. A second concurrent waiter aborts with a diagnostic.
- **Signals coalesce.** Treat reload as an idempotent rescan, never a counter.
- **Atomic publish.** The swap is a single `mem.replace` under the lock; the old
  state is dropped outside the lock.
- **Linux only.** SIGUSR1 / signalfd is Linux-only, inherited from `await_signal`.

## Non-goals

No actual code hot-swap or dynamic linking — "state" is Drift data (config/plugin
descriptors), not executable code. No multi-directory or debounced reload in v1.
