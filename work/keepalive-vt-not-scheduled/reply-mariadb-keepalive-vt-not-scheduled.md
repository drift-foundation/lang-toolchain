# Reply to mariadb team — keepalive VT not scheduled (Issue 2.1 from `toolchain_msg_after_0_31_84.md`)

**Status: investigating; need a minimal extract from your side before I can fix.**

## What I tried

Reduced K's checklist into a standalone repro (`/tmp/keepalive_repro/main.drift`
in my workspace), hitting every shape on your "things that work
locally" list:

- helper `_spawn_keepalive(inner_arc: Arc<Inner>) -> VirtualThread<Int>` —
  spawn happens inside the helper, returns the VT handle.
- helper returns a `ManagedConnection { inner: Arc<Inner>, keepalive_vt:
  VirtualThread<Int> }` struct from `_open()`.
- closure captures `Arc<Inner>` by `move` and runs `while ticks < 100 {
  sleep(100ms); fetch_add(counter); }`.
- main calls `_open()`, sleeps 550ms, reads counter, expects 1..10.

Result: **5 ticks, exactly as expected**. The reduction does not
reproduce. Closure prints `[keepalive] start` at the top of its body.

I matched everything you listed as "local-works": library-helper shape,
struct-wrapped VT, `Arc<Inner>` capture, loop with shared atomic
counter. So whatever's broken is **outside the spawn+sleep+Arc generic
shape** — it's specific to something in `mariadb.rpc.managed`'s
production helper path.

## What I'd like from you

Without `packages/mariadb-rpc/tests/spike/managed_connection_spike.drift`
visible to me, I can't reduce further. Two paths:

### Path A — send a minimal extract (preferred)

Copy `_spawn_keepalive` and `open()` verbatim into a fresh standalone
`.drift` file (no other deps). Strip everything that isn't load-bearing
— `MutexGuard`, type registries, real TLS, real socket setup. Trim
until either it stops failing (in which case the last thing you
deleted IS the trigger) or it's small enough to send.

The local reductions you already enumerated work for me too — what
shape can you NOT delete from `_spawn_keepalive` without it starting
to work?

### Path B — probe in place

If Path A is expensive, run these probes against your failing binary
and send the output. They're cheap and they'd narrow it a lot:

```drift
fn _open() nothrow -> ManagedConnection {
    val inner = ...;
    val inner_for_vt = inner.clone();
    cons.println("[diag] pre-spawn now=" + fmt.format_int(time.elapsed_ms(&t0)));
    val vt = _spawn_keepalive(inner_for_vt);
    cons.println("[diag] post-spawn submit_error=" + fmt.format_int(vt.submit_error));
    cons.println("[diag] post-spawn is_completed=" + fmt.format_bool(vt.is_completed()));
    return ManagedConnection(inner = inner, keepalive_vt = vt);
}

fn main() ... {
    var mc = _open();
    cons.println("[diag] mid-main pre-sleep");
    conc.sleep(...);
    cons.println("[diag] mid-main post-sleep");
    cons.println("[diag] vt.is_completed=" + fmt.format_bool(mc.keepalive_vt.is_completed()));
    ...
}
```

Specifically I want to see:

1. **`submit_error`** on the returned `VirtualThread` (the field on
   `VirtualThread<T>` populated when `exec_submit` returns non-zero).
   If non-zero, the VT was never enqueued and the print-at-top-of-
   closure couldn't have fired regardless.
2. **`is_completed()`** right after spawn returns. Should be `false`.
   If it's `true`, the VT struct is broken / cancelled before run.
3. **`is_completed()`** after main's 550ms sleep. Should be `false`
   (VT still running its loop). If `true`, the VT *did* run to
   completion (or got cancelled) but produced no visible side effect
   — that points at the closure body crashing silently or the captured
   Arc being dropped from under it.
4. **Time elapsed across the spawn call itself**. If `_spawn_keepalive`
   takes more than ~5ms, something inside it is doing real work
   before/after the `conc.spawn(...)` call.

Also try `mc.keepalive_vt.join_timeout(100)` instead of `mc.cancel()`
at the end. If it returns immediately with an error/zero, the VT is in
some bad state. If it hangs the full 100ms, the VT is parked waiting
on something that never fires.

### The "prime with trivial spawn" detail

That's the strongest clue you've given me. The two scenarios:

```drift
// Failing (no prime)
val vt = conc.spawn(...);
return ManagedConnection(inner = ..., keepalive_vt = vt);

// Works (with prime)
val _ = conc.spawn<type Int>(core.callback0(|| => { return 0; }));  // trivial
val vt = conc.spawn(...);
return ManagedConnection(inner = ..., keepalive_vt = vt);
```

The trivial-spawn case adds: one extra `vt_spawn` + `exec_submit` +
no immediate yield (the trivial VT sits enqueued until main yields). I
don't see an obvious reason why this would unblock the second spawn —
unless something in the *first* call has corrupt state that the
*second* call repairs.

A focused diagnostic: try the prime form with `.join()` (force the
trivial VT to complete before returning to the helper). If `.join()`
hangs forever, *no* VT is being scheduled — and the "prime + segfault"
behavior is a memory-corruption symptom from elsewhere, not a
scheduling fix. If `.join()` returns cleanly, the executor IS
scheduling VTs; something specific to your production keepalive
closure is breaking it (and the segfault-after-prime is what I'd
expect from that broken closure being run).

## Toolchain side

Holding the keepalive investigation open at the toolchain
side until I can either (a) reproduce locally from your extract, or
(b) see the probe output narrow which of "submit failed",
"closure body crashes silently", or "VT runs but Arc drops" is the
actual mode.

— K
