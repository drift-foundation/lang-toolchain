Summary: `std.concurrent.Receiver<T>::destroy()` crashes with a `drift_bounds_check_fail` abort when a
program with an outstanding (never-joined) `spawn`ed `VirtualThread` holding a `Receiver<T>` exits —
via either a normal `main()` return or an explicit libc `exit()` call

Classification
- Runtime/stdlib bug (correctness, fail-fast crash — `SIGABRT` via `drift_bounds_check_fail`, not a
  diagnosed compile error; this is a real process crash at runtime, not a compile-time issue)
- Priority: high — reproduces on a minimal, dependency-free 12-line program; hits ANY program that
  `spawn`s a fiber holding a `std.concurrent.Receiver<T>` and then lets the process exit while that
  fiber is still alive (whether blocked in `.recv()` or simply not yet torn down) — a completely
  ordinary "background worker fiber fed by a channel" shape.
- Found on driftc 0.33.78 | abi 20 (certified snapshot `20260710-151831-drift-workflows-712b1d0`)

Symptom
- A program that `conc.spawn`s a fiber which owns (captures/moves-in) a `std.concurrent.Receiver<T>`,
  and then returns from `main()` (or calls `exit()`) while that fiber has not been explicitly stopped
  and joined, aborts with `SIGABRT` instead of exiting cleanly. Confirmed via `coredumpctl gdb`:

  ```
  #4  __GI_abort ()
  #5  drift_error_raise ()
  #6  drift_bounds_check_fail ()
  #7  drift_bounds_check ()
  #8  std.concurrent::Receiver<T>::std.core.Destructible::destroy__inst__...()
  #9  __drift_cb_drop_...()
  #10 __drift_iface_drop_helper ()
  #11 __drift_cb_drop_...()
  #12 drift_vt_registry_cleanup_atexit ()
  #13 __run_exit_handlers (status=0, ...) at ./stdlib/exit.c:118
  #14 __GI_exit (status=<optimized out>) at ./stdlib/exit.c:148
  #15 drift_main ()
  #16 drift_root_vt_call ()
  #17 drift_vt_fiber_entry ()
  #18 _drift_trampoline ()
  ```

- The Drift runtime registers its own `drift_vt_registry_cleanup_atexit` via libc `atexit()`. This
  handler walks the still-registered `VirtualThread` set and tears down their locals (running
  `Destructible::destroy` on each), and `Receiver<T>::destroy()` — real source at
  `stdlib/std/concurrent/concurrent.drift:1351` — bounds-check-fails partway through.
- Because the cleanup is `atexit()`-registered, this fires on **any** path that reaches libc `exit()`
  — a normal `return` from `main()`, or an explicit `extern "C" fn exit(code: Int32)` FFI call. The
  only thing that avoids it is `_exit()` (the raw `exit_group` syscall, which skips all `atexit()`
  handlers) — confirmed as a working escape hatch, see below.

Minimal reproduction (no project dependencies, ~14 lines):

```drift
module main;

import std.core as core;
import std.concurrent as conc;
import std.console as console;

pub fn main() nothrow -> Int {
    var halves = conc.channel<type String>();
    val sender = halves.take_sender();
    val receiver = halves.take_receiver();
    val _vt = conc.spawn(core.callback0(|| captures(move receiver) => {
        var r = move receiver;
        match r.recv() { Ok(_) => {}, Err(_) => {} }
        return 0;
    }));
    val _sendResult = sender.send("hello" + "");
    console.println("ok");
    return 0;
}
```

- Compile: `driftc --target-word-bits 64 -o /tmp/out repro.drift`
- Run: prints `ok`, then `Aborted (core dumped)`, exit code 134.
- Note the spawned fiber's own `receiver.recv()` completes successfully (the sender sends before
  `main` returns) — the crash is not about a fiber stuck blocked forever; it reproduces even when the
  fiber's work is already done and its `receiver` local is (or is about to be) dropped through an
  entirely ordinary function-return path. What matters is only that the fiber was never explicitly
  `.join()`ed before the process exits.
- Confirmed the crash disappears entirely if the `_vt` handle is joined before `main` returns (`val _ =
  _vt.join();` — but that requires the fiber to actually finish, i.e. isn't a fix for a genuine
  run-forever background-worker fiber, only for a bounded one).
- Confirmed a program with `conc.spawn` but **no** channel involved at all exits cleanly (exit code 0,
  no crash) — isolates the trigger specifically to a `Receiver<T>` (or something reachable through it)
  being torn down via this `atexit` cleanup path, not to `spawn`/`VirtualThread` teardown in general.
- Confirmed `exit()` (libc, via an `extern "C" fn exit(code: Int32) nothrow -> Void;` FFI declaration)
  does **not** avoid the crash — it still runs `atexit()` handlers, including
  `drift_vt_registry_cleanup_atexit`. Only `_exit()` (`extern "C" fn _exit(code: Int32) nothrow ->
  Void;`) — which skips `atexit()` entirely per POSIX — avoids it.
- Originally discovered in a real project (`drift-query`) implementing a persistent background
  "engine actor" fiber (a `dqc.invoke` module: one dedicated fiber, fed via a `conc.channel`, that is
  by design *never* torn down — matching a real long-lived server's own worker-thread lifecycle,
  where the process is normally stopped by an external signal, not by its own `main()` returning). The
  project's own test binary (which — unlike a real server — legitimately does want to reach a clean,
  successful `main()` return after its assertions finish) hit this crash consistently, with the exact
  backtrace above. Confirmed independent of that project's specific types by reducing to the
  `String`-only, dependency-free repro above (identical backtrace / same
  `Receiver<T>::destroy__inst__...` frame).

Why this is a real bug, not test infrastructure
- The crash is inside the Drift runtime's own generated code (`Receiver<T>::destroy`, a real stdlib
  method with a documented, seemingly-correct implementation — see "Likely cause" below), triggered
  during the runtime's own `atexit`-registered cleanup path, not anything project-specific.
- It makes "spawn a background fiber fed by a channel, and let your program's `main()` return
  normally when it's done with its own work" — a completely ordinary, textbook pattern — silently
  fatal (`SIGABRT`, non-zero exit code) even when the spawned fiber's own work already completed
  successfully and every assertion/observable side effect in the program was already correct. A test
  suite (or `&&`-chained CI gate) built around exit-code-zero-means-pass will incorrectly report the
  entire run as failed.
- Deterministic — reproduced across three separate runs with the exact same backtrace signature.

Verification
- Reproduced directly against the certified toolchain snapshot
  `~/opt/drift/certified/current/toolchain` (driftc 0.33.78, abi 20).
- Root-caused via `coredumpctl gdb` (systemd-coredump was already capturing the aborts) rather than
  guessing — the backtrace above is the actual crashing call chain, not inferred.
- Confirmed the `_exit()` (raw syscall, skip-atexit) workaround eliminates the crash: same program,
  three repeated runs, exit code 0 every time, `stdout` unchanged.

Likely cause
- `Receiver<T>::destroy()` (`concurrent.drift:1351-1361`) takes the channel's state lock, sets
  `receiver_closed = true`, and uses `mem.replace` to detach the internal `queue: Array<T>` out from
  under the lock (explicitly to avoid running a queued `T`'s destructor while holding the state mutex
  — the method's own doc comment explains this is load-bearing to avoid a self-deadlock). The bug is
  very likely NOT in this function's own logic (it looks straightforward and its own doc comment shows
  real design care) but in the **context** `drift_vt_registry_cleanup_atexit` calls it from: tearing
  down a still-registered `VirtualThread`'s captured locals via a generic `Destructible::destroy`
  fan-out, likely re-entering (or racing) some piece of shared runtime state (the VT registry itself,
  or the channel's own `Arc<ChannelInner<T>>` refcount/state bookkeeping) that is no longer in a state
  `Receiver::destroy()`'s normal-path assumptions expect — e.g. the `mem.replace`'s bounds-checked
  array-header access failing because the `ChannelInner<T>` (or its `queue` array header) was already
  torn down or is being torn down concurrently by a DIFFERENT, similarly-triggered cleanup fan-out for
  a related object (the paired `Sender<T>` on another still-registered VT, or the VT registry's own
  iteration invalidating something mid-walk).
- Given the crash is specifically a `drift_bounds_check_fail` (not a null-deref/UAF-style crash), the
  most likely concrete shape is: the atexit-triggered teardown calls `Receiver::destroy()` on a
  `Receiver<T>` whose backing `ChannelInner<T>`/array-header state has already been partially or fully
  torn down by an earlier step of the SAME `drift_vt_registry_cleanup_atexit` fan-out (e.g. if the
  registry iterates fibers/locals in an order that tears down a shared `Arc`-refcounted structure's
  backing allocation before every remaining strong reference to it has been visited) — i.e. an
  ordering/lifetime bug in `drift_vt_registry_cleanup_atexit` itself, not in `Receiver::destroy`'s own
  logic in isolation.

Pointers for fix
- `drift_vt_registry_cleanup_atexit` (native runtime, not `.drift` source — likely in the Rust/C
  runtime crate backing `libdrift_rt_abi20.a`) is the actual entry point; that is where the
  ordering/lifetime issue almost certainly lives, not in `concurrent.drift`'s `Receiver::destroy`
  itself.
- Worth checking: does the atexit fan-out visit each `VirtualThread`'s captured locals more than once
  (e.g. once via the registry, once via a duplicate strong reference elsewhere), or does it tear down
  a shared `Arc<ChannelInner<T>>`'s backing allocation before all `Sender`/`Receiver` handles pointing
  at it have been destroyed?
- A cheap, purely-diagnostic first step: reproduce under a debug/non-`-O2` build (this repro was only
  observed through an `-O2`-optimized binary with `[No debugging symbols found]`) to get a
  symbol-accurate native backtrace instead of relying on frame-name inference from the optimized
  build.

Test plan
- Add the 12-line repro above (or a close variant) as a regression test: spawn a fiber holding a
  `Receiver<T>`, send it one value, let `main` return without joining. Assert exit code 0.
- Also worth a variant where the fiber is deliberately still blocked in `.recv()` (no `send()` at all)
  when `main` returns, to cover the "abruptly-torn-down-mid-block" shape too, not just the
  "already-finished-but-never-joined" shape this repro isolates.

Owner
- Unassigned. Slot into the runtime/`std.concurrent` queue — likely native runtime code
  (`drift_vt_registry_cleanup_atexit`), not `.drift` stdlib source.

Cross-references
- Discovered 2026-07-10 while implementing `drift-query`'s `work/write-activity-api` Slice 10
  (`src/dqc/invoke.drift`, `test/dqc/invoke_test.drift`), immediately after working around a separate,
  unrelated compiler bug in the same session (see
  `drift-lang/issues/arc-get-recursive-struct-owner-typevar-recursion/description.md`). Worked around
  in that project's test binary by calling raw `_exit()` via an `extern "C"` FFI declaration right
  before returning from `main()`, once every assertion has already run — not a fix, just an escape
  hatch appropriate for a test binary specifically (a real long-lived server process is normally
  stopped by an external signal and never hits this `atexit` path at all, so it's a non-issue there).
